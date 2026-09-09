"""Follow-ups: scheduling, relevance re-checking at send time, and delivery."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.ai.schemas import AiDecision
from app.core.clock import as_utc, utcnow
from app.domain import HandlingMode, MessageType, ReminderKind, ReminderStatus, SalesStage
from app.models import AiDecisionLog, Conversation, Customer, Message, Reminder
from app.services import reminders as reminder_service
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


async def start_conversation(client, ai, text="I'm interested but busy right now"):
    ai.queue_decision(
        AiDecision(
            reply_text="No problem - shall I check back in a couple of days?",
            intent="information_request",
            actions=["schedule_follow_up"],
            follow_up_minutes=2880,
        )
    )
    await post(client, text=text)
    await drain_queue()


async def make_due(db, reminder_id):
    """Pull a reminder's due date into the past."""

    async def _update(session):
        reminder = await session.get(Reminder, reminder_id)
        reminder.due_at = utcnow() - timedelta(minutes=1)

    await db.write(_update)


class TestScheduling:
    async def test_ai_can_request_a_follow_up(self, client, db, ai, meta):
        await start_conversation(client, ai)

        reminder = (await db.execute(select(Reminder))).scalar_one()
        assert reminder.kind == ReminderKind.FOLLOW_UP
        assert reminder.status == ReminderStatus.PENDING
        # SQLite hands datetimes back naive; as_utc is how the app reads them too.
        assert as_utc(reminder.due_at) > utcnow() + timedelta(hours=47)

    async def test_follow_up_without_a_delay_is_not_scheduled(self, client, db, ai, meta):
        """The AI's malformed request is refused; the automatic check-in still runs.

        The backend now guarantees a follow-up on every live turn, so "not
        scheduled" means the AI's own request was rejected - not that the
        conversation is left with no way back.
        """
        ai.queue_decision(
            AiDecision(reply_text="Sure.", intent="small_talk", actions=["schedule_follow_up"])
        )
        await post(client, text="ok")
        await drain_queue()

        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert "follow_up_without_delay" in (decision.rejected_reasons or {})
        assert "schedule_follow_up" not in decision.executed_actions["actions"]
        assert "auto_follow_up" in decision.executed_actions["actions"]

        reminder = (await db.execute(select(Reminder))).scalar_one()
        assert reminder.reason.startswith("automatic check-in")

    async def test_repeat_requests_replace_rather_than_pile_up(self, client, db, ai, meta):
        await start_conversation(client, ai, text="not now")
        await start_conversation(client, ai, text="still not now")

        reminders = list((await db.execute(select(Reminder))).scalars())
        assert len(reminders) == 2
        assert sum(r.status == ReminderStatus.PENDING for r in reminders) == 1
        assert sum(r.resolution == "customer replied" for r in reminders) == 1

    async def test_absurd_delays_are_clamped(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Ok.",
                intent="small_talk",
                actions=["schedule_follow_up"],
                follow_up_minutes=100_000_000,
            )
        )
        await post(client, text="maybe next year")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        assert as_utc(reminder.due_at) < utcnow() + timedelta(days=15)


class TestCancellation:
    async def test_a_reply_cancels_pending_follow_ups(self, client, db, ai, meta):
        await start_conversation(client, ai)
        await post(client, text="actually I have a question")
        await drain_queue()

        reminders = list((await db.execute(select(Reminder))).scalars())
        cancelled = [r for r in reminders if r.status == ReminderStatus.CANCELLED]
        assert [r.resolution for r in cancelled] == ["customer replied"]
        # The reply also earns a fresh check-in, so the conversation is never
        # left with nothing queued.
        assert sum(r.status == ReminderStatus.PENDING for r in reminders) == 1

    async def test_handoff_cancels_pending_follow_ups(self, client, db, ai, meta):
        await start_conversation(client, ai)

        ai.queue_decision(
            AiDecision(
                reply_text="Let me get a colleague for you.",
                intent="human_request",
                actions=["request_human_handoff"],
                handoff_reason="customer asked for a person",
            )
        )
        await post(client, text="can I talk to a human")
        await drain_queue()

        reminders = list((await db.execute(select(Reminder))).scalars())
        assert all(r.status == ReminderStatus.CANCELLED for r in reminders)


class TestRelevanceAtSendTime:
    """State is re-checked when the reminder fires, not when it was created."""

    async def _due_reminder(self, client, db, ai):
        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)
        return reminder

    async def _mutate(self, db, fn):
        await db.write(fn)

    async def test_a_due_reminder_is_sent(self, client, db, ai, meta):
        reminder = await self._due_reminder(client, db, ai)

        ai.queue_decision(
            AiDecision(reply_text="Just checking in - any questions?", intent="small_talk")
        )
        from app.worker.runner import sweep_due_reminders

        assert await sweep_due_reminders() == 1
        await drain_queue()

        updated = await db.get(Reminder, reminder.id)
        assert updated.status == ReminderStatus.SENT
        assert len(meta.sent) == 2  # the original reply plus the follow-up

    async def test_not_sent_after_the_customer_downloaded(self, client, db, ai, meta):
        reminder = await self._due_reminder(client, db, ai)
        sent_before = len(meta.sent)

        async def _mark(session):
            customer = (await session.execute(select(Customer))).scalar_one()
            customer.downloaded_at = utcnow()

        await self._mutate(db, _mark)
        await self._run_reminder(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.status == ReminderStatus.CANCELLED
        assert updated.resolution == "customer already downloaded"
        assert len(meta.sent) == sent_before

    async def test_not_sent_after_activation(self, client, db, ai, meta):
        reminder = await self._due_reminder(client, db, ai)

        async def _mark(session):
            customer = (await session.execute(select(Customer))).scalar_one()
            customer.activated_at = utcnow()

        await self._mutate(db, _mark)
        await self._run_reminder(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.resolution == "customer already activated"

    async def test_not_sent_after_opt_out(self, client, db, ai, meta):
        reminder = await self._due_reminder(client, db, ai)

        async def _mark(session):
            customer = (await session.execute(select(Customer))).scalar_one()
            customer.opted_out_at = utcnow()

        await self._mutate(db, _mark)
        await self._run_reminder(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.resolution == "customer opted out"

    async def test_not_sent_when_a_human_took_over(self, client, db, ai, meta):
        reminder = await self._due_reminder(client, db, ai)

        async def _mark(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.handling_mode = HandlingMode.HUMAN

        await self._mutate(db, _mark)
        await self._run_reminder(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.resolution == "a human is handling this conversation"

    async def test_not_sent_when_the_conversation_is_closed(self, client, db, ai, meta):
        reminder = await self._due_reminder(client, db, ai)

        async def _mark(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.status = "closed"

        await self._mutate(db, _mark)
        await self._run_reminder(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.resolution == "conversation closed"

    async def test_not_sent_when_the_stage_no_longer_takes_follow_ups(
        self, client, db, ai, meta
    ):
        reminder = await self._due_reminder(client, db, ai)

        async def _mark(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.sales_stage = SalesStage.NOT_INTERESTED

        await self._mutate(db, _mark)
        await self._run_reminder(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert "not_interested" in updated.resolution

    async def test_not_sent_when_the_customer_already_replied(self, client, db, ai, meta):
        reminder = await self._due_reminder(client, db, ai)

        async def _mark(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow()

        await self._mutate(db, _mark)
        await self._run_reminder(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.resolution == "customer already replied"

    @staticmethod
    async def _run_reminder(reminder_id):
        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder_id)


class TestOutsideTheServiceWindow:
    async def test_a_late_follow_up_uses_a_template(self, client, db, ai, meta):
        """More than 24h after the last inbound message, free-form is not
        allowed - Meta requires an approved template."""
        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        async def _age_conversation(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)

        await db.write(_age_conversation)

        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        assert len(meta.templates) == 1
        assert meta.templates[0].template == "boomshare_followup"

        updated = await db.get(Reminder, reminder.id)
        assert updated.status == ReminderStatus.SENT
        assert updated.resolution == "template follow-up"

    async def test_the_stored_content_is_what_whatsapp_actually_showed_them(
        self, client, db, ai, meta
    ):
        """The stored `content` is not a display nicety - `context.py` feeds it
        straight back to the model as its own conversation history. A
        placeholder there means the AI's next reply is generated believing it
        said something it never sent."""
        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        async def _age_conversation(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)

        await db.write(_age_conversation)

        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        from app.models import Message

        sent = (
            await db.execute(
                select(Message).where(Message.message_type == MessageType.TEMPLATE)
            )
        ).scalar_one()

        assert sent.content != "[follow-up template]"
        assert "[" not in sent.content
        # Matches the approved body in docs/meta-setup.md, {{1}} filled in.
        assert sent.content.startswith("Hi ")
        assert "just checking in about Boomshare" in sent.content


class TestClaiming:
    async def test_a_reminder_can_only_be_claimed_once(self, client, db, ai, meta, session_factory):
        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()

        async with session_factory() as session:
            first = await reminder_service.claim(session, reminder.id)
            await session.commit()
        async with session_factory() as session:
            second = await reminder_service.claim(session, reminder.id)
            await session.commit()

        assert first is not None
        assert second is None

    async def test_claiming_counts_attempts(self, client, db, ai, meta, session_factory):
        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()

        async with session_factory() as session:
            claimed = await reminder_service.claim(session, reminder.id)
            await session.commit()

        assert claimed.attempts == 1


class TestScheduleGuards:
    async def test_no_follow_up_for_an_opted_out_customer(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Understood, I'll stop there.",
                intent="opt_out",
                actions=["opt_out", "schedule_follow_up"],
                follow_up_minutes=1440,
            )
        )
        await post(client, text="stop messaging me")
        await drain_queue()

        pending = list(
            (
                await db.execute(
                    select(Reminder).where(Reminder.status == ReminderStatus.PENDING)
                )
            ).scalars()
        )
        assert pending == []

        customer = (await db.execute(select(Customer))).scalar_one()
        assert customer.opted_out_at is not None

    async def test_the_acknowledgement_still_goes_out_before_opting_out(
        self, client, db, ai, meta
    ):
        ai.queue_decision(
            AiDecision(
                reply_text="Understood, I'll stop there.", intent="opt_out", actions=["opt_out"]
            )
        )
        await post(client, text="stop messaging me")
        await drain_queue()

        assert len(meta.texts) == 1
        assert "stop" in meta.texts[0].body.lower()

        messages = list((await db.execute(select(Message))).scalars())
        assert len(messages) == 2


class TestFollowUpSendFailure:
    """An unapproved or renamed template is the realistic cause here."""

    async def test_a_failed_template_marks_the_reminder_failed(self, client, db, ai, meta):
        from tests.fakes import meta_bad_request

        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        async def _age(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)

        await db.write(_age)
        meta.fail_template_with = meta_bad_request()

        from app.services.conversation_flow import send_follow_up

        # Must not raise: the reminder is terminal, so a traceback would only
        # obscure the real cause.
        await send_follow_up(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.status == ReminderStatus.FAILED
        assert "invalid recipient" in (updated.resolution or "")

    async def test_the_failed_send_is_recorded_on_a_message_row(self, client, db, ai, meta):
        from tests.fakes import meta_bad_request

        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        async def _age(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)

        await db.write(_age)
        meta.fail_template_with = meta_bad_request()

        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        failed = list(
            (
                await db.execute(select(Message).where(Message.status == "failed"))
            ).scalars()
        )
        assert len(failed) == 1
        assert failed[0].error["status_code"] == 400

    async def test_a_failed_reminder_is_not_retried(self, client, db, ai, meta):
        """`claim` only takes pending rows, so a terminal reminder stays terminal."""
        from tests.fakes import meta_bad_request

        await start_conversation(client, ai)
        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        async def _age(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            conversation.last_inbound_at = utcnow() - timedelta(hours=30)

        await db.write(_age)
        meta.fail_template_with = meta_bad_request()

        from app.services.conversation_flow import send_follow_up
        from app.worker.runner import sweep_due_reminders

        await send_follow_up(reminder.id)
        sent_before = len(meta.sent)

        assert await sweep_due_reminders() == 0
        await send_follow_up(reminder.id)
        assert len(meta.sent) == sent_before


class TestTheDeferredCallback:
    """The live failure: a promised five-minute callback that never went out.

    Transcript, verbatim from a real test conversation:

        customer  "Not right now"                (they had no computer to hand)
        agent     "No problem! If you want, I can check back later."
        customer  "After 5 minutes I will have"
        agent     "Sounds good! I'll check back with you in 5 minutes."

    Nothing was ever sent. Three separate defects had to line up: the model
    read "not right now" as a refusal, `not_interested` blocks every follow-up
    and nothing ever lifted it, and a five-minute delay expressed in hours was
    rounded to zero and then clamped up to an hour.
    """

    @staticmethod
    def _defers(minutes: int) -> AiDecision:
        """What the model actually returns when a customer says "not yet"."""
        return AiDecision(
            reply_text=f"Sounds good! I'll check back with you in {minutes} minutes.",
            intent="information_request",
            suggested_stage=SalesStage.NOT_INTERESTED,
            actions=["schedule_follow_up", "mark_not_interested"],
            follow_up_minutes=minutes,
        )

    async def test_a_five_minute_promise_is_scheduled_for_five_minutes(
        self, client, db, ai, meta
    ):
        ai.queue_decision(self._defers(5))
        await post(client, text="After 5 minutes I will have")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        assert reminder.status == ReminderStatus.PENDING

        due_in = as_utc(reminder.due_at) - utcnow()
        assert timedelta(minutes=4) < due_in <= timedelta(minutes=5)

    async def test_the_conversation_is_not_written_off(self, client, db, ai, meta):
        """`not_interested` would have silently cancelled the callback."""
        ai.queue_decision(self._defers(5))
        await post(client, text="After 5 minutes I will have")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage != SalesStage.NOT_INTERESTED

        decision = (await db.execute(select(AiDecisionLog))).scalar_one()
        assert "deferral_not_refusal" in (decision.rejected_reasons or {})
        assert "schedule_follow_up" in (decision.executed_actions or {})["actions"]

    async def test_the_callback_actually_reaches_the_customer(self, client, db, ai, meta):
        """The whole point: five minutes later, a message goes out."""
        ai.queue_decision(self._defers(5))
        await post(client, text="After 5 minutes I will have")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        ai.queue_decision(
            AiDecision(
                reply_text="Ready when you are - shall I send the download?",
                intent="information_request",
            )
        )
        from app.services.conversation_flow import send_follow_up

        sent_before = len(meta.texts)
        await send_follow_up(reminder.id)

        assert len(meta.texts) == sent_before + 1
        assert (await db.get(Reminder, reminder.id)).status == ReminderStatus.SENT

    async def test_a_customer_who_writes_back_is_no_longer_written_off(
        self, client, db, ai, meta
    ):
        """Even a stage we set in error must not be a dead end.

        `not_interested` stops every follow-up there will ever be, so the one
        signal that clearly contradicts it - the customer messaging us - has to
        lift it without waiting for the model to suggest it.
        """
        ai.queue_decision(
            AiDecision(
                reply_text="No problem, I'll leave it there.",
                intent="not_interested",
                actions=["mark_not_interested"],
            )
        )
        await post(client, text="not interested")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.NOT_INTERESTED

        ai.queue_decision(
            AiDecision(
                reply_text="Great - I'll send it over.",
                intent="buying_intent",
                actions=["schedule_follow_up"],
                follow_up_minutes=5,
            )
        )
        await post(client, text="actually, I have my laptop now")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage == SalesStage.ENGAGED

        pending = list(
            (
                await db.execute(select(Reminder).where(Reminder.status == ReminderStatus.PENDING))
            ).scalars()
        )
        assert len(pending) == 1


class TestTheFollowUpLadder:
    """Following up has to end.

    Every turn queued a check-in, and every check-in was itself a turn, so a
    customer who simply stopped answering was messaged on the same 20-hour
    cycle forever. Verified before the fix: six nudges and still going, with no
    stage that would ever stop it.
    """

    async def test_the_gaps_widen_with_each_unanswered_message(self):
        ladder = reminder_service.follow_up_ladder()
        assert list(ladder) == sorted(ladder), "each check-in should wait longer than the last"

    async def test_the_ladder_ends(self):
        rungs = len(reminder_service.follow_up_ladder())
        assert reminder_service.follow_up_delay(rungs - 1) is not None
        assert reminder_service.follow_up_delay(rungs) is None

    async def test_a_silent_customer_is_not_messaged_forever(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(reply_text="Boomshare records your screen. What for?", intent="greeting")
        )
        await post(client, text="hi")
        await drain_queue()
        replies_to_a_real_message = len(meta.sent)

        from app.services.conversation_flow import send_follow_up

        for index in range(len(reminder_service.follow_up_ladder()) + 3):
            pending = list(
                (
                    await db.execute(
                        select(Reminder).where(Reminder.status == ReminderStatus.PENDING)
                    )
                ).scalars()
            )
            if not pending:
                break
            await make_due(db, pending[0].id)
            ai.queue_decision(AiDecision(reply_text=f"nudge {index}", intent="small_talk"))
            await send_follow_up(pending[0].id)

        nudges = len(meta.sent) - replies_to_a_real_message
        assert nudges == len(reminder_service.follow_up_ladder())

    async def test_a_customer_reply_resets_the_ladder(self, client, db, ai, meta):
        """Answering is the one signal that the conversation is still alive."""
        ai.queue_decision(AiDecision(reply_text="Hey! What would you record?", intent="greeting"))
        await post(client, text="hi")
        await drain_queue()

        from app.services.conversation_flow import send_follow_up

        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)
        # Distinct from the reply above: an identical one would be suppressed
        # as a repeat and would never count against the ladder.
        ai.queue_decision(AiDecision(reply_text="Still there?", intent="small_talk"))
        await send_follow_up(reminder.id)

        async def _count(session):
            conversation = (await session.execute(select(Conversation))).scalar_one()
            return await reminder_service.follow_ups_since_reply(session, conversation)

        assert await db.write(_count) == 1

        await post(client, text="sorry, was busy")
        await drain_queue()

        assert await db.write(_count) == 0

    async def test_each_check_in_is_told_to_say_something_new(self, client, db, ai, meta):
        """The second nudge repeating the first is how a sequence becomes noise."""
        ai.queue_decision(AiDecision(reply_text="What would you record?", intent="greeting"))
        await post(client, text="hi")
        await drain_queue()

        from app.services.conversation_flow import send_follow_up

        directives = []
        for _ in range(len(reminder_service.follow_up_ladder())):
            pending = list(
                (
                    await db.execute(
                        select(Reminder).where(Reminder.status == ReminderStatus.PENDING)
                    )
                ).scalars()
            )
            if not pending:
                break
            await make_due(db, pending[0].id)
            ai.queue_decision(
                AiDecision(reply_text=f"checking in #{len(directives) + 1}", intent="small_talk")
            )
            await send_follow_up(pending[0].id)
            directives.append(
                next(
                    m["content"]
                    for m in ai.last_prompt
                    if m["role"] == "system" and m["content"].startswith("# What to do right now")
                )
            )

        assert len(directives) == len(set(directives)), "two check-ins were given the same job"
        assert "last time" in directives[-1], "the final nudge should say goodbye, not trail off"

    async def test_a_promised_callback_is_still_kept(self, client, db, ai, meta):
        """The cap is on nudges nobody asked for, not on promises we made."""
        ai.queue_decision(
            AiDecision(
                reply_text="No problem - I'll check back in five minutes.",
                intent="information_request",
                actions=["schedule_follow_up"],
                follow_up_minutes=5,
            )
        )
        await post(client, text="give me five minutes")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        assert reminder.status == ReminderStatus.PENDING
        assert as_utc(reminder.due_at) - utcnow() <= timedelta(minutes=5)


class TestOnePersonOneNudge:
    """Sara wrote to both our numbers and was chased twice, half a second apart.

    Being followed up is something that happens to a person. The service window
    and the sales stage belong to a thread; the customer's patience does not.
    """

    async def test_a_second_thread_does_not_chase_the_same_person_again(
        self, db, session_factory
    ):
        import uuid as _uuid
        from datetime import timedelta

        from app.core.clock import utcnow
        from app.domain import ReminderStatus
        from app.models import Conversation, Customer, Reminder
        from app.services import reminders as reminder_service

        async def arrange(session):
            customer = Customer(phone="393481932788", wa_id="393481932788")
            session.add(customer)
            await session.flush()

            threads = []
            for number in ("111222333", "444555666"):
                conversation = Conversation(
                    customer_id=customer.id,
                    channel="whatsapp",
                    phone_number_id=number,
                    sales_stage=SalesStage.LINK_SENT,
                    stage_updated_at=utcnow(),
                )
                session.add(conversation)
                threads.append(conversation)
            await session.flush()

            # The first thread has just chased them.
            session.add(
                Reminder(
                    conversation_id=threads[0].id,
                    customer_id=customer.id,
                    kind="follow_up",
                    status=ReminderStatus.SENT,
                    due_at=utcnow() - timedelta(minutes=1),
                    sent_at=utcnow() - timedelta(minutes=1),
                    reason="automatic check-in 1 of 3",
                )
            )
            # The second thread is about to.
            pending = Reminder(
                conversation_id=threads[1].id,
                customer_id=customer.id,
                kind="follow_up",
                status=ReminderStatus.PROCESSING,
                due_at=utcnow(),
                reason="automatic check-in 1 of 3",
            )
            session.add(pending)
            await session.flush()
            return pending.id, threads[1].id, customer.id

        pending_id, thread_id, customer_id = await db.write(arrange)

        async with session_factory() as session:
            reminder = await session.get(Reminder, pending_id)
            conversation = await session.get(Conversation, thread_id)
            customer = await session.get(Customer, customer_id)

            block = await reminder_service.relevance_block(
                session, reminder, conversation, customer
            )
            assert block == "already followed up on another thread"

    async def test_a_lone_thread_is_still_chased(self, db, session_factory):
        """The guard must not silence the ordinary case."""
        from app.core.clock import utcnow
        from app.domain import ReminderStatus
        from app.models import Conversation, Customer, Reminder
        from app.services import reminders as reminder_service

        async def arrange(session):
            customer = Customer(phone="919999999999", wa_id="919999999999")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                customer_id=customer.id,
                channel="whatsapp",
                phone_number_id="111222333",
                sales_stage=SalesStage.LINK_SENT,
                stage_updated_at=utcnow(),
            )
            session.add(conversation)
            await session.flush()
            reminder = Reminder(
                conversation_id=conversation.id,
                customer_id=customer.id,
                kind="follow_up",
                status=ReminderStatus.PROCESSING,
                due_at=utcnow(),
                reason="automatic check-in 1 of 3",
            )
            session.add(reminder)
            await session.flush()
            return reminder.id, conversation.id, customer.id

        reminder_id, conversation_id, customer_id = await db.write(arrange)

        async with session_factory() as session:
            block = await reminder_service.relevance_block(
                session,
                await session.get(Reminder, reminder_id),
                await session.get(Conversation, conversation_id),
                await session.get(Customer, customer_id),
            )
            assert block is None

"""The callback a customer asked for: "I'll be back in 5 minutes with my laptop".

A live run produced twelve promised callbacks in forty-five minutes and
delivered one:

    10:19  created -> cancelled  would have repeated the last message
    10:23  created -> cancelled  would have repeated the last message
    10:26  created -> cancelled  would have repeated the last message
    ...
    10:39  created -> sent       ai follow-up
    10:40  created -> cancelled  would have repeated the last message
    10:43  created -> cancelled  would have repeated the last message

Each one fired, produced a reply identical to the message before it, had that
reply suppressed - and then scheduled the next callback anyway, because the
decision's actions ran whether or not anything had been sent. The customer saw
silence while the system looked busy.

Three rules come out of that, and each is tested here:

  * a callback is a promise made *inside* a reply, so a turn that sent nothing
    cannot make one
  * pushing the time back moves the callback, it does not add one
  * at most two of them, ever - the automatic ladder is separate
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.ai.schemas import AiDecision
from app.core.clock import as_utc, utcnow
from app.models import AiDecisionLog, Conversation, Reminder
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue


async def post(client, text):
    body, headers = signed(whatsapp_message_payload(text=text))
    assert (await client.post("/webhooks/meta", content=body, headers=headers)).status_code == 200


async def make_due(db, reminder_id):
    async def _update(session):
        (await session.get(Reminder, reminder_id)).due_at = utcnow() - timedelta(minutes=1)

    await db.write(_update)


async def promised(db, *, status: str | None = None) -> list[Reminder]:
    """The callbacks the customer asked for, as opposed to automatic check-ins."""
    rows = (await db.execute(select(Reminder))).scalars()
    return [
        r
        for r in rows
        if (r.payload or {}).get("promise") and (status is None or r.status == status)
    ]


def defers(minutes: int, reason: str):
    return AiDecision(
        reply_text=f"Sounds good - I'll check back in {minutes} minutes.",
        intent="information_request",
        actions=["schedule_follow_up"],
        follow_up_minutes=minutes,
        follow_up_reason=reason,
    )


class TestTheCallbackArrives:
    async def _promise_then_fire(self, client, db, ai, meta, follow_up: AiDecision):
        ai.queue_decision(defers(5, "they are fetching a laptop"))
        await post(client, "back in 5 minutes with my laptop")
        await drain_queue()

        reminder = (await promised(db, status="pending"))[0]
        await make_due(db, reminder.id)

        ai.queue_decision(follow_up)
        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

    async def test_the_callback_is_delivered(self, client, db, ai, meta):
        await self._promise_then_fire(
            client,
            db,
            ai,
            meta,
            AiDecision(reply_text="Are you at your laptop now?", intent="small_talk"),
        )
        assert meta.texts[-1].body == "Are you at your laptop now?"

    async def test_a_suppressed_callback_does_not_schedule_another(self, client, db, ai, meta):
        """The chain: the model repeats itself *and* asks to check back again.

        The repeat is suppressed, so no promise was made - and there must be
        nothing left to keep.
        """
        await self._promise_then_fire(
            client,
            db,
            ai,
            meta,
            AiDecision(
                reply_text="Sounds good - I'll check back in 5 minutes.",
                intent="small_talk",
                actions=["schedule_follow_up"],
                follow_up_minutes=5,
                follow_up_reason="still fetching a laptop",
            ),
        )

        callbacks = await promised(db)
        assert len(callbacks) == 1, "a suppressed callback scheduled another one"
        assert callbacks[0].status != "pending"

    async def test_the_refusal_is_recorded(self, client, db, ai, meta):
        await self._promise_then_fire(
            client,
            db,
            ai,
            meta,
            AiDecision(
                reply_text="Sounds good - I'll check back in 5 minutes.",
                intent="small_talk",
                actions=["schedule_follow_up"],
                follow_up_minutes=5,
                follow_up_reason="still fetching a laptop",
            ),
        )
        decision = (await db.execute(select(AiDecisionLog))).scalars().all()[-1]
        assert "follow_up_without_a_reply" in (decision.rejected_reasons or {})

    async def test_a_delivered_callback_may_still_schedule_the_next(self, client, db, ai, meta):
        """The gate is "did anything go out", not "is this a follow-up"."""
        await self._promise_then_fire(
            client,
            db,
            ai,
            meta,
            AiDecision(
                reply_text="Still not at your laptop? I'll try again shortly.",
                intent="small_talk",
                actions=["schedule_follow_up"],
                follow_up_minutes=10,
                follow_up_reason="they are still fetching a laptop",
            ),
        )
        pending = await promised(db, status="pending")
        assert len(pending) == 1
        assert pending[0].payload["promise"] == "they are still fetching a laptop"

    async def test_only_the_callback_is_gated_not_the_other_actions(
        self, client, db, ai, meta
    ):
        """Handing off is a fact about the customer. It stands whether or not
        our message reached them."""
        ai.queue_decision(AiDecision(reply_text="One moment.", intent="greeting"))
        await post(client, "hi")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="One moment.",  # identical - will be flagged as a repeat
                intent="human_request",
                actions=["request_human_handoff"],
                handoff_reason="asked for a person",
            )
        )
        await post(client, "get me a human please")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.handling_mode == "human"


class TestPushingTheTimeBack:
    """"Actually, make it 10 minutes" moves the callback - it does not add one."""

    async def test_the_earlier_callback_is_replaced(self, client, db, ai, meta):
        ai.queue_decision(defers(5, "they are fetching a laptop in 5 minutes"))
        await post(client, "back in 5 minutes")
        await drain_queue()

        ai.queue_decision(defers(10, "they pushed it back to 10 minutes"))
        await post(client, "actually make it 10 minutes")
        await drain_queue()

        pending = [
            r for r in (await db.execute(select(Reminder))).scalars() if r.status == "pending"
        ]
        assert len(pending) == 1, "two callbacks are live at once"
        assert pending[0].payload["promise"] == "they pushed it back to 10 minutes"

    async def test_the_new_time_is_the_one_that_is_used(self, client, db, ai, meta):
        ai.queue_decision(defers(5, "five minutes"))
        await post(client, "back in 5 minutes")
        await drain_queue()

        ai.queue_decision(defers(10, "ten minutes"))
        await post(client, "actually make it 10 minutes")
        await drain_queue()

        pending = (await promised(db, status="pending"))[0]
        due_in = as_utc(pending.due_at) - utcnow()
        assert timedelta(minutes=9) < due_in <= timedelta(minutes=10)


class TestAtMostTwoPromisedCallbacks:
    """They asked twice; a third chase is pestering. The ladder is separate."""

    async def _defer_and_fire(self, client, db, ai, meta, minutes: int, nth: int) -> bool:
        """Returns whether a callback was actually scheduled."""
        ai.queue_decision(defers(minutes, f"deferral {nth}"))
        await post(client, f"give me {minutes} more minutes")
        await drain_queue()

        pending = await promised(db, status="pending")
        if not pending:
            return False

        await make_due(db, pending[0].id)
        ai.queue_decision(
            AiDecision(reply_text=f"Ready when you are? ({nth})", intent="small_talk")
        )
        from app.services.conversation_flow import send_follow_up

        await send_follow_up(pending[0].id)
        return True

    async def test_two_are_honoured_and_the_third_is_refused(self, client, db, ai, meta):
        assert await self._defer_and_fire(client, db, ai, meta, 5, 1)
        assert await self._defer_and_fire(client, db, ai, meta, 10, 2)
        assert not await self._defer_and_fire(client, db, ai, meta, 15, 3), (
            "a third promised callback was scheduled"
        )
        assert len(await promised(db, status="sent")) == 2

    async def test_the_cap_is_recorded(self, client, db, ai, meta):
        await self._defer_and_fire(client, db, ai, meta, 5, 1)
        await self._defer_and_fire(client, db, ai, meta, 10, 2)
        await self._defer_and_fire(client, db, ai, meta, 15, 3)

        decision = (await db.execute(select(AiDecisionLog))).scalars().all()[-1]
        assert "promised_follow_ups_exhausted" in (decision.rejected_reasons or {})

    async def test_the_customer_is_still_answered_after_the_cap(self, client, db, ai, meta):
        """Refusing a third callback must not refuse a third reply."""
        await self._defer_and_fire(client, db, ai, meta, 5, 1)
        await self._defer_and_fire(client, db, ai, meta, 10, 2)
        before = len(meta.texts)
        await self._defer_and_fire(client, db, ai, meta, 15, 3)

        assert len(meta.texts) > before, "the customer's message went unanswered"

    async def test_the_automatic_ladder_is_untouched(self, client, db, ai, meta):
        """Capping deferrals must not silence the ordinary no-reply check-ins."""
        await self._defer_and_fire(client, db, ai, meta, 5, 1)
        await self._defer_and_fire(client, db, ai, meta, 10, 2)

        rows = (await db.execute(select(Reminder))).scalars()
        assert [r for r in rows if not (r.payload or {}).get("promise")], (
            "the automatic ladder stopped scheduling"
        )

    async def test_an_undelivered_callback_does_not_count(self, client, db, ai, meta):
        """The cap counts what the customer received, not what was queued."""
        ai.queue_decision(defers(5, "first deferral"))
        await post(client, "back in 5")
        await drain_queue()

        # Cancelled before it fires - they replied instead.
        ai.queue_decision(AiDecision(reply_text="No rush at all.", intent="small_talk"))
        await post(client, "still here")
        await drain_queue()

        assert await self._defer_and_fire(client, db, ai, meta, 10, 2)
        assert len(await promised(db, status="sent")) == 1

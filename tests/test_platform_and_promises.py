"""Two failures from one live conversation.

Transcript, verbatim:

    customer  "Office"
    agent     "...Want me to send you the download so you can try it for free?"
    customer  "Yes in mobile"
    agent     "There's no mobile app for Boomshare right now, but it works on
               Windows and Mac..."
              https://boomshare.ai/download?ref=...
    customer  "Ok I am coming with laptop in 4 minutes"
    agent     "Sounds good! I'll check back with you in 4 minutes..."
    (4 minutes later)
    agent     "Sounds good! I'll check back with you in 4 minutes..."

Two things went wrong and they are independent:

1. The reply said there is no mobile app and carried a desktop installer
   underneath it. Nothing checked the machine the customer had just named.
2. The promised callback arrived as a word-for-word copy of the promise,
   because nothing recorded what the callback was *about*.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.ai.objectives import follow_up_objective, next_objective
from app.ai.schemas import AiDecision
from app.core.clock import as_utc, utcnow
from app.domain import CustomerPlatform, SalesStage, read_platform
from app.models import AiDecisionLog, Conversation, DownloadLink, Reminder
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue


async def post(client, text):
    body, headers = signed(whatsapp_message_payload(text=text))
    assert (await client.post("/webhooks/meta", content=body, headers=headers)).status_code == 200


def directive(ai) -> str:
    for message in ai.last_prompt:
        if message["role"] == "system" and message["content"].startswith("# What to do right now"):
            return message["content"]
    return ""


async def make_due(db, reminder_id):
    async def _update(session):
        (await session.get(Reminder, reminder_id)).due_at = utcnow() - timedelta(minutes=1)

    await db.write(_update)


class TestReadingTheReportedPlatform:
    """The backend no longer classifies - it only reads back what was reported.

    This was a substring list, and it failed on everything a real person types:
    `Samsung Galaxy`, `Redmi Note 12`, `my cell`, `celular` and `Pixel 8` all
    came back "not recognised", and the installer went out anyway. Placing an
    unconstrained sentence into one of four buckets is the model's job.
    """

    def test_the_reported_values_round_trip(self):
        assert read_platform("windows") is CustomerPlatform.WINDOWS
        assert read_platform("macos") is CustomerPlatform.MACOS
        assert read_platform("other") is CustomerPlatform.OTHER
        assert read_platform("unknown") is CustomerPlatform.UNKNOWN

    def test_only_the_two_desktop_builds_have_an_installer(self):
        assert CustomerPlatform.WINDOWS.has_installer
        assert CustomerPlatform.MACOS.has_installer
        assert not CustomerPlatform.OTHER.has_installer
        assert not CustomerPlatform.UNKNOWN.has_installer

    def test_anything_unrecognised_reads_as_unknown_never_as_other(self):
        """Concluding "they cannot run it" from a word we do not recognise is
        the mistake this field exists to stop making."""
        for value in (None, "", "  ", "mobile", "Samsung Galaxy", "celular", "not sure"):
            assert read_platform(value) is CustomerPlatform.UNKNOWN, repr(value)


class TestTheInstallerIsNotSentToAPhone:
    @staticmethod
    def _accepts_on_mobile():
        """What the model actually returned: accepted, and on a phone."""
        return AiDecision(
            reply_text=(
                "There's no mobile app for Boomshare right now, but it works on Windows "
                "and Mac. If you have a laptop, I can send you the download link for that."
            ),
            intent="download_request",
            actions=["send_download_link"],
            customer_platform=CustomerPlatform.OTHER,
        )

    async def test_no_link_is_attached(self, client, db, ai, meta):
        ai.queue_decision(self._accepts_on_mobile())
        await post(client, "Yes in mobile")
        await drain_queue()

        assert "http" not in meta.texts[-1].body

    async def test_no_link_is_even_created(self, client, db, ai, meta):
        ai.queue_decision(self._accepts_on_mobile())
        await post(client, "Yes in mobile")
        await drain_queue()

        assert (await db.execute(select(func.count()).select_from(DownloadLink))).scalar() == 0

    async def test_the_reason_is_recorded(self, client, db, ai, meta):
        ai.queue_decision(self._accepts_on_mobile())
        await post(client, "Yes in mobile")
        await drain_queue()

        decision = (await db.execute(select(AiDecisionLog))).scalars().all()[-1]
        assert "download_withheld" in (decision.rejected_reasons or {})
        assert "send_download_link" not in decision.executed_actions["actions"]

    async def test_the_words_still_go_out(self, client, db, ai, meta):
        """Only the attachment was wrong; the explanation is the useful part."""
        ai.queue_decision(self._accepts_on_mobile())
        await post(client, "Yes in mobile")
        await drain_queue()

        assert "no mobile app" in meta.texts[-1].body

    async def test_the_funnel_does_not_claim_a_link_was_sent(self, client, db, ai, meta):
        ai.queue_decision(self._accepts_on_mobile())
        await post(client, "Yes in mobile")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.sales_stage != SalesStage.LINK_SENT

    async def test_it_is_withheld_not_merely_deferred(self, client, db, ai, meta):
        """Offering a download to someone on a phone is as wrong as sending it."""
        ai.queue_decision(self._accepts_on_mobile())
        await post(client, "Yes in mobile")
        await drain_queue()

        decision = (await db.execute(select(AiDecisionLog))).scalars().all()[-1]
        assert "offer_download_link" not in decision.executed_actions["actions"]

    async def test_linux_is_withheld_too(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Linux is in development, coming soon. Any chance you have a Mac?",
                intent="download_request",
                actions=["send_download_link"],
                customer_platform=CustomerPlatform.OTHER,
            )
        )
        await post(client, "I'm on linux")
        await drain_queue()

        assert "http" not in meta.texts[-1].body

    async def test_a_windows_customer_is_completely_unaffected(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go - takes about a minute to install.",
                intent="download_request",
                actions=["send_download_link"],
                customer_platform=CustomerPlatform.WINDOWS,
            )
        )
        await post(client, "send me the link, I'm on windows")
        await drain_queue()

        assert "platform=windows" in meta.texts[-1].body

    async def test_an_unrecognised_platform_does_not_block_the_download(
        self, client, db, ai, meta
    ):
        """The regression risk: withholding from everyone whose words we
        could not place would be worse than the bug being fixed."""
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go - are you on Windows or a Mac?",
                intent="download_request",
                actions=["send_download_link"],
                customer_platform=CustomerPlatform.UNKNOWN,
            )
        )
        await post(client, "send it over")
        await drain_queue()

        assert "http" in meta.texts[-1].body


class TestReachingALaptopReleasesIt:
    """The stale note must not block them forever."""

    async def test_the_download_flows_once_they_name_a_real_machine(
        self, client, db, ai, meta
    ):
        ai.queue_decision(
            AiDecision(
                reply_text="No mobile app yet - Windows and Mac. Do you have a laptop?",
                intent="feature_question",
                customer_platform=CustomerPlatform.OTHER,
            )
        )
        await post(client, "Yes in mobile")
        await drain_queue()

        ai.queue_decision(
            AiDecision(
                reply_text="Great - here you go.",
                intent="download_request",
                actions=["send_download_link"],
                customer_platform=CustomerPlatform.WINDOWS,
            )
        )
        await post(client, "I'm on my windows laptop now")
        await drain_queue()

        assert "http" in meta.texts[-1].body
        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.context_notes["platform"] == "windows"


class TestTheObjectiveReactsOnTheSameTurn:
    """It used to be checked inside the `download_suggested` branch only, so the
    turn that needed it was judged on the stage it was in before."""

    def test_an_unsupported_platform_outranks_the_funnel_position(self):
        for stage in (
            SalesStage.ENGAGED,
            SalesStage.QUALIFIED,
            SalesStage.PRODUCT_EXPLAINED,
            SalesStage.DOWNLOAD_SUGGESTED,
            SalesStage.OBJECTION_HANDLING,
        ):
            objective = next_objective(stage, {"platform": "other"}, replies_sent=3)
            assert "does not ship for" in objective, stage

    def test_it_asks_when_they_will_be_at_a_machine(self):
        objective = next_objective(SalesStage.ENGAGED, {"platform": "other"})
        assert "schedule_follow_up" in objective
        assert "follow_up_reason" in objective

    def test_it_states_what_does_run(self):
        objective = next_objective(SalesStage.ENGAGED, {"platform": "other"})
        assert "Windows and Mac" in objective
        assert "Linux" in objective

    def test_it_still_releases_the_link_when_they_reach_a_laptop(self):
        """The objective is built before the model answers, so on the turn they
        say "I'm on my laptop now" it is still reading the old note. It must not
        tell the model to withhold a download the guardrails will allow."""
        objective = next_objective(SalesStage.ENGAGED, {"platform": "other"})
        assert "hand the download over as normal" in objective

    def test_a_sent_link_still_wins(self):
        objective = next_objective(
            SalesStage.ENGAGED, {"platform": "other"}, download_link_sent=True
        )
        assert "do not re-offer the link" in objective

    def test_an_unknown_platform_leaves_the_ladder_alone(self):
        assert next_objective(SalesStage.ENGAGED, {}, replies_sent=0) == next_objective(
            SalesStage.ENGAGED, {"platform": "unknown"}, replies_sent=0
        )


class TestThePromisedCallback:
    """"I'll check back in 4 minutes" has to come back as something else."""

    @staticmethod
    def _promises(minutes: int = 4):
        return AiDecision(
            reply_text=f"Sounds good! I'll check back with you in {minutes} minutes.",
            intent="information_request",
            actions=["schedule_follow_up"],
            follow_up_minutes=minutes,
            follow_up_reason="they said they would be at their laptop in 4 minutes",
        )

    async def test_the_promise_is_stored_with_the_reminder(self, client, db, ai, meta):
        ai.queue_decision(self._promises())
        await post(client, "Ok I am coming with laptop in 4 minutes")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        assert reminder.payload["promise"] == "they said they would be at their laptop in 4 minutes"
        # The reason is what an operator reads on the row; it used to be a
        # constant copied from `handoff_reason`, a different field entirely.
        assert reminder.reason == "they said they would be at their laptop in 4 minutes"

    async def test_the_follow_up_is_told_what_it_is_about(self, client, db, ai, meta):
        ai.queue_decision(self._promises())
        await post(client, "Ok I am coming with laptop in 4 minutes")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        ai.queue_decision(AiDecision(reply_text="Are you at your laptop?", intent="small_talk"))
        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        assert "at their laptop in 4 minutes" in directive(ai)
        assert "moment you promised to come back" in directive(ai)

    async def test_the_promise_beats_the_generic_ladder(self):
        promised = follow_up_objective(1, promised="they'd be at their laptop")
        generic = follow_up_objective(1)
        assert promised != generic
        assert "they'd be at their laptop" in promised

    async def test_a_check_in_nobody_agreed_to_still_uses_the_ladder(self):
        assert "moment you promised" not in follow_up_objective(1)

    async def test_the_callback_still_arrives_on_time(self, client, db, ai, meta):
        ai.queue_decision(self._promises())
        await post(client, "Ok I am coming with laptop in 4 minutes")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        due_in = as_utc(reminder.due_at) - utcnow()
        assert timedelta(minutes=3) < due_in <= timedelta(minutes=4)


class TestNeverSendingTheSameMessageTwice:
    async def test_a_follow_up_that_repeats_the_promise_is_suppressed(
        self, client, db, ai, meta
    ):
        ai.queue_decision(
            AiDecision(
                reply_text="Sounds good! I'll check back with you in 4 minutes.",
                intent="information_request",
                actions=["schedule_follow_up"],
                follow_up_minutes=4,
                follow_up_reason="they are fetching a laptop",
            )
        )
        await post(client, "back in 4 minutes")
        await drain_queue()
        sent_before = len(meta.texts)

        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        # The exact failure: the model hands back the promise as the follow-up.
        ai.queue_decision(
            AiDecision(
                reply_text="Sounds good! I'll check back with you in 4 minutes.",
                intent="small_talk",
            )
        )
        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        assert len(meta.texts) == sent_before, "the customer was sent the same message twice"

    async def test_the_reminder_is_not_recorded_as_sent(self, client, db, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="I'll check back with you shortly.",
                intent="information_request",
                actions=["schedule_follow_up"],
                follow_up_minutes=4,
                follow_up_reason="they are fetching a laptop",
            )
        )
        await post(client, "back in 4 minutes")
        await drain_queue()

        reminder = (await db.execute(select(Reminder))).scalar_one()
        await make_due(db, reminder.id)

        ai.queue_decision(
            AiDecision(reply_text="I'll check back with you shortly.", intent="small_talk")
        )
        from app.services.conversation_flow import send_follow_up

        await send_follow_up(reminder.id)

        updated = await db.get(Reminder, reminder.id)
        assert updated.status == "cancelled"
        assert "repeat" in (updated.resolution or "")

    async def test_punctuation_and_case_do_not_hide_a_repeat(self, client, db, ai, meta):
        from app.ai.guardrails import is_repeat

        assert is_repeat("I'll check back in 4 minutes!", "I'll check back in 4 minutes.")
        assert is_repeat("Are you  at your laptop?", "are you at your laptop")
        assert not is_repeat("Are you at your laptop?", "Are you at your desk?")
        assert not is_repeat("anything", None)
        assert not is_repeat("", "anything")

    async def test_a_customer_question_is_still_answered(self, client, db, ai, meta):
        """A repeat is poor; leaving a question hanging is worse."""
        ai.queue_decision(AiDecision(reply_text="It records your screen.", intent="greeting"))
        await post(client, "what is it")
        await drain_queue()

        ai.queue_decision(
            AiDecision(reply_text="It records your screen.", intent="information_request")
        )
        await post(client, "sorry, what does it do?")
        await drain_queue()

        assert len(meta.texts) == 2, "the customer's question went unanswered"
        decision = (await db.execute(select(AiDecisionLog))).scalars().all()[-1]
        assert "duplicate_reply" in (decision.rejected_reasons or {})

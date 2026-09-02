"""The objective ladder - what each turn is told to accomplish.

These are the tests that would have caught the original failure: an agent that
answered every question well and never moved the conversation anywhere.
"""

from __future__ import annotations

from sqlalchemy import select

from app.ai.objectives import QUALIFICATION_SLOTS, missing_slots, next_objective
from app.ai.schemas import AiDecision
from app.domain import SalesStage
from app.models import Conversation
from tests.factories import signed, whatsapp_message_payload
from tests.helpers import drain_queue


async def post(client, **kwargs):
    body, headers = signed(whatsapp_message_payload(**kwargs))
    response = await client.post("/webhooks/meta", content=body, headers=headers)
    assert response.status_code == 200


def system_prompt(ai) -> str:
    """Everything the model was told on the most recent call."""
    return "\n".join(m["content"] for m in ai.calls[-1] if m["role"] == "system")


def directive(ai) -> str:
    """Just the turn's objective block, not the whole standing prompt.

    Asserting against the full prompt gives false positives: the standing
    instructions describe the same slots the directives ask about.
    """
    marker = "# What to do right now"
    blocks = [m["content"] for m in ai.calls[-1] if m["role"] == "system"]
    for block in blocks:
        if block.startswith(marker):
            return block
    return ""


class TestQualificationSlots:
    """Notes are a memory, not a checklist. Nothing gates on them except platform."""

    def test_nothing_known_means_everything_is_missing(self):
        assert missing_slots({}) == list(QUALIFICATION_SLOTS)
        assert missing_slots(None) == list(QUALIFICATION_SLOTS)

    def test_known_slots_drop_out(self):
        assert missing_slots({"use_case": "onboarding"}) == ["role", "platform"]

    def test_blank_values_still_count_as_missing(self):
        assert "use_case" in missing_slots({"use_case": "   "})

    def test_a_platform_we_cannot_ship_to_is_still_missing(self):
        """"They are on a phone" answers the question without making them
        installable, so the slot stays open - there is no build to choose."""
        assert "platform" in missing_slots({"platform": "other"})
        assert "platform" not in missing_slots({"platform": "macos"})
        assert "platform" not in missing_slots({"platform": "windows"})

    def test_free_text_left_by_an_older_turn_counts_as_unknown(self):
        """Notes written before the model reported this field hold prose. It is
        read as "not known", never as "cannot run it"."""
        assert "platform" in missing_slots({"platform": "mobile"})
        assert "platform" in missing_slots({"platform": "Samsung Galaxy"})


class TestTheLadderLeadsWithValue:
    """The failure this replaced: an ad click met with a discovery interview."""

    def test_the_first_reply_pitches_rather_than_interrogates(self):
        objective = next_objective(SalesStage.ENGAGED, {}, replies_sent=0)
        assert "what Boomshare is" in objective
        assert "free to start" in objective
        assert "Do NOT ask what they do" in objective

    def test_the_second_reply_connects_to_an_outcome_and_offers(self):
        objective = next_objective(SalesStage.ENGAGED, {}, replies_sent=1)
        assert "concrete outcome" in objective
        assert "offer to send the download" in objective

    def test_later_turns_never_dead_end(self):
        """'Nothing' used to end with 'feel free to reach out'. It must not."""
        objective = next_objective(SalesStage.ENGAGED, {}, replies_sent=3)
        assert "never end on a dead end" in objective
        assert "do not give up" in objective

    def test_the_ladder_does_not_depend_on_collected_facts(self):
        """A customer who volunteers nothing must still get the full sequence."""
        assert next_objective(SalesStage.ENGAGED, {}, replies_sent=0) != next_objective(
            SalesStage.ENGAGED, {}, replies_sent=1
        )

    def test_no_objective_instructs_a_discovery_interview(self):
        """The exact instructions that produced 'what team are you on?'."""
        banned = [
            "find out what they do",
            "the kind of team they sit in",
            "surface the friction",
            "what that explaining costs them",
        ]
        for turn in (0, 1, 2, 5):
            objective = next_objective(SalesStage.ENGAGED, {}, replies_sent=turn).lower()
            for phrase in banned:
                assert phrase not in objective, f"turn {turn} still instructs: {phrase}"

    def test_the_opening_explicitly_forbids_identity_questions(self):
        objective = next_objective(SalesStage.ENGAGED, {}, replies_sent=0)
        assert "what team they are on" in objective  # named so the model avoids it
        assert "reads like a form" in objective


class TestTheLadderCloses:
    def test_an_unknown_platform_no_longer_delays_the_link(self):
        """The build question rides along with the download, never in front of it.

        Asking "Windows or Mac?" on its own cost a whole round trip before the
        one thing the customer came for.
        """
        objective = next_objective(SalesStage.DOWNLOAD_SUGGESTED, {"use_case": "demos"})
        assert "send_download_link" in objective
        assert "Windows or a Mac" in objective
        assert "same message" in objective

    def test_a_known_platform_releases_the_link(self):
        objective = next_objective(SalesStage.DOWNLOAD_SUGGESTED, {"platform": "macos"})
        assert "send_download_link" in objective

    def test_an_unshippable_platform_does_not_release_the_link(self):
        """Speed is not an excuse to send a desktop installer to a phone."""
        objective = next_objective(SalesStage.DOWNLOAD_SUGGESTED, {"platform": "other"})
        assert "schedule_follow_up" in objective
        assert "does not ship for" in objective

    def test_after_the_link_it_stops_selling(self):
        objective = next_objective(SalesStage.ENGAGED, {}, download_link_sent=True)
        assert "do not re-offer the link" in objective

    def test_an_objection_pivots_to_a_benefit_and_schedules(self):
        objective = next_objective(SalesStage.OBJECTION_HANDLING, {})
        assert "pivot to the benefit" in objective
        assert "schedule_follow_up" in objective

    def test_finished_conversations_get_no_objective(self):
        for stage in (
            SalesStage.NOT_INTERESTED,
            SalesStage.HUMAN_HANDOFF,
            SalesStage.DOWNLOADED,
            SalesStage.ACTIVATED,
            SalesStage.CLOSED,
        ):
            assert next_objective(stage, {}) is None


class TestTheObjectiveReachesTheModel:
    async def test_the_directive_is_in_the_prompt(self, client, ai, meta):
        await post(client, text="hi")
        await drain_queue()

        assert "# What to do right now" in system_prompt(ai)
        assert "what they do" in directive(ai)

    async def test_volunteered_facts_are_remembered(self, client, db, ai, meta):
        """Notes are a memory so the chat never repeats itself - not a gate."""
        ai.queue_decision(
            AiDecision(
                reply_text="Boomshare records your screen so you can send a link instead.",
                intent="greeting",
                customer_notes={"use_case": "onboarding walkthroughs"},
            )
        )
        await post(client, text="hi")
        await drain_queue()

        conversation = (await db.execute(select(Conversation))).scalar_one()
        assert conversation.context_notes["use_case"] == "onboarding walkthroughs"

    async def test_the_ladder_advances_with_the_conversation(self, client, ai, meta):
        await post(client, text="can I get more info")
        await drain_queue()
        assert "what Boomshare is" in directive(ai)  # turn 1: pitch

        await post(client, text="ok tell me more")
        await drain_queue()
        assert "offer to send the download" in directive(ai)  # turn 2: connect + offer

    async def test_a_sent_link_switches_the_objective_to_activation(self, client, ai, meta):
        ai.queue_decision(
            AiDecision(
                reply_text="Here you go.",
                intent="download_request",
                actions=["send_download_link"],
                suggested_stage=SalesStage.DOWNLOAD_SUGGESTED,
            )
        )
        await post(client, text="send me the link")
        await drain_queue()

        await post(client, text="ok got it")
        await drain_queue()

        assert "do not re-offer the link" in directive(ai)

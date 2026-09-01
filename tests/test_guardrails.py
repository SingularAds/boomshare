"""Validation of model output.

These are the tests that stop a bad generation becoming a bad business action.
"""

from __future__ import annotations

import pytest

from app.ai.guardrails import (
    MAX_FOLLOW_UP_MINUTES,
    MIN_FOLLOW_UP_MINUTES,
    clamp_follow_up_minutes,
    contains_false_claim,
    resolve_deferral,
    sanitise_reply,
    strip_urls,
    validate_actions,
    validate_decision,
    validate_stage,
)
from app.ai.schemas import AiDecision
from app.domain import AiAction, CustomerIntent, SalesStage


class TestUrlStripping:
    @pytest.mark.parametrize(
        "text",
        [
            "Grab it at https://not-boomshare.example/download now",
            "Go to www.phishing.example and sign in",
            "Here: http://boomshare.ai/free",
        ],
    )
    def test_urls_are_removed(self, text):
        assert "http" not in strip_urls(text)
        assert "www." not in strip_urls(text)

    def test_surrounding_text_survives(self):
        assert strip_urls("Download from https://x.example today") == "Download from today"

    def test_punctuation_is_tidied(self):
        assert strip_urls("Try it here https://x.example .") == "Try it here ."[:-2] + "."

    def test_text_without_urls_is_untouched(self):
        text = "What are you using at the moment?"
        assert strip_urls(text) == text

    def test_sanitise_reports_the_rejection(self):
        cleaned, rejected = sanitise_reply("Get it at https://evil.example", 900)
        assert "url_in_reply" in rejected
        assert "https" not in cleaned


class TestFalseClaims:
    @pytest.mark.parametrize(
        "text",
        [
            "I've sent you the download link",
            "I have emailed you the details",
            "I've booked that for you",
            "The link has been sent",
            "I've asked a colleague to call you",
            "I have added you to the trial",
        ],
    )
    def test_action_claims_are_detected(self, text):
        assert contains_false_claim(text)

    @pytest.mark.parametrize(
        "text",
        [
            "I'll send the link over now",
            "Someone from the team can help with that",
            "Want me to send it?",
            "Happy to walk you through it",
        ],
    )
    def test_future_tense_is_fine(self, text):
        assert not contains_false_claim(text)

    def test_a_false_claim_makes_the_decision_unusable(self):
        decision = AiDecision(reply_text="I've sent you the link", intent=CustomerIntent.GREETING)
        outcome = validate_decision(decision, SalesStage.ENGAGED, 900)
        assert not outcome.usable
        assert "false_action_claim" in outcome.rejected


class TestLength:
    def test_long_reply_is_truncated(self):
        cleaned, rejected = sanitise_reply("x" * 2000, 100)
        assert len(cleaned) <= 100
        assert "reply_truncated" in rejected

    def test_truncation_prefers_a_sentence_boundary(self):
        text = "First sentence here. " + "y" * 200
        cleaned, _ = sanitise_reply(text, 60)
        assert cleaned.endswith(".")

    def test_short_reply_is_untouched(self):
        cleaned, rejected = sanitise_reply("Sounds good!", 900)
        assert cleaned == "Sounds good!"
        assert rejected == {}

    def test_empty_reply_is_unusable(self):
        outcome = validate_decision(AiDecision(reply_text="   "), SalesStage.NEW, 900)
        assert not outcome.usable


class TestStageValidation:
    def test_system_only_stage_is_refused(self):
        stage, rejected = validate_stage(SalesStage.DOWNLOADED, SalesStage.LINK_SENT)
        assert stage is None
        assert "stage_system_only" in rejected

    def test_illegal_transition_is_refused(self):
        stage, rejected = validate_stage(SalesStage.NEW, SalesStage.ACTIVATED)
        assert stage is None
        assert rejected

    def test_legal_transition_is_accepted(self):
        stage, rejected = validate_stage(SalesStage.QUALIFIED, current=SalesStage.ENGAGED)
        assert stage is SalesStage.QUALIFIED
        assert rejected == {}

    def test_no_suggestion_is_not_an_error(self):
        assert validate_stage(None, SalesStage.ENGAGED) == (None, {})

    def test_same_stage_is_not_a_change(self):
        assert validate_stage(SalesStage.ENGAGED, SalesStage.ENGAGED) == (None, {})


class TestActionValidation:
    def test_none_is_dropped(self):
        decision = AiDecision(reply_text="hi", actions=[AiAction.NONE])
        actions, _ = validate_actions(decision)
        assert actions == []

    def test_duplicates_are_collapsed(self):
        decision = AiDecision(
            reply_text="hi", actions=[AiAction.SEND_DOWNLOAD_LINK, AiAction.SEND_DOWNLOAD_LINK]
        )
        actions, _ = validate_actions(decision)
        assert actions == [AiAction.SEND_DOWNLOAD_LINK]

    def test_follow_up_without_a_delay_is_dropped(self):
        decision = AiDecision(reply_text="hi", actions=[AiAction.SCHEDULE_FOLLOW_UP])
        actions, rejected = validate_actions(decision)
        assert actions == []
        assert "follow_up_without_delay" in rejected

    def test_follow_up_with_a_delay_is_kept(self):
        decision = AiDecision(
            reply_text="hi", actions=[AiAction.SCHEDULE_FOLLOW_UP], follow_up_minutes=1440
        )
        actions, _ = validate_actions(decision)
        assert actions == [AiAction.SCHEDULE_FOLLOW_UP]

    def test_opt_out_wins_over_selling(self):
        """'Stop messaging me' plus 'send the link' is nonsense - stopping wins."""
        decision = AiDecision(
            reply_text="Understood, I'll stop there.",
            actions=[AiAction.SEND_DOWNLOAD_LINK, AiAction.OPT_OUT],
        )
        actions, rejected = validate_actions(decision)
        assert actions == [AiAction.OPT_OUT]
        assert "conflicting_with_opt_out" in rejected


class TestFollowUpClamping:
    def test_too_soon_is_raised_to_the_floor(self):
        assert clamp_follow_up_minutes(0)[0] == MIN_FOLLOW_UP_MINUTES

    def test_absurdly_far_is_capped(self):
        assert clamp_follow_up_minutes(10_000_000)[0] == MAX_FOLLOW_UP_MINUTES

    def test_sensible_value_passes_through(self):
        assert clamp_follow_up_minutes(2880)[0] == 2880

    def test_a_five_minute_callback_survives(self):
        """The delay a customer is most likely to name out loud.

        In hours this was 0.083, the model rounded it to zero, and the floor
        turned "I'll message you in five minutes" into an hour of silence.
        """
        assert clamp_follow_up_minutes(5) == (5, {})

    def test_fractions_are_rounded_to_whole_minutes(self):
        assert clamp_follow_up_minutes(5.4)[0] == 5

    def test_none_stays_none(self):
        assert clamp_follow_up_minutes(None) == (None, {})


class TestDeferralIsNotRefusal:
    """"Not right now" must never be recorded as "not interested".

    The model reports a deferral by asking for a follow-up *and* marking the
    lead dead in the same decision. `not_interested` stops every follow-up
    there will ever be, so applying both silently cancels the callback that was
    just promised out loud.
    """

    def test_the_dead_stage_is_dropped_when_a_follow_up_was_asked_for(self):
        stage, actions, rejected = resolve_deferral(
            SalesStage.NOT_INTERESTED, [AiAction.SCHEDULE_FOLLOW_UP]
        )
        assert stage is None
        assert actions == [AiAction.SCHEDULE_FOLLOW_UP]
        assert "deferral_not_refusal" in rejected

    def test_the_dead_action_is_dropped_too(self):
        _, actions, rejected = resolve_deferral(
            None, [AiAction.SCHEDULE_FOLLOW_UP, AiAction.MARK_NOT_INTERESTED]
        )
        assert actions == [AiAction.SCHEDULE_FOLLOW_UP]
        assert "deferral_not_refusal" in rejected

    def test_a_real_refusal_is_left_alone(self):
        stage, actions, rejected = resolve_deferral(
            SalesStage.NOT_INTERESTED, [AiAction.MARK_NOT_INTERESTED]
        )
        assert stage == SalesStage.NOT_INTERESTED
        assert actions == [AiAction.MARK_NOT_INTERESTED]
        assert rejected == {}

    def test_an_ordinary_follow_up_is_left_alone(self):
        stage, actions, rejected = resolve_deferral(
            SalesStage.ENGAGED, [AiAction.SCHEDULE_FOLLOW_UP]
        )
        assert stage == SalesStage.ENGAGED
        assert actions == [AiAction.SCHEDULE_FOLLOW_UP]
        assert rejected == {}

    def test_the_whole_decision_keeps_the_customers_callback(self):
        """End to end through `validate_decision`, as the flow calls it."""
        decision = AiDecision(
            reply_text="No problem - I'll check back in five minutes.",
            suggested_stage=SalesStage.NOT_INTERESTED,
            actions=[AiAction.SCHEDULE_FOLLOW_UP, AiAction.MARK_NOT_INTERESTED],
            follow_up_minutes=5,
        )
        outcome = validate_decision(decision, SalesStage.DOWNLOAD_SUGGESTED, 900)

        assert outcome.usable
        assert outcome.decision.suggested_stage is None
        assert outcome.decision.actions == [AiAction.SCHEDULE_FOLLOW_UP]
        assert outcome.decision.follow_up_minutes == 5


class TestWholeDecision:
    def test_a_good_decision_survives_intact(self):
        decision = AiDecision(
            reply_text="What do you record most often?",
            intent=CustomerIntent.INFORMATION_REQUEST,
            suggested_stage=SalesStage.ENGAGED,
            actions=[AiAction.NONE],
            confidence=0.9,
            customer_notes={"role": "support lead"},
        )
        outcome = validate_decision(decision, SalesStage.CONTACTED, 900)

        assert outcome.usable
        assert outcome.decision.reply_text == "What do you record most often?"
        assert outcome.decision.suggested_stage is SalesStage.ENGAGED
        assert outcome.decision.customer_notes == {"role": "support lead"}

    def test_confidence_is_clamped_to_a_probability(self):
        outcome = validate_decision(AiDecision(reply_text="hi", confidence=9.5), SalesStage.NEW, 900)
        assert outcome.decision.confidence == 1.0

    def test_oversized_notes_are_trimmed(self):
        decision = AiDecision(reply_text="hi", customer_notes={"k" * 200: "v" * 900})
        outcome = validate_decision(decision, SalesStage.NEW, 900)
        key, value = next(iter(outcome.decision.customer_notes.items()))
        assert len(key) <= 64
        assert len(value) <= 256

    def test_a_hallucinated_link_never_reaches_the_customer(self):
        decision = AiDecision(
            reply_text="Sure - download it from https://boomshare-download.example",
            intent=CustomerIntent.DOWNLOAD_REQUEST,
        )
        outcome = validate_decision(decision, SalesStage.ENGAGED, 900)
        assert "http" not in outcome.decision.reply_text
        assert outcome.usable  # the words are fine, only the URL was the problem

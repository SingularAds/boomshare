"""The sales state machine and the AI/application responsibility split."""

from __future__ import annotations

import pytest

from app.core.errors import InvalidStateTransition
from app.domain import (
    ALLOWED_TRANSITIONS,
    STOP_FOLLOW_UP_STAGES,
    SYSTEM_ONLY_STAGES,
    SalesStage,
    can_transition,
    is_ai_suggestable,
    validate_transition,
)


class TestTransitions:
    def test_normal_funnel_progression(self):
        path = [
            SalesStage.NEW,
            SalesStage.CONTACTED,
            SalesStage.ENGAGED,
            SalesStage.QUALIFIED,
            SalesStage.PRODUCT_EXPLAINED,
            SalesStage.DOWNLOAD_SUGGESTED,
            SalesStage.LINK_SENT,
        ]
        for current, target in zip(path, path[1:], strict=False):
            assert can_transition(current, target), f"{current} -> {target}"

    def test_customer_can_skip_ahead(self):
        """'Just send me the link' should not have to walk the whole funnel."""
        assert can_transition(SalesStage.NEW, SalesStage.DOWNLOAD_SUGGESTED)
        assert can_transition(SalesStage.CONTACTED, SalesStage.LINK_SENT)

    def test_cannot_jump_backwards_arbitrarily(self):
        assert not can_transition(SalesStage.LINK_SENT, SalesStage.NEW)
        assert not can_transition(SalesStage.PRODUCT_EXPLAINED, SalesStage.CONTACTED)

    def test_one_step_back_is_allowed_for_objections(self):
        assert can_transition(SalesStage.DOWNLOAD_SUGGESTED, SalesStage.OBJECTION_HANDLING)

    def test_the_funnel_never_slides_backwards(self):
        """Observed in production: link_sent -> download_suggested was accepted,
        and the agent re-offered a link the customer already had."""
        assert not can_transition(SalesStage.LINK_SENT, SalesStage.DOWNLOAD_SUGGESTED)
        assert not can_transition(SalesStage.DOWNLOAD_SUGGESTED, SalesStage.ENGAGED)
        assert not can_transition(SalesStage.QUALIFIED, SalesStage.ENGAGED)

    def test_an_objection_is_an_excursion_not_a_demotion(self):
        """Handling an objection must not cost the progress that earned it."""
        assert can_transition(SalesStage.LINK_SENT, SalesStage.OBJECTION_HANDLING)
        assert can_transition(SalesStage.OBJECTION_HANDLING, SalesStage.DOWNLOAD_SUGGESTED)
        assert not can_transition(SalesStage.OBJECTION_HANDLING, SalesStage.ENGAGED)

    def test_exits_are_reachable_from_anywhere_in_the_funnel(self):
        for stage in [
            SalesStage.NEW,
            SalesStage.ENGAGED,
            SalesStage.PRODUCT_EXPLAINED,
            SalesStage.LINK_SENT,
        ]:
            assert can_transition(stage, SalesStage.NOT_INTERESTED)
            assert can_transition(stage, SalesStage.HUMAN_HANDOFF)
            assert can_transition(stage, SalesStage.DOWNLOADED)

    def test_same_stage_is_a_no_op_not_an_error(self):
        assert can_transition(SalesStage.ENGAGED, SalesStage.ENGAGED)

    def test_not_interested_can_be_revived(self):
        """People change their mind; the funnel must let them come back."""
        assert can_transition(SalesStage.NOT_INTERESTED, SalesStage.ENGAGED)

    def test_activated_is_effectively_terminal(self):
        assert not can_transition(SalesStage.ACTIVATED, SalesStage.ENGAGED)
        assert can_transition(SalesStage.ACTIVATED, SalesStage.HUMAN_HANDOFF)

    def test_every_stage_has_a_transition_table(self):
        for stage in SalesStage:
            assert stage in ALLOWED_TRANSITIONS

    def test_validate_transition_raises_with_context(self):
        with pytest.raises(InvalidStateTransition) as exc:
            validate_transition(SalesStage.ACTIVATED, SalesStage.NEW)
        assert exc.value.current == "activated"
        assert exc.value.target == "new"

    def test_validate_transition_returns_the_target(self):
        assert validate_transition(SalesStage.NEW, SalesStage.ENGAGED) is SalesStage.ENGAGED


class TestSystemOnlyStages:
    def test_install_states_are_system_only(self):
        assert SalesStage.DOWNLOADED in SYSTEM_ONLY_STAGES
        assert SalesStage.ACTIVATED in SYSTEM_ONLY_STAGES
        assert SalesStage.CLOSED in SYSTEM_ONLY_STAGES

    def test_ai_cannot_suggest_them(self):
        assert not is_ai_suggestable(SalesStage.DOWNLOADED)
        assert not is_ai_suggestable(SalesStage.ACTIVATED)

    def test_ai_can_suggest_conversational_stages(self):
        for stage in [
            SalesStage.ENGAGED,
            SalesStage.QUALIFIED,
            SalesStage.PRODUCT_EXPLAINED,
            SalesStage.OBJECTION_HANDLING,
            SalesStage.DOWNLOAD_SUGGESTED,
            SalesStage.LINK_SENT,
            SalesStage.NOT_INTERESTED,
            SalesStage.HUMAN_HANDOFF,
        ]:
            assert is_ai_suggestable(stage)

    def test_link_sent_is_not_downloaded(self):
        """The distinction the brief is emphatic about."""
        assert SalesStage.LINK_SENT is not SalesStage.DOWNLOADED
        assert is_ai_suggestable(SalesStage.LINK_SENT)
        assert not is_ai_suggestable(SalesStage.DOWNLOADED)


class TestFollowUpStages:
    def test_finished_and_opted_out_stages_stop_follow_ups(self):
        for stage in [
            SalesStage.DOWNLOADED,
            SalesStage.ACTIVATED,
            SalesStage.NOT_INTERESTED,
            SalesStage.HUMAN_HANDOFF,
            SalesStage.CLOSED,
        ]:
            assert stage in STOP_FOLLOW_UP_STAGES

    def test_active_funnel_stages_still_take_follow_ups(self):
        for stage in [SalesStage.ENGAGED, SalesStage.DOWNLOAD_SUGGESTED, SalesStage.LINK_SENT]:
            assert stage not in STOP_FOLLOW_UP_STAGES

"""Typed narration AST + clarify schema contract (agent_sidecar_frontend.md §5.5/§5.2).

WORKORDER P2 pins enforced at construction time (specs/narration.py, the pure
type layer):

- narration schema REJECTS numeric args (FE-X2/FE-L2);
- a stable message_key is an enum, never translated prose (spec §9);
- kind fixes which optional payloads are legal (ref / options / action);
- clarify caps at ≤3 questions, each with exactly one default, tiered
  blocking/semantic (spec §5.2).
"""

from __future__ import annotations

import pytest

from quant_forge.specs.narration import (
    MAX_CLARIFY_QUESTIONS,
    ClarifyError,
    ClarifyQuestion,
    NarrationError,
    NarrationNode,
    NarrationOption,
    NarrationRef,
    NumericNarrationArgError,
    is_numeric_token,
    validate_clarify_questions,
)


# --- pin: narration schema rejects numeric args -----------------------------


@pytest.mark.parametrize("bad", [0, 1, 5, -3, 0.42, 1.2, True, False, "0.05", "-1.2e3", "30%", ".5", "42"])
def test_numeric_args_are_rejected(bad) -> None:
    with pytest.raises(NumericNarrationArgError):
        NarrationNode(kind="status", message_key="parse.done", args=[bad])


@pytest.mark.parametrize("ok", ["FTR_ABCD1234", "small_cap", "is_st", "2025Q1_seed", "profile_default", "IC"])
def test_non_numeric_id_tokens_are_allowed(ok) -> None:
    node = NarrationNode(kind="status", message_key="parse.done", args=[ok])
    assert node.args == (ok,)


def test_is_numeric_token_classifies_bool_and_numbers() -> None:
    assert is_numeric_token(True) and is_numeric_token(1) and is_numeric_token(0.5)
    assert is_numeric_token("12.5%") and is_numeric_token("-7")
    assert not is_numeric_token("FTR_1") and not is_numeric_token("holding_days")


# --- pin: stable message_key, not translated prose --------------------------


@pytest.mark.parametrize("bad_key", ["", "Parse Done", "解析完成", "UPPER", "trailing.", ".leading"])
def test_message_key_must_be_a_stable_enum(bad_key) -> None:
    with pytest.raises(NarrationError):
        NarrationNode(kind="status", message_key=bad_key)


# --- kind-specific shape rules ----------------------------------------------


def test_ref_node_requires_a_ref_and_forbids_options() -> None:
    ref = NarrationRef(component_id="factor-tape", artifact_ref="eval.json")
    NarrationNode(kind="ref", message_key="report.metric", ref=ref)
    with pytest.raises(NarrationError):
        NarrationNode(kind="ref", message_key="report.metric")  # missing ref


def test_status_node_forbids_ref_and_options_and_action() -> None:
    ref = NarrationRef(component_id="factor-tape", artifact_ref="eval.json")
    with pytest.raises(NarrationError):
        NarrationNode(kind="status", message_key="x.y", ref=ref)


def test_question_node_requires_two_options_and_exactly_one_default() -> None:
    opts = (NarrationOption("a", "A", is_default=True), NarrationOption("b", "B"))
    NarrationNode(kind="question", message_key="clarify.mktcap", options=opts)
    # zero defaults
    with pytest.raises(NarrationError):
        NarrationNode(
            kind="question",
            message_key="clarify.mktcap",
            options=(NarrationOption("a", "A"), NarrationOption("b", "B")),
        )
    # two defaults
    with pytest.raises(NarrationError):
        NarrationNode(
            kind="question",
            message_key="clarify.mktcap",
            options=(NarrationOption("a", "A", is_default=True), NarrationOption("b", "B", is_default=True)),
        )
    # single option
    with pytest.raises(NarrationError):
        NarrationNode(kind="question", message_key="clarify.mktcap", options=(NarrationOption("a", "A", is_default=True),))


def test_action_suggestion_requires_an_action_token() -> None:
    NarrationNode(kind="action_suggestion", message_key="suggest.confirm", action="confirm_pipeline")
    with pytest.raises(NarrationError):
        NarrationNode(kind="action_suggestion", message_key="suggest.confirm")


def test_node_roundtrips_through_dict() -> None:
    ref = NarrationRef(component_id="factor-tape", artifact_ref="eval.json")
    node = NarrationNode(kind="ref", message_key="report.metric", args=["IC"], ref=ref)
    assert NarrationNode.from_dict(node.to_dict()).to_dict() == node.to_dict()


def test_from_dict_rejects_numeric_args_on_reload() -> None:
    # A persisted/journaled node carrying a number is rejected on reconstruction
    # too (defense for the replay path).
    with pytest.raises(NumericNarrationArgError):
        NarrationNode.from_dict({"kind": "status", "message_key": "x.y", "args": ["0.5"]})


# --- clarify tiering / cap / defaults ---------------------------------------


def _q(key: str, tier: str) -> ClarifyQuestion:
    return ClarifyQuestion(
        question_key=key,
        tier=tier,
        options=(NarrationOption("keep", "保留", is_default=True), NarrationOption("drop", "剔除")),
    )


def test_clarify_cap_is_three() -> None:
    assert MAX_CLARIFY_QUESTIONS == 3
    validate_clarify_questions(tuple(_q(f"clarify.q{i}", "semantic") for i in range(3)))
    with pytest.raises(ClarifyError):
        validate_clarify_questions(tuple(_q(f"clarify.q{i}", "semantic") for i in range(4)))


def test_clarify_tier_must_be_known() -> None:
    with pytest.raises(ClarifyError):
        _q("clarify.q", "urgent")


def test_clarify_question_projects_to_a_question_node() -> None:
    node = _q("clarify.mktcap.basis", "blocking").to_narration_node()
    assert node.kind == "question"
    assert node.message_key == "clarify.mktcap.basis"
    assert sum(1 for o in node.options if o.is_default) == 1


def test_clarify_question_keys_must_be_unique() -> None:
    with pytest.raises(ClarifyError):
        validate_clarify_questions((_q("clarify.dup", "semantic"), _q("clarify.dup", "blocking")))

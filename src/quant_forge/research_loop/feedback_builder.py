"""Build compact feedback for the next lightweight RD iteration."""

from __future__ import annotations

from quant_forge.research_loop.contracts import FactorExperimentResult, ResearchFeedback

_HINT_REPAIR_ERROR = "Repair the runtime or validation error before proposing related variants."
_HINT_EVALUATE_GATE = "Evaluate the candidate gate before the next RD refinement."
_HINT_NEARBY_VARIANTS = "Prefer nearby variants with lower correlation and similar OOS/cost profile."
_HINT_MAP_FIELD = "Use available Data/MCP fields or ask the user to map the missing field."
_HINT_EXECUTABLE_OPERATORS = "Use executable operators from the operator MCP catalog."
_HINT_CLARIFY_DIRECTION = "Clarify expected factor direction before calculation."
_HINT_REDUCE_CORRELATION = "Change the economic thesis or inputs to reduce active-factor correlation."
_HINT_REDUCE_TURNOVER = "Try smoothing, longer horizons, or parameter search to reduce trading intensity."
_HINT_REVISE_SIGNAL = "Revise signal orientation, horizon, or formula family before re-testing."
_HINT_BLOCKING_REASON = "Use the blocking reason as the next RD hypothesis constraint."

# The complete producer-side hint vocabulary. Consumers that read hints back
# from disk (context_builder._next_focus_hints) admit only members of this
# set, so trace tampering cannot inject free text into prompt assembly (P1).
NEXT_HYPOTHESIS_HINT_TEMPLATES: tuple[str, ...] = (
    _HINT_REPAIR_ERROR,
    _HINT_EVALUATE_GATE,
    _HINT_NEARBY_VARIANTS,
    _HINT_MAP_FIELD,
    _HINT_EXECUTABLE_OPERATORS,
    _HINT_CLARIFY_DIRECTION,
    _HINT_REDUCE_CORRELATION,
    _HINT_REDUCE_TURNOVER,
    _HINT_REVISE_SIGNAL,
    _HINT_BLOCKING_REASON,
)


def build_feedback(result: FactorExperimentResult) -> ResearchFeedback:
    decision = result.gate_decision
    if result.error:
        return ResearchFeedback(
            status="failed",
            summary=result.error,
            evidence={"plan_id": result.plan.plan_id},
            next_hypothesis_hint=_HINT_REPAIR_ERROR,
        )
    if decision is None:
        return ResearchFeedback(
            status="pending_gate",
            summary="Candidate gate was not evaluated.",
            evidence={"plan_id": result.plan.plan_id, "plan_status": result.plan.status},
            next_hypothesis_hint=_HINT_EVALUATE_GATE,
        )
    if decision.accepted:
        return ResearchFeedback(
            status="accepted_for_candidate",
            summary="Experiment passed RD candidate gates.",
            evidence={"candidate_ref": result.candidate_ref, "warnings": list(decision.warnings)},
            next_hypothesis_hint=_HINT_NEARBY_VARIANTS,
        )
    unresolved = tuple(reason for reason in decision.blocking_reasons if "missing" in reason or "unknown" in reason)
    return ResearchFeedback(
        status="blocked",
        summary="; ".join(decision.blocking_reasons[:4]) or "Experiment blocked by candidate gate.",
        evidence={
            "candidate_ref": result.candidate_ref,
            "blocking_reasons": list(decision.blocking_reasons),
            "warnings": list(decision.warnings),
        },
        next_hypothesis_hint=_next_hint(decision.blocking_reasons),
        unresolved_items=unresolved,
        requires_user_decision=bool(unresolved),
    )


def _next_hint(reasons: tuple[str, ...]) -> str:
    text = "\n".join(reasons).lower()
    if "field" in text:
        return _HINT_MAP_FIELD
    if "operator" in text:
        return _HINT_EXECUTABLE_OPERATORS
    if "direction" in text:
        return _HINT_CLARIFY_DIRECTION
    if "correlation" in text:
        return _HINT_REDUCE_CORRELATION
    if "turnover" in text or "rebalance" in text:
        return _HINT_REDUCE_TURNOVER
    if "rank_ic" in text or "icir" in text:
        return _HINT_REVISE_SIGNAL
    return _HINT_BLOCKING_REASON

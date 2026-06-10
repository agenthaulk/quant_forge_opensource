"""Build compact feedback for the next lightweight RD iteration."""

from __future__ import annotations

from quant_forge.research_loop.contracts import FactorExperimentResult, ResearchFeedback


def build_feedback(result: FactorExperimentResult) -> ResearchFeedback:
    decision = result.gate_decision
    if result.error:
        return ResearchFeedback(
            status="failed",
            summary=result.error,
            evidence={"plan_id": result.plan.plan_id},
            next_hypothesis_hint="Repair the runtime or validation error before proposing related variants.",
        )
    if decision is None:
        return ResearchFeedback(
            status="pending_gate",
            summary="Candidate gate was not evaluated.",
            evidence={"plan_id": result.plan.plan_id, "plan_status": result.plan.status},
            next_hypothesis_hint="Evaluate the candidate gate before the next RD refinement.",
        )
    if decision.accepted:
        return ResearchFeedback(
            status="accepted_for_candidate",
            summary="Experiment passed RD candidate gates.",
            evidence={"candidate_ref": result.candidate_ref, "warnings": list(decision.warnings)},
            next_hypothesis_hint="Prefer nearby variants with lower correlation and similar OOS/cost profile.",
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
        return "Use available Data/MCP fields or ask the user to map the missing field."
    if "operator" in text:
        return "Use executable operators from the operator MCP catalog."
    if "direction" in text:
        return "Clarify expected factor direction before calculation."
    if "correlation" in text:
        return "Change the economic thesis or inputs to reduce active-factor correlation."
    if "turnover" in text or "rebalance" in text:
        return "Try smoothing, longer horizons, or parameter search to reduce trading intensity."
    if "rank_ic" in text or "icir" in text:
        return "Revise signal orientation, horizon, or formula family before re-testing."
    return "Use the blocking reason as the next RD hypothesis constraint."

"""Structured candidate gates for RD experiment plans and results.

This module is the single authoritative home of the gate clause definitions
shared with ``research_loop.service.apply_gate`` (F-2, FP-5): what counts as
sufficient OOS evidence, how net-return retention is computed, and when OOS
net-return decay is exceeded. Both gates consume the same helpers below, so
the two surfaces cannot diverge again.

Evidence policy (F-1, FP-2 — a configured clause must never silently pass on
missing evidence):

- OOS/net-return clauses (``min_oos_net_annualized_return``,
  ``max_oos_net_return_decay``, ``min_net_return_retention``): missing
  evidence blocks by default and downgrades to a warning when
  ``missing_oos_evidence_blocks`` is False.
- Turnover clauses (``max_rebalance_rate``, ``max_turnover_rate``): missing
  evidence rides the existing ``turnover_blocks_candidate`` channel here; the
  research gate blocks (it has no turnover warn channel).
- ``max_abs_corr_with_active``: missing correlation evidence always blocks
  (fail closed; there is no warn channel for this clause).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from quant_forge.research_loop.contracts import FactorExperimentResult, GateDecision


INSUFFICIENT_OOS_EVIDENCE = "INSUFFICIENT_OOS_EVIDENCE"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class CandidateGateConfig:
    min_score: float = 0.0
    min_coverage: float = 0.0
    min_observations: int = 0
    min_eval_days: int = 0
    min_direction_adjusted_rank_ic: float = 0.0
    min_direction_adjusted_rank_ic_ir: float = 0.0
    min_direction_adjusted_total_return: float | None = None
    min_direction_adjusted_sharpe: float | None = None
    max_drawdown_floor: float | None = None
    max_abs_corr_with_active: float | None = None
    max_rebalance_rate: float | None = None
    max_turnover_rate: float | None = None
    min_oos_net_annualized_return: float | None = None
    min_net_return_retention: float | None = None
    max_oos_net_return_decay: float | None = None
    missing_oos_evidence_blocks: bool = True
    turnover_blocks_candidate: bool = True
    auto_candidate: bool = False

    def __post_init__(self) -> None:
        # Loader parity (F-5): a truthy non-bool (e.g. the string "false")
        # must not silently flip the missing-evidence channel when the config
        # is constructed directly instead of via the strict YAML loader.
        if not isinstance(self.missing_oos_evidence_blocks, bool):
            raise ValueError("missing_oos_evidence_blocks must be a boolean")


@dataclass(frozen=True)
class SegmentEvidence:
    """Normalized per-segment gate evidence shared by both gate surfaces.

    ``periods=None`` means the source did not report a period count (for
    example externally supplied segment dicts); unknown counts are treated as
    usable rather than silently discarding reported return evidence.
    """

    name: str
    net_annualized_return: float | None
    periods: int | None = None


@dataclass(frozen=True)
class OOSReturnEvidence:
    """The authoritative 'sufficient OOS evidence' decomposition (F-2, FP-5).

    Sufficient evidence for an OOS return clause requires at least one OOS
    segment AND a reported ``net_annualized_return`` on every OOS segment.
    A segment with a null return is named in ``unavailable`` — it is never
    silently skipped (the pre-unification candidate-gate behavior).
    """

    observed: tuple[SegmentEvidence, ...]
    unavailable: tuple[str, ...]


def oos_return_evidence(segments: Iterable[SegmentEvidence]) -> OOSReturnEvidence:
    observed: list[SegmentEvidence] = []
    unavailable: list[str] = []
    for segment in segments:
        if not segment.name.upper().startswith("OOS"):
            continue
        # Unify with the decay clause (line ~136): a segment is usable evidence
        # only when it reports a return AND is not a zero-period segment. Both
        # OOS clauses now agree that a 0-period segment is missing evidence
        # (fail closed) rather than counting it as observed here while the
        # decay exceedance check silently skips it. ``_usable`` is module-level
        # and resolves at call time, so referencing it before its definition
        # is fine.
        if not _usable(segment):
            unavailable.append(segment.name)
        else:
            observed.append(segment)
    return OOSReturnEvidence(observed=tuple(observed), unavailable=tuple(unavailable))


def min_oos_return_reasons(
    evidence: OOSReturnEvidence,
    threshold: float,
    *,
    missing_blocks: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One definition of the ``min_oos_net_annualized_return`` clause.

    Returns ``(blocking, warnings)``. Threshold violations always block;
    missing-evidence findings block or warn per ``missing_blocks``.
    """

    blocking: list[str] = []
    missing: list[str] = []
    if not evidence.observed and not evidence.unavailable:
        missing.append(
            f"{INSUFFICIENT_OOS_EVIDENCE}: min_oos_net_annualized_return is configured "
            "but no OOS net_annualized_return evidence is available"
        )
    for name in evidence.unavailable:
        missing.append(f"{INSUFFICIENT_OOS_EVIDENCE}: {name} net_annualized_return unavailable")
    for segment in evidence.observed:
        value = segment.net_annualized_return
        if value is not None and value < threshold:
            blocking.append(f"{segment.name} net_annualized_return below threshold: {value:.6g}")
    if missing_blocks:
        return tuple(blocking + missing), ()
    return tuple(blocking), tuple(missing)


def _usable(segment: SegmentEvidence) -> bool:
    return segment.net_annualized_return is not None and (segment.periods is None or segment.periods > 0)


def oos_decay_evidence_available(segments: Iterable[SegmentEvidence]) -> bool:
    """Decay-clause evidence: a usable IS return plus at least one usable OOS return.

    Usability is exactly ``_usable`` — the same rule the exceedance check
    applies. A reported return on a zero-period segment is NOT evidence: it
    must count as missing (and fail closed under
    ``missing_oos_evidence_blocks``) rather than letting the clause silently
    pass while the exceedance check skips the segment.
    """

    is_present = False
    oos_present = False
    for segment in segments:
        if not _usable(segment):
            continue
        name = segment.name.upper()
        if name == "IS":
            is_present = True
        elif name.startswith("OOS"):
            oos_present = True
    return is_present and oos_present


def oos_net_decay_exceeded(segments: Iterable[SegmentEvidence], threshold: float) -> bool:
    """One definition of ``max_oos_net_return_decay`` exceedance (FP-5).

    The threshold bounds the decay FRACTION ``(IS - OOS) / IS`` for a positive
    IS baseline, matching the knob name. (The research gate previously read
    the same knob as a minimum OOS/IS ratio — two definitions of one config
    quantity; both surfaces now share this one.) A non-positive IS baseline
    blocks only when an OOS segment deteriorates further below it.
    """

    materialized = tuple(segments)
    is_segment = next((item for item in materialized if item.name.upper() == "IS"), None)
    if is_segment is None or not _usable(is_segment):
        return False
    is_return = is_segment.net_annualized_return
    assert is_return is not None  # _usable guarantees it
    for segment in materialized:
        if not segment.name.upper().startswith("OOS") or not _usable(segment):
            continue
        oos_return = segment.net_annualized_return
        assert oos_return is not None
        if is_return > 0:
            if (is_return - oos_return) / is_return > threshold:
                return True
        elif oos_return < is_return:
            return True
    return False


def max_oos_decay_reasons(
    segments: Iterable[SegmentEvidence],
    threshold: float,
    *,
    missing_blocks: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One definition of the ``max_oos_net_return_decay`` clause."""

    materialized = tuple(segments)
    if oos_net_decay_exceeded(materialized, threshold):
        return (f"OOS net return decay exceeds {threshold:.6g}",), ()
    if not oos_decay_evidence_available(materialized):
        reason = (
            f"{INSUFFICIENT_OOS_EVIDENCE}: max_oos_net_return_decay is configured "
            "but IS/OOS net_annualized_return evidence is missing"
        )
        return ((reason,), ()) if missing_blocks else ((), (reason,))
    return (), ()


def net_return_retention_value(net: float | None, gross: float | None) -> float | None:
    """One definition of net-return retention (F-1/F-2, FP-5).

    ``None`` when either side of the ratio is unobserved — never a fabricated
    1.0/0.0 (FP-4). For a non-positive gross baseline the ratio is undefined;
    costs that do not worsen the result count as full retention.
    """

    if net is None or gross is None:
        return None
    if gross <= 0:
        return 1.0 if net >= gross else 0.0
    return float(net / gross)


def min_net_return_retention_reasons(
    retention: float | None,
    threshold: float,
    *,
    missing_blocks: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """One definition of the ``min_net_return_retention`` clause."""

    if retention is None:
        reason = (
            f"{INSUFFICIENT_EVIDENCE}: min_net_return_retention is configured "
            "but net/gross annualized return evidence is missing"
        )
        return ((reason,), ()) if missing_blocks else ((), (reason,))
    if retention < threshold:
        return (f"net_return_retention below threshold: {retention:.6g}",), ()
    return (), ()


def evaluate_candidate(result: FactorExperimentResult, config: CandidateGateConfig | None = None) -> GateDecision:
    gate = config or CandidateGateConfig()
    blocking: list[str] = []
    warnings: list[str] = []
    plan = result.plan
    if plan.status != "ready":
        blocking.extend(plan.blocking_reasons or (f"plan status is {plan.status}",))

    metrics = _direction_adjusted(_merged_metrics(result), expected_direction=plan.expected_direction)
    segments = _segment_evidence(metrics)
    _check_min(blocking, metrics, "coverage", gate.min_coverage)
    _check_min(blocking, metrics, "observations", gate.min_observations)
    _check_min(blocking, metrics, "ic_days", gate.min_eval_days)
    _check_min(blocking, metrics, "direction_adjusted_rank_ic", gate.min_direction_adjusted_rank_ic)
    _check_min(blocking, metrics, "direction_adjusted_rank_ic_ir", gate.min_direction_adjusted_rank_ic_ir)
    score = result.objective_score.score if result.objective_score is not None else _number(metrics.get("score"), 0.0)
    if score < gate.min_score:
        blocking.append(f"score below threshold: {score:.6g}")
    if gate.min_direction_adjusted_total_return is not None:
        _check_min(blocking, metrics, "direction_adjusted_total_return", gate.min_direction_adjusted_total_return)
    if gate.min_direction_adjusted_sharpe is not None:
        _check_min(blocking, metrics, "direction_adjusted_sharpe", gate.min_direction_adjusted_sharpe)
    if gate.max_drawdown_floor is not None:
        _check_min(blocking, metrics, "max_drawdown", gate.max_drawdown_floor)
    if gate.max_abs_corr_with_active is not None:
        corr = _optional_number(result.correlation_summary.get("max_abs_corr_with_active"))
        if corr is None:
            # F-1: a configured correlation cap without correlation evidence
            # must not read as "uncorrelated" (FP-2); fail closed.
            blocking.append(
                f"{INSUFFICIENT_EVIDENCE}: max_abs_corr_with_active is configured "
                "but no active-factor correlation evidence is available"
            )
        elif abs(corr) > gate.max_abs_corr_with_active:
            blocking.append(f"active factor correlation too high: {abs(corr):.6g}")
    if gate.min_oos_net_annualized_return is not None:
        clause_blocking, clause_warnings = min_oos_return_reasons(
            oos_return_evidence(segments),
            gate.min_oos_net_annualized_return,
            missing_blocks=gate.missing_oos_evidence_blocks,
        )
        blocking.extend(clause_blocking)
        warnings.extend(clause_warnings)
    if gate.min_net_return_retention is not None:
        retention = net_return_retention_value(
            _first_number(metrics, ("net_annualized_return", "net_cumulative_return")),
            _first_number(metrics, ("annualized_return", "gross_annualized_return", "gross_cumulative_return")),
        )
        clause_blocking, clause_warnings = min_net_return_retention_reasons(
            retention,
            gate.min_net_return_retention,
            missing_blocks=gate.missing_oos_evidence_blocks,
        )
        blocking.extend(clause_blocking)
        warnings.extend(clause_warnings)
    if gate.max_oos_net_return_decay is not None:
        clause_blocking, clause_warnings = max_oos_decay_reasons(
            segments,
            gate.max_oos_net_return_decay,
            missing_blocks=gate.missing_oos_evidence_blocks,
        )
        blocking.extend(clause_blocking)
        warnings.extend(clause_warnings)

    turnover_findings = turnover_reasons(
        metrics,
        max_rebalance_rate=gate.max_rebalance_rate,
        max_turnover_rate=gate.max_turnover_rate,
    )
    if gate.turnover_blocks_candidate:
        blocking.extend(turnover_findings)
    else:
        warnings.extend(turnover_findings)

    accepted = not blocking
    return GateDecision(
        status="passed" if accepted else "blocked",
        accepted=accepted,
        blocking_reasons=tuple(dict.fromkeys(blocking)),
        warnings=tuple(dict.fromkeys(warnings)),
        should_transition_to_candidate=accepted and gate.auto_candidate,
        should_promote_active=False,
        transition_reason=_transition_reason(result, metrics) if accepted else "",
    )


def _merged_metrics(result: FactorExperimentResult) -> dict[str, Any]:
    metrics = dict(result.evaluation_metrics)
    metrics.update(result.backtest_metrics)
    metrics.update(result.correlation_summary)
    return metrics


def _direction_adjusted(metrics: dict[str, Any], *, expected_direction: str) -> dict[str, Any]:
    adjusted = dict(metrics)
    multiplier = -1.0 if expected_direction == "negative" else 1.0
    aliases = {
        "direction_adjusted_rank_ic": ("rank_ic_mean", "rank_ic"),
        "direction_adjusted_rank_ic_ir": ("rank_icir", "rank_ic_ir", "icir"),
        "direction_adjusted_total_return": ("net_annualized_return", "annualized_return", "total_return"),
        "direction_adjusted_sharpe": ("net_long_short_sharpe", "sharpe"),
    }
    for target, keys in aliases.items():
        if target in adjusted and adjusted[target] is not None:
            continue
        value = _first_number(metrics, keys)
        if value is not None:
            adjusted[target] = value * multiplier
    return adjusted


def _check_min(blocking: list[str], metrics: dict[str, Any], key: str, threshold: float) -> None:
    value = _first_number(metrics, (key,))
    if value is None or value < threshold:
        blocking.append(f"{key} below threshold: {value if value is not None else 'missing'}")


def turnover_reasons(
    metrics: dict[str, Any],
    *,
    max_rebalance_rate: float | None,
    max_turnover_rate: float | None,
) -> tuple[str, ...]:
    """One definition of the turnover-family clauses (FP-5), shared with
    ``service.apply_gate``. ``metrics`` may carry ``rebalance_rate`` and
    ``turnover_rate``/``turnover_mean``; a configured clause without evidence
    yields an INSUFFICIENT_EVIDENCE reason (F-1/FP-2), never a silent pass.
    """

    reasons: list[str] = []
    if max_rebalance_rate is not None:
        value = _first_number(metrics, ("rebalance_rate",))
        if value is None:
            # F-1: configured turnover-family clause + no evidence must not
            # silently pass (FP-2).
            reasons.append(
                f"{INSUFFICIENT_EVIDENCE}: max_rebalance_rate is configured "
                "but rebalance_rate evidence is unavailable"
            )
        elif value > max_rebalance_rate:
            reasons.append(f"rebalance_rate above threshold: {value:.6g}")
    if max_turnover_rate is not None:
        value = _first_number(metrics, ("turnover_rate", "turnover_mean"))
        if value is None:
            reasons.append(
                f"{INSUFFICIENT_EVIDENCE}: max_turnover_rate is configured "
                "but turnover_rate evidence is unavailable"
            )
        elif value > max_turnover_rate:
            reasons.append(f"turnover_rate above threshold: {value:.6g}")
    return tuple(reasons)


def _transition_reason(result: FactorExperimentResult, metrics: dict[str, Any]) -> str:
    return (
        f"RD plan {result.plan.plan_id} passed candidate gates: "
        f"rank_ic={_number(metrics.get('direction_adjusted_rank_ic'), 0.0):.6g}, "
        f"icir={_number(metrics.get('direction_adjusted_rank_ic_ir'), 0.0):.6g}, "
        f"return={_number(metrics.get('direction_adjusted_total_return'), 0.0):.6g}"
    )


def _segments(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metrics.get("segment_metrics") or metrics.get("segments") or ()
    return [dict(item) for item in raw if isinstance(item, dict)]


def _segment_evidence(metrics: dict[str, Any]) -> tuple[SegmentEvidence, ...]:
    evidence: list[SegmentEvidence] = []
    for segment in _segments(metrics):
        name = str(segment.get("name") or "")
        if not name:
            continue
        periods = _optional_number(segment.get("periods"))
        evidence.append(
            SegmentEvidence(
                name=name,
                net_annualized_return=_optional_number(segment.get("net_annualized_return")),
                periods=int(periods) if periods is not None else None,
            )
        )
    return tuple(evidence)


def _first_number(metrics: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _optional_number(metrics.get(key))
        if value is not None:
            return value
    return None


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any, default: float) -> float:
    parsed = _optional_number(value)
    return default if parsed is None else parsed

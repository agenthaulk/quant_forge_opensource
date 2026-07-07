"""Structured candidate gates for RD experiment plans and results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quant_forge.research_loop.contracts import FactorExperimentResult, GateDecision


INSUFFICIENT_OOS_EVIDENCE = "INSUFFICIENT_OOS_EVIDENCE"


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


def evaluate_candidate(result: FactorExperimentResult, config: CandidateGateConfig | None = None) -> GateDecision:
    gate = config or CandidateGateConfig()
    blocking: list[str] = []
    warnings: list[str] = []
    plan = result.plan
    if plan.status != "ready":
        blocking.extend(plan.blocking_reasons or (f"plan status is {plan.status}",))

    metrics = _direction_adjusted(_merged_metrics(result), expected_direction=plan.expected_direction)
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
        corr = abs(_number(result.correlation_summary.get("max_abs_corr_with_active"), 0.0))
        if corr > gate.max_abs_corr_with_active:
            blocking.append(f"active factor correlation too high: {corr:.6g}")
    if gate.min_oos_net_annualized_return is not None:
        oos_returns = _oos_segment_returns(metrics)
        for name, value in oos_returns:
            if value < gate.min_oos_net_annualized_return:
                blocking.append(f"{name} net_annualized_return below threshold: {value:.6g}")
        if not oos_returns:
            _flag_missing_oos_evidence(blocking, warnings, gate, "min_oos_net_annualized_return")
    if gate.min_net_return_retention is not None:
        retention = _net_return_retention(metrics)
        if retention < gate.min_net_return_retention:
            blocking.append(f"net_return_retention below threshold: {retention:.6g}")
    if gate.max_oos_net_return_decay is not None:
        if _oos_net_decay(metrics, gate.max_oos_net_return_decay):
            blocking.append(f"OOS net return decay exceeds {gate.max_oos_net_return_decay:.6g}")
        elif not _oos_decay_evidence(metrics):
            _flag_missing_oos_evidence(blocking, warnings, gate, "max_oos_net_return_decay")

    turnover_reasons = _turnover_reasons(metrics, gate)
    if gate.turnover_blocks_candidate:
        blocking.extend(turnover_reasons)
    else:
        warnings.extend(turnover_reasons)

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


def _turnover_reasons(metrics: dict[str, Any], gate: CandidateGateConfig) -> list[str]:
    reasons: list[str] = []
    if gate.max_rebalance_rate is not None:
        value = _first_number(metrics, ("rebalance_rate",))
        if value is not None and value > gate.max_rebalance_rate:
            reasons.append(f"rebalance_rate above threshold: {value:.6g}")
    if gate.max_turnover_rate is not None:
        value = _first_number(metrics, ("turnover_rate", "turnover_mean"))
        if value is not None and value > gate.max_turnover_rate:
            reasons.append(f"turnover_rate above threshold: {value:.6g}")
    return reasons


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


def _oos_segment_returns(metrics: dict[str, Any]) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    for segment in _segments(metrics):
        name = str(segment.get("name") or "")
        if not name.upper().startswith("OOS"):
            continue
        value = _optional_number(segment.get("net_annualized_return"))
        if value is not None:
            values.append((name, value))
    return values


def _oos_decay_evidence(metrics: dict[str, Any]) -> bool:
    by_name = {str(item.get("name") or "").upper(): item for item in _segments(metrics)}
    is_segment = by_name.get("IS")
    if not is_segment:
        return False
    if _optional_number(is_segment.get("net_annualized_return")) is None:
        return False
    return bool(_oos_segment_returns(metrics))


def _flag_missing_oos_evidence(
    blocking: list[str],
    warnings: list[str],
    gate: CandidateGateConfig,
    clause: str,
) -> None:
    reason = (
        f"{INSUFFICIENT_OOS_EVIDENCE}: {clause} is configured but no OOS "
        "net_annualized_return evidence is available"
    )
    if gate.missing_oos_evidence_blocks:
        blocking.append(reason)
    else:
        warnings.append(reason)


def _net_return_retention(metrics: dict[str, Any]) -> float:
    net_value = _first_number(metrics, ("net_annualized_return", "net_cumulative_return"))
    gross_value = _first_number(metrics, ("annualized_return", "gross_annualized_return", "gross_cumulative_return"))
    if net_value is None:
        return 0.0
    if gross_value is None or gross_value == 0:
        return 1.0 if net_value >= 0 else 0.0
    return net_value / gross_value


def _oos_net_decay(metrics: dict[str, Any], threshold: float) -> bool:
    by_name = {str(item.get("name") or "").upper(): item for item in _segments(metrics)}
    is_segment = by_name.get("IS")
    if not is_segment:
        return False
    is_return = _optional_number(is_segment.get("net_annualized_return"))
    if is_return is None:
        return False
    for name, segment in by_name.items():
        if not name.startswith("OOS"):
            continue
        oos_return = _optional_number(segment.get("net_annualized_return"))
        if oos_return is None:
            continue
        if is_return > 0 and (is_return - oos_return) / max(abs(is_return), 1e-12) > threshold:
            return True
        if is_return <= 0 and oos_return < is_return:
            return True
    return False


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

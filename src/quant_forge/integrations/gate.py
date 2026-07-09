"""Provider-neutral submission-gate evaluator over LOCAL backtest outputs (D-iii).

This module is the public half of the CP2 pre-screen: pure local arithmetic
over the numbers a Quant Forge backtest already produced. It imports no
concrete backend, opens no network connection, reads no credentials and no
environment variables. Provider-specific threshold *values* never live here —
they ship with the adapter that owns them; every spec instance constructed in
this repository's tests uses synthetic numbers.

Honesty contract (FP-I / D-iii):

- Every report carries :data:`~quant_forge.integrations.contracts.PRESCREEN_LOCAL_PROXY_ONLY`:
  a local proxy computed from a local simulation is never the target
  platform's own evaluation, and the report never claims otherwise.
- A threshold left ``None`` on the spec skips its check with status
  ``not_configured`` — reported explicitly, never silently omitted.
- A check whose input metric is not healthy (``status != "available"``) or
  whose input series is absent reports ``not_evaluable`` with
  ``passed=None``. Unobservable values stay ``None``; a verdict is never
  fabricated from an unhealthy input.
- When ``spec.target_region`` and ``spec.data_region`` differ, the report
  sets ``region_alignment="mismatched"`` and adds
  :data:`~quant_forge.integrations.contracts.REGION_MISMATCH`. The report
  shape has no predicted-pass-rate field at all, and none may be added: a
  proxy over mismatched-region data cannot honestly predict a platform
  outcome.

FP-G (ex-post-selection ban): this evaluator measures the backtest that
already happened. Its public surface — :class:`SubmissionGateSpec` fields and
the :func:`evaluate_submission_gate` signature — deliberately offers no
parameter that flips a factor's direction, reweights members, re-selects
candidates, or otherwise adapts the evaluated object to its realized
results. A regression test pins this surface; widening it is a reviewed
contract change.

Input shape: ``report`` is the local backtest mapping — the web payload from
``quant_forge.apps.web.api._backtest_payload`` or the richer backtest
artifact JSON (a superset sharing the same key names). Scalar checks read
the status-carrying ``metrics`` map; the cost-inclusive net series
(``net_long_short_sharpe``, ``net_annualized_return``) keeps the proxy
conservative rather than optimistic, and ``rebalance_turnover_mean`` is this
engine's ``turnover_rate``. Series checks (sub-window Sharpe, concentration)
need the artifact's ``period_returns`` rows and report ``not_evaluable``
when the mapping does not carry them.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from quant_forge.integrations.contracts import (
    PRESCREEN_LOCAL_PROXY_ONLY,
    REGION_MISMATCH,
    PrescreenCheck,
    PrescreenReport,
)

__all__ = [
    "GATE_CHECK_NAMES",
    "SubmissionGateSpec",
    "evaluate_submission_gate",
]

# Fixed evaluation order: every report carries exactly these rows, so a
# consumer can rely on presence and ordering regardless of configuration.
GATE_CHECK_NAMES: tuple[str, ...] = (
    "sharpe",
    "fitness",
    "annualized_return",
    "turnover_min",
    "turnover_max",
    "max_single_name_weight",
    "subwindow_sharpe",
)

# Mirrors the backtesting service's annualization scaling so the sub-window
# proxy uses the same arithmetic as the full-window metric it subdivides.
_TRADING_DAYS_PER_YEAR = 252

_HEALTHY_METRIC_STATUS = "available"

_SHARPE_METRIC_KEY = "net_long_short_sharpe"
_RETURN_METRIC_KEY = "net_annualized_return"
_TURNOVER_METRIC_KEY = "rebalance_turnover_mean"


@dataclass(frozen=True)
class SubmissionGateSpec:
    """Thresholds for one target platform's submission gate.

    Every threshold is optional: ``None`` means the platform (or the caller)
    does not gate on that quantity, and the corresponding check reports
    ``not_configured``. ``turnover_floor`` is not a threshold — it is the
    denominator floor of the fitness scaling and is required whenever
    ``fitness_min`` is configured, because a fitness gate without its floor
    is under-specified and guessing a default would be a silent fallback.

    ``target_region`` names the platform region the gate describes;
    ``data_region`` names the region of the local data the backtest ran on.
    They are compared exactly (no normalization): only an exact match is
    reported as aligned.

    Provider-specific threshold values ship with the adapter that owns them,
    never with this public module (D-i/D-iii).
    """

    sharpe_min: float | None = None
    fitness_min: float | None = None
    turnover_floor: float | None = None
    annualized_return_min: float | None = None
    turnover_min: float | None = None
    turnover_max: float | None = None
    max_single_name_weight: float | None = None
    subwindow_sharpe_min: float | None = None
    target_region: str | None = None
    data_region: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "sharpe_min",
            "fitness_min",
            "turnover_floor",
            "annualized_return_min",
            "turnover_min",
            "turnover_max",
            "max_single_name_weight",
            "subwindow_sharpe_min",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number or None")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, float(value))
        if self.fitness_min is not None and self.turnover_floor is None:
            raise ValueError("fitness_min requires turnover_floor to be configured")
        if self.turnover_floor is not None and self.turnover_floor <= 0.0:
            raise ValueError("turnover_floor must be positive")
        if self.turnover_min is not None and self.turnover_min < 0.0:
            raise ValueError("turnover_min must be non-negative")
        if self.turnover_max is not None and self.turnover_max < 0.0:
            raise ValueError("turnover_max must be non-negative")
        if (
            self.turnover_min is not None
            and self.turnover_max is not None
            and self.turnover_min > self.turnover_max
        ):
            raise ValueError("turnover_min must not exceed turnover_max")
        if self.max_single_name_weight is not None and self.max_single_name_weight <= 0.0:
            raise ValueError("max_single_name_weight must be positive")
        for name in ("target_region", "data_region"):
            region = getattr(self, name)
            if region is None:
                continue
            if not isinstance(region, str) or not region.strip():
                raise ValueError(f"{name} must be a non-empty string or None")


def evaluate_submission_gate(
    spec: SubmissionGateSpec, report: Mapping[str, object]
) -> PrescreenReport:
    """Evaluate one local backtest report against a submission-gate spec.

    Pure local math (D-iii): every configured check is computed from the
    numbers in ``report``; nothing is fetched, simulated, or guessed. The
    result always carries ``PRESCREEN_LOCAL_PROXY_ONLY``, adds
    ``REGION_MISMATCH`` (with ``region_alignment="mismatched"``) when the
    spec's data region differs from its target region, and never contains a
    predicted platform pass-rate.

    FP-G (ex-post-selection ban): this function takes exactly a spec and a
    finished report. It exposes no parameter to flip factor direction,
    reweight members, or re-select based on realized results, and it never
    mutates its inputs; the regression test pins this signature.
    """

    if not isinstance(report, Mapping):
        raise ValueError("report must be a mapping of local backtest outputs")

    sharpe_value, sharpe_healthy = _metric_input(report, _SHARPE_METRIC_KEY)
    return_value, return_healthy = _metric_input(report, _RETURN_METRIC_KEY)
    turnover_value, turnover_healthy = _metric_input(report, _TURNOVER_METRIC_KEY)

    checks = (
        _threshold_check(
            "sharpe", spec.sharpe_min, sharpe_value, sharpe_healthy, at_least=True
        ),
        _fitness_check(
            spec,
            sharpe_value=sharpe_value,
            sharpe_healthy=sharpe_healthy,
            return_value=return_value,
            return_healthy=return_healthy,
            turnover_value=turnover_value,
            turnover_healthy=turnover_healthy,
        ),
        _threshold_check(
            "annualized_return",
            spec.annualized_return_min,
            return_value,
            return_healthy,
            at_least=True,
        ),
        _threshold_check(
            "turnover_min", spec.turnover_min, turnover_value, turnover_healthy, at_least=True
        ),
        _threshold_check(
            "turnover_max", spec.turnover_max, turnover_value, turnover_healthy, at_least=False
        ),
        _derived_check(
            "max_single_name_weight",
            spec.max_single_name_weight,
            _max_single_name_weight(report),
            at_least=False,
        ),
        _derived_check(
            "subwindow_sharpe",
            spec.subwindow_sharpe_min,
            _subwindow_sharpe(report),
            at_least=True,
        ),
    )

    if spec.target_region is None or spec.data_region is None:
        region_alignment = "unknown"
    elif spec.target_region == spec.data_region:
        region_alignment = "aligned"
    else:
        region_alignment = "mismatched"
    warning_codes: tuple[str, ...] = (PRESCREEN_LOCAL_PROXY_ONLY,)
    if region_alignment == "mismatched":
        warning_codes = (PRESCREEN_LOCAL_PROXY_ONLY, REGION_MISMATCH)

    return PrescreenReport(
        checks=checks,
        region_alignment=region_alignment,
        warning_codes=warning_codes,
    )


# ---------------------------------------------------------------------------
# Check construction
# ---------------------------------------------------------------------------


def _threshold_check(
    name: str,
    threshold: float | None,
    value: float | None,
    healthy: bool,
    *,
    at_least: bool,
) -> PrescreenCheck:
    """One scalar check against a status-carrying local metric."""

    if threshold is None:
        return PrescreenCheck(name=name, value=None, threshold=None, status="not_configured")
    if not healthy or value is None:
        # FP-I: an unhealthy input yields no verdict and no value — the
        # metric's own status (insufficient_sample, not_applicable, ...)
        # already explains why, and this row must not overrule it.
        return PrescreenCheck(name=name, value=None, threshold=threshold, status="not_evaluable")
    passed = value >= threshold if at_least else value <= threshold
    return PrescreenCheck(
        name=name,
        value=value,
        threshold=threshold,
        status="passed" if passed else "failed",
    )


def _derived_check(
    name: str, threshold: float | None, value: float | None, *, at_least: bool
) -> PrescreenCheck:
    """One check over a value derived from the ``period_returns`` series."""

    if threshold is None:
        return PrescreenCheck(name=name, value=None, threshold=None, status="not_configured")
    if value is None:
        return PrescreenCheck(name=name, value=None, threshold=threshold, status="not_evaluable")
    passed = value >= threshold if at_least else value <= threshold
    return PrescreenCheck(
        name=name,
        value=value,
        threshold=threshold,
        status="passed" if passed else "failed",
    )


def _fitness_check(
    spec: SubmissionGateSpec,
    *,
    sharpe_value: float | None,
    sharpe_healthy: bool,
    return_value: float | None,
    return_healthy: bool,
    turnover_value: float | None,
    turnover_healthy: bool,
) -> PrescreenCheck:
    """fitness = sharpe * sqrt(|annualized_return| / max(turnover, floor)).

    Computed only when ``fitness_min`` is configured, from the same three
    local inputs the scalar checks read. All three must be healthy: a
    fitness assembled around one unhealthy component would be a fabricated
    number wearing a real formula (FP-I).
    """

    if spec.fitness_min is None:
        return PrescreenCheck(name="fitness", value=None, threshold=None, status="not_configured")
    inputs_healthy = (
        sharpe_healthy
        and sharpe_value is not None
        and return_healthy
        and return_value is not None
        and turnover_healthy
        and turnover_value is not None
    )
    if not inputs_healthy:
        return PrescreenCheck(
            name="fitness", value=None, threshold=spec.fitness_min, status="not_evaluable"
        )
    # Spec validation guarantees turnover_floor is a positive float whenever
    # fitness_min is configured, so the denominator is always positive.
    assert spec.turnover_floor is not None
    denominator = max(turnover_value, spec.turnover_floor)
    fitness = sharpe_value * math.sqrt(abs(return_value) / denominator)
    return PrescreenCheck(
        name="fitness",
        value=fitness,
        threshold=spec.fitness_min,
        status="passed" if fitness >= spec.fitness_min else "failed",
    )


# ---------------------------------------------------------------------------
# Local inputs
# ---------------------------------------------------------------------------


def _metric_input(report: Mapping[str, object], key: str) -> tuple[float | None, bool]:
    """Read one status-carrying metric from the report's ``metrics`` map.

    Accepts both serialized payload entries (mappings with ``value`` /
    ``status`` keys) and in-process ``MetricValue`` dataclass instances
    (attributes of the same names). Returns ``(value, healthy)`` where
    ``healthy`` requires status ``"available"`` AND a finite value; anything
    else — missing map, missing entry, unhealthy status, non-finite value —
    is unhealthy with ``value=None``, never a guessed number.
    """

    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        return (None, False)
    entry = metrics.get(key)
    if entry is None:
        return (None, False)
    if isinstance(entry, Mapping):
        raw_value = entry.get("value")
        raw_status = entry.get("status")
    else:
        raw_value = getattr(entry, "value", None)
        raw_status = getattr(entry, "status", None)
    value = _finite_number(raw_value)
    if raw_status != _HEALTHY_METRIC_STATUS or value is None:
        return (None, False)
    return (value, True)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _period_rows(report: Mapping[str, object]) -> tuple[Mapping[str, object], ...] | None:
    """The artifact's per-period rows, or None when absent or malformed."""

    rows = report.get("period_returns")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    collected: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        collected.append(row)
    return tuple(collected)


def _subwindow_sharpe(report: Mapping[str, object]) -> float | None:
    """Minimum of the two half-window Sharpe ratios, or None if unobservable.

    Splits the complete-period net return series (in period order) at its
    midpoint — with an odd count the second half keeps the extra period —
    and applies the engine's own scaling, ``mean / std(ddof=1) *
    sqrt(252 / holding_days)``, to each half. Incomplete periods are
    excluded exactly as the full-window Sharpe excludes them (COR-6): a
    partial tail must not be scaled as a full holding period. Either half
    lacking two complete observations, a zero standard deviation, a missing
    ``holding_days``, or a malformed row makes the value unobservable.
    """

    rows = _period_rows(report)
    if rows is None:
        return None
    holding_days = report.get("holding_days")
    if (
        isinstance(holding_days, bool)
        or not isinstance(holding_days, int)
        or holding_days <= 0
    ):
        return None
    returns: list[float] = []
    for row in rows:
        if not bool(row.get("is_complete_period", True)):
            continue
        value = _finite_number(row.get("net_period_return"))
        if value is None:
            return None
        returns.append(value)
    midpoint = len(returns) // 2
    halves = (returns[:midpoint], returns[midpoint:])
    scale = math.sqrt(_TRADING_DAYS_PER_YEAR / holding_days)
    sharpes: list[float] = []
    for half in halves:
        if len(half) < 2:
            return None
        deviation = statistics.stdev(half)
        if deviation == 0.0:
            return None
        sharpes.append(statistics.fmean(half) / deviation * scale)
    return min(sharpes)


def _max_single_name_weight(report: Mapping[str, object]) -> float | None:
    """Largest single-name share of gross book value across periods.

    The local engine builds dollar-neutral equal-weight legs (each leg's
    absolute weights sum to 1), so within a period the largest absolute
    single-name weight is ``1 / min(populated leg counts)`` and the gross
    book is the number of populated legs. The reported value is the maximum
    share across all periods; periods with no positions contribute nothing.
    Missing ``period_returns`` or malformed counts make the value
    unobservable (None), never zero.
    """

    rows = _period_rows(report)
    if rows is None:
        return None
    worst: float | None = None
    for row in rows:
        long_count = _leg_count(row.get("long_count"))
        short_count = _leg_count(row.get("short_count"))
        if long_count is None or short_count is None:
            return None
        populated = [count for count in (long_count, short_count) if count > 0]
        if not populated:
            continue
        share = (1.0 / min(populated)) / float(len(populated))
        if worst is None or share > worst:
            worst = share
    return worst


def _leg_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value

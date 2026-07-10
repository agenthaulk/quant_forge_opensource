"""Tests for the provider-neutral submission-gate evaluator (CP2, D-iii).

Pins the honesty contract of quant_forge.integrations.gate:

- every threshold is optional: an unconfigured check is reported (skipped)
  as ``not_configured``, never silently omitted and never a verdict;
- an unhealthy or absent input makes a configured check ``not_evaluable``
  with ``passed=None`` — the evaluator never fabricates a pass/fail
  (the workorder's "unevaluable" is the CP1 closed-set spelling
  ``not_evaluable``);
- fitness arithmetic is exact on synthetic numbers, including the turnover
  floor and the sign convention (abs() applies to the return only);
- region mismatch adds REGION_MISMATCH and the report shape has no
  predicted-pass-rate field to claim anything with;
- FP-G: the public surface offers no ex-post-selection knob, pinned by
  signature and field-allowlist assertions.

Every SubmissionGateSpec in this file uses obviously synthetic values
(9.9-style thresholds, REGION_A/REGION_B regions). Real platform threshold
values live with the adapter that owns them, never in public tests (D-i).
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import math
import statistics

import pytest

from quant_forge.core.contracts import MetricValue
from quant_forge.integrations import gate
from quant_forge.integrations.contracts import (
    PRESCREEN_LOCAL_PROXY_ONLY,
    REGION_MISMATCH,
    PrescreenReport,
)
from quant_forge.integrations.gate import (
    GATE_CHECK_NAMES,
    SubmissionGateSpec,
    evaluate_submission_gate,
)


# ---------------------------------------------------------------------------
# Synthetic report builders (never real platform data)
# ---------------------------------------------------------------------------


def _metric(value: float | None, status: str = "available") -> dict[str, object]:
    return {"value": value, "status": status}


def _healthy_metrics(
    *,
    sharpe: float = 2.0,
    annualized_return: float = 0.32,
    turnover: float = 0.5,
) -> dict[str, dict[str, object]]:
    return {
        "net_long_short_sharpe": _metric(sharpe),
        "net_annualized_return": _metric(annualized_return),
        "rebalance_turnover_mean": _metric(turnover),
    }


def _report(
    *,
    metrics: dict[str, dict[str, object]] | None = None,
    period_returns: list[dict[str, object]] | None = None,
    holding_days: int = 5,
) -> dict[str, object]:
    report: dict[str, object] = {
        "factor_id": "SYNTHETIC_FACTOR",
        "holding_days": holding_days,
        "metrics": _healthy_metrics() if metrics is None else metrics,
    }
    if period_returns is not None:
        report["period_returns"] = period_returns
    return report


def _row(
    net_period_return: float,
    *,
    long_count: int = 5,
    short_count: int = 5,
    is_complete_period: bool = True,
) -> dict[str, object]:
    return {
        "net_period_return": net_period_return,
        "long_count": long_count,
        "short_count": short_count,
        "is_complete_period": is_complete_period,
    }


def _checks_by_name(report: PrescreenReport) -> dict[str, object]:
    return {check.name: check for check in report.checks}


def _half_sharpe(returns: list[float], holding_days: int) -> float:
    return statistics.fmean(returns) / statistics.stdev(returns) * math.sqrt(252 / holding_days)


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


def test_spec_fitness_requires_turnover_floor() -> None:
    with pytest.raises(ValueError, match="turnover_floor"):
        SubmissionGateSpec(fitness_min=9.9)


def test_spec_rejects_non_positive_turnover_floor() -> None:
    with pytest.raises(ValueError, match="turnover_floor"):
        SubmissionGateSpec(fitness_min=9.9, turnover_floor=0.0)


def test_spec_rejects_inverted_turnover_band() -> None:
    with pytest.raises(ValueError, match="turnover_min"):
        SubmissionGateSpec(turnover_min=0.9, turnover_max=0.1)


def test_spec_rejects_non_finite_and_non_numeric_thresholds() -> None:
    with pytest.raises(ValueError, match="sharpe_min"):
        SubmissionGateSpec(sharpe_min=float("nan"))
    with pytest.raises(ValueError, match="annualized_return_min"):
        SubmissionGateSpec(annualized_return_min=float("inf"))
    with pytest.raises(ValueError, match="sharpe_min"):
        SubmissionGateSpec(sharpe_min=True)  # type: ignore[arg-type]


def test_spec_rejects_negative_band_and_zero_concentration() -> None:
    with pytest.raises(ValueError, match="turnover_min"):
        SubmissionGateSpec(turnover_min=-0.1)
    with pytest.raises(ValueError, match="max_single_name_weight"):
        SubmissionGateSpec(max_single_name_weight=0.0)


def test_spec_rejects_blank_region() -> None:
    with pytest.raises(ValueError, match="target_region"):
        SubmissionGateSpec(target_region="  ")


def test_spec_normalizes_integer_thresholds_to_float() -> None:
    spec = SubmissionGateSpec(sharpe_min=9)
    assert isinstance(spec.sharpe_min, float)
    assert spec.sharpe_min == 9.0


# ---------------------------------------------------------------------------
# Scalar checks: pass / fail / not_configured / not_evaluable
# ---------------------------------------------------------------------------


def test_sharpe_check_passes_and_fails_on_net_series() -> None:
    passed = evaluate_submission_gate(
        SubmissionGateSpec(sharpe_min=1.5), _report(metrics=_healthy_metrics(sharpe=2.0))
    )
    sharpe = _checks_by_name(passed)["sharpe"]
    assert sharpe.status == "passed"
    assert sharpe.passed is True
    assert sharpe.value == pytest.approx(2.0)
    assert sharpe.threshold == pytest.approx(1.5)

    failed = evaluate_submission_gate(
        SubmissionGateSpec(sharpe_min=9.9), _report(metrics=_healthy_metrics(sharpe=2.0))
    )
    assert _checks_by_name(failed)["sharpe"].status == "failed"
    assert _checks_by_name(failed)["sharpe"].passed is False


def test_unconfigured_threshold_reports_not_configured_and_skips() -> None:
    report = evaluate_submission_gate(SubmissionGateSpec(), _report())
    assert [check.name for check in report.checks] == list(GATE_CHECK_NAMES)
    for check in report.checks:
        assert check.status == "not_configured"
        assert check.passed is None
        assert check.value is None
        assert check.threshold is None
    # A fully skipped evaluation still tells the truth about what it is.
    assert PRESCREEN_LOCAL_PROXY_ONLY in report.warning_codes


def test_unhealthy_metric_status_yields_not_evaluable_never_a_verdict() -> None:
    metrics = _healthy_metrics()
    metrics["net_long_short_sharpe"] = _metric(None, status="insufficient_sample")
    report = evaluate_submission_gate(SubmissionGateSpec(sharpe_min=9.9), _report(metrics=metrics))
    sharpe = _checks_by_name(report)["sharpe"]
    assert sharpe.status == "not_evaluable"
    assert sharpe.passed is None
    assert sharpe.value is None
    assert sharpe.threshold == pytest.approx(9.9)


def test_missing_metrics_map_or_entry_yields_not_evaluable() -> None:
    no_map = evaluate_submission_gate(
        SubmissionGateSpec(sharpe_min=9.9), {"factor_id": "SYNTHETIC_FACTOR"}
    )
    assert _checks_by_name(no_map)["sharpe"].status == "not_evaluable"

    missing_entry = evaluate_submission_gate(
        SubmissionGateSpec(annualized_return_min=9.9),
        _report(metrics={"net_long_short_sharpe": _metric(2.0)}),
    )
    assert _checks_by_name(missing_entry)["annualized_return"].status == "not_evaluable"


def test_available_status_with_non_finite_value_is_not_evaluable() -> None:
    metrics = _healthy_metrics()
    metrics["net_long_short_sharpe"] = _metric(float("nan"), status="available")
    report = evaluate_submission_gate(SubmissionGateSpec(sharpe_min=9.9), _report(metrics=metrics))
    assert _checks_by_name(report)["sharpe"].status == "not_evaluable"


def test_metric_entries_as_dataclass_instances_are_supported() -> None:
    metrics = {
        "net_long_short_sharpe": MetricValue(
            value=2.0, unit="ratio", status="available", observation_count=10
        ),
        "net_annualized_return": MetricValue(
            value=0.32, unit="return", status="available", observation_count=10
        ),
        "rebalance_turnover_mean": MetricValue(
            value=0.5, unit="ratio", status="available", observation_count=9
        ),
    }
    report = evaluate_submission_gate(
        SubmissionGateSpec(sharpe_min=1.5), {"metrics": metrics, "holding_days": 5}
    )
    assert _checks_by_name(report)["sharpe"].status == "passed"


def test_annualized_return_check_uses_net_metric() -> None:
    report = evaluate_submission_gate(
        SubmissionGateSpec(annualized_return_min=0.3),
        _report(metrics=_healthy_metrics(annualized_return=0.32)),
    )
    row = _checks_by_name(report)["annualized_return"]
    assert row.status == "passed"
    assert row.value == pytest.approx(0.32)


# ---------------------------------------------------------------------------
# Turnover band
# ---------------------------------------------------------------------------


def test_turnover_band_inside_passes_both_bounds() -> None:
    report = evaluate_submission_gate(
        SubmissionGateSpec(turnover_min=0.1, turnover_max=0.7),
        _report(metrics=_healthy_metrics(turnover=0.5)),
    )
    checks = _checks_by_name(report)
    assert checks["turnover_min"].status == "passed"
    assert checks["turnover_max"].status == "passed"


def test_turnover_band_outside_fails_the_violated_bound_only() -> None:
    below = evaluate_submission_gate(
        SubmissionGateSpec(turnover_min=0.1, turnover_max=0.7),
        _report(metrics=_healthy_metrics(turnover=0.05)),
    )
    assert _checks_by_name(below)["turnover_min"].status == "failed"
    assert _checks_by_name(below)["turnover_max"].status == "passed"

    above = evaluate_submission_gate(
        SubmissionGateSpec(turnover_min=0.1, turnover_max=0.7),
        _report(metrics=_healthy_metrics(turnover=0.9)),
    )
    assert _checks_by_name(above)["turnover_min"].status == "passed"
    assert _checks_by_name(above)["turnover_max"].status == "failed"


def test_turnover_band_single_sided_configuration() -> None:
    max_only = evaluate_submission_gate(
        SubmissionGateSpec(turnover_max=0.7), _report(metrics=_healthy_metrics(turnover=0.5))
    )
    assert _checks_by_name(max_only)["turnover_min"].status == "not_configured"
    assert _checks_by_name(max_only)["turnover_max"].status == "passed"


def test_turnover_not_applicable_status_is_not_evaluable() -> None:
    metrics = _healthy_metrics()
    metrics["rebalance_turnover_mean"] = _metric(None, status="not_applicable")
    report = evaluate_submission_gate(
        SubmissionGateSpec(turnover_min=0.1, turnover_max=0.7), _report(metrics=metrics)
    )
    assert _checks_by_name(report)["turnover_min"].status == "not_evaluable"
    assert _checks_by_name(report)["turnover_max"].status == "not_evaluable"


# ---------------------------------------------------------------------------
# Fitness arithmetic (synthetic numbers, exact)
# ---------------------------------------------------------------------------


def test_fitness_arithmetic_above_the_floor() -> None:
    # fitness = 2.0 * sqrt(|0.32| / max(0.5, 0.125)) = 2.0 * 0.8 = 1.6
    report = evaluate_submission_gate(
        SubmissionGateSpec(fitness_min=1.5, turnover_floor=0.125),
        _report(metrics=_healthy_metrics(sharpe=2.0, annualized_return=0.32, turnover=0.5)),
    )
    fitness = _checks_by_name(report)["fitness"]
    assert fitness.value == pytest.approx(1.6)
    assert fitness.status == "passed"


def test_fitness_arithmetic_engages_the_turnover_floor() -> None:
    # turnover 0.02 < floor 0.125: fitness = 2.0 * sqrt(0.32 / 0.125) = 3.2
    report = evaluate_submission_gate(
        SubmissionGateSpec(fitness_min=9.9, turnover_floor=0.125),
        _report(metrics=_healthy_metrics(sharpe=2.0, annualized_return=0.32, turnover=0.02)),
    )
    fitness = _checks_by_name(report)["fitness"]
    assert fitness.value == pytest.approx(3.2)
    assert fitness.status == "failed"


def test_fitness_abs_applies_to_return_but_sign_comes_from_sharpe() -> None:
    # A negative-sharpe factor must never gain fitness from a negative
    # return: fitness = -1.0 * sqrt(|-0.32| / 0.5) = -0.8.
    report = evaluate_submission_gate(
        SubmissionGateSpec(fitness_min=0.5, turnover_floor=0.125),
        _report(metrics=_healthy_metrics(sharpe=-1.0, annualized_return=-0.32, turnover=0.5)),
    )
    fitness = _checks_by_name(report)["fitness"]
    assert fitness.value == pytest.approx(-0.8)
    assert fitness.status == "failed"


def test_fitness_with_any_unhealthy_component_is_not_evaluable() -> None:
    metrics = _healthy_metrics()
    metrics["net_annualized_return"] = _metric(None, status="insufficient_sample")
    report = evaluate_submission_gate(
        SubmissionGateSpec(fitness_min=9.9, turnover_floor=0.125), _report(metrics=metrics)
    )
    fitness = _checks_by_name(report)["fitness"]
    assert fitness.status == "not_evaluable"
    assert fitness.passed is None
    assert fitness.value is None


# ---------------------------------------------------------------------------
# Concentration (max single-name share of gross book)
# ---------------------------------------------------------------------------


def test_max_single_name_weight_from_equal_weight_dollar_neutral_legs() -> None:
    rows = [
        _row(0.01, long_count=5, short_count=4),  # (1/4) / 2 legs = 0.125
        _row(0.02, long_count=10, short_count=10),  # 0.05
    ]
    passed = evaluate_submission_gate(
        SubmissionGateSpec(max_single_name_weight=0.2), _report(period_returns=rows)
    )
    check = _checks_by_name(passed)["max_single_name_weight"]
    assert check.value == pytest.approx(0.125)
    assert check.status == "passed"

    failed = evaluate_submission_gate(
        SubmissionGateSpec(max_single_name_weight=0.1), _report(period_returns=rows)
    )
    assert _checks_by_name(failed)["max_single_name_weight"].status == "failed"


def test_max_single_name_weight_single_populated_leg() -> None:
    rows = [_row(0.01, long_count=4, short_count=0)]  # (1/4) / 1 leg = 0.25
    report = evaluate_submission_gate(
        SubmissionGateSpec(max_single_name_weight=0.3), _report(period_returns=rows)
    )
    assert _checks_by_name(report)["max_single_name_weight"].value == pytest.approx(0.25)


def test_max_single_name_weight_unobservable_inputs_are_not_evaluable() -> None:
    no_rows = evaluate_submission_gate(SubmissionGateSpec(max_single_name_weight=0.2), _report())
    assert _checks_by_name(no_rows)["max_single_name_weight"].status == "not_evaluable"

    malformed = evaluate_submission_gate(
        SubmissionGateSpec(max_single_name_weight=0.2),
        _report(period_returns=[{"net_period_return": 0.01, "long_count": "five"}]),
    )
    assert _checks_by_name(malformed)["max_single_name_weight"].status == "not_evaluable"

    all_empty = evaluate_submission_gate(
        SubmissionGateSpec(max_single_name_weight=0.2),
        _report(period_returns=[_row(0.0, long_count=0, short_count=0)]),
    )
    assert _checks_by_name(all_empty)["max_single_name_weight"].status == "not_evaluable"


# ---------------------------------------------------------------------------
# Sub-window Sharpe (halves of the window)
# ---------------------------------------------------------------------------


def test_subwindow_sharpe_is_the_minimum_of_the_two_halves() -> None:
    rows = [_row(0.01), _row(0.03), _row(0.02), _row(0.04)]
    expected = min(
        _half_sharpe([0.01, 0.03], 5),
        _half_sharpe([0.02, 0.04], 5),
    )
    passed = evaluate_submission_gate(
        SubmissionGateSpec(subwindow_sharpe_min=9.9),
        _report(period_returns=rows, holding_days=5),
    )
    check = _checks_by_name(passed)["subwindow_sharpe"]
    assert check.value == pytest.approx(expected)
    assert check.status == "passed"  # expected ≈ 10.04 > 9.9

    failed = evaluate_submission_gate(
        SubmissionGateSpec(subwindow_sharpe_min=88.8),
        _report(period_returns=rows, holding_days=5),
    )
    assert _checks_by_name(failed)["subwindow_sharpe"].status == "failed"


def test_subwindow_split_gives_the_extra_period_to_the_second_half() -> None:
    rows = [_row(0.01), _row(0.03), _row(0.02), _row(0.04), _row(0.06)]
    expected = min(
        _half_sharpe([0.01, 0.03], 5),
        _half_sharpe([0.02, 0.04, 0.06], 5),
    )
    report = evaluate_submission_gate(
        SubmissionGateSpec(subwindow_sharpe_min=9.9),
        _report(period_returns=rows, holding_days=5),
    )
    assert _checks_by_name(report)["subwindow_sharpe"].value == pytest.approx(expected)


def test_subwindow_excludes_incomplete_periods_like_the_engine() -> None:
    complete_only = evaluate_submission_gate(
        SubmissionGateSpec(subwindow_sharpe_min=9.9),
        _report(period_returns=[_row(0.01), _row(0.03), _row(0.02), _row(0.04)]),
    )
    with_partial_tail = evaluate_submission_gate(
        SubmissionGateSpec(subwindow_sharpe_min=9.9),
        _report(
            period_returns=[
                _row(0.01),
                _row(0.03),
                _row(0.02),
                _row(0.04),
                _row(-0.99, is_complete_period=False),
            ]
        ),
    )
    assert _checks_by_name(with_partial_tail)["subwindow_sharpe"].value == pytest.approx(
        _checks_by_name(complete_only)["subwindow_sharpe"].value
    )


def test_subwindow_unobservable_inputs_are_not_evaluable() -> None:
    too_few = evaluate_submission_gate(
        SubmissionGateSpec(subwindow_sharpe_min=9.9),
        _report(period_returns=[_row(0.01), _row(0.02), _row(0.03)]),
    )
    assert _checks_by_name(too_few)["subwindow_sharpe"].status == "not_evaluable"

    no_rows = evaluate_submission_gate(SubmissionGateSpec(subwindow_sharpe_min=9.9), _report())
    assert _checks_by_name(no_rows)["subwindow_sharpe"].status == "not_evaluable"

    zero_variance = evaluate_submission_gate(
        SubmissionGateSpec(subwindow_sharpe_min=9.9),
        _report(period_returns=[_row(0.01), _row(0.01), _row(0.02), _row(0.04)]),
    )
    assert _checks_by_name(zero_variance)["subwindow_sharpe"].status == "not_evaluable"


# ---------------------------------------------------------------------------
# Region honesty and report shape
# ---------------------------------------------------------------------------


def test_every_report_carries_the_local_proxy_warning() -> None:
    report = evaluate_submission_gate(
        SubmissionGateSpec(sharpe_min=9.9, target_region="REGION_A", data_region="REGION_A"),
        _report(),
    )
    assert PRESCREEN_LOCAL_PROXY_ONLY in report.warning_codes
    assert report.region_alignment == "aligned"
    assert REGION_MISMATCH not in report.warning_codes


def test_region_mismatch_adds_the_code_and_marks_alignment() -> None:
    report = evaluate_submission_gate(
        SubmissionGateSpec(sharpe_min=9.9, target_region="REGION_A", data_region="REGION_B"),
        _report(),
    )
    assert report.region_alignment == "mismatched"
    assert REGION_MISMATCH in report.warning_codes
    assert PRESCREEN_LOCAL_PROXY_ONLY in report.warning_codes


def test_missing_region_information_is_unknown_not_a_claim() -> None:
    report = evaluate_submission_gate(SubmissionGateSpec(sharpe_min=9.9), _report())
    assert report.region_alignment == "unknown"
    assert REGION_MISMATCH not in report.warning_codes


def test_report_shape_has_no_predicted_pass_rate_field() -> None:
    # D-iii: a local proxy must not claim a predicted platform pass-rate.
    # The report shape simply has no field to carry one — keep it that way.
    field_names = {field.name for field in dataclasses.fields(PrescreenReport)}
    assert field_names == {"checks", "region_alignment", "warning_codes"}


def test_evaluator_reports_all_checks_in_fixed_order_and_does_not_mutate_input() -> None:
    payload = _report(period_returns=[_row(0.01), _row(0.03), _row(0.02), _row(0.04)])
    snapshot = copy.deepcopy(payload)
    report = evaluate_submission_gate(
        SubmissionGateSpec(
            sharpe_min=1.5,
            fitness_min=1.5,
            turnover_floor=0.125,
            annualized_return_min=0.1,
            turnover_min=0.1,
            turnover_max=0.7,
            max_single_name_weight=0.2,
            subwindow_sharpe_min=9.9,
        ),
        payload,
    )
    assert [check.name for check in report.checks] == list(GATE_CHECK_NAMES)
    assert payload == snapshot


def test_non_mapping_report_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="mapping"):
        evaluate_submission_gate(SubmissionGateSpec(), None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FP-G: the ex-post-selection ban is pinned on the public surface
# ---------------------------------------------------------------------------


def test_fp_g_public_surface_offers_no_ex_post_selection_knob() -> None:
    # The evaluator takes exactly a spec and a finished report — no third
    # input, no variadic backdoor a direction/reweight/reselect flag could
    # arrive through.
    signature = inspect.signature(evaluate_submission_gate)
    assert list(signature.parameters) == ["spec", "report"]
    for parameter in signature.parameters.values():
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    # The spec carries thresholds and region labels only. Any new field is a
    # reviewed contract change that must consciously edit this allowlist.
    spec_fields = {field.name for field in dataclasses.fields(SubmissionGateSpec)}
    assert spec_fields == {
        "sharpe_min",
        "fitness_min",
        "turnover_floor",
        "annualized_return_min",
        "turnover_min",
        "turnover_max",
        "max_single_name_weight",
        "subwindow_sharpe_min",
        "target_region",
        "data_region",
    }

    # The module's public surface is exactly the spec, the evaluator, and
    # the check-name constant.
    assert gate.__all__ == ["GATE_CHECK_NAMES", "SubmissionGateSpec", "evaluate_submission_gate"]

    # The ban is stated where maintainers will read it.
    assert "FP-G" in (gate.__doc__ or "")
    assert "FP-G" in (evaluate_submission_gate.__doc__ or "")

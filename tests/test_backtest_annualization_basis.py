"""F2 (Phase-1/2 audit): annualization basis under skipped rebalances.

The exposure-day annualization compounds the cumulative return over in-market
trading days only, excluding the flat gap days a skipped rebalance leaves in the
schedule. For a gappy schedule that over-states the annualized return (fewer
days in the denominator ⇒ a larger annual rate). When any rebalance is skipped
the headline switches to a **calendar trading-day** basis (first traded entry to
last traded exit, INCLUDING the skipped-window gap days); both bases are always
emitted with their own method/provenance (qf.metrics.v2) and the old exposure-day
figure is preserved verbatim so nothing is lost.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from quant_forge.backtesting.service import (
    ANNUALIZATION_BASIS_CALENDAR_TIME,
    MIN_ANNUALIZATION_EXPOSURE_DAYS,
    _annualized_return,
    _return_summary,
    run_factor_backtest,
)
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import FactorRepository

# ---------------------------------------------------------------------------
# Unit level: _return_summary two-basis contract
# ---------------------------------------------------------------------------

_RETURNS = np.array([0.05, 0.04, 0.03, 0.06], dtype=float)


def test_return_summary_switches_headline_to_calendar_and_dilutes_positive_rate() -> None:
    exposure = 130  # in-market trading days (>= MIN so it stays reportable)
    calendar = 200  # calendar-time span incl. skipped gap days
    summary = _return_summary(
        _RETURNS,
        holding_days=5,
        exposure_days=exposure,
        calendar_exposure_days=calendar,
        use_calendar_basis=True,
    )
    cum = summary["cumulative_return"]
    exp_metric = summary["annualized_return_exposure"]
    cal_metric = summary["annualized_return_calendar"]

    # Both bases emitted, each with its own denominator and provenance.
    assert exp_metric.value == pytest.approx(_annualized_return(cum, exposure))
    assert cal_metric.value == pytest.approx(_annualized_return(cum, calendar))
    assert exp_metric.observation_count == exposure
    assert cal_metric.observation_count == calendar
    assert exp_metric.method == "geometric_annualization_exposure_days"
    assert cal_metric.method == "geometric_annualization_calendar_time"
    assert cal_metric.source_series == "calendar_trading_days_span"

    # Headline points at the calendar basis and dilutes the positive annual rate
    # (more days in the denominator -> a smaller annualized return).
    assert summary["headline_annualization_basis"] == "calendar_trading_days"
    assert summary["annualized_return"] == pytest.approx(cal_metric.value)
    assert cal_metric.value < exp_metric.value

    # The switch is disclosed, never silent, and the reportable headline mirrors
    # the calendar basis value AND day count.
    assert ANNUALIZATION_BASIS_CALENDAR_TIME in summary["warning_codes"]
    assert ANNUALIZATION_BASIS_CALENDAR_TIME in summary["reportable_annualization"].warning_codes
    assert summary["reportable_annualization"].value == pytest.approx(cal_metric.value)
    assert summary["reportable_annualization"].observation_count == calendar


def test_return_summary_defaults_to_exposure_basis_when_no_skips() -> None:
    exposure = 140
    summary = _return_summary(_RETURNS, holding_days=5, exposure_days=exposure)

    # No calendar override and no skip flag: calendar collapses onto exposure,
    # the headline stays on the exposure-day basis, and no switch code fires.
    assert summary["headline_annualization_basis"] == "exposure_days"
    assert summary["annualized_return_calendar"].value == pytest.approx(
        summary["annualized_return_exposure"].value
    )
    assert summary["annualized_return"] == pytest.approx(summary["annualized_return_exposure"].value)
    assert ANNUALIZATION_BASIS_CALENDAR_TIME not in summary["warning_codes"]
    assert summary["reportable_annualization"].observation_count == exposure


def test_calendar_gate_is_independent_of_exposure_gate() -> None:
    # Exposure below the min-sample floor but calendar above it: the exposure
    # basis is suppressed to None while the calendar (headline) basis reports.
    summary = _return_summary(
        np.array([0.02, 0.03], dtype=float),
        holding_days=5,
        exposure_days=100,  # < MIN_ANNUALIZATION_EXPOSURE_DAYS
        calendar_exposure_days=MIN_ANNUALIZATION_EXPOSURE_DAYS + 40,
        use_calendar_basis=True,
    )
    assert summary["annualized_return_exposure"].value is None
    assert summary["annualized_return_exposure"].status == "insufficient_sample"
    assert summary["annualized_return_calendar"].value is not None
    assert summary["annualized_return"] == pytest.approx(summary["annualized_return_calendar"].value)


# ---------------------------------------------------------------------------
# Integration level: run_factor_backtest artifact
# ---------------------------------------------------------------------------

FACTOR_ID = "FTR_ANNUALIZATION_PROBE"
INSTRUMENTS = ("AAA01", "AAA02", "AAA03", "AAA04", "AAA05", "AAA06")


def _workspace(root: Path, *, all_st_signal_indices: tuple[int, ...], days: int = 200) -> dict[str, Path]:
    """Six-instrument panel; blanked signal days force whole-book skips.

    With delay=1/holding=5 the signal grid is 0,5,10,...; setting every
    instrument to is_st on a signal day empties that day's cross-section
    (REBALANCE_SKIPPED_NO_COVERAGE). Prices are strictly increasing so period
    returns and NAV marks are well defined and the run is comfortably long
    enough for the exposure-day basis to clear the reporting floor.
    """

    data_root = root / "data"
    factor_root = root / "factor_root"
    data_root.mkdir(parents=True)
    dates = list(pd.bdate_range("2026-01-05", periods=days))
    blanked = set(all_st_signal_indices)
    rows: list[dict[str, object]] = []
    for index, instrument in enumerate(INSTRUMENTS):
        for day_index, trade_date in enumerate(dates):
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": instrument,
                    "close": 10.0 + index + 0.02 * day_index * (index + 1),
                    "market_cap": 1_000_000_000.0 * (index + 1),
                    "is_st": bool(day_index in blanked),
                }
            )
    pd.DataFrame(rows).to_parquet(data_root / "panel.parquet", index=False)
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id=FACTOR_ID,
            name="annualization_probe",
            formula="-rank(market_cap)",
            horizon_days=5,
            universe_filters=("is_st == false",),
        )
    )
    return {"data_root": data_root, "factor_root": factor_root}


def _run(paths: dict[str, Path], artifact_root: Path):
    result = run_factor_backtest(
        FACTOR_ID,
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=artifact_root,
        holding_days=5,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    return result, payload


def test_gappy_schedule_headline_uses_calendar_basis(tmp_path: Path) -> None:
    # Three lone interior skips, each opening a 5-day flat gap the exposure-day
    # basis would exclude from the annualization denominator.
    paths = _workspace(tmp_path, all_st_signal_indices=(50, 100, 150))
    result, payload = _run(paths, tmp_path / "artifacts")

    assert payload["skipped_rebalances"] == 3
    assert result.skipped_rebalances == 3

    # Calendar span strictly exceeds in-market exposure by the skipped gap days.
    assert payload["calendar_exposure_days"] > payload["exposure_days"]
    assert payload["calendar_exposure_days"] == payload["exposure_days"] + 3 * 5

    # Both bases must clear the reporting floor for the comparison to be sharp.
    assert payload["exposure_days"] >= MIN_ANNUALIZATION_EXPOSURE_DAYS

    # Headline switched to calendar-time and is disclosed.
    assert payload["headline_annualization_basis"] == "calendar_trading_days"
    assert ANNUALIZATION_BASIS_CALENDAR_TIME in payload["warning_codes"]
    assert ANNUALIZATION_BASIS_CALENDAR_TIME in result.warning_codes

    # Headline == calendar basis, and it genuinely differs from the exposure one.
    assert payload["annualized_return"] == pytest.approx(payload["annualized_return_calendar"])
    assert payload["annualized_return_exposure"] is not None
    assert payload["annualized_return_calendar"] is not None
    assert payload["annualized_return"] != pytest.approx(payload["annualized_return_exposure"])
    # net headline follows the same basis.
    assert payload["net_annualized_return"] == pytest.approx(payload["net_annualized_return_calendar"])

    # Provenance for the calendar basis is complete (qf.metrics.v2).
    cal_prov = payload["metric_provenance"]["annualized_return_calendar"]
    assert cal_prov["method"] == "geometric_annualization_calendar_time"
    assert cal_prov["observation_count"] == payload["calendar_exposure_days"]
    exp_metric = payload["metrics"]["annualized_return_exposure"]
    assert exp_metric["observation_count"] == payload["exposure_days"]
    assert exp_metric["value"] == pytest.approx(payload["annualized_return_exposure"])

    # The dedicated exposure-basis field preserves the OLD headline number.
    cum = payload["gross_cumulative_return"]
    assert payload["annualized_return_exposure"] == pytest.approx(
        _annualized_return(cum, payload["exposure_days"])
    )


def test_full_coverage_schedule_keeps_exposure_basis(tmp_path: Path) -> None:
    # Control: no skips ⇒ calendar span == exposure days exactly, headline stays
    # on the exposure-day basis, and no switch code is emitted (backward
    # compatible: the annualized_return field is unchanged for clean schedules).
    paths = _workspace(tmp_path, all_st_signal_indices=())
    result, payload = _run(paths, tmp_path / "artifacts")

    assert payload["skipped_rebalances"] == 0
    assert payload["calendar_exposure_days"] == payload["exposure_days"]
    assert payload["headline_annualization_basis"] == "exposure_days"
    assert ANNUALIZATION_BASIS_CALENDAR_TIME not in payload["warning_codes"]
    assert payload["annualized_return"] == pytest.approx(payload["annualized_return_exposure"])
    assert payload["annualized_return_exposure"] == pytest.approx(payload["annualized_return_calendar"])
    # The headline MetricValue's observation count is the in-market exposure.
    assert result.metrics["annualized_return"].observation_count == payload["exposure_days"]

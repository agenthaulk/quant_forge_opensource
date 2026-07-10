"""§4.5 coverage honesty (FP-4) + RB-7 synthesis-side rebalance pre-scan.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.5, §13
test_coverage / test_skipped_rebalance, FP-4): per-factor ``rows_scored`` /
``rows_in_composite`` / ``coverage_ratio`` where an unobservable denominator
emits a real ``None`` — never a fabricated 0 — while a genuinely observed
zero stays 0.0; ``rows_required`` / ``rows_full_coverage`` under both
coverage rules. The pre-scan classifies every shared-grid rebalance date
(RB-5: the ``rebalance_indices`` helper, never an independent schedule) as
ok / empty / thin against composite coverage, emitting the engine's own
``REBALANCE_SKIPPED_NO_COVERAGE`` / ``REBALANCE_SKIPPED_THIN`` code literals.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from quant_forge.backtesting.service import (
    REBALANCE_SKIPPED_NO_COVERAGE,
    REBALANCE_SKIPPED_THIN,
    rebalance_indices,
)
from quant_forge.synthesis.service import (
    build_score_matrix,
    combine_apriori,
    prescan_rebalance_coverage,
)

D1 = pd.Timestamp("2026-01-05")
D2 = pd.Timestamp("2026-01-06")


def tidy(rows: list[tuple[pd.Timestamp, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": trade_date, "instrument": instrument, "score": score}
            for trade_date, instrument, score in rows
        ]
    )


def _coverage_by_factor(result) -> dict[str, object]:
    return {entry.factor_id: entry for entry in result.coverage.per_factor}


def test_rows_scored_in_composite_and_ratio_under_all_factors() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy(
                [
                    (D1, "A", 1.0),
                    (D1, "B", 2.0),
                    (D1, "C", 4.0),
                    (D2, "A", 3.0),
                    (D2, "B", 5.0),
                    (D2, "C", 6.0),
                ]
            ),
            "f2": tidy(
                [
                    (D1, "A", 2.0),
                    (D1, "B", 1.0),
                    (D1, "C", 7.0),
                    (D2, "A", 9.0),
                    (D2, "B", np.nan),
                    (D2, "C", np.nan),
                ]
            ),
        }
    )
    result = combine_apriori(matrix, method="equal_weight")
    coverage = result.coverage
    assert coverage.coverage_rule == "all_factors"
    assert coverage.rows_required == 6
    assert coverage.rows_full_coverage == 4
    by_factor = _coverage_by_factor(result)
    assert by_factor["f1"].rows_scored == 6
    assert by_factor["f1"].rows_in_composite == 4
    assert by_factor["f1"].coverage_ratio == pytest.approx(4.0 / 6.0)
    assert by_factor["f2"].rows_scored == 4
    assert by_factor["f2"].rows_in_composite == 4
    assert by_factor["f2"].coverage_ratio == pytest.approx(1.0)
    # Accounting boundary (documented): D2 collapses to a single-name
    # cross-section and is erased by RB-9, but coverage stays the combine-time
    # account — the erasure is disclosed separately, not double-counted here.
    assert result.degenerate_dates == (D2,)
    assert by_factor["f1"].rows_in_composite == 4


def test_unobservable_denominator_emits_real_none_never_zero() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy([(D1, "A", 1.0), (D1, "B", 2.0), (D2, "A", 3.0), (D2, "B", 5.0)]),
            "f2": tidy(
                [(D1, "A", np.nan), (D1, "B", np.nan), (D2, "A", np.nan), (D2, "B", np.nan)]
            ),
        }
    )
    result = combine_apriori(matrix, method="equal_weight", min_factor_coverage=1)
    by_factor = _coverage_by_factor(result)
    # f2 scored zero rows: the ratio denominator is unobservable -> real None
    # (renders n/a), never a fabricated 0 (FP-4).
    assert by_factor["f2"].rows_scored == 0
    assert by_factor["f2"].coverage_ratio is None
    assert by_factor["f1"].coverage_ratio == pytest.approx(1.0)


def test_observed_zero_ratio_stays_a_real_zero() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy([(D1, "A", 1.0), (D1, "B", 2.0)]),
            "f2": tidy([(D2, "A", 3.0), (D2, "B", 4.0)]),
        }
    )
    result = combine_apriori(matrix, method="equal_weight")
    by_factor = _coverage_by_factor(result)
    # Disjoint coverage under all_factors: both factors scored rows (observed
    # denominator) but none entered the composite. That is a REAL 0.0 — the
    # honest observed value — distinct from the unobservable-None case.
    assert by_factor["f1"].rows_scored == 2
    assert by_factor["f1"].rows_in_composite == 0
    assert by_factor["f1"].coverage_ratio == 0.0
    assert by_factor["f1"].coverage_ratio is not None
    assert result.coverage.rows_full_coverage == 0


def test_coverage_rule_labels_follow_the_effective_requirement() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy([(D1, "A", 1.0), (D1, "B", 2.0)]),
            "f2": tidy([(D1, "A", 3.0), (D1, "B", 1.0)]),
        }
    )
    default = combine_apriori(matrix, method="equal_weight")
    assert default.coverage.coverage_rule == "all_factors"
    assert default.coverage.min_factor_coverage == 2
    explicit_all = combine_apriori(matrix, method="equal_weight", min_factor_coverage=2)
    assert explicit_all.coverage.coverage_rule == "all_factors"
    partial = combine_apriori(matrix, method="equal_weight", min_factor_coverage=1)
    assert partial.coverage.coverage_rule == "min_factor_coverage"
    assert partial.coverage.min_factor_coverage == 1


def _prescan_fixture() -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    calendar = list(pd.bdate_range("2026-01-05", periods=12))
    rows: list[tuple[pd.Timestamp, str, float]] = []
    # Signal date index 0: 5 finite names (ok at the default threshold 5).
    rows += [(calendar[0], f"I{n}", float(n + 1)) for n in range(5)]
    # Signal date index 3: rows exist but every score is NaN -> empty.
    rows += [(calendar[3], "I0", np.nan), (calendar[3], "I1", np.nan)]
    # Signal date index 6: 3 finite names -> thin.
    rows += [(calendar[6], f"I{n}", float(n + 1)) for n in range(3)]
    # Signal date index 9 (the excluded final partial slot): 6 finite names.
    rows += [(calendar[9], f"I{n}", float(n + 1)) for n in range(6)]
    # A non-signal date is present and ignored by classification.
    rows += [(calendar[1], "I0", 1.0)]
    return tidy(rows), calendar


def test_prescan_classifies_ok_empty_thin_on_the_shared_grid() -> None:
    composite, calendar = _prescan_fixture()
    scan = prescan_rebalance_coverage(composite, calendar, delay=1, holding=3)
    # Default D3 behavior: the trailing partial slot (index 9) is excluded,
    # exactly like the engine's break, and reported — never silent.
    assert scan.final_partial_excluded is True
    assert [entry.signal_index for entry in scan.entries] == [0, 3, 6]
    grid = rebalance_indices(calendar, delay=1, holding=3, start_signal_index=0)
    assert [entry.signal_index for entry in scan.entries] == grid[: len(scan.entries)]
    assert [entry.signal_date for entry in scan.entries] == [calendar[i] for i in (0, 3, 6)]
    assert [entry.status for entry in scan.entries] == ["ok", "empty", "thin"]
    assert [entry.finite_count for entry in scan.entries] == [5, 0, 3]
    assert scan.ok_count == 1
    assert scan.skipped_no_coverage_count == 1
    assert scan.skipped_thin_count == 1
    assert scan.skipped_no_coverage_dates == (calendar[3],)
    assert scan.skipped_thin_dates == (calendar[6],)
    # The exact engine code literals, imported from the engine module.
    assert scan.warning_codes == (REBALANCE_SKIPPED_NO_COVERAGE, REBALANCE_SKIPPED_THIN)
    assert scan.entries[1].skip_code == REBALANCE_SKIPPED_NO_COVERAGE
    assert scan.entries[2].skip_code == REBALANCE_SKIPPED_THIN
    assert scan.entries[0].skip_code is None
    assert scan.thin_threshold == 5


def test_prescan_includes_the_partial_final_period_when_requested() -> None:
    composite, calendar = _prescan_fixture()
    scan = prescan_rebalance_coverage(
        composite, calendar, delay=1, holding=3, include_partial_final_period=True
    )
    assert scan.final_partial_excluded is False
    assert [entry.signal_index for entry in scan.entries] == [0, 3, 6, 9]
    assert scan.entries[-1].status == "ok"
    assert scan.entries[-1].finite_count == 6


def test_prescan_thin_threshold_mirrors_the_engine_expression() -> None:
    composite, calendar = _prescan_fixture()
    # max(4, group_count): a small group_count floors at 4, a large one raises
    # the bar — the engine's own inline rule.
    low = prescan_rebalance_coverage(composite, calendar, delay=1, holding=3, group_count=2)
    assert low.thin_threshold == 4
    assert [entry.status for entry in low.entries] == ["ok", "empty", "thin"]
    high = prescan_rebalance_coverage(composite, calendar, delay=1, holding=3, group_count=6)
    assert high.thin_threshold == 6
    assert [entry.status for entry in high.entries] == ["thin", "empty", "thin"]
    assert high.ok_count == 0


def test_prescan_validates_inputs() -> None:
    composite, calendar = _prescan_fixture()
    with pytest.raises(ValueError, match="not on the provided calendar"):
        prescan_rebalance_coverage(composite, calendar[:4], delay=1, holding=3)
    with pytest.raises(ValueError, match="strictly increasing"):
        prescan_rebalance_coverage(composite, list(reversed(calendar)), delay=1, holding=3)
    with pytest.raises(ValueError, match="delay"):
        prescan_rebalance_coverage(composite, calendar, delay=0, holding=3)
    with pytest.raises(ValueError, match="holding"):
        prescan_rebalance_coverage(composite, calendar, delay=1, holding=0)
    with pytest.raises(ValueError, match="group_count"):
        prescan_rebalance_coverage(composite, calendar, delay=1, holding=3, group_count=1)
    with pytest.raises(ValueError, match="start_signal_index"):
        prescan_rebalance_coverage(composite, calendar, delay=1, holding=3, start_signal_index=-1)
    with pytest.raises(ValueError, match="missing columns"):
        prescan_rebalance_coverage(pd.DataFrame({"trade_date": []}), calendar, delay=1, holding=3)

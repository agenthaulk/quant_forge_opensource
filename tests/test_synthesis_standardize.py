"""§4.2 standardizers: per-date only, zscore no-dispersion rule, rank ties (RB-3).

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.2, §13
test_standardize): zscore and rank are cross-sectional PER trade_date and never
pool across dates; a zscore cross-section with no dispersion contributes 0 that
date for observed names and the date is marked degenerate for that factor
(never NaN-propagated); rank uses the deterministic ``method='first'`` tie
policy over an instrument-sorted frame and maps onto ``[-1, 1]`` via
``2*r - 1`` so tied inputs get a reproducible order.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from quant_forge.synthesis.methods import STANDARDIZATIONS
from quant_forge.synthesis.service import (
    _STANDARDIZERS,
    build_score_matrix,
    standardize_matrix,
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


def test_zscore_is_per_date_with_mean_zero_and_unit_std() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy(
                [
                    (D1, "A", 1.0),
                    (D1, "B", 2.0),
                    (D1, "C", 4.0),
                    (D1, "D", 9.0),
                    (D2, "A", 100.0),
                    (D2, "B", 50.0),
                    (D2, "C", 10.0),
                ]
            )
        }
    )
    outcome = standardize_matrix(matrix, standardization="zscore")
    for date in (D1, D2):
        cross_section = outcome.matrix.xs(date, level="trade_date")["f1"]
        assert abs(float(cross_section.mean())) < 1e-12
        assert abs(float(cross_section.std(ddof=1)) - 1.0) < 1e-12
    assert outcome.degenerate_dates_by_factor["f1"] == ()


def test_zscore_never_pools_across_dates() -> None:
    base = {"f1": tidy([(D1, "A", 1.0), (D1, "B", 2.0), (D1, "C", 3.0)])}
    with_small_second_date = dict(base)
    with_small_second_date["f1"] = pd.concat(
        [base["f1"], tidy([(D2, "A", 1.0), (D2, "B", 2.0), (D2, "C", 3.0)])],
        ignore_index=True,
    )
    with_huge_second_date = dict(base)
    with_huge_second_date["f1"] = pd.concat(
        [base["f1"], tidy([(D2, "A", 1e6), (D2, "B", 2e6), (D2, "C", 3e6)])],
        ignore_index=True,
    )
    small = standardize_matrix(
        build_score_matrix(with_small_second_date), standardization="zscore"
    ).matrix.xs(D1, level="trade_date")
    huge = standardize_matrix(
        build_score_matrix(with_huge_second_date), standardization="zscore"
    ).matrix.xs(D1, level="trade_date")
    # D1's cross-section is standardized identically no matter what D2 holds.
    pd.testing.assert_frame_equal(small, huge)


def test_zscore_no_dispersion_date_contributes_zero_and_is_marked_degenerate() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy(
                [
                    (D1, "A", 3.0),
                    (D1, "B", 3.0),
                    (D1, "C", 3.0),
                    (D1, "D", np.nan),
                    (D2, "A", 1.0),
                    (D2, "B", 2.0),
                    (D2, "C", 3.0),
                ]
            ),
            "f2": tidy(
                [
                    (D1, "A", 1.0),
                    (D1, "B", 2.0),
                    (D1, "C", 4.0),
                    (D2, "A", 5.0),
                    (D2, "B", 6.0),
                    (D2, "C", 9.0),
                ]
            ),
        }
    )
    outcome = standardize_matrix(matrix, standardization="zscore")
    d1_f1 = outcome.matrix.xs(D1, level="trade_date")["f1"]
    # Observed names contribute exactly 0 that date; the unobserved name stays
    # missing (never a fabricated value), and the date is degenerate for f1 only.
    assert list(d1_f1[["A", "B", "C"]]) == [0.0, 0.0, 0.0]
    assert pd.isna(d1_f1["D"])
    assert outcome.degenerate_dates_by_factor["f1"] == (D1,)
    assert outcome.degenerate_dates_by_factor["f2"] == ()
    # The healthy factor on the same date is standardized normally.
    d1_f2 = outcome.matrix.xs(D1, level="trade_date")["f2"]
    assert abs(float(d1_f2.mean())) < 1e-12


def test_zscore_single_observation_date_is_degenerate_zero() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy([(D1, "A", 5.0), (D2, "A", 1.0), (D2, "B", 2.0)]),
            "f2": tidy([(D1, "A", 1.0), (D1, "B", 2.0), (D2, "A", 3.0), (D2, "B", 4.0)]),
        }
    )
    outcome = standardize_matrix(matrix, standardization="zscore")
    assert float(outcome.matrix.loc[(D1, "A"), "f1"]) == 0.0
    assert outcome.degenerate_dates_by_factor["f1"] == (D1,)


def test_zscore_all_missing_date_stays_missing_and_unmarked() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy([(D1, "A", np.nan), (D1, "B", np.nan), (D2, "A", 1.0), (D2, "B", 3.0)]),
            "f2": tidy([(D1, "A", 1.0), (D1, "B", 2.0), (D2, "A", 3.0), (D2, "B", 4.0)]),
        }
    )
    outcome = standardize_matrix(matrix, standardization="zscore")
    d1_f1 = outcome.matrix.xs(D1, level="trade_date")["f1"]
    assert d1_f1.isna().all()
    # Absence is a coverage fact, not a dispersion fact: no degenerate mark.
    assert outcome.degenerate_dates_by_factor["f1"] == ()


def test_rank_maps_to_unit_interval_with_exact_percentiles() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy(
                [
                    (D1, "A", 10.0),
                    (D1, "B", 20.0),
                    (D1, "C", 30.0),
                    (D1, "D", 40.0),
                    (D2, "A", 7.0),
                    (D2, "B", 9.0),
                    (D2, "C", np.nan),
                ]
            )
        }
    )
    outcome = standardize_matrix(matrix, standardization="rank")
    d1 = outcome.matrix.xs(D1, level="trade_date")["f1"]
    assert np.allclose(list(d1[["A", "B", "C", "D"]]), [-0.5, 0.0, 0.5, 1.0])
    d2 = outcome.matrix.xs(D2, level="trade_date")["f1"]
    # NaN stays missing and is excluded from the percentile denominator.
    assert np.allclose(list(d2[["A", "B"]]), [0.0, 1.0])
    assert pd.isna(d2["C"])
    finite = outcome.matrix["f1"].dropna()
    assert bool(((finite >= -1.0) & (finite <= 1.0)).all())
    assert outcome.degenerate_dates_by_factor["f1"] == ()


def test_rank_ties_resolve_by_instrument_order_and_are_reproducible() -> None:
    rows = [(D1, "A", 5.0), (D1, "B", 5.0), (D1, "C", 1.0)]
    outcome = standardize_matrix(
        build_score_matrix({"f1": tidy(rows)}), standardization="rank"
    )
    d1 = outcome.matrix.xs(D1, level="trade_date")["f1"]
    # method='first' over the instrument-sorted frame: the tied pair resolves
    # in instrument order (A before B), deterministically.
    assert np.allclose(
        list(d1[["A", "B", "C"]]), [2.0 * 2 / 3 - 1.0, 1.0, 2.0 * 1 / 3 - 1.0]
    )
    shuffled = tidy(rows).sample(frac=1.0, random_state=7).reset_index(drop=True)
    reshuffled_outcome = standardize_matrix(
        build_score_matrix({"f1": shuffled}), standardization="rank"
    )
    pd.testing.assert_frame_equal(outcome.matrix, reshuffled_outcome.matrix)


def test_standardizer_names_match_the_shipped_catalog() -> None:
    assert set(_STANDARDIZERS) == {spec.name for spec in STANDARDIZATIONS}
    with pytest.raises(ValueError, match="unknown standardization"):
        standardize_matrix(
            build_score_matrix({"f1": tidy([(D1, "A", 1.0), (D1, "B", 2.0)])}),
            standardization="minmax",
        )


def test_build_score_matrix_validates_inputs() -> None:
    with pytest.raises(ValueError, match="at least one member"):
        build_score_matrix({})
    with pytest.raises(ValueError, match="missing columns"):
        build_score_matrix({"f1": pd.DataFrame({"trade_date": [D1], "value": [1.0]})})
    duplicated = tidy([(D1, "A", 1.0), (D1, "A", 2.0)])
    with pytest.raises(ValueError, match="duplicate"):
        build_score_matrix({"f1": duplicated})


def test_build_score_matrix_masks_non_finite_scores() -> None:
    matrix = build_score_matrix(
        {"f1": tidy([(D1, "A", np.inf), (D1, "B", -np.inf), (D1, "C", 2.0)])}
    )
    assert pd.isna(matrix.loc[(D1, "A"), "f1"])
    assert pd.isna(matrix.loc[(D1, "B"), "f1"])
    assert float(matrix.loc[(D1, "C"), "f1"]) == 2.0


def test_rank_all_tied_cross_section_contributes_zero_and_flags_degenerate() -> None:
    # Codex A-2: an all-tied cross-section carries no ordering information;
    # method='first' alone would fabricate an instrument-ordered ladder the
    # engine trades on tie-break noise. The convention now mirrors zscore's
    # no-dispersion rule: observed names contribute 0.0 and the date is
    # recorded degenerate for the factor (composite-level RB-9 then skips the
    # date when every member degenerates).
    matrix = build_score_matrix(
        {
            "tied": tidy([(D1, "A", 1.0), (D1, "B", 1.0), (D1, "C", 1.0)]),
            "live": tidy([(D1, "A", 1.0), (D1, "B", 2.0), (D1, "C", 3.0)]),
        }
    )
    outcome = standardize_matrix(matrix, standardization="rank")
    tied_col = outcome.matrix["tied"]
    assert set(tied_col.dropna().unique()) == {0.0}
    assert outcome.degenerate_dates_by_factor["tied"] == (D1,)
    # The dispersive member keeps its real ordering (no over-blanking).
    live_col = outcome.matrix["live"]
    assert float(live_col.loc[(D1, "C")]) > float(live_col.loc[(D1, "A")])
    assert outcome.degenerate_dates_by_factor["live"] == ()

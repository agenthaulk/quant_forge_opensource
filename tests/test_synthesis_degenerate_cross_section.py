"""RB-9: all-NaN and all-equal composite dates converge on one flagged skip.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.2 RB-9, §13
test_degenerate_cross_section): after combine, a date whose composite
cross-section is all-NaN OR zero-variance (all finite values equal, including
a single-name cross-section) becomes all-NaN — so the engine drops the date
instead of trading an arbitrary tie-noise long/short split — and the date is
counted under ``DEGENERATE_CROSS_SECTION``. Both degenerate kinds land on the
same explicit outcome, never two silent divergent ones.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from quant_forge.synthesis.service import (
    DEGENERATE_CROSS_SECTION,
    build_apriori_composite,
    build_score_matrix,
    combine_apriori,
)

D1 = pd.Timestamp("2026-01-05")
D2 = pd.Timestamp("2026-01-06")
D3 = pd.Timestamp("2026-01-07")


def tidy(rows: list[tuple[pd.Timestamp, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": trade_date, "instrument": instrument, "score": score}
            for trade_date, instrument, score in rows
        ]
    )


def _date_scores(frame: pd.DataFrame, date: pd.Timestamp) -> pd.Series:
    return frame[frame["trade_date"] == date]["score"]


def test_all_nan_and_all_equal_dates_converge_on_the_same_flagged_skip() -> None:
    matrix = build_score_matrix(
        {
            # D1 healthy; D2 has disjoint member coverage (all_factors masks
            # every row -> all-NaN); D3 is an exact-tie cross-section
            # (zero variance after combining).
            "f1": tidy(
                [
                    (D1, "A", 1.0),
                    (D1, "B", 5.0),
                    (D2, "A", 1.0),
                    (D2, "B", np.nan),
                    (D3, "A", 2.0),
                    (D3, "B", 2.0),
                ]
            ),
            "f2": tidy(
                [
                    (D1, "A", 2.0),
                    (D1, "B", 1.0),
                    (D2, "A", np.nan),
                    (D2, "B", 2.0),
                    (D3, "A", 3.0),
                    (D3, "B", 3.0),
                ]
            ),
        }
    )
    result = combine_apriori(matrix, method="equal_weight")

    # Both degenerate kinds: the whole date is NaN in the composite output.
    assert _date_scores(result.composite, D2).isna().all()
    assert _date_scores(result.composite, D3).isna().all()
    assert result.degenerate_dates == (D2, D3)
    # One explicit warning code, carried once; counts live in the dates tuple.
    assert result.warning_codes == (DEGENERATE_CROSS_SECTION,)
    # The healthy date is untouched.
    assert list(_date_scores(result.composite, D1)) == [3.0, 6.0]


def test_single_finite_name_cross_section_is_degenerate() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy([(D1, "A", 1.0), (D1, "B", 4.0), (D2, "A", 1.0), (D2, "B", np.nan)]),
            "f2": tidy([(D1, "A", 2.0), (D1, "B", 1.0), (D2, "A", 2.0), (D2, "B", np.nan)]),
        }
    )
    result = combine_apriori(matrix, method="equal_weight")
    # One tradeable name has zero cross-sectional variance by construction:
    # the date is erased rather than split into an arbitrary long/short pair.
    assert _date_scores(result.composite, D2).isna().all()
    assert result.degenerate_dates == (D2,)


def test_zscore_no_dispersion_members_chain_into_composite_degeneracy() -> None:
    # §4.2 -> RB-9 chain: both members are all-equal on D2, so zscore maps
    # them to 0-contributions, the combined cross-section is zero-variance,
    # and the date converges on the same flagged skip. Per-factor degenerate
    # marks and the composite-level date are both reported.
    member_scores = {
        "f1": tidy([(D1, "A", 1.0), (D1, "B", 4.0), (D2, "A", 7.0), (D2, "B", 7.0)]),
        "f2": tidy([(D1, "A", 3.0), (D1, "B", 1.0), (D2, "A", 9.0), (D2, "B", 9.0)]),
    }
    result = build_apriori_composite(
        member_scores,
        directions={"f1": 1, "f2": -1},
        standardization="zscore",
        method="equal_weight",
    )
    assert _date_scores(result.composite, D2).isna().all()
    assert result.degenerate_dates == (D2,)
    assert result.warning_codes == (DEGENERATE_CROSS_SECTION,)
    assert result.degenerate_dates_by_factor["f1"] == (D2,)
    assert result.degenerate_dates_by_factor["f2"] == (D2,)
    assert result.standardization == "zscore"
    healthy = _date_scores(result.composite, D1)
    assert healthy.notna().all()
    assert healthy.nunique() == 2


def test_no_degenerate_dates_means_no_warning_codes() -> None:
    matrix = build_score_matrix(
        {
            "f1": tidy([(D1, "A", 1.0), (D1, "B", 5.0)]),
            "f2": tidy([(D1, "A", 2.0), (D1, "B", 1.0)]),
        }
    )
    result = combine_apriori(matrix, method="equal_weight")
    assert result.degenerate_dates == ()
    assert result.warning_codes == ()

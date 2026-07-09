"""§4.3 directions: explicit ±1 applied AFTER standardization, -1 exact negation.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.3, §13
test_direction): the declared direction is +1 or -1 per member, locked at
request time; -1 exactly negates the standardized score and +1 is the
identity. Directions are never defaulted, inferred, or re-derived from data,
and the standardize-then-negate order is observable under rank ties (negating
inputs before ranking with the 'first' tie policy yields a different frame).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from quant_forge.synthesis.service import (
    apply_directions,
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


def _standardized() -> pd.DataFrame:
    matrix = build_score_matrix(
        {
            "f1": tidy(
                [(D1, "A", 1.0), (D1, "B", 4.0), (D1, "C", np.nan), (D2, "A", 2.0), (D2, "B", 9.0)]
            ),
            "f2": tidy(
                [(D1, "A", 3.0), (D1, "B", 1.0), (D1, "C", 2.0), (D2, "A", 5.0), (D2, "B", 4.0)]
            ),
        }
    )
    return standardize_matrix(matrix, standardization="zscore").matrix


def test_minus_one_exactly_negates_and_plus_one_is_identity() -> None:
    standardized = _standardized()
    directed = apply_directions(standardized, {"f1": -1, "f2": 1})
    assert np.array_equal(
        directed["f1"].to_numpy(), (-standardized["f1"]).to_numpy(), equal_nan=True
    )
    assert np.array_equal(
        directed["f2"].to_numpy(), standardized["f2"].to_numpy(), equal_nan=True
    )
    # Missing values stay missing under either direction.
    assert pd.isna(directed.loc[(D1, "C"), "f1"])


def test_direction_is_required_for_every_factor_and_only_known_factors() -> None:
    standardized = _standardized()
    with pytest.raises(ValueError, match="missing"):
        apply_directions(standardized, {"f1": 1})
    with pytest.raises(ValueError, match="unknown"):
        apply_directions(standardized, {"f1": 1, "f2": -1, "f3": 1})


@pytest.mark.parametrize("bad_direction", [0, 2, -2, True, False, 1.0, -1.0, "1", None])
def test_direction_values_are_validated(bad_direction: object) -> None:
    standardized = _standardized()
    with pytest.raises(ValueError, match="direction must be"):
        apply_directions(standardized, {"f1": bad_direction, "f2": 1})


def test_direction_applies_after_standardization_not_before() -> None:
    # Under the deterministic 'first' rank tie policy, negate-after-rank and
    # rank-of-negated-inputs are observably different frames on ties. §4.3
    # pins negate-after: the declared -1 exactly negates the standardized
    # score rather than re-ranking inverted inputs.
    rows = [(D1, "A", 5.0), (D1, "B", 5.0), (D1, "C", 1.0)]
    matrix = build_score_matrix({"f1": tidy(rows)})
    ranked = standardize_matrix(matrix, standardization="rank").matrix
    directed = apply_directions(ranked, {"f1": -1})
    assert np.array_equal(
        directed["f1"].to_numpy(), (-ranked["f1"]).to_numpy(), equal_nan=True
    )

    negated_inputs = build_score_matrix(
        {"f1": tidy([(date, instrument, -score) for date, instrument, score in rows])}
    )
    rank_of_negated = standardize_matrix(negated_inputs, standardization="rank").matrix
    assert not np.array_equal(
        directed["f1"].to_numpy(), rank_of_negated["f1"].to_numpy(), equal_nan=True
    )

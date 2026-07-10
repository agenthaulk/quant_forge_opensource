"""§4.4 a-priori combine: equal_weight == uniform, raw weights, exclusion rule.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.4, §13
test_combine_apriori): ``equal_weight`` equals ``weighted`` with a uniform raw
vector; ``weighted`` uses the caller's raw declared weights and echoes them
raw in ``weights_effective`` (never normalized for display); a missing factor
at ``(date, instrument)`` is excluded from that name's sum per the coverage
rule — under the default ``all_factors`` rule the whole row is masked, under
``min_factor_coverage=k`` the name sums only its available members.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from quant_forge.synthesis.methods import SYNTHESIS_METHODS
from quant_forge.synthesis.service import (
    APRIORI_METHODS,
    build_score_matrix,
    combine_apriori,
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


def _matrix(with_gap: bool = False) -> pd.DataFrame:
    # Three instruments so masking one row under all_factors still leaves a
    # non-degenerate (>=2 distinct finite values) cross-section on D1.
    f2_rows = [
        (D1, "A", 2.0),
        (D1, "B", np.nan if with_gap else 10.0),
        (D1, "C", 1.0),
        (D2, "A", 1.0),
        (D2, "B", 7.0),
    ]
    return build_score_matrix(
        {
            "f1": tidy(
                [(D1, "A", 1.0), (D1, "B", 2.0), (D1, "C", 4.0), (D2, "A", 3.0), (D2, "B", 5.0)]
            ),
            "f2": tidy(f2_rows),
        }
    )


def _score_at(result_frame: pd.DataFrame, date: pd.Timestamp, instrument: str) -> float:
    row = result_frame[
        (result_frame["trade_date"] == date) & (result_frame["instrument"] == instrument)
    ]
    assert len(row) == 1
    return row["score"].iloc[0]


def test_equal_weight_equals_weighted_with_uniform_raw_ones() -> None:
    matrix = _matrix()
    equal = combine_apriori(matrix, method="equal_weight")
    uniform = combine_apriori(matrix, method="weighted", weights={"f1": 1.0, "f2": 1.0})
    pd.testing.assert_frame_equal(equal.composite, uniform.composite)
    assert equal.weights_effective == {"f1": 1.0, "f2": 1.0}
    # Full-coverage row: the literal §4.4 sum of the member scores.
    assert _score_at(equal.composite, D1, "A") == 3.0
    assert _score_at(equal.composite, D1, "B") == 12.0
    assert equal.method == "equal_weight"
    assert equal.warning_codes == ()


def test_weighted_uses_raw_declared_weights_and_echoes_them_unnormalized() -> None:
    result = combine_apriori(_matrix(), method="weighted", weights={"f1": 2, "f2": 3})
    assert _score_at(result.composite, D1, "A") == 2.0 * 1.0 + 3.0 * 2.0
    assert _score_at(result.composite, D1, "B") == 2.0 * 2.0 + 3.0 * 10.0
    # Raw echo: exactly the declared magnitudes, never rescaled to sum to 1.
    assert result.weights_effective == {"f1": 2.0, "f2": 3.0}
    assert sum(result.weights_effective.values()) != 1.0


def test_all_factors_rule_masks_partial_rows_instead_of_summing_them() -> None:
    result = combine_apriori(_matrix(with_gap=True), method="weighted", weights={"f1": 2, "f2": 3})
    # (D1, B) is missing f2: under the default all_factors rule the row is
    # excluded entirely (a NaN row, still visible in the tidy frame) — never a
    # partial sum, never a fabricated value.
    assert pd.isna(_score_at(result.composite, D1, "B"))
    assert result.coverage.coverage_rule == "all_factors"
    assert result.coverage.min_factor_coverage == 2


def test_min_factor_coverage_sums_only_available_members() -> None:
    result = combine_apriori(
        _matrix(with_gap=True),
        method="weighted",
        weights={"f1": 2, "f2": 3},
        min_factor_coverage=1,
    )
    # The missing member is excluded from the name's sum (fewer terms, the
    # pinned §4.4 formula): only w_f1 * t_f1 remains for (D1, B).
    assert _score_at(result.composite, D1, "B") == 2.0 * 2.0
    assert _score_at(result.composite, D1, "A") == 2.0 * 1.0 + 3.0 * 2.0
    assert result.coverage.coverage_rule == "min_factor_coverage"
    assert result.coverage.min_factor_coverage == 1


def test_composite_frame_is_tidy_sorted_engine_shape() -> None:
    result = combine_apriori(_matrix(with_gap=True), method="equal_weight")
    frame = result.composite
    assert list(frame.columns) == ["trade_date", "instrument", "score"]
    keys = list(zip(frame["trade_date"], frame["instrument"]))
    assert keys == sorted(keys)
    # NaN rows are preserved for downstream coverage/pre-scan visibility.
    assert int(frame["score"].isna().sum()) == 1


def test_method_and_weight_validation() -> None:
    matrix = _matrix()
    with pytest.raises(ValueError, match="unknown a-priori method"):
        combine_apriori(matrix, method="ic_weighted")
    with pytest.raises(ValueError, match="does not accept"):
        combine_apriori(matrix, method="equal_weight", weights={"f1": 1.0, "f2": 1.0})
    with pytest.raises(ValueError, match="requires a weights mapping"):
        combine_apriori(matrix, method="weighted")
    with pytest.raises(ValueError, match="missing for factors"):
        combine_apriori(matrix, method="weighted", weights={"f1": 1.0})
    with pytest.raises(ValueError, match="unknown factors"):
        combine_apriori(matrix, method="weighted", weights={"f1": 1.0, "f2": 1.0, "f3": 1.0})
    with pytest.raises(ValueError, match="must be finite"):
        combine_apriori(matrix, method="weighted", weights={"f1": float("nan"), "f2": 1.0})
    with pytest.raises(ValueError, match="must be a number"):
        combine_apriori(matrix, method="weighted", weights={"f1": True, "f2": 1.0})
    with pytest.raises(ValueError, match="not all be zero"):
        combine_apriori(matrix, method="weighted", weights={"f1": 0.0, "f2": 0.0})


def test_min_factor_coverage_bounds_and_member_count() -> None:
    matrix = _matrix()
    with pytest.raises(ValueError, match="between 1 and 2"):
        combine_apriori(matrix, method="equal_weight", min_factor_coverage=0)
    with pytest.raises(ValueError, match="between 1 and 2"):
        combine_apriori(matrix, method="equal_weight", min_factor_coverage=3)
    with pytest.raises(ValueError, match="must be an integer"):
        combine_apriori(matrix, method="equal_weight", min_factor_coverage=True)
    single = build_score_matrix({"f1": tidy([(D1, "A", 1.0), (D1, "B", 2.0)])})
    with pytest.raises(ValueError, match="at least 2 member factors"):
        combine_apriori(single, method="equal_weight")


def test_apriori_methods_agree_with_the_shipped_catalog() -> None:
    # The a-priori set implemented here is exactly the catalog's non-fitted,
    # currently-available methods; fitted methods stay reserved for a later
    # phase and are rejected by combine_apriori above.
    catalog_apriori = {spec.name for spec in SYNTHESIS_METHODS if not spec.is_fitted}
    assert set(APRIORI_METHODS) == catalog_apriori
    assert all(spec.available for spec in SYNTHESIS_METHODS if not spec.is_fitted)

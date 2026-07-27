"""Gold-vector coverage for the conditional grammar: comparisons + where / and_ / or_ / not_.

The grammar batch adds one AST node family and four registry operators:

* infix comparisons (``> < >= <= == !=``) — grammar-level like arithmetic,
  exactly one link per node (chained ``a < b < c`` is rejected), producing a
  0/1 mask with NaN propagation (a comparison against a missing value is
  unknown, never silently False);
* ``where(cond, a, b)`` — elementwise selection over truthiness ``cond > 0``;
  a NaN condition yields NaN and a NaN in the selected arm passes through;
* ``and_ / or_`` — Kleene strong logic: a determinate arm decides alone, every
  remaining NaN case stays NaN;
* ``not_`` — negation over the same truthiness; NaN stays NaN.

All four are pointwise (PIT-safe by construction: row t reads only row t of its
operands); the composition ``ts_sum(mask, n) == n`` is the persistence idiom the
grammar is expected to unlock without a dedicated primitive.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.factor_engine.formula_parser import (
    SUPPORTED_OPERATORS,
    FormulaParseError,
    formula_lookback_rows,
    inspect_formula,
    parse_formula_node,
)
from quant_forge.operator_registry import (
    load_default_operator_registry,
    resolve_formula_operators,
)

NEW_OPERATORS = ("and_", "not_", "or_", "where")


def _scores(panel: pd.DataFrame, formula: str) -> np.ndarray:
    return execute_factor_formula(panel, formula)["score"].to_numpy(dtype=float)


def _single_instrument(close: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2024-01-01", periods=len(close)),
            "instrument": ["A"] * len(close),
            "close": close,
        }
    )


def _two_columns(close: list[float], volume: list[float]) -> pd.DataFrame:
    frame = _single_instrument(close)
    frame["volume"] = volume
    return frame


# ---------------------------------------------------------------------------
# Registry / executor reconciliation
# ---------------------------------------------------------------------------


def test_new_operators_reconcile_registry_and_executor() -> None:
    registry = load_default_operator_registry()  # raises if the two sides drift
    for name in NEW_OPERATORS:
        assert name in SUPPORTED_OPERATORS
        spec = registry.operators[name]
        assert spec.execution_status == "implemented"
        assert spec.audit_status == "core_reviewed"
        assert spec.pit_safety == {"uses_future_data": False, "alignment": "t"}


def test_conditional_formulas_are_executable_through_the_resolver() -> None:
    for formula in (
        "where(close > delay(close, 5), 1, -1)",
        "and_(close > delay(close, 1), volume > delay(volume, 1))",
        "or_(close > delay(close, 5), volume > delay(volume, 5))",
        "not_(close > delay(close, 1))",
        "ts_sum(delta(close, 1) > 0, 3) == 3",
        "where(or_(close - volume > 0, ts_sum(delta(close, 1) > 0, 3) == 3), 1, where(close - volume < 0, -1, 0))",
    ):
        result = resolve_formula_operators(formula)
        assert result.executable is True, (formula, result.blocking_errors)


# ---------------------------------------------------------------------------
# Comparison gold vectors (0/1 mask + NaN propagation)
# ---------------------------------------------------------------------------


def test_comparison_gold_vector_each_op() -> None:
    panel = _two_columns([1.0, 2.0, 3.0, 2.0], [2.0, 2.0, 2.0, 2.0])
    np.testing.assert_allclose(_scores(panel, "close > volume"), [0.0, 0.0, 1.0, 0.0])
    np.testing.assert_allclose(_scores(panel, "close < volume"), [1.0, 0.0, 0.0, 0.0])
    np.testing.assert_allclose(_scores(panel, "close >= volume"), [0.0, 1.0, 1.0, 1.0])
    np.testing.assert_allclose(_scores(panel, "close <= volume"), [1.0, 1.0, 0.0, 1.0])
    np.testing.assert_allclose(_scores(panel, "close == volume"), [0.0, 1.0, 0.0, 1.0])
    np.testing.assert_allclose(_scores(panel, "close != volume"), [1.0, 0.0, 1.0, 0.0])


def test_comparison_propagates_nan_instead_of_false() -> None:
    panel = _two_columns([1.0, np.nan, 3.0], [2.0, 2.0, np.nan])
    np.testing.assert_allclose(_scores(panel, "close > volume"), [0.0, np.nan, np.nan], equal_nan=True)
    np.testing.assert_allclose(_scores(panel, "close == volume"), [0.0, np.nan, np.nan], equal_nan=True)


def test_comparison_against_scalar_constant() -> None:
    panel = _single_instrument([-1.0, 0.0, 2.0])
    np.testing.assert_allclose(_scores(panel, "close > 0"), [0.0, 0.0, 1.0])
    np.testing.assert_allclose(_scores(panel, "close - delay(close, 1) > 0"), [np.nan, 1.0, 1.0], equal_nan=True)


# ---------------------------------------------------------------------------
# where gold vectors
# ---------------------------------------------------------------------------


def test_where_selects_on_positive_truthiness() -> None:
    # sign(...) style conditions: only the positive arm is true; zero and
    # negative both select the second arm.
    panel = _two_columns([2.0, -3.0, 0.0, 5.0], [10.0, 20.0, 30.0, 40.0])
    np.testing.assert_allclose(_scores(panel, "where(close, volume, -1)"), [10.0, -1.0, -1.0, 40.0])
    np.testing.assert_allclose(_scores(panel, "where(close > 0, 1, -1)"), [1.0, -1.0, -1.0, 1.0])


def test_where_nan_condition_yields_nan() -> None:
    panel = _two_columns([1.0, np.nan, -1.0], [7.0, 7.0, 7.0])
    np.testing.assert_allclose(_scores(panel, "where(close, volume, 0)"), [7.0, np.nan, 0.0], equal_nan=True)


def test_where_nan_in_selected_arm_passes_through() -> None:
    panel = _two_columns([1.0, 1.0, -1.0], [np.nan, 5.0, np.nan])
    # Selected arm NaN -> NaN; unselected arm NaN leaves the result untouched.
    np.testing.assert_allclose(_scores(panel, "where(close, volume, 0)"), [np.nan, 5.0, 0.0], equal_nan=True)
    np.testing.assert_allclose(_scores(panel, "where(close * -1, volume, 0)"), [0.0, 0.0, np.nan], equal_nan=True)


# ---------------------------------------------------------------------------
# and_ / or_ / not_ Kleene tables
# ---------------------------------------------------------------------------

TRUE, FALSE, UNKNOWN = 1.0, 0.0, np.nan


def _kleene_panel() -> pd.DataFrame:
    # Nine rows enumerate the full {true, false, unknown} x {true, false,
    # unknown} table in fixed order.
    left = [TRUE, TRUE, TRUE, FALSE, FALSE, FALSE, UNKNOWN, UNKNOWN, UNKNOWN]
    right = [TRUE, FALSE, UNKNOWN, TRUE, FALSE, UNKNOWN, TRUE, FALSE, UNKNOWN]
    return _two_columns(left, right)


def test_and_full_kleene_table() -> None:
    np.testing.assert_allclose(
        _scores(_kleene_panel(), "and_(close, volume)"),
        [1.0, 0.0, np.nan, 0.0, 0.0, 0.0, np.nan, 0.0, np.nan],
        equal_nan=True,
    )


def test_or_full_kleene_table() -> None:
    np.testing.assert_allclose(
        _scores(_kleene_panel(), "or_(close, volume)"),
        [1.0, 1.0, 1.0, 1.0, 0.0, np.nan, 1.0, np.nan, np.nan],
        equal_nan=True,
    )


def test_not_gold_vector() -> None:
    panel = _single_instrument([2.0, 0.0, -1.0, np.nan])
    np.testing.assert_allclose(_scores(panel, "not_(close)"), [0.0, 1.0, 1.0, np.nan], equal_nan=True)
    np.testing.assert_allclose(_scores(panel, "not_(not_(close))"), [1.0, 0.0, 0.0, np.nan], equal_nan=True)


# ---------------------------------------------------------------------------
# Persistence idiom + composed signal shape
# ---------------------------------------------------------------------------


def test_persistence_idiom_ts_sum_of_mask() -> None:
    # Rises on bars 2,3,4 only: the three-in-a-row condition first holds at bar
    # 4. delta(close, 1) is NaN on bar 0, so the first two windows carry a NaN
    # (min_periods) and the idiom starts honest, not optimistically False.
    panel = _single_instrument([1.0, 1.0, 2.0, 3.0, 4.0, 3.0])
    np.testing.assert_allclose(
        _scores(panel, "ts_sum(delta(close, 1) > 0, 3) == 3"),
        [np.nan, np.nan, np.nan, 0.0, 1.0, 0.0],
        equal_nan=True,
    )


def test_composed_two_sided_signal_executes() -> None:
    # The two-branch selection shape (long / short / flat) parses and executes
    # end to end; row 0 is NaN because the five-row momentum arms are not yet
    # formed and the condition itself is NaN (honest warmup).
    panel = _two_columns(
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 5.0, 4.0],
        [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    )
    scores = _scores(
        panel,
        "where(ts_sum(delta(close, 1) > 0, 3) == 3, 1, where(ts_sum(delta(close, 1) < 0, 3) == 3, -1, 0))",
    )
    np.testing.assert_allclose(
        scores, [np.nan, np.nan, np.nan, 1.0, 1.0, 1.0, 0.0, 0.0], equal_nan=True
    )


# ---------------------------------------------------------------------------
# Point-in-time contract (pointwise nodes)
# ---------------------------------------------------------------------------


def test_conditional_grammar_is_pointwise_pit_safe() -> None:
    # Perturbing rows strictly after t leaves every output at <= t unchanged.
    base = _two_columns([1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 4.0, 3.0, 2.0, 1.0])
    perturbed = base.copy()
    perturbed.loc[perturbed.index[-1], ["close", "volume"]] = [100.0, -100.0]
    for formula in (
        "where(close > volume, close, volume)",
        "and_(close > 2, volume > 2)",
        "or_(close > 4, volume > 4)",
        "not_(close > volume)",
        "ts_sum(delta(close, 1) > 0, 2) == 2",
    ):
        full = _scores(base, formula)
        changed = _scores(perturbed, formula)
        np.testing.assert_allclose(full[:-1], changed[:-1], equal_nan=True, err_msg=formula)


def test_rolling_mask_alignment_with_null_instrument_label() -> None:
    # A groupby-rolling operand omits rows whose instrument label is null; the
    # conditional nodes must realign those rows to NaN by index instead of
    # combining shortened arrays positionally (which would shift or broadcast).
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-01", "2024-01-01"]),
            "instrument": ["A", None],
            "close": [10.0, 20.0],
        }
    )
    np.testing.assert_allclose(_scores(panel, "ts_sum(close, 1) - close"), [0.0, np.nan], equal_nan=True)
    np.testing.assert_allclose(_scores(panel, "ts_sum(close, 1) > close"), [0.0, np.nan], equal_nan=True)
    np.testing.assert_allclose(
        _scores(panel, "where(ts_sum(close, 1) > 5, 1, -1)"), [1.0, np.nan], equal_nan=True
    )
    np.testing.assert_allclose(
        _scores(panel, "and_(ts_sum(close, 1) > 5, close > 5)"), [1.0, np.nan], equal_nan=True
    )
    # not_ is pinned separately: its pre-fix symptom was a length error rather
    # than a silent broadcast, so it exercises the single-operand path.
    np.testing.assert_allclose(_scores(panel, "not_(ts_sum(close, 1))"), [0.0, np.nan], equal_nan=True)
    np.testing.assert_allclose(
        _scores(panel, "or_(ts_sum(close, 1) > 5, close > 50)"), [1.0, np.nan], equal_nan=True
    )


# ---------------------------------------------------------------------------
# Grammar bounds and rejections
# ---------------------------------------------------------------------------


def test_chained_comparison_is_rejected() -> None:
    with pytest.raises(FormulaParseError, match="chained comparisons"):
        parse_formula_node("1 < close < 3")


def test_python_boolean_keywords_are_rejected() -> None:
    for formula in ("close > 1 and volume > 1", "close > 1 or volume > 1", "not close"):
        with pytest.raises(FormulaParseError):
            parse_formula_node(formula)


def test_ternary_expression_is_rejected() -> None:
    with pytest.raises(FormulaParseError):
        parse_formula_node("1 if close > 0 else -1")


def test_identity_and_membership_comparisons_are_rejected() -> None:
    for formula in ("close is volume", "close in volume"):
        with pytest.raises(FormulaParseError):
            parse_formula_node(formula)


def test_non_numeric_constants_in_comparisons_are_rejected() -> None:
    for formula in ("close > True", "close > None", "close == False"):
        with pytest.raises(FormulaParseError):
            parse_formula_node(formula)


def test_conditional_operator_arity_is_enforced() -> None:
    registry = load_default_operator_registry()
    known = set(registry.operators)
    assert not inspect_formula("where(close, 1)", known_operators=known).is_valid
    assert not inspect_formula("where(close, 1, 2, 3)", known_operators=known).is_valid
    assert not inspect_formula("and_(close)", known_operators=known).is_valid
    assert not inspect_formula("or_(close, volume, 1)", known_operators=known).is_valid
    assert not inspect_formula("not_(close, volume)", known_operators=known).is_valid


def test_window_bound_still_enforced_inside_conditional_arms() -> None:
    result = inspect_formula(
        "where(ts_sum(delta(close, 1) > 0, 800) == 800, 1, 0)",
        known_operators=set(load_default_operator_registry().operators),
    )
    assert result.is_valid is False


def test_fields_are_collected_through_comparisons() -> None:
    result = inspect_formula(
        "where(close > volume, market_cap, 0)",
        known_operators=set(load_default_operator_registry().operators),
    )
    assert result.is_valid is True
    assert set(result.fields) == {"close", "volume", "market_cap"}
    assert set(result.operators) == {"where"}


# ---------------------------------------------------------------------------
# Lookback derivation
# ---------------------------------------------------------------------------


def test_lookback_flows_through_comparisons_and_conditionals() -> None:
    assert formula_lookback_rows("close > volume") == 0
    assert formula_lookback_rows("close > delay(close, 5)") == 5
    assert formula_lookback_rows("where(close > delay(close, 5), 1, -1)") == 5
    assert formula_lookback_rows("and_(close > delay(close, 3), volume > delay(volume, 7))") == 7
    # delta contributes its shift; the rolling mask window adds window - 1.
    assert formula_lookback_rows("ts_sum(delta(close, 1) > 0, 3) == 3") == 3
    assert (
        formula_lookback_rows(
            "where(ts_sum(delta(close, 1) > 0, 3) == 3, 1, where(delay(close, 10) > close, -1, 0))"
        )
        == 10
    )

"""RB-10 / RF-1 composite id: all-input hash, colon-free, canonical-stable.

Design contract (docs/design/multi_factor_portfolio_backtest.md §11 RB-10,
§13 test_composite_id): ``composite_id = COMPOSITE_<hash>`` over the canonical
JSON of ALL run inputs — ordered ``(factor_id, direction)`` list, method,
method_params, standardization, backtest window, decay, delay, top_quantile,
coverage_rule, min_factor_coverage, universe_filters. Changing ANY single
input changes the id (that is what prevents ``_merge_score_updates``
stale-blend poisoning); mapping insertion order never does. The id is
colon-free and uppercase-hex so it is a fixed point of the catalog's
precomputed-id canonicalization (write-time formula == read-time formula).
"""

from __future__ import annotations

import re

import pytest
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.catalog import is_precomputed_formula
from quant_forge.synthesis.service import composite_formula, derive_composite_id

BASE: dict[str, object] = {
    "factor_refs": (("F_MEM_ALPHA", 1), ("F_MEM_BETA", -1)),
    "method": "equal_weight",
    "method_params": None,
    "standardization": "zscore",
    "backtest_start": "2026-01-01",
    "backtest_end": "2026-03-31",
    "decay_days": 0,
    "execution_delay_days": 1,
    "top_quantile": 0.3,
    "coverage_rule": "all_factors",
    "min_factor_coverage": None,
    "universe_filters": ("is_st == false",),
}

# Golden literal: any accidental change to the canonical-JSON recipe (key set,
# ordering rules, digest, prefix, casing) re-mints every id and must be a
# loud, reviewed change — exactly the RB-10 stability guarantee.
GOLDEN_BASE_ID = "COMPOSITE_7E32508F6356"


def _make(**overrides: object) -> str:
    return derive_composite_id(**{**BASE, **overrides})  # type: ignore[arg-type]


def test_id_shape_is_colon_free_and_registry_valid() -> None:
    composite_id = _make()
    assert composite_id.startswith("COMPOSITE_")
    assert re.fullmatch(r"COMPOSITE_[0-9A-F]{12}", composite_id)
    assert ":" not in composite_id
    # RF-1: the id must satisfy the FactorDefinition charset so registration
    # cannot raise, and the derived formula must enter the precomputed path.
    definition = FactorDefinition(
        factor_id=composite_id,
        name=composite_id,
        formula=composite_formula(composite_id),
        status="candidate",
        horizon_days=5,
        source="synthesis",
    )
    assert is_precomputed_formula(definition.formula)


def test_id_is_deterministic_and_matches_golden() -> None:
    assert _make() == _make()
    assert _make() == GOLDEN_BASE_ID


def test_changing_any_single_input_changes_the_id() -> None:
    variants: list[dict[str, object]] = [
        # member ORDER is identity-bearing (ordered list per §11 RB-10)
        {"factor_refs": (("F_MEM_BETA", -1), ("F_MEM_ALPHA", 1))},
        # member set
        {"factor_refs": (("F_MEM_ALPHA", 1), ("F_MEM_GAMMA", -1))},
        # one member's direction
        {"factor_refs": (("F_MEM_ALPHA", 1), ("F_MEM_BETA", 1))},
        # member count
        {"factor_refs": (("F_MEM_ALPHA", 1), ("F_MEM_BETA", -1), ("F_MEM_GAMMA", 1))},
        # method (and its params)
        {"method": "weighted", "method_params": {"weights": {"F_MEM_ALPHA": 1.0, "F_MEM_BETA": 2.0}}},
        # method param VALUE only (same method as the variant above)
        {"method": "weighted", "method_params": {"weights": {"F_MEM_ALPHA": 1.0, "F_MEM_BETA": 3.0}}},
        {"standardization": "rank"},
        {"backtest_start": "2026-01-02"},
        {"backtest_end": "2026-04-30"},
        {"decay_days": 10},
        {"execution_delay_days": 2},
        {"top_quantile": 0.2},
        {"coverage_rule": "min_factor_coverage", "min_factor_coverage": 1},
        {"min_factor_coverage": 2},
        {"universe_filters": ()},
    ]
    ids = [_make()] + [_make(**variant) for variant in variants]
    assert len(set(ids)) == len(ids), "every single-input change must mint a fresh id"


def test_id_is_stable_across_mapping_insertion_order() -> None:
    forward = _make(
        method="weighted",
        method_params={"weights": {"F_MEM_ALPHA": 1.0, "F_MEM_BETA": 2.0}, "note_a": 1, "note_b": 2},
    )
    reordered = _make(
        method="weighted",
        method_params={"note_b": 2, "note_a": 1, "weights": {"F_MEM_BETA": 2.0, "F_MEM_ALPHA": 1.0}},
    )
    assert forward == reordered


def test_id_input_validation() -> None:
    with pytest.raises(ValueError):
        _make(factor_refs=(("F_MEM_ALPHA", 1),))  # a composite needs >= 2 members
    with pytest.raises(ValueError):
        _make(factor_refs=(("F_MEM_ALPHA", 1), ("F_MEM_BETA", 2)))  # direction must be +/-1
    with pytest.raises(ValueError):
        _make(top_quantile=0.0)
    with pytest.raises(ValueError):
        _make(execution_delay_days=0)
    with pytest.raises(ValueError):
        _make(decay_days=-1)
    with pytest.raises(ValueError):
        _make(method_params={"weights": object()})  # not JSON-serializable

"""RB-6 universe pinning + CP0 member-formula pinning.

Design contract (docs/design/multi_factor_portfolio_backtest.md §3 RB-6, §13
test_universe_pinning, CP0 amendments): the workflow resolves ONE explicit
``universe_filters`` set for every member fetch and the composite; members
declaring conflicting universes are rejected with the typed
``UNIVERSE_MISMATCH`` error, never silently unioned. Under
``min_factor_coverage < all``, an out-of-universe name (for example an ST
name) covered only by a permissive member cannot enter the composite because
the pinned universe bounds every member fetch. The per-member fetch plan pins
each member's formula string at plan-build time so provenance never depends
on later registry state.
"""

from __future__ import annotations

import pandas as pd
import pytest
from quant_forge.core.contracts import FactorDefinition
from quant_forge.synthesis.service import (
    UNIVERSE_MISMATCH,
    SynthesisPreconditionError,
    UniverseMismatchError,
    build_apriori_composite,
    build_member_fetch_plan,
    resolve_pinned_universe,
)

D1 = pd.Timestamp("2026-01-05")


def _member(factor_id: str, universe_filters: tuple[str, ...] = (), formula: str = "rank(close)") -> FactorDefinition:
    return FactorDefinition(
        factor_id=factor_id,
        name=factor_id.lower(),
        formula=formula,
        universe_filters=universe_filters,
    )


def tidy(rows: list[tuple[pd.Timestamp, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": trade_date, "instrument": instrument, "score": score}
            for trade_date, instrument, score in rows
        ]
    )


def test_conflicting_member_universes_raise_the_typed_error() -> None:
    members = [
        _member("FTR_A", ("drop_is_st",)),
        _member("FTR_B", ("min_listing_days=60",)),
    ]
    with pytest.raises(UniverseMismatchError) as excinfo:
        resolve_pinned_universe(members)
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, SynthesisPreconditionError)
    assert excinfo.value.code == UNIVERSE_MISMATCH
    assert "FTR_A" in str(excinfo.value)
    assert "FTR_B" in str(excinfo.value)


def test_unanimous_declarations_pin_one_canonical_set() -> None:
    members = [
        _member("FTR_A", ("drop_is_st", "min_listing_days=60")),
        _member("FTR_B", ("min_listing_days=60", "drop_is_st")),
    ]
    # Order- and duplicate-insensitive comparison; canonical sorted output.
    assert resolve_pinned_universe(members) == ("drop_is_st", "min_listing_days=60")


def test_empty_declarations_adopt_the_pin_or_the_default() -> None:
    strict = _member("FTR_A", ("drop_is_st",))
    permissive = _member("FTR_B")
    assert resolve_pinned_universe([strict, permissive]) == ("drop_is_st",)
    assert resolve_pinned_universe(
        [_member("FTR_A"), _member("FTR_B")], default=("drop_is_st",)
    ) == ("drop_is_st",)
    assert resolve_pinned_universe([_member("FTR_A"), _member("FTR_B")]) == ()


def test_requested_set_wins_and_must_match_non_empty_declarations() -> None:
    strict = _member("FTR_A", ("drop_is_st",))
    permissive = _member("FTR_B")
    assert resolve_pinned_universe(
        [strict, permissive], requested=("drop_is_st",), default=("other",)
    ) == ("drop_is_st",)
    with pytest.raises(UniverseMismatchError):
        resolve_pinned_universe([strict, permissive], requested=("min_listing_days=60",))
    # An explicit empty request conflicts with a non-empty declaration too:
    # the resolution never silently widens a member's declared universe.
    with pytest.raises(UniverseMismatchError):
        resolve_pinned_universe([strict, permissive], requested=())


def test_filter_normalization_collapses_duplicates_and_whitespace() -> None:
    members = [
        _member("FTR_A", (" drop_is_st ", "drop_is_st")),
        _member("FTR_B", ("drop_is_st",)),
    ]
    assert resolve_pinned_universe(members) == ("drop_is_st",)


def test_st_name_through_permissive_member_cannot_enter_under_partial_coverage() -> None:
    # §13: the pinned universe bounds membership. Both members are fetched
    # with the SAME pinned set (drop_is_st), so the ST name never receives a
    # score from any member and cannot enter the composite even under
    # min_factor_coverage=1.
    strict = _member("FTR_A", ("drop_is_st",))
    permissive = _member("FTR_B")
    pinned = resolve_pinned_universe([strict, permissive])
    assert pinned == ("drop_is_st",)

    pinned_universe_rows = [(D1, "STK001", 1.0), (D1, "STK002", 2.0), (D1, "STK003", 5.0)]
    pinned_fetch = {
        "FTR_A": tidy(pinned_universe_rows),
        "FTR_B": tidy([(D1, "STK001", 3.0), (D1, "STK002", 1.0), (D1, "STK003", 2.0)]),
    }
    result = build_apriori_composite(
        pinned_fetch,
        directions={"FTR_A": 1, "FTR_B": 1},
        standardization="rank",
        method="equal_weight",
        min_factor_coverage=1,
    )
    assert "STK_ST" not in set(result.composite["instrument"])

    # Negative control: had the permissive member been fetched WITHOUT the
    # pin, its ST row would enter the composite under min_factor_coverage=1 —
    # demonstrating that the pinned fetch, not the coverage rule, is the
    # guard that closes this hole.
    unpinned_fetch = {
        "FTR_A": tidy(pinned_universe_rows),
        "FTR_B": tidy(
            [(D1, "STK001", 3.0), (D1, "STK002", 1.0), (D1, "STK003", 2.0), (D1, "STK_ST", 9.0)]
        ),
    }
    leaked = build_apriori_composite(
        unpinned_fetch,
        directions={"FTR_A": 1, "FTR_B": 1},
        standardization="rank",
        method="equal_weight",
        min_factor_coverage=1,
    )
    leaked_rows = leaked.composite[leaked.composite["instrument"] == "STK_ST"]
    assert len(leaked_rows) == 1
    assert leaked_rows["score"].notna().all()


def test_member_validation_in_resolution() -> None:
    with pytest.raises(ValueError, match="at least one member"):
        resolve_pinned_universe([])
    with pytest.raises(ValueError, match="unique"):
        resolve_pinned_universe([_member("FTR_A"), _member("FTR_A")])


def test_fetch_plan_pins_formulas_universe_and_directions() -> None:
    member_a = _member("FTR_A", ("drop_is_st",), formula="rank(close)")
    member_b = _member("FTR_B", (), formula="ts_delta(close, 5)")
    plan = build_member_fetch_plan(
        [member_a, member_b],
        directions={"FTR_A": 1, "FTR_B": -1},
        universe_filters=("drop_is_st",),
    )
    assert [spec.factor_id for spec in plan] == ["FTR_A", "FTR_B"]
    # CP0: formulas captured at plan-build time — the provenance claim is the
    # string that will actually be fetched, independent of later registry
    # edits (a subsequently saved definition does not alter the plan).
    assert plan[0].formula == "rank(close)"
    assert plan[1].formula == "ts_delta(close, 5)"
    drifted = _member("FTR_B", (), formula="rank(volume)")
    assert drifted.formula != plan[1].formula
    assert plan[1].formula == "ts_delta(close, 5)"
    # Every member carries the one pinned universe set and its declared direction.
    assert all(spec.universe_filters == ("drop_is_st",) for spec in plan)
    assert [spec.direction for spec in plan] == [1, -1]
    assert plan[0].provenance_entry() == {
        "factor_id": "FTR_A",
        "direction": 1,
        "source": "user",
        "formula": "rank(close)",
    }


def test_fetch_plan_validation() -> None:
    member_a = _member("FTR_A")
    member_b = _member("FTR_B")
    with pytest.raises(ValueError, match="at least 2"):
        build_member_fetch_plan([member_a], directions={"FTR_A": 1}, universe_filters=())
    with pytest.raises(ValueError, match="unique"):
        build_member_fetch_plan(
            [member_a, _member("FTR_A")],
            directions={"FTR_A": 1},
            universe_filters=(),
        )
    with pytest.raises(ValueError, match="missing"):
        build_member_fetch_plan(
            [member_a, member_b], directions={"FTR_A": 1}, universe_filters=()
        )
    with pytest.raises(ValueError, match="unknown"):
        build_member_fetch_plan(
            [member_a, member_b],
            directions={"FTR_A": 1, "FTR_B": -1, "FTR_C": 1},
            universe_filters=(),
        )
    with pytest.raises(ValueError, match="direction must be"):
        build_member_fetch_plan(
            [member_a, member_b],
            directions={"FTR_A": True, "FTR_B": -1},
            universe_filters=(),
        )

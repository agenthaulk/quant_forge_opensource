"""Contract tests for the Phase B spec layer (quant_forge.specs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quant_forge.core.contracts import SimulationProfile, TransactionCostModel
from quant_forge.specs import (
    AgentTaskSpec,
    FactorSpec,
    RunManifest,
    StrategySpec,
    canonical_fingerprint,
    factor_spec_from_idea,
    load_factor_spec,
    manifest_for,
    save_factor_spec,
    validate_factor_spec,
)


def _spec(**overrides: object) -> FactorSpec:
    payload: dict[str, object] = {
        "factor_id": "FTR_SMALL_CAP",
        "name": "small_cap",
        "formula_dsl": "-rank(market_cap)",
        "thesis": "Small caps outperform in the local demo panel.",
        "expected_direction": "positive",
        "horizon_days": 5,
        "universe_filters": ("is_st == false",),
    }
    payload.update(overrides)
    return FactorSpec(**payload)  # type: ignore[arg-type]


# --- (a) FactorSpec delegates kernel invariants ---------------------------------


def test_factor_spec_rejects_bad_factor_id_via_kernel_contract() -> None:
    with pytest.raises(ValueError, match="factor_id"):
        _spec(factor_id="1bad id!")


def test_factor_spec_rejects_non_positive_horizon_via_kernel_contract() -> None:
    with pytest.raises(ValueError, match="horizon_days"):
        _spec(horizon_days=0)


def test_factor_spec_rejects_empty_formula_and_bad_direction() -> None:
    with pytest.raises(ValueError, match="formula_dsl"):
        _spec(formula_dsl="   ")
    with pytest.raises(ValueError, match="expected_direction"):
        _spec(expected_direction="sideways")


def test_simulation_profile_keeps_lookahead_states_unrepresentable() -> None:
    with pytest.raises(ValueError, match="execution_delay_days"):
        SimulationProfile(execution_delay_days=0)
    with pytest.raises(ValueError, match="top_quantile"):
        SimulationProfile(top_quantile=0.6)


def test_factor_spec_rejects_schema_version_mismatch() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        _spec(schema_version="qf.factor_spec.v0")


def test_factor_spec_dict_round_trip() -> None:
    spec = _spec(
        simulation=SimulationProfile(execution_delay_days=2, top_quantile=0.2),
        costs=TransactionCostModel(commission_bps=3.0, slippage_bps=5.0),
        capabilities_required=("long_short",),
        metadata={"note": "round-trip"},
    )
    assert FactorSpec.from_dict(spec.to_dict()) == spec


# --- (b) validation gate fails closed --------------------------------------------


def test_validation_gate_passes_known_good_formula() -> None:
    result = validate_factor_spec(_spec(formula_dsl="rank(market_cap)"))
    assert result.status == "ready"
    assert result.unresolved_operators == ()
    assert result.unresolved_fields == ()
    assert result.blocking_reasons == ()


def test_validation_gate_blocks_unknown_operator_with_reason() -> None:
    result = validate_factor_spec(_spec(formula_dsl="quantum_rank(market_cap)"))
    assert result.status == "blocked"
    assert "quantum_rank" in result.unresolved_operators
    assert any("quantum_rank" in reason for reason in result.blocking_reasons)


def test_validation_gate_blocks_unknown_field_with_reason() -> None:
    result = validate_factor_spec(_spec(formula_dsl="rank(alien_signal)"))
    assert result.status == "blocked"
    assert result.unresolved_fields == ("alien_signal",)
    assert any("alien_signal" in reason for reason in result.blocking_reasons)


def test_validation_gate_blocks_unparseable_formula() -> None:
    result = validate_factor_spec(_spec(formula_dsl="rank(market_cap"))
    assert result.status == "blocked"
    assert result.blocking_reasons


# --- (c) manifest fingerprint determinism and sensitivity ------------------------


def test_canonical_fingerprint_is_deterministic_and_sensitive() -> None:
    payload = {"factor_id": "FTR_SMALL_CAP", "horizon_days": 5, "nested": {"b": 2, "a": 1}}
    same_other_order = {"nested": {"a": 1, "b": 2}, "horizon_days": 5, "factor_id": "FTR_SMALL_CAP"}
    assert canonical_fingerprint(payload) == canonical_fingerprint(same_other_order)
    changed = dict(payload, horizon_days=10)
    assert canonical_fingerprint(payload) != canonical_fingerprint(changed)


def test_manifest_for_binds_spec_and_request() -> None:
    spec = _spec()
    request = {"kind": "evaluate", "factor_id": spec.factor_id}
    kwargs = {
        "run_id": "run-0001",
        "created_at": "2026-01-02T03:04:05",
        "request": request,
        "registry_version": "qf.operator_registry.v1",
        "input_refs": ("artifact://panel/sha256-abc",),
    }
    first = manifest_for(spec, **kwargs)
    second = manifest_for(spec, **kwargs)
    assert first == second
    assert first.spec_fingerprint == canonical_fingerprint(spec.to_dict())

    other_spec = _spec(horizon_days=10)
    assert manifest_for(other_spec, **kwargs).spec_fingerprint != first.spec_fingerprint
    other_request = dict(kwargs, request={"kind": "backtest", "factor_id": spec.factor_id})
    assert manifest_for(spec, **other_request).request_hash != first.request_hash


def test_run_manifest_rejects_bad_created_at_and_empty_run_id() -> None:
    spec = _spec()
    with pytest.raises(ValueError, match="ISO"):
        manifest_for(spec, run_id="run-1", created_at="yesterday", request={})
    with pytest.raises(ValueError, match="run_id"):
        manifest_for(spec, run_id="  ", created_at="2026-01-02T03:04:05", request={})


# --- (d) YAML round-trip ----------------------------------------------------------


def test_factor_spec_yaml_round_trip(tmp_path: Path) -> None:
    spec = _spec(
        simulation=SimulationProfile(execution_delay_days=2, top_quantile=0.25, decay_days=3),
        costs=TransactionCostModel(commission_bps=2.5, slippage_bps=7.5, short_borrow_bps_annual=100.0),
        capabilities_required=("long_short",),
        metadata={"owner": "expert", "revision": 2},
    )
    path = tmp_path / "factor_spec.yaml"
    save_factor_spec(spec, path)
    loaded = load_factor_spec(path)
    assert loaded == spec
    assert loaded.simulation == spec.simulation
    assert loaded.costs == spec.costs


def test_load_factor_spec_rejects_schema_version_mismatch(tmp_path: Path) -> None:
    spec = _spec()
    path = tmp_path / "factor_spec.yaml"
    save_factor_spec(spec, path)
    text = path.read_text(encoding="utf-8").replace("qf.factor_spec.v1", "qf.factor_spec.v9")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_factor_spec(path)


# --- (e) AgentTaskSpec budgets and tool guard -------------------------------------


def _task(**overrides: object) -> AgentTaskSpec:
    payload: dict[str, object] = {
        "task_id": "task-001",
        "task_type": "evaluate",
        "objective": "Evaluate the drafted small-cap factor on research splits.",
        "max_rounds": 3,
        "allowed_tools": ("evaluate_factor", "list_available_fields"),
    }
    payload.update(overrides)
    return AgentTaskSpec(**payload)  # type: ignore[arg-type]


def test_agent_task_valid_construction() -> None:
    task = _task()
    assert task.sample_role_filter == "research_evaluation"


def test_agent_task_rejects_bad_budget_and_empty_tools() -> None:
    with pytest.raises(ValueError, match="max_rounds"):
        _task(max_rounds=0)
    with pytest.raises(ValueError, match="allowed_tools"):
        _task(allowed_tools=())
    with pytest.raises(ValueError, match="objective"):
        _task(objective="  ")
    with pytest.raises(ValueError, match="task_type"):
        _task(task_type="mine_test_set")


@pytest.mark.parametrize("tool", ["exec", "shell", "Exec", " SHELL "])
def test_agent_task_rejects_codegen_execution_surface(tool: str) -> None:
    with pytest.raises(ValueError, match="forbidden tool"):
        _task(allowed_tools=("evaluate_factor", tool))


# --- (f) NL flow -------------------------------------------------------------------


def test_nl_flow_yields_ready_spec_for_known_idea() -> None:
    spec, result = factor_spec_from_idea("small cap non-st stocks perform better")
    assert result.status == "ready"
    assert spec.formula_dsl == "-rank(market_cap)"
    assert spec.universe_filters == ("is_st == false",)
    assert spec.expected_direction == "positive"
    # Drafting must never write to a factor root; spec is an in-memory proposal.
    assert spec.as_factor_definition().factor_id == spec.factor_id


def test_nl_flow_gate_blocks_spec_with_bogus_operator() -> None:
    spec, _ = factor_spec_from_idea("small cap non-st stocks perform better")
    tampered = FactorSpec(
        factor_id=spec.factor_id,
        name=spec.name,
        formula_dsl="fantasy_alpha(market_cap)",
        thesis=spec.thesis,
        expected_direction=spec.expected_direction,
        horizon_days=spec.horizon_days,
        universe_filters=spec.universe_filters,
    )
    result = validate_factor_spec(tampered)
    assert result.status == "blocked"
    assert "fantasy_alpha" in result.unresolved_operators


# --- StrategySpec ------------------------------------------------------------------


def test_strategy_spec_delegates_id_discipline_and_bounds() -> None:
    spec = StrategySpec(
        strategy_id="STRAT_SMALL_CAP",
        name="small cap long-short",
        ranking_factor_ids=("FTR_SMALL_CAP",),
        holding_days=5,
    )
    assert spec.benchmark == "cash"
    with pytest.raises(ValueError, match="kernel id validation"):
        StrategySpec(strategy_id="9bad!", name="x", ranking_factor_ids=("FTR_A",))
    with pytest.raises(ValueError, match="kernel id validation"):
        StrategySpec(strategy_id="STRAT_A", name="x", ranking_factor_ids=("no good",))
    with pytest.raises(ValueError, match="ranking factor"):
        StrategySpec(strategy_id="STRAT_A", name="x", ranking_factor_ids=())
    with pytest.raises(ValueError, match="holding_days"):
        StrategySpec(strategy_id="STRAT_A", name="x", ranking_factor_ids=("FTR_A",), holding_days=0)


def test_strategy_spec_surfaces_unsupported_capabilities() -> None:
    spec = StrategySpec(
        strategy_id="STRAT_A",
        name="capability probe",
        ranking_factor_ids=("FTR_A",),
        capabilities_required=("long_short", "sector_neutral"),
    )
    assert spec.unsupported_capabilities(("long_short",)) == ("sector_neutral",)
    assert spec.unsupported_capabilities(("long_short", "sector_neutral")) == ()


def test_strategy_spec_dict_round_trip() -> None:
    spec = StrategySpec(
        strategy_id="STRAT_A",
        name="round trip",
        ranking_factor_ids=("FTR_A", "FTR_B"),
        holding_days=10,
        simulation=SimulationProfile(execution_delay_days=2),
        costs=TransactionCostModel(commission_bps=1.0),
        capabilities_required=("long_short",),
    )
    assert StrategySpec.from_dict(spec.to_dict()) == spec


def test_run_manifest_normalizes_input_refs_from_list() -> None:
    manifest = RunManifest(
        run_id="run-1",
        created_at="2026-01-02T03:04:05",
        spec_fingerprint="f" * 64,
        data_fingerprint="",
        registry_version="qf.operator_registry.v1",
        request_hash="a" * 64,
        input_refs=["artifact://x"],  # type: ignore[arg-type]
        sample_role="research_evaluation",
    )
    assert manifest.input_refs == ("artifact://x",)

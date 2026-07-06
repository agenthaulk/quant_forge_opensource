"""Contract tests for the Phase B spec layer (quant_forge.specs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quant_forge.core.contracts import SimulationProfile, TransactionCostModel
from quant_forge.specs import (
    KNOWN_AGENT_TOOLS,
    LEGAL_TRANSITIONS,
    RUN_STATES,
    SAMPLE_ROLES,
    SPEC_KINDS,
    UNVERIFIED_PROVENANCE,
    AgentTaskSpec,
    FactorSpec,
    RunEvent,
    RunManifest,
    StrategySpec,
    canonical_fingerprint,
    factor_spec_from_idea,
    is_legal_transition,
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
    # All gate-declared surfaces are verified today; nothing silently skipped.
    assert result.unchecked == ()


def test_validation_gate_blocks_reserved_capabilities_by_name() -> None:
    result = validate_factor_spec(
        _spec(formula_dsl="rank(market_cap)", capabilities_required=("long_short",))
    )
    assert result.status == "blocked"
    assert "capability not available: long_short" in result.blocking_reasons


def test_validation_gate_blocks_unknown_universe_filter_form() -> None:
    result = validate_factor_spec(
        _spec(formula_dsl="rank(market_cap)", universe_filters=("market_cap > 1e9",))
    )
    assert result.status == "blocked"
    assert "unsupported universe filter: market_cap > 1e9" in result.blocking_reasons


def test_validation_gate_accepts_executor_filter_forms_case_insensitively() -> None:
    # Same normalization the executor applies (strip + lower).
    result = validate_factor_spec(
        _spec(formula_dsl="rank(market_cap)", universe_filters=(" IS_ST == FALSE ",))
    )
    assert result.status == "ready"


def test_factor_spec_surfaces_unsupported_capabilities() -> None:
    spec = _spec(capabilities_required=("long_short", "sector_neutral"))
    assert spec.unsupported_capabilities(("long_short",)) == ("sector_neutral",)
    assert spec.unsupported_capabilities(("long_short", "sector_neutral")) == ()


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


def test_canonical_fingerprint_rejects_non_finite_floats() -> None:
    with pytest.raises(ValueError, match="serializable"):
        canonical_fingerprint({"metric": float("nan")})
    with pytest.raises(ValueError, match="serializable"):
        canonical_fingerprint({"nested": {"values": [1.0, float("inf")]}})


def test_canonical_fingerprint_normalizes_unicode_to_nfc() -> None:
    # NFC single codepoint U+00E9 vs NFD "e" + U+0301 combining acute accent,
    # in both keys and values (ASCII escapes so no editor can re-normalize them).
    nfc = {"name": "caf\u00e9", "caf\u00e9": 1}
    nfd = {"name": "cafe\u0301", "cafe\u0301": 1}
    assert nfc["name"] != nfd["name"]  # distinct codepoints, same rendered text
    assert canonical_fingerprint(nfc) == canonical_fingerprint(nfd)
    assert canonical_fingerprint(nfc) != canonical_fingerprint({"name": "cafe", "cafe": 1})


def _manifest_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "run_id": "run-0001",
        "created_at": "2026-01-02T03:04:05",
        "request": {"kind": "evaluate", "factor_id": "FTR_SMALL_CAP"},
        "data_fingerprint": "sha256-panel-abc",
        "registry_version": "qf.operator_registry.v1",
        "sample_role": "research_evaluation",
        "input_refs": ("artifact://panel/sha256-abc",),
    }
    kwargs.update(overrides)
    return kwargs


def test_manifest_for_binds_spec_and_request() -> None:
    spec = _spec()
    kwargs = _manifest_kwargs(request={"kind": "evaluate", "factor_id": spec.factor_id})
    first = manifest_for(spec, **kwargs)
    second = manifest_for(spec, **kwargs)
    assert first == second
    assert first.spec_fingerprint == canonical_fingerprint(spec.to_dict())
    assert first.spec_kind == "factor"
    assert first.spec_schema_version == "qf.factor_spec.v1"

    other_spec = _spec(horizon_days=10)
    assert manifest_for(other_spec, **kwargs).spec_fingerprint != first.spec_fingerprint
    other_request = dict(kwargs, request={"kind": "backtest", "factor_id": spec.factor_id})
    assert manifest_for(spec, **other_request).request_hash != first.request_hash


def test_run_manifest_rejects_bad_created_at_and_empty_run_id() -> None:
    spec = _spec()
    with pytest.raises(ValueError, match="ISO"):
        manifest_for(spec, **_manifest_kwargs(created_at="yesterday"))
    with pytest.raises(ValueError, match="run_id"):
        manifest_for(spec, **_manifest_kwargs(run_id="  "))


def test_manifest_for_requires_explicit_provenance_and_sample_role() -> None:
    spec = _spec()
    for omitted in ("data_fingerprint", "registry_version", "sample_role"):
        kwargs = _manifest_kwargs()
        del kwargs[omitted]
        with pytest.raises(TypeError):
            manifest_for(spec, **kwargs)


def test_run_manifest_rejects_empty_provenance_and_accepts_typed_sentinel() -> None:
    spec = _spec()
    with pytest.raises(ValueError, match="data_fingerprint"):
        manifest_for(spec, **_manifest_kwargs(data_fingerprint=""))
    with pytest.raises(ValueError, match="registry_version"):
        manifest_for(spec, **_manifest_kwargs(registry_version=""))

    manifest = manifest_for(
        spec,
        **_manifest_kwargs(
            data_fingerprint=UNVERIFIED_PROVENANCE,
            registry_version=UNVERIFIED_PROVENANCE,
        ),
    )
    # The sentinel is typed and distinguishable — gates can grep for it.
    assert manifest.data_fingerprint == UNVERIFIED_PROVENANCE
    assert manifest.registry_version == UNVERIFIED_PROVENANCE
    assert manifest.data_fingerprint != ""


def test_run_manifest_rejects_sample_role_outside_kernel_vocabulary() -> None:
    # Pin the mirror of the kernel literals (core.contracts / backtesting.service).
    assert SAMPLE_ROLES == frozenset(
        {
            "research_evaluation",
            "in_sample_backtest",
            "external_oos_backtest",
            "staggered_entry_cohort",
            "staggered_entry_backtest",
        }
    )
    spec = _spec()
    with pytest.raises(ValueError, match="sample_role"):
        manifest_for(spec, **_manifest_kwargs(sample_role="production_backtest"))
    for role in sorted(SAMPLE_ROLES):
        assert manifest_for(spec, **_manifest_kwargs(sample_role=role)).sample_role == role


def test_canonical_fingerprint_wraps_unserializable_payload_into_value_error() -> None:
    with pytest.raises(ValueError, match="serializable"):
        canonical_fingerprint({"bad": {1, 2, 3}})


def test_manifest_for_accepts_strategy_spec() -> None:
    strategy = StrategySpec(
        strategy_id="STRAT_SMALL_CAP",
        name="small cap long-short",
        ranking_factor_ids=("FTR_SMALL_CAP",),
        holding_days=5,
    )
    manifest = manifest_for(
        strategy,
        **_manifest_kwargs(request={"kind": "backtest", "strategy_id": strategy.strategy_id}),
    )
    assert manifest.spec_kind == "strategy"
    assert manifest.spec_schema_version == "qf.strategy_spec.v1"
    assert manifest.spec_fingerprint == canonical_fingerprint(strategy.to_dict())


def test_manifest_for_rejects_non_spec_payloads() -> None:
    with pytest.raises(ValueError, match="unsupported spec type"):
        manifest_for({"factor_id": "FTR_X"}, **_manifest_kwargs())  # type: ignore[arg-type]


def _direct_manifest_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "run_id": "run-1",
        "created_at": "2026-01-02T03:04:05",
        "spec_fingerprint": "f" * 64,
        "spec_kind": "factor",
        "spec_schema_version": "qf.factor_spec.v1",
        "data_fingerprint": "sha256-panel-abc",
        "registry_version": "qf.operator_registry.v1",
        "request_hash": "a" * 64,
        "sample_role": "research_evaluation",
    }
    kwargs.update(overrides)
    return kwargs


def test_run_manifest_rejects_bad_spec_kind_and_empty_spec_schema_version() -> None:
    assert SPEC_KINDS == frozenset({"factor", "strategy"})
    with pytest.raises(ValueError, match="spec_kind"):
        RunManifest(**_direct_manifest_kwargs(spec_kind="portfolio"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="spec_schema_version"):
        RunManifest(**_direct_manifest_kwargs(spec_schema_version="  "))  # type: ignore[arg-type]


def test_run_manifest_requires_explicit_sample_role() -> None:
    # No dataclass-level default: omitting sample_role is a construction error.
    kwargs = _direct_manifest_kwargs()
    del kwargs["sample_role"]
    with pytest.raises(TypeError):
        RunManifest(**kwargs)  # type: ignore[arg-type]


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
        "allowed_tools": ("evaluate_factor", "read_catalog"),
    }
    payload.update(overrides)
    return AgentTaskSpec(**payload)  # type: ignore[arg-type]


def test_agent_task_valid_construction() -> None:
    task = _task()
    assert task.sample_role_filter == "research_evaluation"


def test_agent_task_accepts_any_subset_of_declared_catalog() -> None:
    task = _task(allowed_tools=tuple(sorted(KNOWN_AGENT_TOOLS)))
    assert set(task.allowed_tools) == KNOWN_AGENT_TOOLS


def test_agent_task_rejects_bad_budget_and_empty_tools() -> None:
    with pytest.raises(ValueError, match="max_rounds"):
        _task(max_rounds=0)
    with pytest.raises(ValueError, match="allowed_tools"):
        _task(allowed_tools=())
    with pytest.raises(ValueError, match="objective"):
        _task(objective="  ")
    with pytest.raises(ValueError, match="task_type"):
        _task(task_type="mine_test_set")


@pytest.mark.parametrize(
    "tool",
    ["exec", "shell", "bash", "subprocess", "Exec", " SHELL ", "totally_made_up_tool", ""],
)
def test_agent_task_rejects_tools_outside_declared_catalog(tool: str) -> None:
    # Allowlist polarity: anything not in KNOWN_AGENT_TOOLS is unrepresentable.
    with pytest.raises(ValueError, match="allowed_tools"):
        _task(allowed_tools=("evaluate_factor", tool))


def test_agent_task_rejects_sample_role_filter_outside_kernel_vocabulary() -> None:
    with pytest.raises(ValueError, match="sample_role_filter"):
        _task(sample_role_filter="all_samples")
    task = _task(sample_role_filter="external_oos_backtest")
    assert task.sample_role_filter == "external_oos_backtest"


# --- (f) NL flow -------------------------------------------------------------------


def test_nl_flow_yields_ready_spec_for_known_idea() -> None:
    spec, result = factor_spec_from_idea("small cap non-st stocks perform better")
    assert result.status == "ready"
    assert spec.formula_dsl == "-rank(market_cap)"
    assert spec.universe_filters == ("is_st == false",)
    assert spec.expected_direction == "positive"
    # Drafting must never write to a factor root; spec is an in-memory proposal.
    assert spec.as_factor_definition().factor_id == spec.factor_id


def test_nl_flow_discloses_generic_fallback_parse() -> None:
    spec, result = factor_spec_from_idea("buy companies with strong ESG and dividend growth")
    assert spec.formula_dsl == "rank(close)"
    assert any("generic fallback" in warning for warning in result.warnings)


def test_nl_flow_no_fallback_warning_for_recognized_idea() -> None:
    _, result = factor_spec_from_idea("small cap non-st stocks perform better")
    assert not any("generic fallback" in warning for warning in result.warnings)


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


def test_factor_spec_from_dict_raises_value_error_on_unknown_component_keys() -> None:
    payload = _spec().to_dict()
    payload["simulation"] = {"bogus_key": 1}
    with pytest.raises(ValueError, match="simulation payload"):
        FactorSpec.from_dict(payload)
    payload = _spec().to_dict()
    payload["costs"] = {"not_a_cost_field": 2.0}
    with pytest.raises(ValueError, match="costs payload"):
        FactorSpec.from_dict(payload)


def test_strategy_spec_from_dict_raises_value_error_on_unknown_component_keys() -> None:
    spec = StrategySpec(strategy_id="STRAT_A", name="x", ranking_factor_ids=("FTR_A",))
    payload = spec.to_dict()
    payload["simulation"] = {"bogus_key": 1}
    with pytest.raises(ValueError, match="simulation payload"):
        StrategySpec.from_dict(payload)
    payload = spec.to_dict()
    payload["costs"] = {"not_a_cost_field": 2.0}
    with pytest.raises(ValueError, match="costs payload"):
        StrategySpec.from_dict(payload)


def test_run_manifest_normalizes_input_refs_from_list() -> None:
    manifest = RunManifest(
        **_direct_manifest_kwargs(  # type: ignore[arg-type]
            data_fingerprint=UNVERIFIED_PROVENANCE,
            input_refs=["artifact://x"],
        )
    )
    assert manifest.input_refs == ("artifact://x",)


# --- RunEvent and the run state machine ---------------------------------------------


def _event(**overrides: object) -> RunEvent:
    payload: dict[str, object] = {
        "event_id": "evt-001",
        "run_id": "run-0001",
        "ts": "2026-01-02T03:04:05",
        "type": "start",
        "stage": "planning",
        "actor": "system",
    }
    payload.update(overrides)
    return RunEvent(**payload)  # type: ignore[arg-type]


def test_run_event_valid_construction_and_defaults() -> None:
    event = _event()
    assert event.severity == "info"
    assert event.parent_event_id == ""
    assert event.payload_ref == ""
    assert event.message == ""
    assert event.schema_version == "qf.run.event.v1"


def test_run_event_rejects_vocabulary_violations() -> None:
    with pytest.raises(ValueError, match="event type"):
        _event(type="teleport")
    with pytest.raises(ValueError, match="event stage"):
        _event(stage="deployment")
    with pytest.raises(ValueError, match="event actor"):
        _event(actor="intern")
    with pytest.raises(ValueError, match="event severity"):
        _event(severity="catastrophic")
    with pytest.raises(ValueError, match="event_id"):
        _event(event_id="  ")
    with pytest.raises(ValueError, match="run_id"):
        _event(run_id="")
    with pytest.raises(ValueError, match="ISO"):
        _event(ts="yesterday")
    with pytest.raises(ValueError, match="schema_version"):
        _event(schema_version="qf.run.event.v0")


def test_run_state_machine_matches_corpus() -> None:
    # The transition table covers exactly the corpus states.
    assert set(LEGAL_TRANSITIONS) == RUN_STATES
    for targets in LEGAL_TRANSITIONS.values():
        assert targets <= RUN_STATES
    # Terminal states go nowhere.
    for terminal in ("failed", "cancelled", "completed"):
        assert LEGAL_TRANSITIONS[terminal] == frozenset()


def test_every_legal_transition_is_legal() -> None:
    for source, targets in LEGAL_TRANSITIONS.items():
        for target in targets:
            assert is_legal_transition(source, target), (source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("completed", "running"),
        ("queued", "paused"),
        ("failed", "running"),
        ("cancelled", "queued"),
        ("partial", "running"),
    ],
)
def test_illegal_transitions_are_false(source: str, target: str) -> None:
    assert is_legal_transition(source, target) is False


def test_is_legal_transition_raises_on_unknown_states() -> None:
    with pytest.raises(ValueError, match="unknown run state"):
        is_legal_transition("limbo", "running")
    with pytest.raises(ValueError, match="unknown run state"):
        is_legal_transition("running", "limbo")

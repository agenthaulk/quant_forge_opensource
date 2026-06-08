from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest

import quant_forge.apps.cli.main as cli_main
import quant_forge.apps.web.server as web_server
import quant_forge.research_loop.llm as rd_llm
from quant_forge.apps.web.server import (
    create_local_web_server,
    run_idea_workflow,
    run_research_campaign_workflow,
    run_research_once_workflow,
)
from quant_forge.config import LLMProviderSettings, LLMSettings, PathSettings, QuantForgeConfig
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition, SimulationProfile
from quant_forge.data.local import LocalPanelDataProvider, PANEL_FILE, create_demo_workspace
from quant_forge.llm_client import LLMChatResult
from quant_forge.llm_factor_parser import ParsedFactor
from quant_forge.research_loop.campaign import ResearchCampaignResult, ResearchCampaignRoundResult
from quant_forge.research_loop.config import ResearchLLMConfig, load_research_loop_config


def test_web_workbench_rule_workflow_runs_end_to_end(tmp_path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    result = run_idea_workflow(config, "非ST的小市值股票未来表现更好", parser_mode="rule")

    assert result["parser"]["source"] == "rule"
    assert result["factor"]["formula"] == "-rank(market_cap)"
    assert result["factor"]["universe_filters"] == ["is_st == false"]
    assert result["evaluation"]["observations"] > 0
    assert result["backtest"]["periods"] > 0
    assert "gross_annualized_return" in result["backtest"]
    assert "net_annualized_return" in result["backtest"]
    assert "rebalance_rate" in result["backtest"]
    assert "turnover_rate" in result["backtest"]
    assert {metric["name"] for metric in result["backtest"]["segment_metrics"]} == {"IS", "OOS1", "OOS2"}
    assert result["backtest"]["assumptions"]
    assert paths["factor_root"].exists()


def test_web_research_once_workflow_runs_end_to_end(tmp_path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    result = run_research_once_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        rd_config=_local_rd_config(config),
    )

    assert result["rd_stage"] == "research"
    assert result["workflow_type"] == "research"
    assert result["seed_factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert result["candidates"][0]["factor"]["formula"] != "-rank(market_cap)"
    assert result["candidates"][0]["factor"]["formula"] in {"rank(return_5d)", "-rank(volatility_5d)"}
    assert result["candidates"][0]["factor"]["status"] == "candidate"
    assert result["accepted_candidate_ids"]
    backtest = result["candidates"][0]["backtest"]
    assert "net_annualized_return" in backtest
    assert "rebalance_rate" in backtest
    assert "turnover_rate" in backtest
    assert backtest["segment_metrics"]
    assert Path(result["report_path"]).exists()
    assert paths["factor_root"].exists()


def test_web_research_campaign_workflow_runs_end_to_end(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    result = run_research_campaign_workflow(
        config,
        ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"],
        rounds=2,
        rd_config=_local_rd_config(config),
    )

    assert result["rd_stage"] == "factor_synthesis"
    assert result["workflow_type"] == "campaign"
    assert result["rounds_requested"] == 2
    assert result["rounds_completed"] >= 1
    assert result["round_results"]
    assert result["final_factor_id"]
    assert result["final_factor"]["source"] == "research_campaign"
    assert result["final_factor"]["factor_id"] == result["final_factor_id"]
    assert result["final_backtest"]["simulation_profile"]["decay_days"] == 0
    assert Path(result["final_evaluation"]["artifact_path"]).exists()
    assert Path(result["final_backtest"]["artifact_path"]).exists()
    assert result["artifacts"]


def test_web_research_campaign_workflow_supports_audit_seed_source_and_2025_profile(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    _rewrite_demo_panel_to_2025(config.paths.data_root)
    audit_path = (
        config.paths.artifact_root
        / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_DIR
        / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_FILE
    )
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            [
                {"rank": 2, "factor_id": "FTR_DEMO_SMALL_CAP"},
                {"rank": 1, "factor_id": "FTR_DEMO_MOMENTUM"},
            ]
        ),
        encoding="utf-8",
    )
    rd_path = tmp_path / "rd_2025.yaml"
    rd_path.write_text(
        """
objective: balanced
llm:
  hypothesis_mode: local
  review_mode: local
  campaign_mode: local
simulation:
  execution_delay_days: 1
  top_quantile: 0.3
  nan_policy: drop
  neutralization: none
  truncation: null
  decay_days: 0
  test_period:
    start: "2025-01-01"
    end: "2025-12-31"
""",
        encoding="utf-8",
    )
    rd_config = load_research_loop_config(rd_path, config.research, config.simulation)

    result = run_research_campaign_workflow(
        config,
        ["IGNORED_SEED"],
        seed_source_path=str(audit_path),
        rounds=1,
        rd_config=rd_config,
    )

    assert result["seed_factor_ids"] == ["FTR_DEMO_MOMENTUM", "FTR_DEMO_SMALL_CAP"]
    assert result["seed_source_path"] == str(audit_path.resolve(strict=False))
    assert result["seed_source_label"] == "Audit JSON seed source"
    assert result["simulation_profile"]["test_period_start"] == "2025-01-01"
    assert result["simulation_profile"]["test_period_end"] == "2025-12-31"
    assert result["final_backtest"]["simulation_profile"]["test_period_start"] == "2025-01-01"
    assert result["final_backtest"]["simulation_profile"]["test_period_end"] == "2025-12-31"
    assert result["status"] == "ok"


def test_web_research_campaign_workflow_rejects_non_allowlisted_seed_source_path(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    audit_path = tmp_path / "outside" / "top20.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(["FTR_DEMO_SMALL_CAP"]), encoding="utf-8")

    with pytest.raises(ValueError, match="campaign seed source path is not allowed"):
        run_research_campaign_workflow(
            config,
            ["IGNORED_SEED"],
            seed_source_path=str(audit_path),
            rounds=1,
        )


def test_web_research_campaign_workflow_rejects_escape_seed_source_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = load_research_loop_config(Path("configs/rd.yaml"), config.research, config.simulation)
    monkeypatch.chdir(tmp_path / "demo")
    audit_path = tmp_path / "outside" / "top20.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(["FTR_DEMO_SMALL_CAP"]), encoding="utf-8")

    with pytest.raises(ValueError, match="campaign seed source path is not allowed"):
        run_research_campaign_workflow(
            config,
            ["IGNORED_SEED"],
            seed_source_path="../outside/top20.json",
            rounds=1,
            rd_config=rd_config,
        )


def test_web_research_once_rejects_invalid_explicit_candidate_count(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    with pytest.raises(ValueError, match="max_candidates must be between 1 and 10"):
        run_research_once_workflow(config, "FTR_DEMO_SMALL_CAP", max_candidates=0, rd_config=_local_rd_config(config))


def test_web_html_uses_default_rd_config_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_DEEPSEEK_KEY", "set")
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/v1",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")
    rd_path = tmp_path / "rd.yaml"
    rd_path.write_text(
        """
objective: rank_icir
default_max_candidates: 2
default_interval_days: 5
allowed_interval_days: [5]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_server, "DEFAULT_RD_CONFIG_PATH", rd_path)
    html = web_server._index_html(config)

    assert "/api/research/run-once" in html
    assert "/api/research/campaign" in html
    assert "/api/research/schedule" in html
    assert "LLM 语义解析" in html
    assert "本地规则解析" in html
    assert "是否改用本地规则解析" in html
    assert "rd-objective" in html
    assert "rd-campaign-seeds" in html
    assert "rd-campaign" in html
    assert 'value="5"' in html
    assert 'value="2"' in html
    assert '<option value="rank_icir" selected>ICIR</option>' in html
    assert '<option value="balanced" selected>' not in html
    assert '<option value="5" selected>5天</option>' in html
    assert "LLM parser: deepseek / fake-deepseek" in html
    assert "RD optimizer: research local deterministic" in html
    assert '<option value="deepseek" selected>deepseek / fake-deepseek · env QF_TEST_DEEPSEEK_KEY</option>' in html
    assert '<option value="glm">glm / fake-glm · env QF_TEST_GLM_KEY</option>' in html
    assert "毛年化收益" in html
    assert "净年化收益" in html
    assert "调仓率" in html
    assert "换手率" in html
    assert "风险提示" in html
    assert "研究口径，不是生产交易口径" in html


def test_web_research_scheduler_http_start_stop_and_validation(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = _local_rd_config(config)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config, rd_config=rd_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        started = _post_json(
            f"{base_url}/api/research/schedule",
            {
                "action": "start",
                "seed_factor_id": "FTR_DEMO_SMALL_CAP",
                "objective": "balanced",
                "interval_days": 1,
                "max_candidates": 1,
            },
        )
        assert started["enabled"] is True
        assert started["run_count"] == 1
        assert started["last_result"]["accepted_candidate_ids"]
        assert Path(started["last_result"]["report_path"]).exists()

        stopped = _post_json(f"{base_url}/api/research/schedule", {"action": "stop"})
        assert stopped["enabled"] is False
        assert stopped["run_count"] == 1

        try:
            _post_json(
                f"{base_url}/api/research/schedule",
                {
                    "action": "start",
                    "seed_factor_id": "FTR_DEMO_SMALL_CAP",
                    "objective": "balanced",
                    "interval_days": 2,
                    "max_candidates": 1,
                },
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode("utf-8"))
            assert "interval_days must be one of" in body["error"]
        else:
            raise AssertionError("invalid interval should return HTTP 400")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_research_campaign_http_runs_and_returns_final_factor(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = _local_rd_config(config)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config, rd_config=rd_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        payload = _post_json(
            f"{base_url}/api/research/campaign",
            {
                "seed_factor_ids": ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"],
                "objective": "balanced",
                "rounds": 2,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    assert payload["final_factor_id"]
    assert payload["final_factor"]["source"] == "research_campaign"
    assert payload["round_results"]
    assert payload["artifacts"]


def test_web_research_campaign_http_supports_precomputed_seeds(tmp_path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()
    factor_values_root = tmp_path / "factor_values"
    factor_values_overlay_root = tmp_path / "factor_values_overlay"
    seed_factor_ids = [
        "WQ_ALPHA_011",
        "WQ_ALPHA_012",
        "WQ_ALPHA_013",
        "WQ_ALPHA_014",
        "WQ_ALPHA_015",
        "WQ_ALPHA_016",
    ]
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_011",
        factor_name="wq_alpha_011",
        scores=1.0 - panel.groupby("trade_date")["market_cap"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_012",
        factor_name="wq_alpha_012",
        scores=panel.groupby("trade_date")["return_5d"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_013",
        factor_name="wq_alpha_013",
        scores=1.0 - panel.groupby("trade_date")["volatility_5d"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_014",
        factor_name="wq_alpha_014",
        scores=panel.groupby("trade_date")["volume"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_015",
        factor_name="wq_alpha_015",
        scores=panel.groupby("trade_date")["close"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_016",
        factor_name="wq_alpha_016",
        scores=panel.groupby("trade_date")["return_1d"].rank(pct=True),
    )
    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=paths["data_root"],
            factor_root=paths["factor_root"],
            factor_values_root=factor_values_root,
            factor_values_overlay_root=factor_values_overlay_root,
            artifact_root=paths["artifact_root"],
        )
    ).resolve(tmp_path / "demo")
    rd_config = _local_rd_config(config)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config, rd_config=rd_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        payload = _post_json(
            f"{base_url}/api/research/campaign",
            {
                "seed_factor_ids": seed_factor_ids,
                "objective": "balanced",
                "rounds": 5,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    assert payload["final_factor_id"]
    assert payload["final_factor"]["factor_id"] == payload["final_factor_id"]
    assert payload["final_factor"]["source"] == "research_campaign"
    assert payload["final_factor"]["formula"] == f"precomputed:factor_id={payload['final_factor_id']}"
    assert Path(payload["final_evaluation"]["artifact_path"]).exists()
    assert Path(payload["final_backtest"]["artifact_path"]).exists()
    overlay_dir = (
        factor_values_overlay_root
        / "合成因子"
        / f"factor_id={payload['final_factor_id']}"
        / "incremental"
    )
    assert tuple(overlay_dir.glob("*.parquet"))
    assert payload["round_results"]


def test_web_status_keeps_active_llm_provider_when_key_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_TEST_DEEPSEEK_KEY", raising=False)
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/deepseek",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status = _get_json(f"{base_url}/api/status")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    assert status["llm"]["provider"] == "deepseek"
    providers = {provider["provider"]: provider for provider in status["llm"]["providers"]}
    assert providers["deepseek"]["runtime_ready"] == "false"
    assert "QF_TEST_DEEPSEEK_KEY" in providers["deepseek"]["runtime_error"]
    assert providers["glm"]["runtime_ready"] == "true"


def test_web_workbench_uses_llm_factor_horizon(monkeypatch, tmp_path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    captured: dict[str, int | None] = {}

    def fake_parse_factor_idea(text, llm, *, mode):
        factor = FactorDefinition(
            factor_id="FTR_LLM_HORIZON",
            name="llm_horizon",
            formula="-rank(market_cap)",
            horizon_days=11,
            universe_filters=("is_st == false",),
            source="llm",
        )
        return ParsedFactor(factor=factor, source="llm", provider="deepseek", model="fake")

    def fake_evaluate_factor(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        horizon_days,
        horizon_days_matrix,
        sample_splits,
        simulation_profile,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
    ):
        assert factor_values_root == config.paths.factor_values_root
        assert factor_values_overlay_root == config.paths.factor_values_overlay_root
        assert factor_values_manifest_root == config.paths.factor_values_manifest_root
        captured["horizon_days"] = horizon_days
        captured["horizon_count"] = len(horizon_days_matrix)
        captured["split_count"] = len(sample_splits)
        captured["decay_days"] = simulation_profile.decay_days
        return EvaluationResult(
            factor_id=factor_id,
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.1,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=Path(artifact_root) / "evaluations" / f"{factor_id}.json",
        )

    def fake_run_factor_backtest(factor_id, *, factor_root, data_root, artifact_root, simulation_profile):
        return BacktestResult(
            factor_id=factor_id,
            periods=1,
            holding_days=11,
            cumulative_return=0.01,
            annualized_return=0.01,
            annualized_volatility=0.0,
            max_drawdown=0.0,
            artifact_path=Path(artifact_root) / "backtests" / f"{factor_id}.json",
            simulation_profile=simulation_profile,
            net_annualized_return=0.01,
            net_long_short_sharpe=0.5,
            rebalance_rate=0.25,
            turnover_rate=1.0,
            warnings=("research semantics",),
        )

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)
    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)

    def fake_run_factor_backtest_with_holding(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        simulation_profile,
        holding_days,
        transaction_costs,
        sample_splits,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
    ):
        assert holding_days == 11
        assert transaction_costs.commission_bps == 0.0
        assert len(sample_splits) == 3
        assert factor_values_root == config.paths.factor_values_root
        assert factor_values_overlay_root == config.paths.factor_values_overlay_root
        assert factor_values_manifest_root == config.paths.factor_values_manifest_root
        captured["top_quantile_basis_points"] = int(simulation_profile.top_quantile * 10000)
        return fake_run_factor_backtest(
            factor_id,
            factor_root=factor_root,
            data_root=data_root,
            artifact_root=artifact_root,
            simulation_profile=simulation_profile,
        )

    monkeypatch.setattr(web_server, "run_factor_backtest", fake_run_factor_backtest_with_holding)

    result = run_idea_workflow(config, "非ST的小市值股票未来表现更好", parser_mode="llm")

    assert captured["horizon_days"] == 11
    assert captured["horizon_count"] == 4
    assert captured["split_count"] == 3
    assert captured["decay_days"] == 0
    assert captured["top_quantile_basis_points"] == 3000
    assert result["factor"]["horizon_days"] == 11
    assert result["backtest"]["holding_days"] == 11
    assert paths["factor_root"].exists()


def test_web_llm_mode_does_not_silently_fallback_to_rule_parser(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(llm=LLMSettings(provider="rule")).resolve(tmp_path / "demo")

    with pytest.raises(RuntimeError, match="local rule parser"):
        run_idea_workflow(config, "小市值", parser_mode="llm")


def test_web_research_once_uses_shared_llm_for_hypothesis_and_review(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="openai_compatible",
            providers={
                "openai_compatible": LLMProviderSettings(
                    provider="openai_compatible",
                    model="fake-local",
                    base_url="http://localhost/v1",
                    api_key_required=False,
                )
            },
        )
    ).resolve(tmp_path / "demo")
    calls: list[str] = []

    def fake_generate_chat_text(llm, messages, *, temperature=0.0, max_tokens=1200):
        text = messages[-1]["content"]
        calls.append(text)
        if "Generate up to" in text:
            return LLMChatResult(
                content=json.dumps(
                    {
                        "hypotheses": [
                            {
                                "text": "非ST的动量股票未来表现更好",
                                "rationale": "Momentum should be compared against the seed without duplicating it.",
                                "formula_dsl": "rank(return_5d)",
                                "input_fields": ["return_5d"],
                                "expected_direction": "positive",
                                "universe_constraints": ["is_st == false"],
                            }
                        ]
                    }
                ),
                provider="openai_compatible",
                model="fake-local",
            )
        return LLMChatResult(
            content=json.dumps(
                {
                    "summary": "LLM review accepted the local evidence.",
                    "strengths": ["positive evidence"],
                    "risks": ["research only"],
                    "next_hypotheses": ["compare a lower-turnover variant"],
                }
            ),
            provider="openai_compatible",
            model="fake-local",
        )

    monkeypatch.setattr(rd_llm, "generate_chat_text", fake_generate_chat_text)

    result = run_research_once_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        rd_config=_llm_rd_config(config),
    )

    assert len(calls) == 2
    assert result["generation"]["source"] == "llm_hypothesis"
    assert result["generation"]["provider"] == "openai_compatible"
    assert "raw_response" not in result["generation"]
    assert result["candidates"][0]["self_review"]["source"] == "llm_self_review"
    assert result["candidates"][0]["factor"]["formula"] == "rank(return_5d)"


def test_web_research_llm_missing_formula_dsl_is_blocked(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="openai_compatible",
            providers={
                "openai_compatible": LLMProviderSettings(
                    provider="openai_compatible",
                    model="fake-local",
                    base_url="http://localhost/v1",
                    api_key_required=False,
                )
            },
        )
    ).resolve(tmp_path / "demo")

    def fake_generate_chat_text(llm, messages, *, temperature=0.0, max_tokens=1200):
        return LLMChatResult(
            content=json.dumps(
                {
                    "hypotheses": [
                        {
                            "text": "unmapped alpha idea",
                            "rationale": "LLM requested parameter search instead of formula_dsl.",
                            "source": "parameter_search",
                            "parameter_search_fallback": True,
                        }
                    ]
                }
            ),
            provider="openai_compatible",
            model="fake-local",
        )

    monkeypatch.setattr(rd_llm, "generate_chat_text", fake_generate_chat_text)

    rd_config = _llm_rd_config(config)
    rd_config = replace(rd_config, parameter_search=replace(rd_config.parameter_search, enabled=True))

    result = run_research_once_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        rd_config=rd_config,
    )

    assert result["candidates"] == []
    assert result["blocked_plans"][0]["plan"]["status"] == "blocked_missing_formula"
    assert result["blocked_plans"][0]["error"] == "formula_dsl is missing"


def test_web_research_campaign_uses_shared_llm_planner(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="openai_compatible",
            providers={
                "openai_compatible": LLMProviderSettings(
                    provider="openai_compatible",
                    model="fake-local",
                    base_url="http://localhost/v1",
                    api_key_required=False,
                )
            },
        )
    ).resolve(tmp_path / "demo")

    def fake_generate_chat_text(llm, messages, *, temperature=0.0, max_tokens=1200):
        return LLMChatResult(
            content=json.dumps(
                {
                    "summary": "Prefer compact top5 first, then broad top20.",
                    "strategy_names": ["top5_equal", "top20_equal"],
                }
            ),
            provider="openai_compatible",
            model="fake-local",
        )

    monkeypatch.setattr(rd_llm, "generate_chat_text", fake_generate_chat_text)

    result = run_research_campaign_workflow(
        config,
        ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"],
        rounds=2,
        rd_config=_llm_rd_config(config),
    )

    assert result["optimizer"]["source"] == "llm_campaign_planner"
    assert result["optimizer"]["provider"] == "openai_compatible"
    assert result["optimizer"]["strategy_names"] == ["top5_equal", "top20_equal"]
    assert "raw_response" not in result["optimizer"]
    assert result["final_factor_id"]


def test_web_research_llm_mode_requires_configured_provider_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_MISSING_RD_KEY", raising=False)
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="https://api.deepseek.com",
                    api_key_env="QF_MISSING_RD_KEY",
                )
            },
        )
    ).resolve(tmp_path / "demo")

    with pytest.raises(RuntimeError, match="Missing API key"):
        run_research_once_workflow(
            config,
            "FTR_DEMO_SMALL_CAP",
            max_candidates=1,
            rd_config=_llm_rd_config(config),
        )


def test_cli_web_startup_does_not_validate_active_llm_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_MISSING_WEB_START_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: deepseek
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: QF_MISSING_WEB_START_KEY
""",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_local_web(*, host, port, config, rd_config):
        captured["host"] = host
        captured["port"] = port
        captured["provider"] = config.llm.provider
        captured["rd_objective"] = rd_config.objective

    monkeypatch.setattr(web_server, "run_local_web", fake_run_local_web)

    assert cli_main.main(["web", "--config", str(config_path), "--port", "8766"]) == 0
    assert captured == {
        "host": "127.0.0.1",
        "port": 8766,
        "provider": "deepseek",
        "rd_objective": "balanced",
    }


def test_web_workbench_uses_selected_llm_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/deepseek",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")
    captured: dict[str, str] = {}

    def fake_parse_factor_idea(text, llm, *, mode):
        captured["provider"] = llm.provider
        captured["model"] = llm.model
        factor = FactorDefinition(
            factor_id="FTR_LLM_PROVIDER",
            name="llm_provider",
            formula="-rank(market_cap)",
            horizon_days=5,
            source="llm",
        )
        return ParsedFactor(factor=factor, source="llm", provider=llm.provider, model=llm.model)

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)

    result = run_idea_workflow(config, "小市值", parser_mode="llm", llm_provider="glm")

    assert captured == {"provider": "glm", "model": "fake-glm"}
    assert result["parser"]["provider"] == "glm"


def test_web_html_keeps_active_llm_provider_visible_when_key_is_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_TEST_DEEPSEEK_KEY", raising=False)
    monkeypatch.setenv("QF_TEST_GLM_KEY", "set")
    config = QuantForgeConfig(
        llm=LLMSettings(
            provider="deepseek",
            providers={
                "deepseek": LLMProviderSettings(
                    provider="deepseek",
                    model="fake-deepseek",
                    base_url="http://localhost/deepseek",
                    api_key_env="QF_TEST_DEEPSEEK_KEY",
                ),
                "glm": LLMProviderSettings(
                    provider="glm",
                    model="fake-glm",
                    base_url="http://localhost/glm",
                    api_key_env="QF_TEST_GLM_KEY",
                ),
            },
        )
    ).resolve(tmp_path / "demo")

    html = web_server._index_html(config)

    assert "LLM parser: deepseek / fake-deepseek" in html
    assert "RD optimizer: research local deterministic" in html
    assert (
        '<option value="deepseek" selected>deepseek / fake-deepseek · missing env QF_TEST_DEEPSEEK_KEY</option>'
        in html
    )
    assert '<option value="glm">glm / fake-glm · env QF_TEST_GLM_KEY</option>' in html


def test_web_html_uses_existing_factor_as_default_rd_seed(tmp_path) -> None:
    factor_root = tmp_path / "factor_root"
    web_server.FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="FTR_CUSTOM_SEED",
            name="custom_seed",
            formula="rank(return_5d)",
            source="test",
        )
    )
    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=tmp_path / "data",
            factor_root=factor_root,
            artifact_root=tmp_path / "artifacts",
        )
    )

    html = web_server._index_html(config)

    assert 'id="rd-seed" value="FTR_CUSTOM_SEED"' in html


def test_web_html_prefers_repo_level_top20_audit_seed_source_for_campaign_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    repo_audit_path = (
        tmp_path / "artifacts" / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_DIR / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_FILE
    )
    repo_audit_path.parent.mkdir(parents=True, exist_ok=True)
    repo_audit_path.write_text(
        json.dumps(
            [
                {"rank": 2, "factor_id": "FTR_CATALOG_SECOND"},
                {"rank": 1, "factor_id": "FTR_AUDIT_FIRST"},
            ]
        ),
        encoding="utf-8",
    )
    artifact_root = tmp_path / "runtime_artifacts"
    fallback_audit_path = (
        artifact_root / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_DIR / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_FILE
    )
    fallback_audit_path.parent.mkdir(parents=True, exist_ok=True)
    fallback_audit_path.write_text(json.dumps(["FTR_FALLBACK_ONLY"]), encoding="utf-8")
    factor_root = tmp_path / "factor_root"
    web_server.FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="FTR_CATALOG_FIRST",
            name="catalog_first",
            formula="rank(return_5d)",
            source="test",
        )
    )
    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=tmp_path / "data",
            factor_root=factor_root,
            artifact_root=artifact_root,
        )
    )
    rd_config = load_research_loop_config(None, config.research, config.simulation)

    html = web_server._index_html(config, rd_config)

    assert (
        'id="rd-campaign-seed-source-path" type="hidden" value="'
        + str(repo_audit_path.resolve(strict=False))
        + '"'
    ) in html
    assert "seed source: Top20 audit JSON (rank order)" in html
    assert "FTR_AUDIT_FIRST\nFTR_CATALOG_SECOND" in html
    assert "FTR_CATALOG_FIRST" not in html.split('id="rd-campaign-seeds"', 1)[1].split("</textarea>", 1)[0]
    assert "FTR_FALLBACK_ONLY" not in html


def test_web_html_falls_back_to_artifact_root_top20_audit_seed_source_when_repo_level_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact_root = Path("runtime_artifacts")
    audit_path = (
        tmp_path / "runtime_artifacts" / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_DIR / web_server.DEFAULT_CAMPAIGN_SEED_AUDIT_FILE
    )
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            [
                {"rank": 2, "factor_id": "FTR_FALLBACK_SECOND"},
                {"rank": 1, "factor_id": "FTR_FALLBACK_FIRST"},
            ]
        ),
        encoding="utf-8",
    )
    factor_root = tmp_path / "factor_root"
    web_server.FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="FTR_CATALOG_FIRST",
            name="catalog_first",
            formula="rank(return_5d)",
            source="test",
        )
    )
    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=tmp_path / "data",
            factor_root=factor_root,
            artifact_root=artifact_root,
        )
    )
    rd_config = load_research_loop_config(None, config.research, config.simulation)

    html = web_server._index_html(config, rd_config)

    assert (
        'id="rd-campaign-seed-source-path" type="hidden" value="'
        + str(audit_path.resolve(strict=False))
        + '"'
    ) in html
    assert "seed source: Top20 audit JSON (rank order)" in html
    assert "FTR_FALLBACK_FIRST\nFTR_FALLBACK_SECOND" in html
    assert "FTR_CATALOG_FIRST" not in html.split('id="rd-campaign-seeds"', 1)[1].split("</textarea>", 1)[0]


def test_web_html_does_not_fake_rd_seed_when_factor_root_empty(tmp_path) -> None:
    factor_root = tmp_path / "factor_root"
    factor_root.mkdir()
    config = QuantForgeConfig(
        paths=PathSettings(
            data_root=tmp_path / "data",
            factor_root=factor_root,
            artifact_root=tmp_path / "artifacts",
        )
    )

    html = web_server._index_html(config)

    assert 'id="rd-seed" value="FTR_DEMO_SMALL_CAP"' not in html
    assert 'placeholder="先创建或配置一个因子"' in html


def test_web_research_cards_include_cache_paths_and_artifacts() -> None:
    html = web_server._index_html(QuantForgeConfig())

    assert "factor_values:" in html
    assert "artifacts:" in html


def test_web_campaign_workflow_marks_partial_errors_as_warning(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    warning_result = ResearchCampaignResult(
        rd_stage="factor_synthesis",
        seed_factor_ids=("FTR_DEMO_SMALL_CAP",),
        objective="balanced",
        rounds_requested=1,
        rounds_completed=1,
        round_results=(
            ResearchCampaignRoundResult(
                round_index=1,
                input_seed_factor_ids=("FTR_DEMO_SMALL_CAP",),
                candidates=(),
                selected_factor_ids=("FTR_DEMO_SMALL_CAP",),
                errors=("round 1 seed FTR_DEMO_SMALL_CAP: partial failure",),
            ),
        ),
        final_factor_id="FTR_DEMO_SMALL_CAP",
        final_factor=FactorDefinition(
            factor_id="FTR_DEMO_SMALL_CAP",
            name="demo_small_cap",
            formula="-rank(market_cap)",
            source="research_campaign",
        ),
        final_evaluation=EvaluationResult(
            factor_id="FTR_DEMO_SMALL_CAP",
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.1,
            rank_ic_std=0.0,
            rank_icir=1.0,
            ic_days=1,
            artifact_path=tmp_path / "eval.json",
            simulation_profile=SimulationProfile(test_period_start="2025-01-01", test_period_end="2025-12-31"),
        ),
        final_backtest=BacktestResult(
            factor_id="FTR_DEMO_SMALL_CAP",
            periods=1,
            holding_days=5,
            cumulative_return=0.01,
            annualized_return=0.01,
            annualized_volatility=0.0,
            max_drawdown=0.0,
            artifact_path=tmp_path / "backtest.json",
            simulation_profile=SimulationProfile(test_period_start="2025-01-01", test_period_end="2025-12-31"),
            net_annualized_return=0.01,
        ),
        final_score=1.23,
        artifacts=(),
        errors=("round 1 seed FTR_DEMO_SMALL_CAP: partial failure",),
    )

    def fake_run_research_campaign(*args, **kwargs):
        return warning_result

    monkeypatch.setattr(web_server, "_run_research_campaign", fake_run_research_campaign)

    payload = run_research_campaign_workflow(config, ["FTR_DEMO_SMALL_CAP"], rounds=1)

    assert payload["status"] == "warning"
    assert payload["status_label"] == "Campaign 完成，但存在 partial errors"
    assert payload["errors"] == ["round 1 seed FTR_DEMO_SMALL_CAP: partial failure"]


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _write_precomputed_seed(
    factor_values_root: Path,
    panel: pd.DataFrame,
    *,
    factor_id: str,
    factor_name: str,
    scores: pd.Series,
) -> None:
    factor_dir = factor_values_root / f"factor_id={factor_id}"
    factor_dir.mkdir(parents=True, exist_ok=True)
    (factor_dir / "2024.metadata.json").write_text(
        json.dumps(
            {
                "factor_id": factor_id,
                "factor_name": factor_name,
                "factor_store_key": f"factor_id={factor_id}",
                "schema_version": "qf.canonical_factor_values.v1",
            }
        ),
        encoding="utf-8",
    )
    payload = panel[["trade_date", "instrument"]].copy()
    payload["factor_id"] = factor_id
    payload["factor_value"] = pd.to_numeric(scores, errors="coerce")
    payload["trade_date"] = pd.to_datetime(payload["trade_date"]).dt.strftime("%Y-%m-%d")
    payload[["trade_date", "instrument", "factor_id", "factor_value"]].to_parquet(
        factor_dir / "2024.parquet",
        index=False,
    )


def _rewrite_demo_panel_to_2025(data_root: Path) -> None:
    panel_path = data_root / PANEL_FILE
    panel = pd.read_parquet(panel_path)
    trade_dates = pd.to_datetime(panel["trade_date"])
    panel["trade_date"] = trade_dates + pd.offsets.DateOffset(years=1)
    panel.to_parquet(panel_path, index=False)


def _local_rd_config(config: QuantForgeConfig):
    return replace(
        load_research_loop_config(Path("configs/rd.yaml"), config.research, config.simulation),
        llm=ResearchLLMConfig(hypothesis_mode="local", review_mode="local", campaign_mode="local"),
    )


def _llm_rd_config(config: QuantForgeConfig):
    return replace(
        load_research_loop_config(Path("configs/rd.yaml"), config.research, config.simulation),
        llm=ResearchLLMConfig(hypothesis_mode="llm", review_mode="llm", campaign_mode="llm"),
    )

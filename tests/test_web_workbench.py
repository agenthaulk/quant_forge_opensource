from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.cli.main as cli_main
import quant_forge.apps.web.server as web_server
import quant_forge.research_loop.llm as rd_llm
from quant_forge.apps.web.server import (
    create_local_web_server,
    run_idea_workflow,
    run_research_once_workflow,
)
from quant_forge.config import LLMProviderSettings, LLMSettings, PathSettings, QuantForgeConfig
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition
from quant_forge.data.local import create_demo_workspace
from quant_forge.llm_client import LLMChatResult
from quant_forge.llm_factor_parser import ParsedFactor
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
    assert "/api/research/campaign" not in html
    assert "/api/research/schedule" in html
    assert "LLM 语义解析" in html
    assert "本地规则解析" in html
    assert "是否改用本地规则解析" in html
    assert "rd-objective" in html
    assert "rd-campaign-seeds" not in html
    assert "rd-campaign" not in html
    assert "RD Campaign" not in html
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


def test_web_research_unknown_endpoint_returns_404(tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_config = _local_rd_config(config)
    server = create_local_web_server(host="127.0.0.1", port=0, config=config, rd_config=rd_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        try:
            _post_json(
                f"{base_url}/api/research/not-supported",
                {"seed_factor_id": "FTR_DEMO_SMALL_CAP"},
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            body = json.loads(exc.read().decode("utf-8"))
            assert "unknown endpoint" in body["error"]
        else:
            raise AssertionError("unknown endpoint should return HTTP 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


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
    assert status["rd"]["research_stage"] == "research"
    assert "synthesis_stage" not in status["rd"]
    assert "campaign_mode" not in status["rd"]
    assert "synthesis_campaign_mode" not in status["rd"]
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


def _local_rd_config(config: QuantForgeConfig):
    return replace(
        load_research_loop_config(Path("configs/rd.yaml"), config.research, config.simulation),
        llm=ResearchLLMConfig(hypothesis_mode="local", review_mode="local"),
    )


def _llm_rd_config(config: QuantForgeConfig):
    return replace(
        load_research_loop_config(Path("configs/rd.yaml"), config.research, config.simulation),
        llm=ResearchLLMConfig(hypothesis_mode="llm", review_mode="llm"),
    )

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from quant_forge.config import LLMSettings
from quant_forge.factor_library.repository import parse_idea_to_definition
from quant_forge.llm_factor_parser import GENERIC_FALLBACK_WARNING, _factor_from_llm_json, parse_factor_idea
from quant_forge.specs import nl_flow


class FakeLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        assert self.headers["Authorization"] == "Bearer test-key"
        assert request["model"] == "fake-deepseek"
        content = {
            "name": "small_cap_non_st",
            "formula": "-rank(market_cap)",
            "description": "Small non-ST stocks receive higher scores.",
            "horizon_days": 5,
            "universe_filters": ["is_st == false"],
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


class FakeOpenAICompatibleHandler(BaseHTTPRequestHandler):
    expected_path = "/v1/chat/completions"
    expected_model = "fake-openai"

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        assert self.path == self.expected_path
        assert self.headers["Authorization"] == "Bearer test-key"
        assert request["model"] == self.expected_model
        assert request["messages"][0]["role"] == "system"
        content = {
            "name": "close_strength",
            "formula": "rank(close)",
            "description": "Close-price strength.",
            "horizon_days": 7,
            "universe_filters": [],
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


class FakeNoAuthOpenAICompatibleHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        assert self.path == "/v1/chat/completions"
        assert self.headers.get("Authorization") is None
        assert request["model"] == "fake-local"
        content = {
            "name": "local_close_strength",
            "formula": "rank(close)",
            "description": "Close-price strength from a local no-auth endpoint.",
            "horizon_days": 5,
            "universe_filters": [],
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


class FakeClaudeHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        assert self.path == "/v1/messages"
        assert self.headers["x-api-key"] == "test-key"
        assert self.headers["anthropic-version"] == "2023-06-01"
        assert request["model"] == "fake-claude"
        assert "You convert Chinese or English factor ideas" in request["system"]
        assert request["messages"][0]["role"] == "user"
        content = {
            "name": "low_volatility",
            "formula": "-rank(volatility_5d)",
            "description": "Lower short-term volatility receives higher scores.",
            "horizon_days": 5,
            "universe_filters": [],
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"content": [{"type": "text", "text": json.dumps(content)}]}).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


def test_deepseek_parser_reads_openai_compatible_response(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLMHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("QF_TEST_DEEPSEEK_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "非ST的小市值股票未来表现更好",
            LLMSettings(
                provider="deepseek",
                model="fake-deepseek",
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key_env="QF_TEST_DEEPSEEK_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.source == "llm"
    assert parsed.factor.formula == "-rank(market_cap)"
    assert parsed.factor.universe_filters == ("is_st == false",)


def test_llm_factor_json_canonicalizes_safe_alias() -> None:
    factor = _factor_from_llm_json(
        {
            "name": "alias_stddev",
            "formula": "rank(-ts_stddev(return_1d, 20))",
            "description": "alias test",
            "horizon_days": 5,
            "universe_filters": [],
        },
        "alias stddev",
    )

    assert factor.formula == "rank(-stddev(return_1d, 20))"


def test_llm_factor_json_rejects_likely_alias_and_draft_operator() -> None:
    with pytest.raises(RuntimeError, match="operator registry gate"):
        _factor_from_llm_json(
            {
                "name": "rolling_std",
                "formula": "rolling_std(return_1d, 20)",
                "description": "ambiguous alias",
                "horizon_days": 5,
                "universe_filters": [],
            },
            "rolling std",
        )

    with pytest.raises(RuntimeError, match="operator registry gate"):
        _factor_from_llm_json(
            {
                "name": "industry_neutral",
                "formula": "industry_neutralize(rank(return_1d), industry)",
                "description": "draft operator",
                "horizon_days": 5,
                "universe_filters": [],
            },
            "industry neutral",
        )


def test_openai_parser_reads_provider_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAICompatibleHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "收盘价强势",
            LLMSettings(
                provider="openai",
                model="fake-openai",
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key_env="OPENAI_API_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.provider == "openai"
    assert parsed.model == "fake-openai"
    assert parsed.factor.formula == "rank(close)"


def test_openai_compatible_parser_allows_no_auth_local_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeNoAuthOpenAICompatibleHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    try:
        parsed = parse_factor_idea(
            "收盘价强势",
            LLMSettings(
                provider="openai_compatible",
                model="fake-local",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key_required=False,
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.provider == "openai_compatible"
    assert parsed.factor.formula == "rank(close)"


def test_glm_parser_uses_v4_chat_completion_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeGLMHandler(FakeOpenAICompatibleHandler):
        expected_path = "/api/paas/v4/chat/completions"
        expected_model = "fake-glm"

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGLMHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "收盘价强势",
            LLMSettings(
                provider="glm",
                model="fake-glm",
                base_url=f"http://127.0.0.1:{server.server_port}/api/paas/v4",
                api_key_env="GLM_API_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.provider == "glm"
    assert parsed.factor.formula == "rank(close)"


def test_minimax_parser_uses_openai_compatible_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMiniMaxHandler(FakeOpenAICompatibleHandler):
        expected_model = "fake-minimax"

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMiniMaxHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "收盘价强势",
            LLMSettings(
                provider="minimax",
                model="fake-minimax",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key_env="MINIMAX_API_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.provider == "minimax"
    assert parsed.factor.formula == "rank(close)"


def test_claude_parser_reads_anthropic_messages_response(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClaudeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "低波动股票",
            LLMSettings(
                provider="claude",
                model="fake-claude",
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key_env="ANTHROPIC_API_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.provider == "claude"
    assert parsed.factor.formula == "-rank(volatility_5d)"


def test_openai_parser_requires_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(ValueError, match=r"llm\.providers\.openai\.model"):
        parse_factor_idea(
            "收盘价强势",
            LLMSettings(
                provider="openai",
                base_url="https://api.openai.com/v1",
                api_key_env="OPENAI_API_KEY",
            ),
        )


def test_deepseek_parser_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QF_TEST_DEEPSEEK_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Missing API key"):
        parse_factor_idea(
            "非ST的小市值股票未来表现更好",
            LLMSettings(
                provider="deepseek",
                model="fake-deepseek",
                base_url="https://api.deepseek.com",
                api_key_env="QF_TEST_DEEPSEEK_KEY",
            ),
        )


def test_llm_mode_rejects_rule_provider_without_silent_fallback() -> None:
    with pytest.raises(RuntimeError, match="local rule parser"):
        parse_factor_idea("小市值股票", LLMSettings(provider="rule"), mode="llm")


def test_llm_factor_identity_includes_horizon() -> None:
    base = {
        "name": "small_cap_non_st",
        "formula": "-rank(market_cap)",
        "description": "Small non-ST stocks receive higher scores.",
        "universe_filters": ["is_st == false"],
    }

    five_day = _factor_from_llm_json({**base, "horizon_days": 5}, "非ST的小市值股票未来表现更好")
    twenty_day = _factor_from_llm_json({**base, "horizon_days": 20}, "非ST的小市值股票未来表现更好")

    assert five_day.factor_id != twenty_day.factor_id


def test_llm_month_horizon_normalizes_to_trading_days() -> None:
    payload = {
        "name": "small_cap_non_st",
        "formula": "-rank(market_cap)",
        "description": "Small non-ST stocks receive higher scores.",
        "horizon_days": 30,
        "universe_filters": ["is_st == false"],
    }

    factor = _factor_from_llm_json(payload, "非ST的小市值股票在未来一个月表现更好")

    assert factor.horizon_days == 21


def test_llm_explicit_day_horizon_is_preserved() -> None:
    payload = {
        "name": "small_cap_non_st",
        "formula": "-rank(market_cap)",
        "description": "Small non-ST stocks receive higher scores.",
        "horizon_days": 30,
        "universe_filters": ["is_st == false"],
    }

    factor = _factor_from_llm_json(payload, "非ST的小市值股票在未来30天表现更好")

    assert factor.horizon_days == 30


def test_rule_parser_supports_volume_strength() -> None:
    factor = parse_idea_to_definition("非ST且成交量更高的股票未来表现更好")

    assert factor.formula == "rank(volume)"
    assert factor.name == "volume_strength"
    assert factor.universe_filters == ("is_st == false",)


def test_rule_mode_generic_fallback_carries_shared_warning_contract() -> None:
    # F-010 no-silent-fallback: unrecognized text lands on the generic
    # catch-all formula, and the parse result must say so.
    parsed = parse_factor_idea(
        "今天天气很好，和因子研究无关的一句话。",
        LLMSettings(provider="rule"),
        mode="rule",
    )

    assert parsed.factor.formula == "rank(close)"
    assert parsed.warnings == (GENERIC_FALLBACK_WARNING,)
    # Single definition: the parser reuses the nl_flow warning contract
    # verbatim instead of maintaining a parallel copy.
    assert GENERIC_FALLBACK_WARNING == nl_flow._FALLBACK_WARNING
    assert "generic fallback formula rank(close)" in GENERIC_FALLBACK_WARNING
    assert "review before running" in GENERIC_FALLBACK_WARNING


def test_rule_mode_recognized_idea_carries_no_fallback_warning() -> None:
    parsed = parse_factor_idea(
        "非ST的小市值股票未来表现更好",
        LLMSettings(provider="rule"),
        mode="rule",
    )

    assert parsed.factor.formula == "-rank(market_cap)"
    assert parsed.warnings == ()


def test_llm_mode_generic_formula_carries_identical_fallback_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # The LLM path is covered identically to the rule path: a rank(close)
    # answer cannot be distinguished from a guess, so it is flagged for review.
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAICompatibleHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "收盘价强势",
            LLMSettings(
                provider="openai",
                model="fake-openai",
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key_env="OPENAI_API_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.factor.formula == "rank(close)"
    assert parsed.warnings == (GENERIC_FALLBACK_WARNING,)


class FakeMomentumHandler(BaseHTTPRequestHandler):
    """Mimics the real degradation: an unmappable fundamentals idea comes back
    as a confident-looking momentum formula (the workbench prompt only allows a
    handful of price-volume formulas, so the model picks the nearest one)."""

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        json.loads(self.rfile.read(length).decode("utf-8"))
        content = {
            "name": "profit_growth_momentum",
            "formula": "rank(return_5d)",
            "description": "年报、季报利润同比增加高的公司，在未来21天的表现会更好",
            "horizon_days": 21,
            "universe_filters": [],
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        return


def test_fundamentals_idea_no_longer_flagged_out_of_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    # Since fundamentals are now supported (netprofit_yoy et al.), the earlier
    # reported profit-growth idea must NOT carry the out-of-scope warning any
    # more -- the domain is expressible. (Real DeepSeek would return
    # rank(netprofit_yoy); the fake here still returns rank(return_5d), but the
    # point of this regression guard is only that no out-of-scope warning fires.)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMomentumHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("QF_TEST_DEEPSEEK_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "年报、季报利润同比增加高的公司，在未来21天的表现会更好",
            LLMSettings(
                provider="deepseek",
                model="fake-deepseek",
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key_env="QF_TEST_DEEPSEEK_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.warnings == (), "fundamentals are supported -> no out-of-scope warning"


def test_out_of_scope_detector_flags_only_alt_data() -> None:
    from quant_forge.specs.nl_flow import out_of_scope_data_warnings

    # Genuinely-unavailable alternative data + unexposed line items still warn.
    for idea in (
        "根据舆情和研报情绪选股",
        "北向资金持续流入的股票",
        "龙虎榜机构净买入的股票",
        "商誉占比低、存货周转快的公司",  # no exposed goodwill/inventory field
        "high analyst sentiment names",
        "stocks with strong northbound flow",
        "low goodwill and lean inventory names",
    ):
        assert out_of_scope_data_warnings(idea), idea

    # Now-supported fundamentals/valuation ideas, in-scope price-volume ideas,
    # and the demo seeds do NOT warn; short English terms never match inside
    # unrelated words (approach/broad).
    for idea in (
        "年报、季报利润同比增加高的公司未来更好",  # now supported (netprofit_yoy)
        "现金流充裕、负债率低的公司",              # now supported (n_cashflow_act/debt_to_assets)
        "市盈率低、ROE 高的股票",                   # now supported (pe_ttm/roe)
        "stocks with rising earnings and revenue",  # now supported
        "非ST的小市值股票未来表现更好",
        "过去5天涨幅较大的股票，短期动量继续",
        "低波动率的股票更稳",
        "估值越低的股票，长期收益越好",
        "approach the broad market with high volume",
    ):
        assert not out_of_scope_data_warnings(idea), idea


def test_llm_mode_specific_formula_carries_no_fallback_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLMHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("QF_TEST_DEEPSEEK_KEY", "test-key")
    try:
        parsed = parse_factor_idea(
            "非ST的小市值股票未来表现更好",
            LLMSettings(
                provider="deepseek",
                model="fake-deepseek",
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key_env="QF_TEST_DEEPSEEK_KEY",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert parsed.factor.formula == "-rank(market_cap)"
    assert parsed.warnings == ()

"""CP7-H input-shape limits: provider error bodies, factor free text, horizon bound.

Covers review items P3 (llm_client error-body cap), P4 (parser/web factor
free-text caps + draft-only idea validation), and the contracts half of P5
(horizon_days upper bound). See docs/reviews/untrusted_text_dataflow_review.md.
"""

from __future__ import annotations

from io import BytesIO
import urllib.error

import pytest

import quant_forge.llm_client as llm_client
from quant_forge.apps.web.api import _factor_from_request
from quant_forge.config import LLMSettings
from quant_forge.core.contracts import FactorDefinition
from quant_forge.llm_client import generate_chat_text
from quant_forge.llm_factor_parser import _factor_from_llm_json

HTTP_ERROR_BODY_CAP = 120
DESCRIPTION_CAP = 500
UNIVERSE_FILTER_CAP = 120
HORIZON_DAYS_CAP = 750


# ---------------------------------------------------------------------------
# P3: provider HTTP error bodies are capped, single-line extracts
# ---------------------------------------------------------------------------


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.example.invalid/v1/chat/completions",
        code=code,
        msg="error",
        hdrs={},
        fp=BytesIO(body),
    )


def test_provider_http_error_body_is_capped_single_line(monkeypatch) -> None:
    monkeypatch.setenv("QF_TEST_LLM_API_KEY", "not-a-real-key")
    body = ("detail line one\nUPSTREAM_MARKER line two\n" + "x" * 900).encode("utf-8")

    def fake_urlopen(_request, *, timeout):
        raise _http_error(400, body)

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        generate_chat_text(
            LLMSettings(
                provider="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key_env="QF_TEST_LLM_API_KEY",
                timeout_seconds=1.0,
            ),
            [{"role": "user", "content": "hello"}],
        )

    message = str(excinfo.value)
    prefix = "LLM request failed with HTTP 400: "
    assert message.startswith(prefix)
    assert "\n" not in message
    assert "\r" not in message
    assert len(message) <= len(prefix) + HTTP_ERROR_BODY_CAP
    # Only a short leading extract survives; the 900-char tail must not.
    assert "x" * 200 not in message


# ---------------------------------------------------------------------------
# P4: LLM-parser factor free text is capped and control-character free
# ---------------------------------------------------------------------------


def _parser_payload(**overrides) -> dict:
    payload = {
        "name": "small_cap_non_st",
        "formula": "-rank(market_cap)",
        "description": "Small non-ST stocks receive higher scores.",
        "horizon_days": 5,
        "universe_filters": ["is_st == false"],
    }
    payload.update(overrides)
    return payload


def test_llm_parser_caps_description_and_universe_filters() -> None:
    long_description = "line one\nline two\x07 " + "d" * 600
    long_filter = "is_st == false" + " AND " + "f" * 300
    factor = _factor_from_llm_json(
        _parser_payload(description=long_description, universe_filters=["is_st == false", long_filter]),
        "非ST的小市值股票未来表现更好",
    )

    assert len(factor.description) <= DESCRIPTION_CAP
    assert "\n" not in factor.description
    assert "\x07" not in factor.description
    assert factor.description.startswith("line one line two")
    assert factor.universe_filters[0] == "is_st == false"
    assert len(factor.universe_filters[1]) <= UNIVERSE_FILTER_CAP


def test_llm_parser_keeps_legitimate_description_and_filters_unchanged() -> None:
    factor = _factor_from_llm_json(_parser_payload(), "非ST的小市值股票未来表现更好")

    assert factor.description == "Small non-ST stocks receive higher scores."
    assert factor.universe_filters == ("is_st == false",)


# ---------------------------------------------------------------------------
# P4: web-path factor JSON is slugged/capped and draft-only
# ---------------------------------------------------------------------------


def _web_factor_payload(**overrides) -> dict:
    payload = {
        "factor_id": "FTR_WEB_SHAPE_1",
        "name": "small_cap_non_st",
        "formula": "-rank(market_cap)",
        "status": "draft",
        "description": "Small non-ST stocks receive higher scores.",
        "horizon_days": 5,
        "universe_filters": ["is_st == false"],
    }
    payload.update(overrides)
    return payload


def test_web_factor_request_slugs_name_and_caps_free_text() -> None:
    factor = _factor_from_request(
        _web_factor_payload(
            name="Weird Name!! 有中文",
            description="web\nline\x00two " + "w" * 600,
            universe_filters=["is_st == false", "u" * 300 + "\n tail"],
        )
    )

    assert factor.name == "weird_name"
    assert len(factor.description) <= DESCRIPTION_CAP
    assert "\n" not in factor.description
    assert "\x00" not in factor.description
    assert len(factor.universe_filters[1]) <= UNIVERSE_FILTER_CAP
    assert "\n" not in factor.universe_filters[1]


def test_web_factor_request_accepts_only_draft_status() -> None:
    draft = _factor_from_request(_web_factor_payload(status="draft"))
    assert draft.status == "draft"

    for status in ("active", "candidate", "inactive", "archived"):
        with pytest.raises(ValueError, match="draft"):
            _factor_from_request(_web_factor_payload(status=status))


def test_web_factor_request_passes_factor_definition_through_unchanged() -> None:
    factor = FactorDefinition(
        factor_id="FTR_PASSTHROUGH_1",
        name="Existing_Name",
        formula="rank(close)",
        status="candidate",
        description="existing description",
        horizon_days=5,
    )

    assert _factor_from_request(factor) is factor


# ---------------------------------------------------------------------------
# P5 (contracts half): horizon_days upper bound
# ---------------------------------------------------------------------------


def test_horizon_days_boundary_accepted_and_above_rejected() -> None:
    accepted = FactorDefinition(
        factor_id="FTR_HORIZON_OK",
        name="horizon_ok",
        formula="rank(close)",
        horizon_days=HORIZON_DAYS_CAP,
    )
    assert accepted.horizon_days == HORIZON_DAYS_CAP

    with pytest.raises(ValueError, match="horizon_days"):
        FactorDefinition(
            factor_id="FTR_HORIZON_TOO_BIG",
            name="horizon_too_big",
            formula="rank(close)",
            horizon_days=HORIZON_DAYS_CAP + 1,
        )


def test_existing_legitimate_horizons_unaffected() -> None:
    for horizon in (1, 5, 21, 63, 140):
        factor = FactorDefinition(
            factor_id=f"FTR_HORIZON_{horizon}",
            name=f"horizon_{horizon}",
            formula="rank(close)",
            horizon_days=horizon,
        )
        assert factor.horizon_days == horizon

"""Contract tests for the P1 synthesis method catalog endpoint + schema layer.

Covers ``GET /api/synthesis/methods`` and ``quant_forge.synthesis.methods``:

- the catalog payload equals the design §9 literal JSON — the post-P6 end
  state: ALL FOUR methods ``available: true`` now that the fitted
  implementation (point-in-time IC/ICIR, §4.4) has landed, with
  ``is_fitted`` truthful per method nature;
- generic ParamSpec discipline over the wire payload: every ``params[]``
  entry carries only declared ParamSpec fields with a renderable ``type``
  (the frontend form is schema-driven with zero per-method hardcoding),
  defaults respect declared bounds, and ``standardizations`` is exactly
  ``zscore`` + ``rank``;
- the route requires the control token when the binding is gated;
- routing dispatches the builder late-bound through the server module
  namespace (the monkeypatch seam contract shared by all GET builders);
- ``validate_params_against_schema`` unit coverage: required/missing,
  unknown names, per-type checks (int/float/bool/enum), minimum/maximum
  bounds, and structural weights-map validation;
- ``apply_param_defaults`` unit coverage: declared defaults fill absent
  optional params, explicit values always win, and the input is never
  mutated.
"""

from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig, WebSettings
from quant_forge.data.local import create_demo_workspace
from quant_forge.synthesis.methods import (
    PARAM_TYPES,
    ParamSpec,
    apply_param_defaults,
    method_catalog_payload,
    validate_params_against_schema,
)


JSON_CONTENT_TYPE = "application/json; charset=utf-8"

# Design §9 literal payload — the post-P6 end state: every name, label,
# param, help string, AND `available: true` on all four methods, verbatim.
EXPECTED_CATALOG = {
    "methods": [
        {
            "name": "equal_weight",
            "label": "等权合成",
            "available": True,
            "required_standardization": False,
            "is_fitted": False,
            "params": [],
        },
        {
            "name": "weighted",
            "label": "先验加权合成",
            "available": True,
            "required_standardization": False,
            "is_fitted": False,
            "params": [
                {
                    "name": "weights",
                    "label": "各因子权重",
                    "type": "weights",
                    "required": True,
                    "help": "为每个已选因子提供一个先验权重；原样回显，不归一化展示。",
                }
            ],
        },
        {
            "name": "ic_weighted",
            "label": "IC 加权合成（拟合）",
            "available": True,
            "required_standardization": False,
            "is_fitted": True,
            "params": [
                {
                    "name": "ic_min_periods",
                    "label": "IC 最小拟合期数",
                    "type": "int",
                    "required": False,
                    "default": 6,
                    "minimum": 3,
                    "maximum": 60,
                    "help": (
                        "点位时序拟合的最小已实现期数；不足则该期退化为等权并标注；"
                        "窗口内无任一可拟合期则整体退化为等权且 is_fitted=false（NO_FITTED_PERIODS）。"
                    ),
                }
            ],
        },
        {
            "name": "icir_weighted",
            "label": "ICIR 加权合成（拟合）",
            "available": True,
            "required_standardization": False,
            "is_fitted": True,
            "params": [
                {
                    "name": "ic_min_periods",
                    "label": "ICIR 最小拟合期数",
                    "type": "int",
                    "required": False,
                    "default": 6,
                    "minimum": 3,
                    "maximum": 60,
                    "help": (
                        "以 IC 均值/IC 标准差作为权重；窗口内仅用已实现的前向收益，杜绝前视；"
                        "IC 标准差为 0 或权重非有限时该期退化为等权。"
                    ),
                }
            ],
        },
    ],
    "standardizations": [
        {"name": "zscore", "label": "截面 Z-Score（按日）"},
        {"name": "rank", "label": "截面排序标准化（按日）"},
    ],
}

METHOD_KEYS = {"name", "label", "available", "required_standardization", "is_fitted", "params"}
PARAM_SPEC_KEYS = {
    "name",
    "label",
    "type",
    "required",
    "default",
    "minimum",
    "maximum",
    "choices",
    "help",
}
PARAM_SPEC_ALWAYS_KEYS = {"name", "label", "type", "required", "help"}


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


@pytest.fixture()
def web_app(web_config):
    server = create_local_web_server(host="127.0.0.1", port=0, config=web_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


# ---------------------------------------------------------------------------
# GET /api/synthesis/methods — §9 contract (CP0 interim availability)
# ---------------------------------------------------------------------------


def test_synthesis_methods_payload_matches_design_catalog(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/synthesis/methods")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload == EXPECTED_CATALOG


def test_synthesis_methods_paramspec_discipline_is_generic(web_app) -> None:
    """Schema rules the frontend relies on, asserted without §9 hardcoding.

    ``renderParamSpecInputHtml`` renders purely from ``params[]``; these
    invariants must hold for ANY future catalog entry, so they are checked
    generically over whatever the endpoint returns.
    """

    status, _, body = _get(f"{web_app}/api/synthesis/methods")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"methods", "standardizations"}

    methods = payload["methods"]
    assert methods, "catalog must declare at least one method"
    for method in methods:
        assert set(method) == METHOD_KEYS, method["name"]
        assert isinstance(method["available"], bool)
        assert isinstance(method["is_fitted"], bool)
        pinned = method["required_standardization"]
        assert pinned is False or (isinstance(pinned, str) and pinned)
        for spec in method["params"]:
            assert PARAM_SPEC_ALWAYS_KEYS <= set(spec) <= PARAM_SPEC_KEYS, spec["name"]
            assert spec["type"] in PARAM_TYPES
            assert isinstance(spec["required"], bool)
            if spec["type"] == "enum":
                assert spec.get("choices"), spec["name"]
            if spec.get("default") is not None and spec["type"] in {"int", "float"}:
                if spec.get("minimum") is not None:
                    assert spec["default"] >= spec["minimum"], spec["name"]
                if spec.get("maximum") is not None:
                    assert spec["default"] <= spec["maximum"], spec["name"]

    by_name = {method["name"]: method for method in methods}
    # A-priori methods are runnable now and never claim fitting.
    for name in ("equal_weight", "weighted"):
        assert by_name[name]["available"] is True
        assert by_name[name]["is_fitted"] is False
    # The weighted method advertises its weights mapping to the dynamic form.
    weighted_params = {spec["name"]: spec for spec in by_name["weighted"]["params"]}
    assert weighted_params["weights"]["type"] == "weights"
    assert weighted_params["weights"]["required"] is True
    # Fitted methods are runnable post-P6 and truthfully claim their nature;
    # the RUN-level is_fitted can still downgrade (NO_FITTED_PERIODS, RB-8).
    for name in ("ic_weighted", "icir_weighted"):
        assert by_name[name]["available"] is True
        assert by_name[name]["is_fitted"] is True
        params = {spec["name"]: spec for spec in by_name[name]["params"]}
        assert params["ic_min_periods"]["type"] == "int"
        assert params["ic_min_periods"]["default"] == 6
        assert params["ic_min_periods"]["minimum"] == 3
        assert params["ic_min_periods"]["maximum"] == 60

    standardizations = payload["standardizations"]
    assert [entry["name"] for entry in standardizations] == ["zscore", "rank"]
    for entry in standardizations:
        assert set(entry) == {"name", "label"}
        assert entry["label"].strip()


def test_synthesis_methods_requires_control_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_WEB_TOKEN", "token-for-tests")
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig(
        web=WebSettings(allow_docker_bind=True, control_token_env="QF_TEST_WEB_TOKEN")
    ).resolve(tmp_path / "demo")
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, content_type, body = _get(f"{base_url}/api/synthesis/methods")
        assert status == 401
        assert content_type == JSON_CONTENT_TYPE
        assert json.loads(body.decode("utf-8")) == {"error": "unauthorized"}

        status, _, body = _get(
            f"{base_url}/api/synthesis/methods",
            headers={"Authorization": "Bearer token-for-tests"},
        )
        assert status == 200
        assert json.loads(body.decode("utf-8")) == EXPECTED_CATALOG
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_routing_dispatches_synthesis_methods_via_server_namespace(monkeypatch, web_app) -> None:
    seen: dict[str, object] = {}

    def fake_synthesis_methods(config):
        seen["synthesis_methods"] = True
        return {"echo": "synthesis-methods"}

    monkeypatch.setattr(web_server, "_synthesis_methods_payload", fake_synthesis_methods)

    status, _, body = _get(f"{web_app}/api/synthesis/methods")

    assert status == 200
    assert json.loads(body.decode("utf-8")) == {"echo": "synthesis-methods"}
    assert seen == {"synthesis_methods": True}


# ---------------------------------------------------------------------------
# Catalog constants — single source shared by the payload and validation
# ---------------------------------------------------------------------------


def test_method_catalog_payload_is_rebuilt_per_call() -> None:
    first = method_catalog_payload()
    assert first == EXPECTED_CATALOG
    # Mutating a served payload must never poison later responses.
    first["methods"][0]["available"] = False
    first["standardizations"].clear()
    assert method_catalog_payload() == EXPECTED_CATALOG


# ---------------------------------------------------------------------------
# ParamSpec declaration guards
# ---------------------------------------------------------------------------


def test_param_spec_rejects_contract_breaking_declarations() -> None:
    with pytest.raises(ValueError, match="param name is required"):
        ParamSpec(name="  ", label="x", type="int")
    with pytest.raises(ValueError, match="param label is required"):
        ParamSpec(name="x", label=" ", type="int")
    with pytest.raises(ValueError, match="unsupported param type"):
        ParamSpec(name="x", label="x", type="matrix")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="enum param requires choices"):
        ParamSpec(name="x", label="x", type="enum")
    with pytest.raises(ValueError, match="choices are only valid for enum"):
        ParamSpec(name="x", label="x", type="int", choices=("a",))
    with pytest.raises(ValueError, match="only valid for numeric params"):
        ParamSpec(name="x", label="x", type="bool", minimum=0)
    with pytest.raises(ValueError, match="minimum must be <= maximum"):
        ParamSpec(name="x", label="x", type="float", minimum=2, maximum=1)


def test_param_spec_to_dict_matches_design_wire_shape() -> None:
    weights = ParamSpec(name="weights", label="各因子权重", type="weights", required=True, help="h")
    assert weights.to_dict() == {
        "name": "weights",
        "label": "各因子权重",
        "type": "weights",
        "required": True,
        "help": "h",
    }
    bounded = ParamSpec(
        name="ic_min_periods",
        label="IC 最小拟合期数",
        type="int",
        required=False,
        default=6,
        minimum=3,
        maximum=60,
        help="h",
    )
    assert bounded.to_dict() == {
        "name": "ic_min_periods",
        "label": "IC 最小拟合期数",
        "type": "int",
        "required": False,
        "default": 6,
        "minimum": 3,
        "maximum": 60,
        "help": "h",
    }
    enum = ParamSpec(name="mode", label="Mode", type="enum", choices=("a", "b"))
    assert enum.to_dict()["choices"] == ["a", "b"]


# ---------------------------------------------------------------------------
# validate_params_against_schema — schema-driven server-side re-validation
# ---------------------------------------------------------------------------


INT_SPEC = ParamSpec(name="periods", label="Periods", type="int", minimum=3, maximum=60)
FLOAT_SPEC = ParamSpec(name="ratio", label="Ratio", type="float", minimum=0.0, maximum=1.0)
BOOL_SPEC = ParamSpec(name="flag", label="Flag", type="bool")
ENUM_SPEC = ParamSpec(name="mode", label="Mode", type="enum", choices=("fast", "slow"))
WEIGHTS_SPEC = ParamSpec(name="weights", label="Weights", type="weights", required=True)


def test_validate_params_requires_mapping_and_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="params must be a mapping"):
        validate_params_against_schema((INT_SPEC,), ["not-a-mapping"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown parameters"):
        validate_params_against_schema((INT_SPEC,), {"periods": 5, "extra": 1})
    with pytest.raises(ValueError, match="synthesis.params: unknown parameters"):
        validate_params_against_schema((), {"weights": {}}, owner="synthesis.params")


def test_validate_params_required_and_optional_presence() -> None:
    with pytest.raises(ValueError, match="missing required parameter: weights"):
        validate_params_against_schema((WEIGHTS_SPEC,), {})
    # An absent OPTIONAL param passes and is not fabricated into the result.
    assert validate_params_against_schema((INT_SPEC,), {}) == {}


def test_validate_params_int_typing_and_bounds() -> None:
    assert validate_params_against_schema((INT_SPEC,), {"periods": 6}) == {"periods": 6}
    with pytest.raises(ValueError, match="must be an integer"):
        validate_params_against_schema((INT_SPEC,), {"periods": 6.5})
    with pytest.raises(ValueError, match="must be an integer"):
        validate_params_against_schema((INT_SPEC,), {"periods": True})
    with pytest.raises(ValueError, match="must be >= 3"):
        validate_params_against_schema((INT_SPEC,), {"periods": 2})
    with pytest.raises(ValueError, match="must be <= 60"):
        validate_params_against_schema((INT_SPEC,), {"periods": 61})


def test_validate_params_float_finiteness_and_bounds() -> None:
    assert validate_params_against_schema((FLOAT_SPEC,), {"ratio": 0.5}) == {"ratio": 0.5}
    # Ints are numbers for a float param; no coercion is performed.
    assert validate_params_against_schema((FLOAT_SPEC,), {"ratio": 1}) == {"ratio": 1}
    with pytest.raises(ValueError, match="must be a number"):
        validate_params_against_schema((FLOAT_SPEC,), {"ratio": "0.5"})
    with pytest.raises(ValueError, match="must be a number"):
        validate_params_against_schema((FLOAT_SPEC,), {"ratio": True})
    with pytest.raises(ValueError, match="must be finite"):
        validate_params_against_schema((FLOAT_SPEC,), {"ratio": math.nan})
    with pytest.raises(ValueError, match="must be <= 1.0"):
        validate_params_against_schema((FLOAT_SPEC,), {"ratio": 1.5})


def test_validate_params_bool_and_enum_membership() -> None:
    assert validate_params_against_schema((BOOL_SPEC,), {"flag": True}) == {"flag": True}
    with pytest.raises(ValueError, match="must be a boolean"):
        validate_params_against_schema((BOOL_SPEC,), {"flag": 1})
    assert validate_params_against_schema((ENUM_SPEC,), {"mode": "fast"}) == {"mode": "fast"}
    with pytest.raises(ValueError, match=r"must be one of \['fast', 'slow'\]"):
        validate_params_against_schema((ENUM_SPEC,), {"mode": "medium"})
    with pytest.raises(ValueError, match=r"must be one of \['fast', 'slow'\]"):
        validate_params_against_schema((ENUM_SPEC,), {"mode": True})


def test_validate_params_weights_mapping_structure() -> None:
    good = {"weights": {"FTR_A": 0.7, "FTR_B": -0.3}}
    result = validate_params_against_schema((WEIGHTS_SPEC,), good)
    assert result == good
    assert isinstance(result, dict)
    with pytest.raises(ValueError, match="must be a mapping of factor_id to number"):
        validate_params_against_schema((WEIGHTS_SPEC,), {"weights": [0.7, 0.3]})
    with pytest.raises(ValueError, match="empty factor_id key"):
        validate_params_against_schema((WEIGHTS_SPEC,), {"weights": {" ": 1.0}})
    with pytest.raises(ValueError, match=r"weights\[FTR_A\] must be a number"):
        validate_params_against_schema((WEIGHTS_SPEC,), {"weights": {"FTR_A": "1"}})
    with pytest.raises(ValueError, match=r"weights\[FTR_A\] must be a number"):
        validate_params_against_schema((WEIGHTS_SPEC,), {"weights": {"FTR_A": True}})
    with pytest.raises(ValueError, match=r"weights\[FTR_A\] must be finite"):
        validate_params_against_schema((WEIGHTS_SPEC,), {"weights": {"FTR_A": math.inf}})


def test_validate_params_weights_bounds_apply_per_entry() -> None:
    nonneg = ParamSpec(name="weights", label="Weights", type="weights", required=True, minimum=0)
    with pytest.raises(ValueError, match=r"weights\[FTR_A\] must be >= 0"):
        validate_params_against_schema((nonneg,), {"weights": {"FTR_A": -0.1}})
    assert validate_params_against_schema((nonneg,), {"weights": {"FTR_A": 0.0}}) == {
        "weights": {"FTR_A": 0.0}
    }


def test_validate_params_returns_plain_dict_copy() -> None:
    params = {"periods": 6}
    result = validate_params_against_schema((INT_SPEC,), params)
    assert result == {"periods": 6}
    assert result is not params


# ---------------------------------------------------------------------------
# apply_param_defaults — schema-driven default resolution (P6 honesty rule)
# ---------------------------------------------------------------------------


DEFAULTED_SPEC = ParamSpec(
    name="ic_min_periods", label="IC 最小拟合期数", type="int", default=6, minimum=3, maximum=60
)


def test_apply_param_defaults_fills_absent_declared_defaults() -> None:
    # The run's echoed params (and its composite-id digest) must state what
    # it ACTUALLY used: an omitted optional param resolves to the catalog
    # default instead of staying silently implicit.
    assert apply_param_defaults((DEFAULTED_SPEC,), {}) == {"ic_min_periods": 6}


def test_apply_param_defaults_never_overrides_explicit_values() -> None:
    assert apply_param_defaults((DEFAULTED_SPEC,), {"ic_min_periods": 12}) == {
        "ic_min_periods": 12
    }


def test_apply_param_defaults_leaves_defaultless_params_absent() -> None:
    # No declared default -> nothing is fabricated (weights stays required
    # and its absence is the validator's concern, not a silent fill).
    assert apply_param_defaults((WEIGHTS_SPEC,), {}) == {}


def test_apply_param_defaults_returns_copy_without_mutating_input() -> None:
    params: dict[str, object] = {}
    resolved = apply_param_defaults((DEFAULTED_SPEC,), params)
    assert resolved == {"ic_min_periods": 6}
    assert params == {}
    assert resolved is not params

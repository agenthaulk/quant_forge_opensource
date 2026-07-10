"""Registry ``precomputed_values_present`` presence probe + early refusal.

A composite's DEFINITION persists in ``factor_root`` (``FactorRepository.save``)
independent of whether its VALUES are actually reachable under the configured
``factor_values_root`` / ``factor_values_overlay_root`` — the multi-factor
module writes composite values into a per-run overlay directory that a later
run/deployment does not read. The fix is honesty, not persistence: the
registry row exposes value presence, the picker disables a row it cannot use,
and the server refuses precisely and EARLY instead of failing deep inside
composite materialization with an opaque "no rows" error.

Covers:

- ``FactorValueStore.has_stored_values`` / ``GET /api/registry/factors`` (and
  the per-factor detail route) surfacing ``precomputed_values_present``:
  ``null`` for a normal DSL formula, ``true``/``false`` for a precomputed
  formula probed against the SAME roots the scoring path reads, and ``null``
  when the probe itself fails (FP-4: unobservable is null, never a guess);
- ``_prepare_multi_factor_backtest`` (and therefore BOTH
  ``preflight_multi_factor_backtest`` and the ``POST
  /api/jobs/multi-factor-backtest`` route it gates) refusing a dangling
  precomputed member before any panel load or engine work;
- the ``run_multi_factor_backtest_workflow`` member-fetch backstop: a
  precomputed member whose stored rows exist but do not cover this request's
  universe/date signature raises a message distinct from the dangling-member
  refusal above.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

import quant_forge.apps.web.api as web_api
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import PathSettings, QuantForgeConfig
from quant_forge.core.contracts import FactorDefinition
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_engine.value_store import FactorValueStore
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.research_loop.config import load_research_loop_config


JSON_CONTENT_TYPE = "application/json; charset=utf-8"

DANGLING_MEMBER_MESSAGE = (
    "factor COMPOSITE_DEAD is precomputed but has no stored values under "
    "the configured factor_values_root/factor_values_overlay_root: its "
    "values were materialized for a past run only and are not present for "
    "this run"
)


# ---------------------------------------------------------------------------
# Fixtures + local helpers (own copies, mirroring neighboring web test files —
# each test module in this suite defines its own, there is no shared conftest)
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_config(tmp_path):
    """A demo workspace with a REAL (empty-until-written) value-store root.

    Unlike the bare ``QuantForgeConfig().resolve(...)`` fixture used by
    sibling web test modules (which leaves ``factor_values_root`` /
    ``factor_values_overlay_root`` at ``None``), presence tests need a
    configured root to probe against — a dangling composite is "materialized
    elsewhere", not "no root configured at all".
    """

    create_demo_workspace(tmp_path / "demo")
    base = QuantForgeConfig(
        paths=PathSettings(
            factor_values_root=Path("factor_values"),
            factor_values_overlay_root=Path("factor_values_overlay"),
        )
    )
    return base.resolve(tmp_path / "demo")


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


def _get(url: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _post(url: str, payload: dict) -> tuple[int, str, bytes]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _rd_config(config: QuantForgeConfig):
    return load_research_loop_config(web_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)


def _save_precomputed_factor(config: QuantForgeConfig, factor_id: str, *, universe_filters: tuple[str, ...] = ()) -> None:
    FactorRepository(config.paths.factor_root).save(
        FactorDefinition(
            factor_id=factor_id,
            name=factor_id.lower(),
            formula=f"precomputed:factor_id={factor_id}",
            status="candidate",
            horizon_days=5,
            universe_filters=universe_filters,
            source="synthesis",
        )
    )


def _write_stored_values(
    config: QuantForgeConfig,
    *,
    factor_id: str,
    factor_name: str,
    formula: str,
    formula_signature: str = "test-fixture-signature",
) -> None:
    """Write one value row through the store's OWN path resolution.

    Mirrors how the synthesis materializer writes composite values
    (``FactorValueStore._resolve_factor_paths`` -> ``write_incremental_values``,
    never a hand-built directory; see ``tests/test_synthesis_materialize.py``)
    but minimally: presence tests only need a value FILE to exist, not a full
    composite build.
    """

    store = FactorValueStore(
        config.paths.factor_values_root or config.paths.factor_values_overlay_root,
        write_root=config.paths.factor_values_overlay_root,
    )
    paths = store._resolve_factor_paths(factor_id=factor_id, factor_name=factor_name, formula=formula)
    assert paths.write_dir is not None
    scores = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-01-05"]),
            "instrument": ["STK000"],
            "score": [1.0],
        }
    )
    store.write_incremental_values(
        paths.write_dir,
        factor_id=factor_id,
        factor_name=factor_name,
        formula_signature=formula_signature,
        scores=scores,
    )


def _request(second_factor_id: str) -> dict:
    return {
        "factor_refs": [
            {"factor_id": "FTR_DEMO_SMALL_CAP", "direction": 1},
            {"factor_id": second_factor_id, "direction": 1},
        ],
        "synthesis": {"method": "equal_weight", "params": {}},
        "standardization": {"method": "zscore", "params": {}},
        "parameters": {"holding_days": 5},
    }


# ---------------------------------------------------------------------------
# (a) normal DSL factor row -> precomputed_values_present is null
# ---------------------------------------------------------------------------


def test_registry_row_precomputed_values_present_is_null_for_dsl_formula(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/registry/factors")

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    by_id = {row["factor_id"]: row for row in json.loads(body.decode("utf-8"))["factors"]}
    # A formula-backed factor computes scores on demand; "are values present"
    # is not a meaningful question for it, so the key stays null — never a
    # guessed True/False.
    assert by_id["FTR_DEMO_SMALL_CAP"]["precomputed_values_present"] is None
    assert by_id["FTR_DEMO_MOMENTUM"]["precomputed_values_present"] is None


# ---------------------------------------------------------------------------
# (b) precomputed WITH stored values -> true
# ---------------------------------------------------------------------------


def test_registry_row_precomputed_values_present_true_when_stored(web_config, web_app) -> None:
    _save_precomputed_factor(web_config, "COMPOSITE_LIVE")
    _write_stored_values(
        web_config,
        factor_id="COMPOSITE_LIVE",
        factor_name="composite_live",
        formula="precomputed:factor_id=COMPOSITE_LIVE",
    )

    status, _, body = _get(f"{web_app}/api/registry/factors")
    by_id = {row["factor_id"]: row for row in json.loads(body.decode("utf-8"))["factors"]}

    assert by_id["COMPOSITE_LIVE"]["precomputed_values_present"] is True


# ---------------------------------------------------------------------------
# (c) precomputed with NO stored values -> false (dangling composite)
# ---------------------------------------------------------------------------


def test_registry_row_precomputed_values_present_false_when_dangling(web_config, web_app) -> None:
    _save_precomputed_factor(web_config, "COMPOSITE_DEAD")

    status, _, body = _get(f"{web_app}/api/registry/factors")
    by_id = {row["factor_id"]: row for row in json.loads(body.decode("utf-8"))["factors"]}

    assert by_id["COMPOSITE_DEAD"]["precomputed_values_present"] is False


def test_registry_detail_carries_precomputed_values_present(web_config, web_app) -> None:
    """The detail route shares ``_registry_factor_row``; the key travels too."""

    _save_precomputed_factor(web_config, "COMPOSITE_DEAD")

    status, _, body = _get(f"{web_app}/api/registry/factors/COMPOSITE_DEAD")

    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["factor"]["precomputed_values_present"] is False


# ---------------------------------------------------------------------------
# (d) preflight/prepare refuses a dangling precomputed member, EARLY
# ---------------------------------------------------------------------------


def test_prepare_multi_factor_backtest_refuses_dangling_precomputed_member(web_config) -> None:
    _save_precomputed_factor(web_config, "COMPOSITE_DEAD")

    with pytest.raises(ValueError) as excinfo:
        web_api.preflight_multi_factor_backtest(
            web_config,
            factor_refs=_request("COMPOSITE_DEAD")["factor_refs"],
            synthesis=_request("COMPOSITE_DEAD")["synthesis"],
            standardization=_request("COMPOSITE_DEAD")["standardization"],
            parameters=_request("COMPOSITE_DEAD")["parameters"],
            rd_config=_rd_config(web_config),
        )

    assert str(excinfo.value) == DANGLING_MEMBER_MESSAGE


def test_multi_factor_backtest_route_rejects_dangling_precomputed_member(web_config, web_app) -> None:
    _save_precomputed_factor(web_config, "COMPOSITE_DEAD")

    status, content_type, body = _post(
        f"{web_app}/api/jobs/multi-factor-backtest", _request("COMPOSITE_DEAD")
    )

    assert status == 400
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"error"}
    assert payload["error"] == DANGLING_MEMBER_MESSAGE


# ---------------------------------------------------------------------------
# (e) probe-exception path -> null, never a guess
# ---------------------------------------------------------------------------


def test_registry_row_precomputed_values_present_null_on_probe_error(monkeypatch, web_config, web_app) -> None:
    _save_precomputed_factor(web_config, "COMPOSITE_BROKEN")

    def _raise(self, *, factor_id, factor_name, formula):
        raise RuntimeError("probe boom")

    monkeypatch.setattr(FactorValueStore, "has_stored_values", _raise)

    status, _, body = _get(f"{web_app}/api/registry/factors")
    by_id = {row["factor_id"]: row for row in json.loads(body.decode("utf-8"))["factors"]}

    assert by_id["COMPOSITE_BROKEN"]["precomputed_values_present"] is None


def test_prepare_multi_factor_backtest_does_not_guess_refusal_on_probe_error(monkeypatch, web_config) -> None:
    """An unobservable presence probe never blocks a run that might be fine."""

    _save_precomputed_factor(web_config, "COMPOSITE_UNKNOWN")
    _write_stored_values(
        web_config,
        factor_id="COMPOSITE_UNKNOWN",
        factor_name="composite_unknown",
        formula="precomputed:factor_id=COMPOSITE_UNKNOWN",
    )

    def _raise(self, *, factor_id, factor_name, formula):
        raise RuntimeError("probe boom")

    monkeypatch.setattr(FactorValueStore, "has_stored_values", _raise)

    # Does not raise for the dangling-member refusal: None is not False.
    web_api.preflight_multi_factor_backtest(
        web_config,
        factor_refs=_request("COMPOSITE_UNKNOWN")["factor_refs"],
        synthesis=_request("COMPOSITE_UNKNOWN")["synthesis"],
        standardization=_request("COMPOSITE_UNKNOWN")["standardization"],
        parameters=_request("COMPOSITE_UNKNOWN")["parameters"],
        rd_config=_rd_config(web_config),
    )


# ---------------------------------------------------------------------------
# Backstop: stored values exist but do not cover THIS request (distinct msg)
# ---------------------------------------------------------------------------


def test_workflow_backstop_refuses_when_stored_values_do_not_cover_request(web_config) -> None:
    _save_precomputed_factor(web_config, "COMPOSITE_STALE")
    # has_stored_values() is TRUE (a value file exists, so the early refusal
    # above does not fire) but the row carries a formula_signature that will
    # never match the signature the workflow recomputes at read time, so
    # prepare_factor_scores_result reads back zero rows for THIS request —
    # the universe/signature-mismatch case, distinct from "no file at all".
    _write_stored_values(
        web_config,
        factor_id="COMPOSITE_STALE",
        factor_name="composite_stale",
        formula="precomputed:factor_id=COMPOSITE_STALE",
        formula_signature="stale-signature-from-a-different-run",
    )

    with pytest.raises(ValueError) as excinfo:
        web_api.run_multi_factor_backtest_workflow(
            web_config,
            factor_refs=_request("COMPOSITE_STALE")["factor_refs"],
            synthesis=_request("COMPOSITE_STALE")["synthesis"],
            standardization=_request("COMPOSITE_STALE")["standardization"],
            parameters=_request("COMPOSITE_STALE")["parameters"],
            rd_config=_rd_config(web_config),
        )

    message = str(excinfo.value)
    assert "COMPOSITE_STALE" in message
    assert "none are readable for this request" in message
    # Distinct from the dangling-member (no file at all) refusal message.
    assert "materialized for a past run only" not in message

"""Seam regression: the staggered-entry workflow must late-bind through server.

``run_staggered_entry_workflow`` resolves ``run_staggered_entry_backtest``
through :mod:`quant_forge.apps.web.server` at call time, exactly like its
``evaluate_factor`` / ``run_factor_backtest`` / ``parse_factor_idea`` siblings.
A direct module-level import in :mod:`quant_forge.apps.web.api` would make a
monkeypatch on ``web_server.run_staggered_entry_backtest`` a silent no-op, so
this test pins the late-binding contract by spying on the server namespace.
"""

from __future__ import annotations

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import run_staggered_entry_workflow
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.research_loop.config import ResearchLoopConfig


def test_web_staggered_entry_workflow_routes_backtest_through_server_seam(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")

    calls: dict[str, object] = {}
    sentinel = {"sample_role": "staggered_entry_backtest", "seam": "server"}

    def spy_run_staggered_entry_backtest(factor_id, **kwargs):
        calls["factor_id"] = factor_id
        calls["kwargs"] = kwargs
        return sentinel

    # Patch on the server namespace only. If the api call site were bound to a
    # direct module-level import instead of ``_server.run_staggered_entry_backtest``,
    # this patch would not take effect and the spy would never run.
    monkeypatch.setattr(web_server, "run_staggered_entry_backtest", spy_run_staggered_entry_backtest)

    result = run_staggered_entry_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        formation_trading_days=5,
        rd_config=ResearchLoopConfig(),
    )

    # The spy ran (late-bound through the server namespace) and its return
    # value flowed straight back out of the workflow.
    assert calls["factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert result is sentinel

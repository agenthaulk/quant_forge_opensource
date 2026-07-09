"""Single-page workbench HTML template for the local web adapter.

Per decision D8 (CP6-1) the page carries no inline application script: all
executable frontend code lives in static ES modules under ``static/`` served
by :mod:`quant_forge.apps.web.routing`. This module keeps the server-side
template rendering (forms, selects, Chinese UI text, rd-config-driven
defaults) and emits a ``<script type="application/json">`` page-config block
plus a ``<script type="module" src="/static/app.js">`` entry tag.
"""

from __future__ import annotations

from html import escape
import json
from typing import Any

from quant_forge.apps.web.api import (
    MAX_RD_ITERATIONS,
    _active_llm,
    _default_seed_factor_id,
    _json_safe,
    _llm_provider_options,
    _paths_payload,
    _rd_optimizer_label,
)
from quant_forge.config import QuantForgeConfig
from quant_forge.research_loop.config import ResearchLoopConfig, load_research_loop_config


def _selected_attr(selected: bool) -> str:
    return " selected" if selected else ""


def _script_json(value: Any) -> str:
    return (
        json.dumps(_json_safe(value), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _provider_options_script_payload(options: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "provider": option.get("provider", ""),
            "apiKeyEnv": option.get("api_key_env", ""),
            "runtimeReady": option.get("runtime_ready", "false"),
        }
        for option in options
    )


def _page_config_json(
    *, control_token_required: bool, provider_options: tuple[dict[str, str], ...]
) -> str:
    """Serialize the per-render dynamic values consumed by /static/app.js."""

    return _script_json(
        {
            "controlTokenRequired": control_token_required,
            "llmProviderOptions": _provider_options_script_payload(provider_options),
        }
    )


def _provider_readiness_label(option: dict[str, str]) -> str:
    if option.get("runtime_ready") == "true":
        return " · env " + option["api_key_env"] if option["api_key_env"] else " · no auth"
    api_key_env = option.get("api_key_env", "")
    if api_key_env:
        return " · missing env " + api_key_env
    return " · not ready"


def _index_html(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig | None = None,
    *,
    control_token_required: bool = False,
    redact_runtime: bool = False,
) -> str:
    from quant_forge.apps.web import server as _server

    research_config = rd_config or load_research_loop_config(_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    paths = _paths_payload(config)
    provider_options = _llm_provider_options(config)
    active_llm = _active_llm(config)
    active_provider = active_llm.provider if active_llm.provider not in {"rule", "deterministic"} else ""
    provider = escape(active_llm.provider)
    model = escape(active_llm.model)
    parser_label = escape(active_provider or "未配置 LLM provider")
    rd_optimizer_label = escape(_rd_optimizer_label(config, research_config))
    seed_factor_id = escape(_default_seed_factor_id(config))
    if redact_runtime:
        paths = {
            "data_root": "protected",
            "factor_root": "protected",
            "factor_values_root": "protected",
            "factor_values_overlay_root": "protected",
            "factor_values_manifest_root": "protected",
            "artifact_root": "protected",
        }
        provider_options = ()
        active_provider = ""
        provider = "protected"
        model = "protected"
        parser_label = "需要控制令牌"
        rd_optimizer_label = "需要控制令牌"
        seed_factor_id = ""
    data_root = escape(paths["data_root"])
    factor_root = escape(paths["factor_root"])
    factor_values_root = escape(paths["factor_values_root"])
    factor_values_overlay_root = escape(paths["factor_values_overlay_root"])
    artifact_root = escape(paths["artifact_root"])
    interval_options = "\n".join(
        f'      <option value="{day}"{_selected_attr(day == research_config.default_interval_days)}>{day}天</option>'
        for day in research_config.allowed_interval_days
    )
    objective_options = "\n".join(
        f'      <option value="{value}"{_selected_attr(value == research_config.objective)}>{label}</option>'
        for value, label in (
            ("balanced", "IC / ICIR 优先"),
            ("rank_ic", "Rank IC"),
            ("rank_icir", "ICIR"),
            ("annualized_return", "回测收益"),
        )
    )
    llm_provider_options = "\n".join(
        (
            f'      <option value="{escape(option["provider"])}"'
            f'{_selected_attr(option["provider"] == active_provider)}>'
            f'{escape(option["provider"])} / {escape(option["model"])}'
            f'{escape(_provider_readiness_label(option))}</option>'
        )
        for option in provider_options
    )
    if not llm_provider_options:
        llm_provider_options = '      <option value="">需要控制令牌</option>' if redact_runtime else '      <option value="">未配置 LLM provider</option>'
    page_config_json = _page_config_json(
        control_token_required=control_token_required,
        provider_options=provider_options,
    )
    rd_seed_html = (
        f'<input id="rd-seed" value="{seed_factor_id}">'
        if seed_factor_id
        else '<input id="rd-seed" value="" placeholder="先创建或配置一个因子">'
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quant Forge</title>
  <style>
    :root {{
      color-scheme: light dark;
      --ink: #17211d;
      --muted: #65736e;
      --faint: #6b7873;
      --line: #d9e0dc;
      --line-strong: #b7c4be;
      --surface: #fbfcfa;
      --panel: #ffffff;
      --wash: #f2f6f1;
      --accent: #134b3c;
      --accent-2: #1f6f63;
      --accent-ink: #ffffff;
      --blue: #265f8f;
      --bad: #9b2f31;
      --warn: #985b10;
      --mono: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace;
      --surface-translucent: rgba(251, 252, 250, .92);
      --ok-wash: #e8f2ec;
      --ok-line: #bcd8c8;
      --warn-wash: #f8f0dd;
      --warn-line: #e3cf9a;
      --bad-wash: #f9ecec;
      --bad-line: #e5bcbc;
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-width: 320px; }}
    body {{
      margin: 0;
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(19,75,60,.045) 1px, transparent 1px),
        linear-gradient(180deg, rgba(38,95,143,.04) 1px, transparent 1px),
        var(--surface);
      background-size: 40px 40px;
    }}
    .app-shell {{
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(300px, 388px) minmax(0, 1fr);
    }}
    .control-rail {{
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
      padding: 22px;
      border-right: 1px solid var(--line);
      background: rgba(251, 252, 250, .94);
      backdrop-filter: blur(8px);
    }}
    .workbench {{
      min-width: 0;
      padding: 22px 28px 32px;
    }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{
      margin-bottom: 4px;
      font-size: 26px;
      line-height: 1.08;
      letter-spacing: 0;
    }}
    h2 {{
      margin-bottom: 8px;
      font-size: 15px;
      color: var(--ink);
      letter-spacing: 0;
    }}
    h3 {{
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    label {{
      display: block;
      margin: 14px 0 7px;
      font-size: 12px;
      font-weight: 800;
      color: var(--muted);
    }}
    textarea, select, input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease;
    }}
    textarea:focus, select:focus, input:focus {{
      border-color: var(--accent-2);
      box-shadow: 0 0 0 3px rgba(31, 111, 99, .12);
    }}
    textarea {{
      min-height: 126px;
      resize: vertical;
      padding: 12px;
    }}
    select {{ padding: 10px 12px; }}
    input {{ padding: 10px 12px; }}
    button {{
      width: 100%;
      margin-top: 14px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 13px 16px;
      background: var(--accent);
      color: var(--accent-ink);
      font-weight: 800;
      cursor: pointer;
      transition: transform .12s ease, background .12s ease;
    }}
    button:hover {{ background: #0f3f32; }}
    button:active {{ transform: translateY(1px); }}
    button.secondary {{
      border-color: var(--line-strong);
      background: #fff;
      color: var(--ink);
    }}
    button.danger {{
      border-color: var(--bad);
      background: #fff;
      color: var(--bad);
    }}
    button.danger:hover {{
      background: #fff6f6;
    }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    code {{
      background: #eef5ef;
      border: 1px solid var(--line);
      border-radius: 5px;
      padding: 2px 6px;
    }}
    .brand {{
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .brand-mark {{
      display: inline-grid;
      place-items: center;
      width: 36px;
      height: 36px;
      margin-bottom: 12px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: #fff;
      color: var(--accent);
      font-family: var(--mono);
      font-weight: 900;
    }}
    .brand-subtitle {{
      margin-bottom: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .sr-only {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }}
    .runtime-strip {{
      display: grid;
      gap: 8px;
      margin: 18px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .runtime-row {{
      display: grid;
      grid-template-columns: 78px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      min-width: 0;
      font-size: 12px;
    }}
    .runtime-row span:first-child {{
      color: var(--faint);
      font-weight: 800;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      word-break: break-word;
    }}
    .path-meta {{
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      line-height: 1.45;
      word-break: break-all;
    }}
    .section-title {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 14px;
      margin: 0 0 14px;
    }}
    .section-title p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .form-block {{
      margin: 18px 0;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(132px, 1fr));
      gap: 10px;
      margin: 14px 0 20px;
    }}
    .tile, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .tile {{
      min-height: 94px;
      padding: 14px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }}
    .tile b {{
      display: block;
      margin-top: 10px;
      color: var(--ink);
      font-family: var(--mono);
      font-size: clamp(20px, 2.3vw, 28px);
      line-height: 1.05;
      letter-spacing: 0;
    }}
    .panel {{
      margin-bottom: 14px;
      padding: 18px;
    }}
    .hero-panel {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
      border-top: 4px solid var(--accent);
    }}
    .hero-panel > p {{
      grid-column: 1 / -1;
      margin: 0;
    }}
    hr {{
      margin: 20px 0;
      border: 0;
      border-top: 1px solid var(--line);
    }}
    .button-row {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .button-row button {{
      padding: 11px 10px;
      font-size: 13px;
    }}
    .param-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }}
    .param-grid label {{
      margin: 0;
    }}
    .param-grid span {{
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
    }}
    .param-grid input {{
      min-width: 0;
      padding: 9px 10px;
      font-family: var(--mono);
    }}
    .pill {{
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 8px;
      margin: 2px 4px 2px 0;
      color: var(--muted);
      background: #fff;
      font-size: 11px;
      font-family: var(--mono);
    }}
    .ok {{ color: var(--accent-2); font-weight: 800; }}
    .warn {{ color: var(--warn); font-weight: 800; }}
    .err {{ color: var(--bad); font-weight: 800; white-space: pre-wrap; }}
    .formula {{
      max-width: 100%;
      overflow-wrap: anywhere;
      color: var(--accent);
      font-family: var(--mono);
      font-size: clamp(18px, 2vw, 24px);
      font-weight: 800;
      margin: 10px 0;
    }}
    .formula-badge {{
      justify-self: end;
      min-width: 104px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--wash);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      text-align: right;
    }}
    /* CP9-2 DSL formula highlighting: token-referencing declarations only. */
    .formula .dsl-fn    {{ color: var(--accent-2); }}
    .formula .dsl-id    {{ color: var(--ink); }}
    .formula .dsl-num   {{ color: var(--blue); }}
    .formula .dsl-str   {{ color: var(--warn); }}
    .formula .dsl-op,
    .formula .dsl-punct {{ color: var(--muted); }}
    .evidence-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .comparison-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    .comparison-table th,
    .comparison-table td {{
      border-bottom: 1px solid var(--line);
      padding: 8px 6px;
      text-align: left;
      vertical-align: top;
    }}
    .comparison-table th {{
      color: var(--muted);
      font-weight: 800;
    }}
    .comparison-table code {{
      display: inline-block;
      max-width: 220px;
      overflow-wrap: anywhere;
    }}
    .empty-state {{
      min-height: 240px;
      display: grid;
      align-content: center;
      border-style: dashed;
      background: rgba(255, 255, 255, .72);
    }}
    .empty-state h3 {{
      color: var(--accent);
      font-size: 13px;
      letter-spacing: .08em;
    }}
    .lab-stepper ol {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; padding: 0; list-style: none; }}
    .lab-stepper .step {{ display: inline-flex; align-items: center; gap: 6px;
      padding: 5px 10px; border: 1px solid var(--line); border-radius: 999px;
      background: var(--panel); color: var(--muted); font-size: 12px; font-weight: 800; }}
    .lab-stepper .step-index {{ display: inline-grid; place-items: center; width: 16px; height: 16px;
      border-radius: 50%; background: var(--wash); font-family: var(--mono); font-size: 10px; }}
    .lab-stepper .step.is-done   {{ border-color: var(--accent-2); color: var(--accent-2); }}
    .lab-stepper .step.is-done .step-index {{ background: var(--ok-wash); }}
    .lab-stepper .step.is-active {{ border-color: var(--accent); background: var(--accent); color: var(--accent-ink); }}
    .lab-stepper .step.is-active .step-index {{ background: rgba(255,255,255,.2); }}
    .lab-stepper .step-link {{ width: auto; margin: 0; padding: 0; border: 0;
      background: transparent; color: inherit; font: inherit; cursor: pointer; }}
    .lab-stepper .step-link:disabled {{ opacity: 1; cursor: default; }}
    .lab-stepper .step-link:not(:disabled):hover {{ text-decoration: underline; }}
    .lab-stepper .step-link:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 2px; }}
    .lab-tabs {{ position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; gap: 8px;
      margin: 0 -4px 18px; padding: 8px 4px; background: var(--surface-translucent);
      backdrop-filter: blur(6px); border-bottom: 1px solid var(--line); }}
    .lab-tab {{ width: auto; margin: 0; padding: 9px 14px; border: 1px solid var(--line);
      border-radius: 8px; background: var(--panel); color: var(--ink);
      font-size: 13px; font-weight: 800; cursor: pointer; }}
    .lab-tab:hover {{ background: var(--wash); }}
    .lab-tab[aria-selected="true"] {{ background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }}
    .lab-tab[aria-selected="true"]:hover {{ background: var(--accent); }}
    .lab-tab:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 2px; }}
    .lab-tab-dot {{ width: 7px; height: 7px; margin-left: 6px; border-radius: 50%;
      display: inline-block; vertical-align: 1px; box-shadow: 0 0 0 1px var(--panel); }}
    /* Author display beats the UA [hidden] rule; visibility keeps the dot's
       layout slot so tab widths stay stable when a status appears. */
    .lab-tab-dot[hidden] {{ visibility: hidden; }}
    .lab-tab-dot.is-running {{ background: var(--warn); }}
    .lab-tab-dot.is-done    {{ background: var(--accent-2); }}
    .lab-tab-dot.is-error   {{ background: var(--bad); }}
    .lab-tabpanel {{ min-width: 0; }}
    .lab-tabpanel:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 4px; }}
    /* CP9-2 workbench module nav (Studio segmentedControl affordance,
       token-referencing declarations only). */
    .lab-module-nav {{ display: inline-flex; flex-wrap: wrap; max-width: 100%;
      gap: 4px; margin: 0 0 16px; padding: 4px;
      border: 1px solid var(--line); border-radius: 10px; background: var(--wash); }}
    .lab-module-tab {{ width: auto; margin: 0; padding: 8px 14px; border: 1px solid transparent;
      border-radius: 7px; background: transparent; color: var(--muted);
      font-size: 13px; font-weight: 800; cursor: pointer; }}
    /* Hover background parity with .lab-tab (A-MAJOR-1): the wash token, not
       the primary-button hover fill (button:hover outranks the base
       transparent bg), backs a hovered UNSELECTED segment so its ink text
       keeps >=4.5:1 in both themes. The selected segment re-pins its own
       panel bg below and stays legible. */
    .lab-module-tab:hover {{ color: var(--ink); background: var(--wash); }}
    /* Active-segment border uses the accent token so the selected state
       reads at >=3:1 against the nav wash in both themes (WCAG 1.4.11) —
       line-strong only reached 1.65:1 / 1.88:1 there. Same aria-current
       accent-border language as .registry-row / .docs-row / .ext-card. */
    .lab-module-tab[aria-selected="true"] {{ background: var(--panel); border-color: var(--accent); color: var(--accent); }}
    .lab-module-tab[aria-selected="true"]:hover {{ color: var(--accent); }}
    .lab-module-tab:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 2px; }}
    .lab-module-tab .pill {{ margin-left: 6px; }}
    .lab-module-panel {{ min-width: 0; }}
    .lab-module-panel:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 4px; }}
    .anchor-nav {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }}
    .anchor-nav a {{ padding: 4px 9px; border: 1px solid var(--line); border-radius: 6px;
      background: var(--panel); color: var(--muted); font-size: 11px; font-weight: 800;
      text-decoration: none; }}
    .anchor-nav a:hover {{ color: var(--accent); border-color: var(--accent-2); }}
    .anchor-nav a:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 2px; }}
    .report-section {{ scroll-margin-top: 72px; }}
    .eyebrow {{ margin: 0 0 5px; color: var(--accent); font-size: 11px; font-weight: 800;
      letter-spacing: .1em; text-transform: uppercase; }}
    .sparkline-row {{ margin: 0 0 10px; }}
    .sparkline {{ display: block; max-width: 100%; color: var(--accent-2); }}
    /* CP9-1 inline-SVG charts (charts.js). SVG <text> inherits the app's
       system sans (D8: no external font) instead of the SVG-default serif;
       all colors come from the theme tokens the module references. */
    .qf-chart {{ display: block; max-width: 100%; height: auto; color: var(--accent-2); }}
    .qf-chart text {{ font-family: inherit; font-size: 11px; }}
    .qf-chart .qf-note {{ font-size: 10px; }}
    .qf-chart--empty text {{ font-size: 12px; }}
    .qf-chart-row {{ margin: 0 0 12px; }}
    .status-pill {{ display: inline-block; padding: 3px 9px; border: 1px solid var(--line);
      border-radius: 999px; font-size: 10px; font-weight: 800; }}
    .status-pill--ok      {{ background: var(--ok-wash);   border-color: var(--ok-line);   color: var(--accent-2); }}
    .status-pill--fail    {{ background: var(--bad-wash);  border-color: var(--bad-line);  color: var(--bad); }}
    .status-pill--running {{ background: var(--warn-wash); border-color: var(--warn-line); color: var(--warn); }}
    .status-pill--neutral {{ color: var(--muted); }}
    .status-badge--legacy {{ background: var(--warn-wash); border: 1px solid var(--warn-line);
      color: var(--warn); border-radius: 5px; padding: 1px 5px; font-size: 10px; font-weight: 800; }}
    .metric-blocked {{ color: var(--muted); font-weight: 400; }}
    .metric-missing {{ color: var(--faint); }}
    /* Long status labels (e.g. insufficient_sample) never overflow their metric
       cell: they wrap and expose the full token via the title attribute. Inside
       a metric tile the status renders as a smaller muted label — a status is
       not a number, so it sheds the big mono value type — keeping the FP-4
       contract that a withheld metric shows its label, never a fabricated 0. */
    .metric-status {{ overflow-wrap: anywhere; }}
    .metric-blocked, .metric-missing {{ overflow-wrap: anywhere; }}
    .tile b .metric-status, .tile b .metric-blocked, .tile b .metric-missing {{
      display: block; font-family: inherit; font-size: 13px; font-weight: 700;
      line-height: 1.25; letter-spacing: 0; color: var(--muted); }}
    .notice {{ border: 1px solid var(--line); border-left: 4px solid var(--muted);
      border-radius: 8px; padding: 10px 12px; margin: 0 0 10px; font-size: 13px; }}
    .notice.warn {{ border-left-color: var(--warn); background: var(--warn-wash); color: var(--ink); }}
    .notice.err  {{ border-left-color: var(--bad);  background: var(--bad-wash);  color: var(--ink); }}
    /* CP6-3 Data console + Registry: token-referencing declarations only
       (zero new color literals), so both themes come from the variables. */
    .notice.ok  {{ border-left-color: var(--accent-2); background: var(--ok-wash); color: var(--ink); }}
    .pill.muted {{ color: var(--faint); }}
    .tile-range {{ display: block; margin-top: 10px; color: var(--ink);
      font-family: var(--mono); font-size: 13px; line-height: 1.4; }}
    .tag-chips  {{ margin-top: 6px; }}
    .registry-layout {{ display: grid; grid-template-columns: minmax(240px, 320px) minmax(0, 1fr);
      gap: 14px; align-items: start; }}
    .registry-list {{ display: grid; gap: 8px; align-content: start; }}
    .registry-row {{ width: 100%; margin: 0; padding: 10px 12px; border: 1px solid var(--line);
      border-radius: 8px; background: var(--panel); color: var(--ink);
      font-size: 13px; font-weight: 400; text-align: left; cursor: pointer; }}
    .registry-row:hover {{ background: var(--wash); }}
    .registry-row[aria-current="true"] {{ border-color: var(--accent); box-shadow: inset 2px 0 0 var(--accent); }}
    .registry-row:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 2px; }}
    .registry-row-name {{ display: flex; justify-content: space-between; gap: 8px;
      align-items: baseline; font-weight: 800; }}
    .registry-row-formula {{ display: block; margin-top: 4px; color: var(--muted);
      font-family: var(--mono); font-size: 11px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .registry-detail {{ min-width: 0; }}
    .registry-runs-toolbar {{ display: flex; align-items: center; gap: 10px; margin: 0 0 10px; }}
    .registry-runs-toolbar label {{ margin: 0; }}
    .registry-runs-toolbar select {{ width: auto; padding: 6px 10px; font-size: 12px; }}
    .nowrap {{ white-space: nowrap; }}
    .table-scroll {{ overflow-x: auto; }}
    /* CP6-4 Docs + Extensions: token-referencing declarations only. */
    .docs-layout {{ display: grid; grid-template-columns: minmax(240px, 320px) minmax(0, 1fr); gap: 14px; align-items: start; }}
    .docs-nav {{ display: grid; gap: 8px; align-content: start; }}
    .docs-nav-section {{ margin: 8px 0 0; color: var(--muted); font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
    .docs-row {{ width: 100%; margin: 0; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--ink); font-size: 13px; font-weight: 400; text-align: left; cursor: pointer; }}
    .docs-row:hover {{ background: var(--wash); }}
    .docs-row[aria-current="true"] {{ border-color: var(--accent); box-shadow: inset 2px 0 0 var(--accent); }}
    .docs-row:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 2px; }}
    .docs-row .meta {{ display: block; margin-top: 2px; font-family: var(--mono); font-size: 11px; overflow-wrap: anywhere; }}
    .docs-detail {{ min-width: 0; }}
    /* overflow-wrap inherits: one declaration keeps long unbroken tokens
       (relpaths, inline code) from overflowing the panel at narrow widths.
       break-word (not anywhere) so table columns keep word-based min
       widths; fenced blocks keep white-space: pre and scroll. */
    .docs-article {{ font-size: 14px; line-height: 1.65; overflow-wrap: break-word; }}
    .docs-article h1 {{ font-size: 22px; margin: 0 0 12px; }}
    .docs-article h2 {{ font-size: 17px; margin: 22px 0 8px; }}
    .docs-article h3 {{ font-size: 14px; margin: 18px 0 6px; color: var(--ink); text-transform: none; letter-spacing: 0; }}
    .docs-article h4, .docs-article h5, .docs-article h6 {{ font-size: 13px; margin: 14px 0 6px; }}
    .docs-article p {{ margin: 0 0 12px; }}
    .docs-article ul, .docs-article ol {{ margin: 0 0 12px; padding-left: 22px; }}
    .docs-article li {{ margin: 3px 0; }}
    .docs-article pre {{ margin: 0 0 14px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--wash); font-family: var(--mono); font-size: 12px; line-height: 1.55; overflow-x: auto; }}
    .docs-article pre code {{ border: 0; padding: 0; background: none; }}
    .docs-article blockquote {{ margin: 0 0 12px; padding: 8px 12px; border-left: 3px solid var(--line-strong); border-radius: 0 6px 6px 0; background: var(--wash); color: var(--muted); }}
    .docs-article hr {{ margin: 18px 0; border: 0; border-top: 1px solid var(--line); }}
    .docs-article .docs-table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin: 0 0 14px; }}
    .docs-article .docs-table th, .docs-article .docs-table td {{ border-bottom: 1px solid var(--line); padding: 7px 6px; text-align: left; vertical-align: top; }}
    .docs-article .docs-table th {{ color: var(--muted); font-weight: 800; }}
    .docs-link {{ color: var(--accent); }}
    .docs-link:focus-visible {{ outline: 2px solid var(--accent-2); outline-offset: 2px; }}
    .docs-external-url {{ color: var(--muted); font-family: var(--mono); font-size: .92em; overflow-wrap: anywhere; }}
    .docs-image-alt {{ color: var(--faint); font-style: italic; }}
    .ext-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }}
    .ext-card {{ margin-bottom: 0; }}
    .ext-card[aria-current="true"] {{ border-color: var(--accent); box-shadow: inset 2px 0 0 var(--accent); }}
    .ext-card-head {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; align-items: baseline; }}
    .ext-card-head .ext-version {{ color: var(--muted); font-family: var(--mono); font-size: 11px; }}
    .ext-contribs {{ margin-top: 8px; }}
    .ext-points {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    .ext-point {{ display: inline-flex; gap: 6px; align-items: center; }}
    /* On the tinted notice washes --muted lands under 4.5:1 in the light
       theme (4.31 on --bad-wash); secondary text inside notices keeps its
       hierarchy through size instead of color. */
    .notice .meta {{ color: var(--ink); font-size: 12px; }}
    /* Deep-link scrollIntoView targets need the same sticky-strip
       clearance as .report-section anchors. */
    .docs-detail, .ext-card {{ scroll-margin-top: 72px; }}
    /* CP10 multi-factor synthesis module: token-referencing declarations
       only (zero new color literals), so both themes come from the
       variables. */
    .synth-factor-list {{ display: grid; gap: 8px; max-height: 340px; overflow: auto; margin: 6px 0 4px; }}
    .synth-factor-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 10px; align-items: center;
      padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }}
    .synth-factor-name {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin: 0;
      min-width: 0; overflow-wrap: anywhere; font-size: 13px; font-weight: 800; color: var(--ink); }}
    .synth-factor-name input {{ width: auto; margin: 0; }}
    .synth-direction-label {{ display: inline-flex; align-items: center; gap: 6px; margin: 0; }}
    .synth-direction-label select {{ width: auto; padding: 6px 8px; font-size: 12px; }}
    .synth-factor-formula {{ grid-column: 1 / -1; color: var(--muted); font-family: var(--mono);
      font-size: 11px; overflow-wrap: anywhere; }}
    .synth-param {{ margin: 10px 0; }}
    .synth-param label {{ margin: 0; }}
    .synth-param .param-grid {{ margin-top: 6px; }}
    .synth-check-label {{ display: inline-flex; align-items: center; gap: 8px; }}
    .synth-check-label input {{ width: auto; }}
    /* Weights inputs are labeled by factor NAME; the raw factor_id rides
       along in mono, de-emphasized by weight/family (not by a lower-contrast
       color: --muted stays >= 4.5:1 on --panel in both themes). */
    .synth-weight-id {{ color: var(--muted); font-family: var(--mono); font-weight: 400; }}
    /* Below tablet width the picker row stacks: the direction control drops
       under the factor name instead of sharing its row, so long factor ids,
       the status pill, and the ±1 select never overlap at 375px. */
    @media (max-width: 640px) {{
      .synth-factor-row {{ grid-template-columns: minmax(0, 1fr); }}
      .synth-direction-label {{ justify-self: start; }}
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --ink: #e6ece8; --muted: #9fb0a8; --faint: #7d8d86;
        --line: #2e3a34; --line-strong: #46554d;
        --surface: #121815; --panel: #1a221e; --wash: #202a25;
        --surface-translucent: rgba(18, 24, 21, .92);
        --accent: #4ea27f; --accent-2: #63b391; --accent-ink: #121815; --blue: #6da3cf;
        --bad: #d98b83; --warn: #d3a34f;
        --ok-wash:  #1b2f26; --ok-line:  #2f4f3f;
        --warn-wash: #332a17; --warn-line: #54451f;
        --bad-wash: #33201e; --bad-line: #543230;
      }}
      body {{ background: var(--surface); }}
      .control-rail {{ background: var(--surface-translucent); }}
      textarea, select, input, button.secondary, button.danger {{ background: var(--panel); }}
      button:hover {{ background: var(--accent-2); }}
      button.danger:hover {{ background: var(--bad-wash); }}
      code {{ background: var(--wash); }}
      .brand-mark, .pill, .runtime-strip {{ background: var(--panel); }}
      .empty-state {{ background: var(--surface-translucent); }}
      .lab-tab:hover {{ background: var(--wash); }}
      .lab-tab[aria-selected="true"]:hover {{ background: var(--accent); }}
      .lab-module-tab:hover {{ background: var(--wash); }}
      .lab-module-tab[aria-selected="true"]:hover {{ background: var(--panel); }}
    }}
    @media (max-width: 900px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .control-rail {{
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }}
      .workbench {{ padding: 18px; }}
      /* The sticky tab strip wraps to up to three rows at narrow widths;
         anchor jumps need the taller clearance so sections never tuck
         under it. */
      .report-section {{ scroll-margin-top: 156px; }}
      .docs-detail, .ext-card {{ scroll-margin-top: 156px; }}
      .grid {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      .evidence-grid {{ grid-template-columns: 1fr; }}
      .registry-layout {{ grid-template-columns: 1fr; }}
      .docs-layout {{ grid-template-columns: 1fr; }}
      /* Below the collapse point the dense tables keep a readable minimum
         width and scroll sideways inside .table-scroll instead of
         crushing cell content into vertical letter stacks. */
      .data-fields-table {{ min-width: 640px; }}
      .registry-runs-table {{ min-width: 520px; }}
      .docs-article .docs-table {{ min-width: 480px; }}
      .hero-panel {{ grid-template-columns: 1fr; }}
      .formula-badge {{ justify-self: start; text-align: left; }}
    }}
  </style>
</head>
<body>
<main class="app-shell">
  <aside class="control-rail">
    <div class="brand">
      <div class="brand-mark">QF</div>
      <h1>Quant Forge</h1>
      <p class="brand-subtitle">Factor research console</p>
    </div>
    <p id="runtime-llm-sr" class="sr-only">LLM parser: {provider} / {model}</p>
    <p id="runtime-rd-sr" class="sr-only">RD optimizer: {rd_optimizer_label}</p>
    <div class="runtime-strip">
      <div class="runtime-row"><span>LLM</span><strong id="runtime-llm">{provider} / {model}</strong></div>
      <div class="runtime-row"><span>RD</span><strong id="runtime-rd">{rd_optimizer_label}</strong></div>
      <div class="runtime-row"><span>data</span><div id="runtime-data-root" class="path-meta">{data_root}</div></div>
      <div class="runtime-row"><span>factors</span><div id="runtime-factor-root" class="path-meta">{factor_root}</div></div>
      <div class="runtime-row"><span>values</span><div id="runtime-factor-values-root" class="path-meta">{factor_values_root or '未配置'}</div></div>
      <div class="runtime-row"><span>overlay</span><div id="runtime-factor-values-overlay-root" class="path-meta">{factor_values_overlay_root or '未配置'}</div></div>
      <div class="runtime-row"><span>artifacts</span><div id="runtime-artifact-root" class="path-meta">{artifact_root}</div></div>
    </div>
    <div class="form-block">
      <div class="section-title">
        <h2>01 Parse</h2>
        <p>idea → factor</p>
      </div>
      <label for="idea">因子观点</label>
      <textarea id="idea">非ST的小市值股票未来表现更好</textarea>
      <label for="parser">解析方式</label>
      <select id="parser">
        <option value="llm">LLM 语义解析: {parser_label}</option>
        <option value="rule">本地规则解析</option>
      </select>
      <label for="llm-provider">LLM Provider</label>
      <select id="llm-provider">
{llm_provider_options}
      </select>
      <label for="llm-api-key-mode">LLM API Key</label>
      <select id="llm-api-key-mode">
        <option value="config">配置文件 / 环境变量加载</option>
        <option value="manual">手动输入（仅前端联调）</option>
      </select>
      <input id="llm-api-key" type="password" autocomplete="off" data-secret-policy="not-submitted" disabled>
      <p id="llm-api-key-status" class="meta"></p>
      <label>评测参数</label>
      <div class="param-grid" id="validation-controls">
        <label><span>持有期 / 天</span><input id="param-holding-days" type="number" min="1" step="1" disabled></label>
        <label><span>Decay / 天</span><input id="param-decay-days" type="number" min="0" step="1" disabled></label>
        <label><span>Top Quantile</span><input id="param-top-quantile" type="number" min="0.01" max="0.5" step="0.01" disabled></label>
        <label><span>Delay / 天</span><input id="param-delay-days" type="number" min="1" step="1" disabled></label>
        <label><span>评测开始</span><input id="param-evaluation-start" type="date" disabled></label>
        <label><span>评测结束</span><input id="param-evaluation-end" type="date" disabled></label>
        <label><span>回测开始</span><input id="param-backtest-start" type="date" disabled></label>
        <label><span>回测结束</span><input id="param-backtest-end" type="date" disabled></label>
        <label><span>手续费 bps</span><input id="param-commission-bps" type="number" min="0" step="0.1" disabled></label>
        <label><span>滑点 bps</span><input id="param-slippage-bps" type="number" min="0" step="0.1" disabled></label>
        <label><span>融券成本 bps/年</span><input id="param-short-borrow-bps" type="number" min="0" step="1" disabled></label>
      </div>
      <button id="run">解析因子</button>
      <button id="validate-run" class="secondary" disabled>验证并评测</button>
      <button id="staggered-run" class="secondary" disabled>首月逐日建仓稳健性回测</button>
      <button id="cancel-run" class="secondary danger" disabled>中断本次运行</button>
      <p id="status" class="meta"></p>
    </div>
    <div class="form-block">
      <div class="section-title">
        <h2>02 Research</h2>
        <p>seed → candidate</p>
      </div>
      <label for="rd-seed">Seed Factor</label>
      {rd_seed_html}
      <label for="rd-objective">目标优先级</label>
      <select id="rd-objective">
{objective_options}
      </select>
      <label for="rd-max">候选数量</label>
      <input id="rd-max" type="number" min="1" max="10" value="{research_config.default_max_candidates}">
      <label for="rd-iterations">RD迭代次数</label>
      <input id="rd-iterations" type="number" min="1" max="{MAX_RD_ITERATIONS}" step="1" value="1">
      <label for="rd-interval">自动周期</label>
      <select id="rd-interval">
{interval_options}
      </select>
      <div class="button-row">
        <button id="rd-run">运行一次</button>
        <button id="rd-start" class="secondary">开启</button>
        <button id="rd-stop" class="secondary">停止</button>
      </div>
      <button id="rd-cancel" class="secondary danger" disabled>中断本次RD</button>
      <p id="rd-status" class="meta"></p>
    </div>
  </aside>
  <section class="workbench">
    <nav class="lab-stepper" aria-label="研究流程">
      <ol>
        <li class="step is-active" data-step="idea"><span class="step-index">1</span>想法</li>
        <li class="step is-pending" data-step="parse"><span class="step-index">2</span>解析</li>
        <li class="step is-pending" data-step="validate"><span class="step-index">3</span>验证</li>
        <li class="step is-pending" data-step="report"><span class="step-index">4</span><button type="button" class="step-link" data-step-action="report" disabled>因子报告</button></li>
        <li class="step is-pending" data-step="rd"><span class="step-index">5</span><button type="button" class="step-link" data-step-action="rd">RD 循环</button></li>
      </ol>
    </nav>
    <div class="lab-tabs" role="tablist" aria-label="工作台视图">
      <button class="lab-tab" role="tab" id="lab-tab-factor" aria-controls="lab-panel-factor" aria-selected="true">LLM 因子工作台 <span class="lab-tab-dot" hidden></span></button>
      <button class="lab-tab" role="tab" id="lab-tab-history" aria-controls="lab-panel-history" aria-selected="false" tabindex="-1">研究历史 <span class="lab-tab-dot" hidden></span></button>
      <button class="lab-tab" role="tab" id="lab-tab-data" aria-controls="lab-panel-data" aria-selected="false" tabindex="-1">数据 <span class="lab-tab-dot" hidden></span></button>
      <button class="lab-tab" role="tab" id="lab-tab-registry" aria-controls="lab-panel-registry" aria-selected="false" tabindex="-1">注册表 <span class="lab-tab-dot" hidden></span></button>
      <button class="lab-tab" role="tab" id="lab-tab-docs" aria-controls="lab-panel-docs" aria-selected="false" tabindex="-1">文档 <span class="lab-tab-dot" hidden></span></button>
      <button class="lab-tab" role="tab" id="lab-tab-extensions" aria-controls="lab-panel-extensions" aria-selected="false" tabindex="-1">扩展 <span class="lab-tab-dot" hidden></span></button>
    </div>
    <div id="error" class="err"></div>
    <section class="lab-tabpanel" role="tabpanel" id="lab-panel-factor" aria-labelledby="lab-tab-factor" tabindex="0">
      <div class="lab-module-nav" role="tablist" aria-label="工作台模块">
        <button class="lab-module-tab" role="tab" id="lab-module-single" aria-controls="lab-module-panel-single" aria-selected="true">单因子研究</button>
        <button class="lab-module-tab" role="tab" id="lab-module-multi" aria-controls="lab-module-panel-multi" aria-selected="false" tabindex="-1">多因子策略回测</button>
      </div>
      <div class="lab-module-panel" role="tabpanel" id="lab-module-panel-single" aria-labelledby="lab-module-single" tabindex="0">
        <div class="section-title">
          <h2>Factor Tape</h2>
          <p>解析、评价、回测集中展示</p>
        </div>
        <div id="result">
          <div class="panel empty-state">
            <h3>等待输入</h3>
            <p class="meta">输入因子观点后运行，公式、IC、回测收益、缓存路径会在这里展开。</p>
          </div>
        </div>
        <div id="staggered-result"></div>
        <section class="report-section" id="report-comparison">
          <div class="section-title">
            <h2>Benchmark</h2>
            <p>qf factor bench 多因子横向对比（并入因子报告的对比区）</p>
          </div>
          <div id="bench-result">
            <div class="panel empty-state">
              <h3>暂无 bench 结果</h3>
              <p class="meta">运行 qf factor bench 后，多因子指标状态表会展示在这里。</p>
            </div>
          </div>
        </section>
        <section class="report-section" id="workbench-rd">
          <div class="section-title">
            <h2>RD Loop</h2>
            <p>候选因子与研究证据</p>
          </div>
          <div id="rd-result">
            <div class="panel empty-state">
              <h3>等待运行</h3>
              <p class="meta">RD 候选、gate、report path 和分段证据会展示在这里。</p>
            </div>
          </div>
        </section>
      </div>
      <div class="lab-module-panel" role="tabpanel" id="lab-module-panel-multi" aria-labelledby="lab-module-multi" tabindex="0" hidden>
        <div class="section-title">
          <h2>多因子策略回测</h2>
          <p>组合层多因子策略合成与回测</p>
        </div>
        <!-- CP10 mount: the multi-factor module claims #multi-result and the lab-module-multi nav hook; do not rename. -->
        <div id="multi-result">
          <div class="panel" id="synth-form">
            <h3>合成配置</h3>
            <p class="meta">选择至少 2 个因子并显式设定方向；合成方法与参数由后端方法目录声明，权重与方法为先验声明（非拟合）。</p>
            <label>因子选择</label>
            <div id="synth-factors">
              <div class="panel empty-state">
                <h3>等待加载</h3>
                <p class="meta">打开本模块后，注册表因子目录会加载到这里。</p>
              </div>
            </div>
            <label>合成方法与标准化</label>
            <div id="synth-method-mount">
              <div class="panel empty-state">
                <h3>等待加载</h3>
                <p class="meta">方法目录加载后，合成方法、标准化与动态参数表单会展示在这里。</p>
              </div>
            </div>
            <div id="synth-standardization-mount"></div>
            <div id="synth-params"></div>
            <label>回测参数</label>
            <div class="param-grid" id="synth-backtest-params">
              <label><span>持有期 / 天（必填）</span><input id="synth-param-holding-days" type="number" min="1" step="1" value="5"></label>
              <label><span>Decay / 天</span><input id="synth-param-decay-days" type="number" min="0" step="1"></label>
              <label><span>Top Quantile</span><input id="synth-param-top-quantile" type="number" min="0.01" max="0.5" step="0.01"></label>
              <label><span>Delay / 天</span><input id="synth-param-delay-days" type="number" min="1" step="1"></label>
              <label><span>评测开始</span><input id="synth-param-evaluation-start" type="date"></label>
              <label><span>评测结束</span><input id="synth-param-evaluation-end" type="date"></label>
              <label><span>回测开始</span><input id="synth-param-backtest-start" type="date"></label>
              <label><span>回测结束</span><input id="synth-param-backtest-end" type="date"></label>
              <label><span>手续费 bps</span><input id="synth-param-commission-bps" type="number" min="0" step="0.1"></label>
              <label><span>滑点 bps</span><input id="synth-param-slippage-bps" type="number" min="0" step="0.1"></label>
              <label><span>融券成本 bps/年</span><input id="synth-param-short-borrow-bps" type="number" min="0" step="1"></label>
            </div>
            <p class="meta">持有期为必填（预填 5 仅为建议值）；其余留空的参数由后端 profile 默认值决定。</p>
            <p id="synth-run-hint" class="meta" aria-live="polite">已选 0 个因子，运行需至少勾选 2 个；方法目录尚未加载或不可用。</p>
            <button id="synth-run" disabled aria-describedby="synth-run-hint">合成并回测</button>
            <button id="synth-cancel" class="secondary danger" disabled>中断本次运行</button>
            <p id="synth-status" class="meta" aria-live="polite"></p>
          </div>
          <div id="synth-report">
            <div class="panel empty-state">
              <h3>等待运行</h3>
              <p class="meta">合成回测完成后，评价、样本内回测、外部样本外评测与合成 provenance 会展示在这里。</p>
            </div>
          </div>
        </div>
      </div>
    </section>
    <section class="lab-tabpanel" role="tabpanel" id="lab-panel-history" aria-labelledby="lab-tab-history" tabindex="0" hidden>
      <div class="section-title">
        <h2>研究历史</h2>
        <p>run index 最近运行记录</p>
      </div>
      <div id="history-result">
        <div class="panel empty-state">
          <h3>暂无研究历史</h3>
          <p class="meta">评价、回测、bench、RD 运行记录到 run index 后会展示在这里。</p>
        </div>
      </div>
    </section>
    <section class="lab-tabpanel" role="tabpanel" id="lab-panel-data" aria-labelledby="lab-tab-data" tabindex="0" hidden>
      <div class="section-title">
        <h2>数据控制台</h2>
        <p>字段目录、覆盖范围与质量门</p>
      </div>
      <div id="data-result">
        <div class="panel empty-state">
          <h3>等待加载</h3>
          <p class="meta">打开本页签后，字段目录、覆盖范围和质量门结果会展示在这里。</p>
        </div>
      </div>
    </section>
    <section class="lab-tabpanel" role="tabpanel" id="lab-panel-registry" aria-labelledby="lab-tab-registry" tabindex="0" hidden>
      <div class="section-title">
        <h2>注册表</h2>
        <p>因子定义与证据链</p>
      </div>
      <div id="registry-result">
        <div class="panel empty-state">
          <h3>等待加载</h3>
          <p class="meta">因子目录加载后，定义详情与关联运行记录会展示在这里。</p>
        </div>
      </div>
    </section>
    <section class="lab-tabpanel" role="tabpanel" id="lab-panel-docs" aria-labelledby="lab-tab-docs" tabindex="0" hidden>
      <div class="section-title">
        <h2>文档</h2>
        <p>仓库 docs/ 只读渲染</p>
      </div>
      <div id="docs-result">
        <div class="panel empty-state">
          <h3>等待加载</h3>
          <p class="meta">打开本页签后，文档目录与渲染内容会展示在这里。</p>
        </div>
      </div>
    </section>
    <section class="lab-tabpanel" role="tabpanel" id="lab-panel-extensions" aria-labelledby="lab-tab-extensions" tabindex="0" hidden>
      <div class="section-title">
        <h2>扩展</h2>
        <p>声明式扩展注册表（只读）</p>
      </div>
      <div id="extensions-result">
        <div class="panel empty-state">
          <h3>等待加载</h3>
          <p class="meta">扩展清单加载后，manifest 校验状态与贡献点会展示在这里。</p>
        </div>
      </div>
    </section>
  </section>
</main>
<script type="application/json" id="qf-page-config">{page_config_json}</script>
<script type="module" src="/static/app.js"></script>
</body>
</html>"""

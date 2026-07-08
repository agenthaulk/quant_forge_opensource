"""Single-page workbench HTML template for the local web adapter."""

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
    llm_provider_options_json = _script_json(_provider_options_script_payload(provider_options))
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
      color-scheme: light;
      --ink: #17211d;
      --muted: #65736e;
      --faint: #87948e;
      --line: #d9e0dc;
      --line-strong: #b7c4be;
      --surface: #fbfcfa;
      --panel: #ffffff;
      --wash: #f2f6f1;
      --accent: #134b3c;
      --accent-2: #1f6f63;
      --blue: #265f8f;
      --bad: #9b2f31;
      --warn: #a36213;
      --mono: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace;
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
      color: #fff;
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
    @media (max-width: 900px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .control-rail {{
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }}
      .workbench {{ padding: 18px; }}
      .grid {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      .evidence-grid {{ grid-template-columns: 1fr; }}
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
    <div class="section-title">
      <h2>Factor Tape</h2>
      <p>解析、评价、回测集中展示</p>
    </div>
    <div id="error" class="err"></div>
    <div id="result">
      <div class="panel empty-state">
        <h3>等待输入</h3>
        <p class="meta">输入因子观点后运行，公式、IC、回测收益、缓存路径会在这里展开。</p>
      </div>
    </div>
    <div id="staggered-result"></div>
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
    <div class="section-title">
      <h2>Benchmark</h2>
      <p>qf factor bench 多因子横向对比</p>
    </div>
    <div id="bench-result">
      <div class="panel empty-state">
        <h3>暂无 bench 结果</h3>
        <p class="meta">运行 qf factor bench 后，多因子指标状态表会展示在这里。</p>
      </div>
    </div>
  </section>
</main>
<script>
const button = document.getElementById('run');
const validateButton = document.getElementById('validate-run');
const staggeredButton = document.getElementById('staggered-run');
const cancelButton = document.getElementById('cancel-run');
const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const resultEl = document.getElementById('result');
const staggeredResultEl = document.getElementById('staggered-result');
const llmProviderSelect = document.getElementById('llm-provider');
const llmApiKeyMode = document.getElementById('llm-api-key-mode');
const llmApiKeyInput = document.getElementById('llm-api-key');
const llmApiKeyStatus = document.getElementById('llm-api-key-status');
let llmProviderOptions = {llm_provider_options_json};
const validationInputs = {{
  holding_days: document.getElementById('param-holding-days'),
  decay_days: document.getElementById('param-decay-days'),
  top_quantile: document.getElementById('param-top-quantile'),
  execution_delay_days: document.getElementById('param-delay-days'),
  evaluation_start: document.getElementById('param-evaluation-start'),
  evaluation_end: document.getElementById('param-evaluation-end'),
  backtest_start: document.getElementById('param-backtest-start'),
  backtest_end: document.getElementById('param-backtest-end'),
  commission_bps: document.getElementById('param-commission-bps'),
  slippage_bps: document.getElementById('param-slippage-bps'),
  short_borrow_bps_annual: document.getElementById('param-short-borrow-bps')
}};
const rdRun = document.getElementById('rd-run');
const rdStart = document.getElementById('rd-start');
const rdStop = document.getElementById('rd-stop');
const rdCancel = document.getElementById('rd-cancel');
const rdStatusEl = document.getElementById('rd-status');
const rdResultEl = document.getElementById('rd-result');
const historyResultEl = document.getElementById('history-result');
const benchResultEl = document.getElementById('bench-result');
let activeIdeaJobId = null;
let activeRdJobId = null;
let parsedIdea = null;
let validatedFactorId = null;
const controlTokenRequired = {str(control_token_required).lower()};

function pct(value) {{
  if (value === undefined || value === null || Number.isNaN(Number(value))) return 'n/a';
  return (Number(value) * 100).toFixed(2) + '%';
}}
function num(value, digits = 4) {{
  if (value === undefined || value === null || Number.isNaN(Number(value))) return 'n/a';
  return Number(value).toFixed(digits);
}}
function metricNum(value, status, digits = 4) {{
  if (status && status !== 'available' && status !== 'legacy') return esc(status);
  return num(value, digits);
}}
function esc(value) {{
  return String(value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function profilePeriodText(profile) {{
  const start = profile.test_period_start || 'full available data';
  const end = profile.test_period_end || 'latest available data';
  return `${{start}} -> ${{end}}`;
}}
function clearGlobalError() {{
  errorEl.textContent = '';
}}
function resetIdeaResult(title, message) {{
  resultEl.innerHTML = `
    <div class="panel empty-state">
      <h3>${{esc(title)}}</h3>
      <p class="meta">${{esc(message)}}</p>
    </div>`;
}}
function resetStaggeredResult() {{
  staggeredResultEl.innerHTML = '';
}}
function resetRdResult(title, message) {{
  rdResultEl.innerHTML = `
    <div class="placeholder">
      <div class="panel">
        <h3>${{esc(title)}}</h3>
        <p class="meta">${{esc(message)}}</p>
      </div>
    </div>`;
}}
function optimizationStatusText(payload) {{
  const status = payload.optimization_status || (payload.optimization_performed ? 'performed' : 'no_optimization_performed');
  if (status === 'performed') return 'performed';
  if (status === 'attempted_no_acceptance') return 'attempted_no_acceptance';
  return 'no_optimization_performed';
}}
function setValidationInputsEnabled(enabled) {{
  Object.values(validationInputs).forEach(input => {{
    input.disabled = !enabled;
  }});
  validateButton.disabled = !enabled;
}}
function setStaggeredEnabled(enabled) {{
  staggeredButton.disabled = !enabled;
}}
function currentProviderOption() {{
  return llmProviderOptions.find(option => option.provider === llmProviderSelect.value) || null;
}}
function providerReadinessLabel(option) {{
  if (option.runtimeReady === 'true') {{
    return option.apiKeyEnv ? ` · env ${{option.apiKeyEnv}}` : ' · no auth';
  }}
  return option.apiKeyEnv ? ` · missing env ${{option.apiKeyEnv}}` : ' · not ready';
}}
function setRuntimeText(id, value) {{
  const element = document.getElementById(id);
  if (element) element.textContent = value || '未配置';
}}
function hydrateRuntimeStatus(status) {{
  const llm = status.llm || {{}};
  const rd = status.rd || {{}};
  const paths = status.paths || {{}};
  const llmLabel = `${{llm.provider || '未配置'}} / ${{llm.model || '未配置'}}`;
  const rdMode = `${{rd.hypothesis_mode || 'unknown'}}/${{rd.review_mode || 'unknown'}}`;
  const rdLabel = `${{rd.research_stage || 'research'}} ${{rdMode}} ${{rd.provider || ''}} ${{rd.model || ''}}`.trim();
  setRuntimeText('runtime-llm', llmLabel);
  setRuntimeText('runtime-rd', rdLabel);
  setRuntimeText('runtime-data-root', paths.data_root || '');
  setRuntimeText('runtime-factor-root', paths.factor_root || '');
  setRuntimeText('runtime-factor-values-root', paths.factor_values_root || '');
  setRuntimeText('runtime-factor-values-overlay-root', paths.factor_values_overlay_root || '');
  setRuntimeText('runtime-artifact-root', paths.artifact_root || '');
  setRuntimeText('runtime-llm-sr', `LLM parser: ${{llmLabel}}`);
  setRuntimeText('runtime-rd-sr', `RD optimizer: ${{rdLabel}}`);
  llmProviderOptions = (llm.providers || []).map(option => ({{
    provider: option.provider || '',
    model: option.model || '',
    apiKeyEnv: option['api' + '_key_env'] || '',
    runtimeReady: option['runtime' + '_ready'] || 'false'
  }}));
  if (llmProviderOptions.length) {{
    llmProviderSelect.innerHTML = llmProviderOptions.map(option => {{
      const selected = option.provider === llm.provider ? ' selected' : '';
      return `<option value="${{esc(option.provider)}}"${{selected}}>${{esc(option.provider)}} / ${{esc(option.model)}}${{esc(providerReadinessLabel(option))}}</option>`;
    }}).join('');
  }}
  const parserOption = document.querySelector('#parser option[value="llm"]');
  if (parserOption) parserOption.textContent = `LLM 语义解析: ${{llm.provider || '未配置 LLM provider'}}`;
  syncLlmApiKeyControls();
}}
async function refreshRuntimeStatus() {{
  if (!controlTokenRequired) return;
  const token = window.sessionStorage.getItem('qf_control_token') || '';
  if (!token) return;
  const response = await fetch('/api/status', {{
    headers: {{Authorization: `Bearer ${{token}}`}}
  }});
  if (!response.ok) return;
  hydrateRuntimeStatus(await response.json());
}}
function syncLlmApiKeyControls() {{
  const option = currentProviderOption();
  const keyEnv = option && option.apiKeyEnv ? option.apiKeyEnv : '';
  const configReady = option && option.runtimeReady === 'true';
  const manual = llmApiKeyMode.value === 'manual';
  llmApiKeyInput.disabled = !manual;
  if (!manual) llmApiKeyInput.value = '';
  if (manual) {{
    llmApiKeyInput.placeholder = '仅前端联调，不提交后端';
    llmApiKeyStatus.textContent = keyEnv
      ? `手动输入不会保存或提交；后端正式调用仍读取 ${{keyEnv}}`
      : '手动输入不会保存或提交；请在 local config 中配置 API key 环境变量名后运行';
    return;
  }}
  llmApiKeyInput.placeholder = configReady
    ? `已通过 ${{keyEnv || 'provider config'}} 加载`
    : (keyEnv ? `未检测到 ${{keyEnv}}` : '当前 provider 未配置 API key 环境变量名');
  llmApiKeyStatus.textContent = configReady
    ? 'API key 已由配置文件 / 环境变量加载，前端不展示密钥'
    : 'LLM 运行前需要在本地配置 API key 环境变量名并设置对应环境变量';
}}
function fillValidationInputs(parameters) {{
  const values = parameters || {{}};
  const evaluationPeriod = ((values.evaluation || {{}}).test_period) || {{}};
  const backtest = values.backtest || {{}};
  const backtestSimulation = backtest.simulation || {{}};
  const backtestPeriod = backtest.test_period || {{}};
  const costs = values.transaction_costs || {{}};
  const resolved = {{
    holding_days: values.holding_days,
    decay_days: valueOr(values.decay_days, backtestSimulation.decay_days),
    top_quantile: valueOr(values.top_quantile, backtestSimulation.top_quantile),
    execution_delay_days: valueOr(values.execution_delay_days, backtestSimulation.execution_delay_days),
    evaluation_start: valueOr(values.evaluation_start, evaluationPeriod.start),
    evaluation_end: valueOr(values.evaluation_end, evaluationPeriod.end),
    backtest_start: valueOr(values.backtest_start, backtestPeriod.start),
    backtest_end: valueOr(values.backtest_end, backtestPeriod.end),
    commission_bps: valueOr(values.commission_bps, costs.commission_bps),
    slippage_bps: valueOr(values.slippage_bps, costs.slippage_bps),
    short_borrow_bps_annual: valueOr(values.short_borrow_bps_annual, costs.short_borrow_bps_annual)
  }};
  Object.entries(validationInputs).forEach(([name, input]) => {{
    const value = resolved[name];
    input.value = value === undefined || value === null ? '' : value;
  }});
}}
function currentEvaluationSimulation() {{
  const source = (parsedIdea && parsedIdea.parameters && parsedIdea.parameters.evaluation) || {{}};
  const simulation = source.simulation || {{}};
  return {{
    decay_days: simulation.decay_days,
    top_quantile: simulation.top_quantile,
    execution_delay_days: simulation.execution_delay_days
  }};
}}
function validationParameters() {{
  const evaluationStart = validationInputs.evaluation_start.value || null;
  const evaluationEnd = validationInputs.evaluation_end.value || null;
  const backtestStart = validationInputs.backtest_start.value || null;
  const backtestEnd = validationInputs.backtest_end.value || null;
  const decayDays = Number(validationInputs.decay_days.value);
  const topQuantile = Number(validationInputs.top_quantile.value);
  const executionDelayDays = Number(validationInputs.execution_delay_days.value);
  const commissionBps = Number(validationInputs.commission_bps.value);
  const slippageBps = Number(validationInputs.slippage_bps.value);
  const shortBorrowBpsAnnual = Number(validationInputs.short_borrow_bps_annual.value);
  const payload = {{
    holding_days: Number(validationInputs.holding_days.value),
    decay_days: decayDays,
    top_quantile: topQuantile,
    execution_delay_days: executionDelayDays,
    evaluation_start: evaluationStart,
    evaluation_end: evaluationEnd,
    backtest_start: backtestStart,
    backtest_end: backtestEnd,
    commission_bps: commissionBps,
    slippage_bps: slippageBps,
    short_borrow_bps_annual: shortBorrowBpsAnnual,
    evaluation: {{
      test_period: {{ start: evaluationStart, end: evaluationEnd }}
    }},
    backtest: {{
      simulation: {{
        decay_days: decayDays,
        top_quantile: topQuantile,
        execution_delay_days: executionDelayDays
      }},
      test_period: {{ start: backtestStart, end: backtestEnd }}
    }},
    transaction_costs: {{
      commission_bps: commissionBps,
      slippage_bps: slippageBps,
      short_borrow_bps_annual: shortBorrowBpsAnnual
    }}
  }};
  const evaluationSimulation = currentEvaluationSimulation();
  if (
    evaluationSimulation.decay_days !== undefined ||
    evaluationSimulation.top_quantile !== undefined ||
    evaluationSimulation.execution_delay_days !== undefined
  ) {{
    payload.evaluation.simulation = evaluationSimulation;
  }}
  return payload;
}}
function valueOr(value, fallback) {{
  return value === undefined || value === null ? fallback : value;
}}
function hasStableDispersion(periods) {{
  return Number(periods || 0) > 1;
}}
function numIfStable(value, periods, digits = 2) {{
  return hasStableDispersion(periods) ? num(value, digits) : 'n/a';
}}
function pctIfStable(value, periods) {{
  return hasStableDispersion(periods) ? pct(value) : 'n/a';
}}
function metricPill(label, metric) {{
  if (!metric) return '';
  const status = metric.status || 'unknown';
  const method = metric.method ? ` · ${{metric.method}}` : '';
  const n = metric.observation_count !== undefined ? ` · N=${{metric.observation_count}}` : '';
  const value = metric.value === undefined || metric.value === null ? 'n/a' : num(metric.value, 4);
  return `<span class="pill">${{esc(label)}} ${{esc(value)}} · ${{esc(status)}}${{esc(method)}}${{esc(n)}}</span>`;
}}
function pctMetric(metric) {{
  if (!metric || metric.value === undefined || metric.value === null) return 'n/a';
  return pct(metric.value);
}}
function parserDefaultParameterMessage(parser) {{
  const source = (parser && parser.source) || '';
  if (source.toLowerCase() === 'llm') {{
    return 'LLM 已生成默认评测参数。确认或修改左侧参数后，点击“验证并评测”。';
  }}
  return '解析器已生成默认评测参数。确认或修改左侧参数后，点击“验证并评测”。';
}}
function assumptionLabel(text) {{
  if (text === 'rebalance_rate tracks component replacement per rebalance') {{
    return '调仓率 = 相邻调仓的成分替换率';
  }}
  if (text === 'turnover_rate estimates true portfolio weight turnover') {{
    return '换手率 = 基于组合权重变化估算的真实换手率';
  }}
  return text;
}}
function renderParsed(payload) {{
  const factor = payload.factor;
  resultEl.innerHTML = `
    <div class="panel hero-panel">
      <div>
        <h3>${{esc(factor.factor_id)}} · ${{esc(payload.parser.source)}} / ${{esc(payload.parser.provider)}} / ${{esc(payload.parser.model)}}</h3>
        <div class="formula">${{esc(factor.formula)}}</div>
        <p>${{esc(factor.description || '')}}</p>
        <p class="meta">${{esc(parserDefaultParameterMessage(payload.parser))}}</p>
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${{factor.horizon_days}}<br>
        ${{esc((factor.universe_filters || []).join(' · ') || 'FULL')}}
      </div>
    </div>
    <div class="panel">
      <h3>待确认参数</h3>
      <p>
        <span class="pill">holding ${{esc(payload.parameters.holding_days)}}d</span>
        <span class="pill">decay ${{esc(payload.parameters.decay_days)}}</span>
        <span class="pill">top ${{esc(payload.parameters.top_quantile)}}</span>
        <span class="pill">delay ${{esc(payload.parameters.execution_delay_days)}}d</span>
        <span class="pill">evaluation ${{esc(profilePeriodText({{test_period_start: payload.parameters.evaluation_start, test_period_end: payload.parameters.evaluation_end}}))}}</span>
        <span class="pill">backtest ${{esc(profilePeriodText({{test_period_start: payload.parameters.backtest_start, test_period_end: payload.parameters.backtest_end}}))}}</span>
        <span class="pill">commission ${{esc(payload.parameters.commission_bps)}} bps</span>
        <span class="pill">slippage ${{esc(payload.parameters.slippage_bps)}} bps</span>
        <span class="pill">short borrow ${{esc(payload.parameters.short_borrow_bps_annual)}} bps/year</span>
      </p>
    </div>`;
}}
function render(payload) {{
  const factor = payload.factor;
  const evaluation = payload.evaluation;
  const inSampleBacktest = payload.in_sample_backtest || null;
  const backtest = payload.backtest;
  const effectiveHoldingDays = (payload.parameters && payload.parameters.holding_days) || backtest.holding_days || factor.horizon_days;
  const evaluationProfile = evaluation.simulation_profile || {{}};
  const inSampleProfile = inSampleBacktest ? (inSampleBacktest.simulation_profile || {{}}) : {{}};
  const backtestProfile = backtest.simulation_profile || {{}};
  const profile = Object.keys(backtestProfile).length ? backtestProfile : evaluationProfile;
  const splitRows = (evaluation.split_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} ICIR ${{metricNum(metric.rank_icir, metric.rank_icir_status, 2)}} · HAC t ${{metricNum(metric.rank_ic_t_stat, metric.rank_ic_t_stat_status, 2)}} · days ${{metric.ic_days}}</span>`
  ).join(' ');
  const horizonRows = (evaluation.horizon_metrics || []).map(metric =>
    `<span class="pill">${{metric.horizon_days}}日 IC ${{metricNum(metric.rank_ic_mean, metric.rank_ic_mean_status)}} / ICIR ${{metricNum(metric.rank_icir, metric.rank_icir_status, 2)}} / HAC t ${{metricNum(metric.rank_ic_t_stat, metric.rank_ic_t_stat_status, 2)}}</span>`
  ).join(' ');
  const groupRows = (backtest.group_returns || []).map(metric =>
    `<span class="pill">${{esc(metric.group)}} ${{pct(metric.mean_return)}}</span>`
  ).join(' ');
  const segmentRows = (backtest.segment_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} net ann ${{pct(metric.net_annualized_return)}} · sharpe ${{num(metric.net_long_short_sharpe, 2)}}</span>`
  ).join(' ');
  const warningRows = [
    ...(evaluation.warning_codes || []),
    ...(backtest.warning_codes || []),
    ...(evaluation.warnings || []),
    ...(backtest.warnings || [])
  ].map(item =>
    `<span class="pill">${{esc(item)}}</span>`
  ).join(' ');
  const assumptionRows = (backtest.assumptions || []).map(item =>
    `<span class="pill">${{esc(assumptionLabel(item))}}</span>`
  ).join(' ');
  const cacheRows = [
    `eval ${{evaluation.score_source || 'computed'}} · cached ${{evaluation.score_cached_rows || 0}} · computed ${{evaluation.score_computed_rows || 0}}`,
    evaluation.factor_values_path ? `eval path ${{evaluation.factor_values_path}}` : '',
    `backtest ${{backtest.score_source || 'computed'}} · cached ${{backtest.score_cached_rows || 0}} · computed ${{backtest.score_computed_rows || 0}}`,
    backtest.factor_values_path ? `backtest path ${{backtest.factor_values_path}}` : ''
  ].filter(Boolean).map(item => `<span class="pill">${{esc(item)}}</span>`).join(' ');
  const evaluationMetrics = evaluation.metrics || {{}};
  const backtestMetrics = backtest.metrics || {{}};
  const metricRows = [
    metricPill('HAC t-stat', evaluationMetrics.rank_ic_t_stat),
    metricPill('可报告毛年化收益', backtestMetrics.annualized_return),
    metricPill('可报告净年化收益', backtestMetrics.net_annualized_return),
    metricPill('净值最大回撤', backtestMetrics.max_drawdown),
    metricPill('再平衡换手', backtestMetrics.rebalance_turnover_mean || backtestMetrics.rebalance_rate)
  ].filter(Boolean).join(' ');
  const coverage = evaluation.coverage_lineage || {{}};
  const singlePeriodWarning = Number(backtest.periods || 0) === 1
    ? `<div class="notice warn">外部样本外仅包含 1 个完整持有期。累计收益可计算；年化收益、波动率、Sharpe、再平衡率以及无日频净值支持的最大回撤不可报告。</div>`
    : '';
  resultEl.innerHTML = `
    <div class="panel hero-panel">
      <div>
        <h3>${{esc(factor.factor_id)}} · ${{esc(payload.parser.source)}} / ${{esc(payload.parser.provider)}} / ${{esc(payload.parser.model)}}</h3>
        <div class="formula">${{esc(factor.formula)}}</div>
        <p>${{esc(factor.description || '')}}</p>
        <p class="meta">evaluation period: ${{esc(profilePeriodText(evaluationProfile))}}</p>
        <p class="meta">backtest period: ${{esc(profilePeriodText(backtestProfile))}}</p>
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${{effectiveHoldingDays}}<br>
        ${{esc((factor.universe_filters || []).join(' · ') || 'FULL')}}
      </div>
    </div>
    <div class="panel">
      <h3>样本内研究评价</h3>
      <div class="grid">
        <div class="tile">Rank IC<b>${{metricNum(evaluation.rank_ic_mean, evaluation.rank_ic_mean_status)}}</b></div>
        <div class="tile">ICIR<b>${{metricNum(evaluation.rank_icir, evaluation.rank_icir_status, 2)}}</b></div>
        <div class="tile">HAC t-stat<b>${{metricNum(evaluation.rank_ic_t_stat, evaluation.rank_ic_t_stat_status, 2)}}</b></div>
        <div class="tile">IC Days<b>${{evaluation.ic_days}}</b></div>
        <div class="tile">Joint Coverage<b>${{pct(valueOr(coverage.joint_coverage, evaluation.coverage))}}</b></div>
        <div class="tile">Horizon / Delay<b>${{effectiveHoldingDays}}日 / ${{valueOr(evaluationProfile.execution_delay_days, profile.execution_delay_days)}}日</b></div>
      </div>
      <p class="meta">research_evaluation · ${{esc(profilePeriodText(evaluationProfile))}}</p>
    </div>
    ${{inSampleBacktest ? `
    <div class="panel">
      <h3>样本内组合回测</h3>
      <div class="grid">
        <div class="tile">毛累计收益<b>${{pct(inSampleBacktest.gross_cumulative_return ?? inSampleBacktest.cumulative_return)}}</b></div>
        <div class="tile">净累计收益<b>${{pct(inSampleBacktest.net_cumulative_return)}}</b></div>
        <div class="tile">完整持有期数<b>${{valueOr(inSampleBacktest.completed_periods, inSampleBacktest.periods)}}</b></div>
        <div class="tile">Exposure Days<b>${{valueOr(inSampleBacktest.exposure_days, 0)}}</b></div>
        <div class="tile">可报告净年化收益<b>${{pct(inSampleBacktest.net_annualized_return)}}</b></div>
        <div class="tile">年化Sharpe<b>${{num(inSampleBacktest.net_long_short_sharpe ?? inSampleBacktest.long_short_sharpe, 2)}}</b></div>
        <div class="tile">净值最大回撤<b>${{pct(inSampleBacktest.net_max_drawdown ?? inSampleBacktest.max_drawdown)}}</b></div>
        <div class="tile">Rebalance Turnover<b>${{pct(inSampleBacktest.rebalance_turnover_mean ?? inSampleBacktest.turnover_rate)}}</b></div>
      </div>
      <p class="meta">${{esc(inSampleBacktest.sample_role || 'in_sample_backtest')}} · ${{esc(profilePeriodText(inSampleProfile))}}</p>
    </div>` : ''}}
    <div class="panel">
      <h3>外部样本外组合评测</h3>
      ${{singlePeriodWarning}}
      <div class="grid">
        <div class="tile">毛累计收益<b>${{pct(backtest.gross_cumulative_return ?? backtest.cumulative_return)}}</b></div>
        <div class="tile">净累计收益<b>${{pct(backtest.net_cumulative_return)}}</b></div>
        <div class="tile">完整持有期数<b>${{valueOr(backtest.completed_periods, backtest.periods)}}</b></div>
        <div class="tile">Exposure Days<b>${{valueOr(backtest.exposure_days, 0)}}</b></div>
        <div class="tile">可报告毛年化收益<b>${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</b></div>
        <div class="tile">可报告净年化收益<b>${{pct(backtest.net_annualized_return)}}</b></div>
        <div class="tile">年化波动率<b>${{pct(backtest.net_annualized_volatility ?? backtest.annualized_volatility)}}</b></div>
        <div class="tile">年化Sharpe<b>${{num(backtest.net_long_short_sharpe ?? backtest.long_short_sharpe, 2)}}</b></div>
        <div class="tile">净值最大回撤<b>${{pct(backtest.net_max_drawdown ?? backtest.max_drawdown)}}</b></div>
        <div class="tile">Initial Build Turnover<b>${{pct(backtest.initial_build_turnover)}}</b></div>
        <div class="tile">Rebalance Turnover<b>${{pct(backtest.rebalance_turnover_mean ?? backtest.turnover_rate)}}</b></div>
        <div class="tile">Replacement Rate<b>${{pct(backtest.replacement_rate_mean ?? backtest.rebalance_rate)}}</b></div>
        <div class="tile">持有期<b>${{backtest.holding_days}}日</b></div>
        <div class="tile">Decay<b>${{valueOr(profile.decay_days, 0)}}</b></div>
        <div class="tile">Top Quantile<b>${{num(valueOr(profile.top_quantile, valueOr(backtest.top_quantile, 0)), 2)}}</b></div>
        <div class="tile">Delay<b>${{valueOr(profile.execution_delay_days, 1)}}日</b></div>
      </div>
      <p class="meta">external_oos_backtest · ${{esc(profilePeriodText(backtestProfile))}}</p>
    </div>
    <div class="panel">
      <h3>样本充分性与诊断</h3>
      <p>${{metricRows || '<span class="pill">暂无指标状态</span>'}}</p>
      <p>${{warningRows || '<span class="pill">研究口径，不是生产交易口径</span>'}}</p>
      <p>${{cacheRows || '<span class="pill">computed</span>'}}</p>
    </div>
    <div class="evidence-grid">
      <div class="panel">
        <h3>三段验证</h3>
        <p>${{splitRows || '<span class="pill">暂无</span>'}}</p>
        <h3>回测分段</h3>
        <p>${{segmentRows || '<span class="pill">暂无</span>'}}</p>
        <h3>多周期评价</h3>
        <p>${{horizonRows || '<span class="pill">暂无</span>'}}</p>
      </div>
      <div class="panel">
        <h3>分组收益</h3>
        <p>${{groupRows || '<span class="pill">暂无</span>'}}</p>
        <h3>风险提示</h3>
        <p>${{warningRows || '<span class="pill">研究口径，不是生产交易口径</span>'}}</p>
        <h3>口径说明</h3>
        <p>${{assumptionRows || '<span class="pill">研究口径，不是生产交易口径</span>'}}</p>
        <h3>因子值缓存</h3>
        <p>${{cacheRows || '<span class="pill">computed</span>'}}</p>
      </div>
    </div>
    <div class="panel">
      <h3>Artifacts</h3>
      <p class="meta">${{esc(evaluation.artifact_path)}}</p>
      ${{inSampleBacktest ? `<p class="meta">${{esc(inSampleBacktest.artifact_path)}}</p>` : ''}}
      <p class="meta">${{esc(backtest.artifact_path)}}</p>
    </div>`;
}}
function renderStaggered(payload) {{
  const terminal = (payload.daily_nav || []).slice(-1)[0] || {{}};
  const cohortRows = (payload.cohorts || []).map(cohort =>
    `<span class="pill">${{esc(cohort.signal_date)}} · weight ${{pct(cohort.capital_weight)}} · net ${{pct(cohort.net_cumulative_return)}}</span>`
  ).join(' ');
  staggeredResultEl.innerHTML = `
    <div class="panel">
      <h3>首月逐日建仓稳健性回测</h3>
      <div class="grid">
        <div class="tile">Staggered 净累计收益<b>${{pct(payload.strategy_cumulative_return)}}</b></div>
        <div class="tile">基准累计收益<b>${{pct(payload.benchmark_cumulative_return)}}</b></div>
        <div class="tile">相对财富收益<b>${{pct(payload.relative_wealth_excess_return)}}</b></div>
        <div class="tile">Cohorts<b>${{valueOr(payload.cohort_count, 0)}}</b></div>
        <div class="tile">Terminal NAV<b>${{num(terminal.net_nav, 4)}}</b></div>
        <div class="tile">Inactive Cash<b>${{pct(terminal.inactive_cash_weight)}}</b></div>
      </div>
      <p class="meta">${{esc(payload.sample_role || 'staggered_entry_backtest')}} · ${{esc(payload.formation_window_mode || 'first_month')}}</p>
      <p>${{cohortRows || '<span class="pill">暂无 cohort 明细</span>'}}</p>
      <p class="meta">${{esc(payload.artifact_path || '')}}</p>
    </div>`;
}}
function comparisonRows(payload) {{
  const chain = payload.iteration_chain || {{}};
  return payload.comparison_rows || chain.comparison_rows || [];
}}
function renderComparisonTable(payload) {{
  const rows = comparisonRows(payload);
  const body = rows.map(row => {{
    const gate = row.gate_passed === true ? 'pass' : (row.gate_passed === false ? 'fail' : 'n/a');
    return `
      <tr>
        <td>${{esc(row.round || 1)}}</td>
        <td>${{esc(row.role || '')}}</td>
        <td><code>${{esc(row.factor_id || '')}}</code><br><span class="meta">${{esc(row.factor_status || '')}}</span></td>
        <td><code>${{esc(row.formula || '')}}</code></td>
        <td>${{num(row.selection_score, 4)}}<br><span class="meta">IC ${{num(row.selection_rank_ic, 4)}} / ICIR ${{num(row.selection_icir, 2)}}</span></td>
        <td>${{pct(row.selection_net_cumulative_return)}}<br><span class="meta">ann ${{pct(row.selection_net_annualized_return)}} · periods ${{esc(row.selection_completed_periods ?? row.selection_backtest_periods ?? '')}}</span></td>
        <td>${{pct(row.external_oos_net_cumulative_return)}}<br><span class="meta">ann ${{pct(row.external_oos_net_annualized_return)}} · periods ${{esc(row.external_oos_completed_periods ?? row.external_oos_periods ?? '')}}</span></td>
        <td>${{esc(gate)}}<br><span class="meta">${{esc((row.gate_reasons || []).join('; '))}}</span></td>
      </tr>`;
  }}).join('');
  return `
    <div class="panel">
      <h3>RD 因子迭代对比</h3>
      <p class="meta">selection 样本用于 RD 排序和 gate；external OOS 只用于审计展示，不参与 winner 选择。</p>
      <table class="comparison-table">
        <thead>
          <tr>
            <th>轮次</th>
            <th>角色</th>
            <th>因子</th>
            <th>公式</th>
            <th>Selection</th>
            <th>样本内回测</th>
            <th>External OOS</th>
            <th>Gate</th>
          </tr>
        </thead>
        <tbody>${{body || '<tr><td colspan="8">暂无比较行</td></tr>'}}</tbody>
      </table>
    </div>`;
}}
function renderResearch(payload) {{
  const candidates = payload.candidates || [];
  const chain = payload.iteration_chain || {{}};
  const rounds = chain.rounds || [];
  const reportPaths = payload.round_report_paths || chain.round_report_paths || [];
  const aggregateAccepted = Array.from(new Set([
    ...((payload.accepted_candidate_ids || []).filter(Boolean)),
    ...rounds.flatMap(item => item.accepted_candidate_ids || []).filter(Boolean)
  ]));
  const recommendedFactor = payload.recommended_factor_id || payload.final_factor_id || 'none';
  const lastAcceptedFactor = payload.last_accepted_factor_id || 'none';
  const lastExploredFactor = payload.last_explored_factor_id || payload.final_factor_id || 'none';
  const recommendationBasis = payload.recommendation_basis || (payload.last_accepted_factor_id ? 'accepted_candidate' : 'original_seed_retained');
  const recommendationLabel = recommendationBasis === 'accepted_candidate'
    ? '通过 gate 的最终推荐'
    : '无通过 gate 候选，保留原始 seed';
  const explorationSeed = payload.next_exploration_seed_factor_id || 'none';
  const explorationReason = payload.next_exploration_seed_reason || 'none';
  const explorationGate = payload.next_exploration_seed_gate_passed === true
    ? '通过 gate'
    : (payload.next_exploration_seed_gate_passed === false ? '未过 gate，仅用于探索' : '无下一轮探索 seed');
  const optimizationLabel = optimizationStatusText(payload);
  const optimizationScope = Number(payload.iteration_count || 1) > 1 ? ' (aggregate)' : '';
  const chainError = payload.chain_error || chain.chain_error || '';
  const failedRoundIndex = payload.failed_round_index || chain.failed_round_index || '';
  const partialNotice = chainError
    ? `<p class="meta err">RD stopped at round ${{esc(failedRoundIndex || '?')}}: ${{esc(chainError)}}</p>`
    : '';
  const roundRows = rounds.map(item =>
    `<span class="pill">#${{item.round}} seed ${{esc(item.seed_factor_id)}} → ${{esc(item.selected_next_seed_factor_id || item.top_candidate_id || 'stop')}} · ${{esc(item.selection_reason || 'completed')}} · score ${{item.top_score === null || item.top_score === undefined ? 'n/a' : num(item.top_score, 4)}}</span>`
  ).join(' ');
  const reportRows = reportPaths.map(path => `<span class="pill">${{esc(path)}}</span>`).join(' ');
  const cards = candidates.map(candidate => {{
    const factor = candidate.factor;
    const evaluation = candidate.evaluation;
    const backtest = candidate.backtest;
    const evaluationProfile = evaluation.simulation_profile || {{}};
    const backtestProfile = backtest.simulation_profile || {{}};
    const profile = Object.keys(backtestProfile).length ? backtestProfile : evaluationProfile;
    const gate = candidate.gate_passed ? '<span class="ok">candidate</span>' : '<span class="err">draft</span>';
    const cacheText = `${{evaluation.score_source || 'computed'}} / ${{backtest.score_source || 'computed'}} · cached ${{evaluation.score_cached_rows || 0}}/${{backtest.score_cached_rows || 0}} · computed ${{evaluation.score_computed_rows || 0}}/${{backtest.score_computed_rows || 0}}`;
    const cachePaths = [evaluation.factor_values_path, backtest.factor_values_path].filter(Boolean).join(' / ');
    const artifacts = [evaluation.artifact_path, backtest.artifact_path].filter(Boolean).join(' / ');
    const reviewWarnings = ((candidate.self_review && candidate.self_review.normalization_warnings) || []).join('; ');
    return `
      <div class="panel hero-panel">
        <div>
          <h3>${{esc(factor.factor_id)}} · ${{gate}}</h3>
          <div class="formula">${{esc(factor.formula)}}</div>
          <p>${{esc(candidate.hypothesis.text)}}</p>
          <p class="meta">${{esc(candidate.hypothesis.rationale)}}</p>
          <p class="meta">evaluation period: ${{esc(profilePeriodText(evaluationProfile))}}</p>
          <p class="meta">backtest period: ${{esc(profilePeriodText(backtestProfile))}}</p>
          <p class="meta">研究口径，不是生产交易口径。</p>
        </div>
        <div class="formula-badge">
          score<br>${{num(candidate.score, 4)}}
        </div>
        <p>
          <span class="pill">score ${{num(candidate.score, 4)}}</span>
          <span class="pill">split ICIR ${{num(valueOr(candidate.split_weighted_icir, 0), 2)}}</span>
          <span class="pill">IC ${{metricNum(evaluation.rank_ic_mean, evaluation.rank_ic_mean_status)}}</span>
          <span class="pill">ICIR ${{metricNum(evaluation.rank_icir, evaluation.rank_icir_status, 2)}}</span>
          <span class="pill">HAC t-stat ${{metricNum(evaluation.rank_ic_t_stat, evaluation.rank_ic_t_stat_status, 2)}}</span>
          <span class="pill">decay ${{valueOr(profile.decay_days, 0)}}</span>
          <span class="pill">top ${{num(valueOr(profile.top_quantile, valueOr(backtest.top_quantile, 0)), 2)}}</span>
          <span class="pill">periods ${{esc(backtest.periods)}}</span>
          <span class="pill">net LS Sharpe ${{num(backtest.net_long_short_sharpe ?? backtest.long_short_sharpe, 2)}}</span>
          <span class="pill">gross ${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</span>
          <span class="pill">net ${{pct(backtest.net_annualized_return)}}</span>
          <span class="pill">rebalance rate ${{pct(backtest.rebalance_rate)}}</span>
          <span class="pill">turnover rate ${{pct(backtest.turnover_rate)}}</span>
          <span class="pill">factor cache ${{esc(cacheText)}}</span>
        </p>
        <p class="meta">${{esc((candidate.self_review && candidate.self_review.summary) || '')}}</p>
        <p class="meta">review normalization: ${{esc(reviewWarnings || 'none')}}</p>
        <p class="meta">factor_values: ${{esc(cachePaths || 'none')}}</p>
        <p class="meta">artifacts: ${{esc(artifacts || 'not generated')}}</p>
        <p class="meta">${{esc((backtest.warnings || []).join('; ') || 'research semantics, not production trading semantics')}}</p>
        <p class="meta">${{esc((candidate.gate_reasons || []).join('; '))}}</p>
      </div>`;
  }}).join('');
  rdResultEl.innerHTML = `
    <div class="panel">
      <h3>${{esc(payload.seed_factor_id)}} · ${{esc(payload.objective)}}</h3>
      <p class="meta">workflow: ${{esc(payload.workflow_type || payload.rd_stage || 'research')}}</p>
      <p class="meta">iterations: ${{esc(payload.iteration_count || 1)}} / ${{esc(payload.requested_iterations || 1)}} · original seed ${{esc(payload.original_seed_factor_id || payload.seed_factor_id)}} · recommended factor ${{esc(recommendedFactor)}} (${{esc(recommendationLabel)}}) · last accepted ${{esc(lastAcceptedFactor)}} · last explored ${{esc(lastExploredFactor)}} · ${{esc(payload.stopped_reason || 'completed')}}</p>
      <p class="meta">next exploration seed: ${{esc(explorationSeed)}} · ${{esc(explorationReason)}} · ${{esc(explorationGate)}}</p>
      <p class="meta">optimization: ${{esc(optimizationLabel)}}${{optimizationScope}}</p>
      ${{partialNotice}}
      <p class="meta">accepted: ${{esc(aggregateAccepted.join(', ') || 'none')}}</p>
      <p class="meta">report: ${{esc(payload.report_path || 'not generated')}}</p>
      <p class="meta">round reports: ${{reportRows || '<span class="pill">same as report</span>'}}</p>
      <p>${{roundRows || '<span class="pill">single round</span>'}}</p>
    </div>
    ${{cards || '<div class="panel"><h3>无候选</h3></div>'}}
    ${{renderComparisonTable(payload)}}`;
}}
function rdPayload() {{
  return {{
    seed_factor_id: document.getElementById('rd-seed').value,
    objective: document.getElementById('rd-objective').value,
    max_candidates: Number(document.getElementById('rd-max').value),
    iterations: Number(document.getElementById('rd-iterations').value)
  }};
}}
function sleep(ms) {{
  return new Promise(resolve => setTimeout(resolve, ms));
}}
function controlHeaders() {{
  const headers = {{'Content-Type': 'application/json'}};
  if (!controlTokenRequired) return headers;
  let token = window.sessionStorage.getItem('qf_control_token') || '';
  if (!token) {{
    token = window.prompt('请输入本次 Web 控制令牌') || '';
    if (token) {{
      window.sessionStorage.setItem('qf_control_token', token);
      setTimeout(() => refreshRuntimeStatus().catch(() => {{}}), 0);
    }}
  }}
  if (!token) throw new Error('需要 Web 控制令牌');
  headers.Authorization = `Bearer ${{token}}`;
  return headers;
}}
async function postJson(url, payload) {{
  const response = await fetch(url, {{
    method: 'POST',
    headers: controlHeaders(),
    body: JSON.stringify(payload)
  }});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}}
async function getJob(jobId) {{
  const response = await fetch(`/api/jobs/${{encodeURIComponent(jobId)}}`, {{
    headers: controlHeaders()
  }});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}}
async function cancelJob(jobId) {{
  return postJson(`/api/jobs/${{encodeURIComponent(jobId)}}/cancel`, {{}});
}}
function storedControlHeaders() {{
  if (!controlTokenRequired) return {{}};
  const token = window.sessionStorage.getItem('qf_control_token') || '';
  if (!token) return null;
  return {{Authorization: `Bearer ${{token}}`}};
}}
async function fetchPanelJson(url) {{
  const headers = storedControlHeaders();
  if (headers === null) return null;
  const response = await fetch(url, {{headers}});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}}
function metricValueText(entry, digits = 4) {{
  if (!entry) return 'not_recorded';
  const status = entry.status || 'unknown';
  if (status === 'available') return num(entry.value, digits);
  return esc(status);
}}
function metricStatusSuffix(entry) {{
  if (!entry) return '';
  const status = entry.status || 'unknown';
  const n = entry.observation_count === undefined || entry.observation_count === null ? '' : ` · N=${{entry.observation_count}}`;
  return `${{esc(status)}}${{n}}`;
}}
function renderHistory(payload) {{
  const rows = (payload && payload.runs) || [];
  if (!rows.length) {{
    historyResultEl.innerHTML = `
      <div class="panel empty-state">
        <h3>暂无研究历史</h3>
        <p class="meta">评价、回测、bench、RD 运行记录到 run index 后会展示在这里。</p>
      </div>`;
    return;
  }}
  const body = rows.map(row => {{
    const dataWindow = row.data_window || {{}};
    const windowText = dataWindow.status === 'available'
      ? `${{dataWindow.start_date}} .. ${{dataWindow.end_date}}`
      : (dataWindow.status || 'unavailable');
    const highlights = Object.entries(row.metric_highlights || {{}}).map(([name, entry]) =>
      `<span class="pill">${{esc(name)}} ${{metricValueText(entry)}} · ${{metricStatusSuffix(entry)}}</span>`
    ).join(' ');
    return `
      <tr>
        <td>${{esc(row.kind || '')}}<br><span class="meta">${{esc(row.run_id || '')}}</span></td>
        <td>${{esc(row.created_at || '')}}</td>
        <td><code>${{esc((row.factor_ids || []).join(', '))}}</code></td>
        <td>${{esc(windowText)}}<br><span class="meta">warnings ${{esc(row.warnings_count ?? 'n/a')}}</span></td>
        <td>${{highlights || '<span class="pill">无指标摘要</span>'}}</td>
      </tr>`;
  }}).join('');
  historyResultEl.innerHTML = `
    <div class="panel">
      <h3>研究历史 · 最近 ${{rows.length}} 条</h3>
      <table class="comparison-table">
        <thead>
          <tr>
            <th>类型 / run_id</th>
            <th>时间</th>
            <th>因子</th>
            <th>数据窗口 / 状态</th>
            <th>指标摘要</th>
          </tr>
        </thead>
        <tbody>${{body}}</tbody>
      </table>
    </div>`;
}}
function renderBench(payload) {{
  const latest = payload && payload.latest;
  if (!latest) {{
    benchResultEl.innerHTML = `
      <div class="panel empty-state">
        <h3>暂无 bench 结果</h3>
        <p class="meta">运行 qf factor bench 后，多因子指标状态表会展示在这里。</p>
      </div>`;
    return;
  }}
  if (!latest.available) {{
    benchResultEl.innerHTML = `
      <div class="panel">
        <h3>Benchmark · ${{esc(latest.run_id || '')}}</h3>
        <p class="meta">${{esc(latest.reason || 'bench artifact 不可用')}}</p>
      </div>`;
    return;
  }}
  const factors = latest.factors || [];
  const metricNames = Array.from(new Set(factors.flatMap(row => Object.keys(row.metrics || {{}}))));
  const head = metricNames.map(name => `<th>${{esc(name)}}</th>`).join('');
  const body = factors.map(row => {{
    const statusCell = row.status === 'error'
      ? `<span class="err">error</span><br><span class="meta">${{esc(row.error || '')}}</span>`
      : `${{esc(row.status || '')}}<br><span class="meta">warnings ${{esc(row.warnings_count ?? 'n/a')}}</span>`;
    const cells = metricNames.map(name => {{
      const entry = (row.metrics || {{}})[name];
      if (!entry) return '<td><span class="meta">not_recorded</span></td>';
      return `<td>${{metricValueText(entry)}}<br><span class="meta">${{metricStatusSuffix(entry)}}</span></td>`;
    }}).join('');
    return `
      <tr>
        <td><code>${{esc(row.factor_id || '')}}</code></td>
        <td>${{statusCell}}</td>
        ${{cells}}
      </tr>`;
  }}).join('');
  const summary = latest.summary || {{}};
  benchResultEl.innerHTML = `
    <div class="panel">
      <h3>Benchmark · ${{esc(latest.run_id || '')}}</h3>
      <p class="meta">${{esc(latest.created_at || '')}} · evaluated ${{esc(summary.evaluated_factor_count ?? 'n/a')}} · errors ${{esc(summary.error_factor_count ?? 'n/a')}} · 指标不可用时展示状态标签，不显示为 0。</p>
      <table class="comparison-table">
        <thead>
          <tr>
            <th>因子</th>
            <th>状态</th>
            ${{head}}
          </tr>
        </thead>
        <tbody>${{body || '<tr><td colspan="2">暂无因子行</td></tr>'}}</tbody>
      </table>
    </div>`;
}}
async function refreshHistoryPanel() {{
  try {{
    const payload = await fetchPanelJson('/api/research/history');
    if (payload) renderHistory(payload);
  }} catch (error) {{
    historyResultEl.innerHTML = `<div class="panel"><h3>研究历史</h3><p class="meta err">${{esc(error.message)}}</p></div>`;
  }}
}}
async function refreshBenchPanel() {{
  try {{
    const payload = await fetchPanelJson('/api/bench');
    if (payload) renderBench(payload);
  }} catch (error) {{
    benchResultEl.innerHTML = `<div class="panel"><h3>Benchmark</h3><p class="meta err">${{esc(error.message)}}</p></div>`;
  }}
}}
async function waitForJob(jobId, statusEl, slowText, isActive) {{
  const slowTimer = setTimeout(() => {{
    if (isActive(jobId)) {{
      statusEl.innerHTML = `<span class="warn">${{esc(slowText)}}</span>`;
    }}
  }}, 10000);
  try {{
    while (isActive(jobId)) {{
      const job = await getJob(jobId);
      if (job.status === 'completed') return job.result;
      if (job.status === 'failed') throw new Error(job.error || 'request failed');
      if (job.status === 'cancelled') throw new Error('运行已中断');
      if (job.slow) {{
        statusEl.innerHTML = `<span class="warn">${{esc(slowText)}} · ${{Math.round(job.runtime_seconds)}}s</span>`;
      }}
      await sleep(750);
    }}
    throw new Error('运行已中断');
  }} finally {{
    clearTimeout(slowTimer);
  }}
}}
async function submitParse(parserMode) {{
  const job = await postJson('/api/jobs/parse-idea', {{
      text: document.getElementById('idea').value,
      parser_mode: parserMode,
      llm_provider: llmProviderSelect.value
  }});
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，LLM 仍在解析因子',
    jobId => activeIdeaJobId === jobId
  );
}}
async function submitValidation() {{
  if (!parsedIdea) throw new Error('请先解析因子');
  const job = await postJson('/api/jobs/validate-idea', {{
      factor: parsedIdea.factor,
      parser: parsedIdea.parser,
      parameters: validationParameters()
  }});
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，系统仍在计算因子或回测',
    jobId => activeIdeaJobId === jobId
  );
}}
async function submitStaggeredEntry() {{
  const factorId = validatedFactorId || (parsedIdea && parsedIdea.factor && parsedIdea.factor.factor_id);
  if (!factorId) throw new Error('请先完成验证并评测');
  const job = await postJson('/api/jobs/staggered-entry', {{
      factor_id: factorId,
      parameters: validationParameters()
  }});
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，系统仍在执行首月逐日建仓稳健性回测',
    jobId => activeIdeaJobId === jobId
  );
}}
llmProviderSelect.addEventListener('change', syncLlmApiKeyControls);
llmApiKeyMode.addEventListener('change', syncLlmApiKeyControls);
syncLlmApiKeyControls();
refreshRuntimeStatus().catch(() => {{}});
refreshHistoryPanel();
refreshBenchPanel();
button.addEventListener('click', async () => {{
  button.disabled = true;
  validateButton.disabled = true;
  cancelButton.disabled = true;
  clearGlobalError();
  resetIdeaResult('解析中', '因子解析完成后，公式和默认评测参数会在这里刷新。');
  statusEl.textContent = '解析中...';
  parsedIdea = null;
  validatedFactorId = null;
  fillValidationInputs({{}});
  setValidationInputsEnabled(false);
  setStaggeredEnabled(false);
  resetStaggeredResult();
  const parserMode = document.getElementById('parser').value;
  try {{
    const payload = await submitParse(parserMode);
    parsedIdea = payload;
    fillValidationInputs(payload.parameters);
    setValidationInputsEnabled(true);
    renderParsed(payload);
    statusEl.innerHTML = '<span class="ok">解析完成，等待确认参数</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }}
    if (parserMode === 'llm') {{
      const fallback = window.confirm(`LLM 无法使用：${{error.message}}\n\n是否改用本地规则解析？`);
      if (fallback) {{
        try {{
          const payload = await submitParse('rule');
          parsedIdea = payload;
          fillValidationInputs(payload.parameters);
          setValidationInputsEnabled(true);
          renderParsed(payload);
          statusEl.innerHTML = '<span class="ok">已使用本地规则解析，等待确认参数</span>';
          return;
        }} catch (fallbackError) {{
          errorEl.textContent = fallbackError.message;
          statusEl.textContent = '运行失败';
          return;
        }}
      }}
    }}
    errorEl.textContent = error.message;
    statusEl.textContent = '运行失败';
  }} finally {{
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
  }}
}});
validateButton.addEventListener('click', async () => {{
  validateButton.disabled = true;
  button.disabled = true;
  cancelButton.disabled = true;
  clearGlobalError();
  resetIdeaResult('验证与评测中', '评测完成后，IC、回测收益和 artifact 路径会在这里刷新。');
  statusEl.textContent = '验证与评测中...';
  try {{
    const payload = await submitValidation();
    render(payload);
    parsedIdea = {{
      parser: payload.parser,
      factor: payload.factor,
      parameters: payload.parameters || validationParameters()
    }};
    validatedFactorId = payload.factor.factor_id;
    fillValidationInputs(parsedIdea.parameters);
    document.getElementById('rd-seed').value = payload.factor.factor_id;
    setStaggeredEnabled(true);
    statusEl.innerHTML = '<span class="ok">验证完成</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }}
    errorEl.textContent = error.message;
    statusEl.textContent = '验证失败';
  }} finally {{
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
    setValidationInputsEnabled(Boolean(parsedIdea));
    setStaggeredEnabled(Boolean(validatedFactorId));
  }}
}});
staggeredButton.addEventListener('click', async () => {{
  staggeredButton.disabled = true;
  validateButton.disabled = true;
  button.disabled = true;
  cancelButton.disabled = true;
  clearGlobalError();
  staggeredResultEl.innerHTML = `
    <div class="panel">
      <h3>首月逐日建仓稳健性回测运行中</h3>
      <p class="meta">完成后会显示 cohort、等权组合 NAV 和 artifact。</p>
    </div>`;
  statusEl.textContent = '首月逐日建仓稳健性回测中...';
  try {{
    const payload = await submitStaggeredEntry();
    renderStaggered(payload);
    statusEl.innerHTML = '<span class="ok">首月逐日建仓稳健性回测完成</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }}
    errorEl.textContent = error.message;
    statusEl.textContent = '稳健性回测失败';
  }} finally {{
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
    setValidationInputsEnabled(Boolean(parsedIdea));
    setStaggeredEnabled(Boolean(validatedFactorId));
  }}
}});
cancelButton.addEventListener('click', async () => {{
  const jobId = activeIdeaJobId;
  if (!jobId) return;
  cancelButton.disabled = true;
  clearGlobalError();
  resetIdeaResult('中断中', '已请求取消当前运行，等待后端安全停止。');
  statusEl.innerHTML = '<span class="warn">已请求中断本次运行；当前安全阶段结束后停止</span>';
  try {{
    await cancelJob(jobId);
  }} catch (error) {{
    errorEl.textContent = error.message;
    cancelButton.disabled = false;
  }}
}});
rdRun.addEventListener('click', async () => {{
  rdRun.disabled = true;
  rdCancel.disabled = true;
  clearGlobalError();
  resetRdResult('RD 运行中', 'RD 候选、gate、report path 和分段证据会在本次运行完成后刷新。');
  rdStatusEl.textContent = 'RD 运行中...';
  try {{
    const job = await postJson('/api/jobs/research-run-once', rdPayload());
    activeRdJobId = job.job_id;
    rdCancel.disabled = false;
    const payload = await waitForJob(
      job.job_id,
      rdStatusEl,
      '已运行超过10秒，RD 仍在生成、评价或回测',
      jobId => activeRdJobId === jobId
    );
    renderResearch(payload);
    clearGlobalError();
    rdStatusEl.innerHTML = '<span class="ok">RD 完成</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      resetRdResult('RD 已中断', '本次 RD 已取消，未产生新的候选结果。');
      rdStatusEl.textContent = 'RD 已中断';
    }} else {{
      rdStatusEl.textContent = error.message;
    }}
  }} finally {{
    activeRdJobId = null;
    rdRun.disabled = false;
    rdCancel.disabled = true;
  }}
}});
rdCancel.addEventListener('click', async () => {{
  const jobId = activeRdJobId;
  if (!jobId) return;
  rdCancel.disabled = true;
  clearGlobalError();
  resetRdResult('RD 中断中', '已请求取消当前 RD，等待后端安全停止。');
  rdStatusEl.innerHTML = '<span class="warn">已请求中断本次RD；当前安全阶段结束后停止</span>';
  try {{
    await cancelJob(jobId);
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
    rdCancel.disabled = false;
  }}
}});
rdStart.addEventListener('click', async () => {{
  rdStart.disabled = true;
  clearGlobalError();
  resetRdResult('调度启动中', '调度开启后，最近一次 RD 结果会在这里刷新。');
  rdStatusEl.textContent = '调度启动中...';
  try {{
    const payload = rdPayload();
    payload.action = 'start';
    payload.interval_days = Number(document.getElementById('rd-interval').value);
    const status = await postJson('/api/research/schedule', payload);
    rdStatusEl.innerHTML = '<span class="ok">调度已开启</span>';
    if (status.last_result) renderResearch(status.last_result);
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
  }} finally {{
    rdStart.disabled = false;
  }}
}});
rdStop.addEventListener('click', async () => {{
  rdStop.disabled = true;
  clearGlobalError();
  try {{
    const status = await postJson('/api/research/schedule', {{action: 'stop'}});
    rdStatusEl.textContent = status.run_count ? `调度已停止，累计运行 ${{status.run_count}} 次` : '调度已停止';
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
  }} finally {{
    rdStop.disabled = false;
  }}
}});
</script>
</body>
</html>"""

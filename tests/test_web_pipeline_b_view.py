"""Contract tests for the P3 frontend surfaces (WORKORDER P3):

- pipeline B (rd_optimize) cards in ``static/views/pipeline.js``: the RD
  confirm gate (rounds / candidates-per-round / objective + cost preview +
  fixed_policy disclosure) and the terminal leaderboard card, kind-dispatched
  off the SAME shared status machine;
- the editable-formula card ``static/views/formula.js`` (spec §5.3): a
  <textarea> single source of truth + an aria-hidden dsl.js highlight overlay,
  a read-only pre-validation call, and — the load-bearing behavioural pin — a
  Node smoke proving the overlay is NEVER repainted mid-IME-composition;
- the leaderboard reuse of ``static/views/research.js``: the external-OOS
  column labelled audit-only (「审计」), the dedup disposition rendered
  truthfully as executed / executed-unique / duplicate-after-execution /
  skipped (F5 — result-signature dups are detected AFTER full execution, never
  "reused"), availability-aware metrics that render n/a + status for a withheld
  metric and exclude unscorable rows from the numeric ranking (F4), and the
  canonical dsl highlighter in the comparison formula cell (F2c).

Mirrors this project's web-test conventions (string-contract pins on the JS
source + a stdlib Node smoke harness that imports the REAL modules and drives
their pure render functions with fixtures — see tests/test_web_pipeline_view.py
and tests/test_web_charts.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import quant_forge.apps.web.server as web_server


PIPELINE_JS_PATH = web_server.STATIC_ROOT / "views" / "pipeline.js"
FORMULA_JS_PATH = web_server.STATIC_ROOT / "views" / "formula.js"
RESEARCH_JS_PATH = web_server.STATIC_ROOT / "views" / "research.js"


def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# pipeline.js: pipeline B (rd_optimize) confirm + leaderboard cards
# ---------------------------------------------------------------------------


def test_pipeline_module_registers_rd_render_functions_before_controller() -> None:
    pipeline_js = _static_module_text("views/pipeline.js")
    controller_marker = pipeline_js.index("// [controller]")
    for render_fn in (
        "export function renderRdConfirmCard(",
        "export function renderRdRunningCard(",
        "export function renderRdPausedFailureCard(",
        "export function renderRdLeaderboardCard(",
    ):
        assert render_fn in pipeline_js, render_fn
        assert pipeline_js.index(render_fn) < controller_marker, render_fn


def test_rd_confirm_card_carries_rounds_candidates_objective_cost_and_fixed_policy() -> None:
    # WORKORDER P3: RD confirm card = rounds / candidates-per-round / objective
    # + cost preview + fixed_policy disclosure (the evaluation interval/sample
    # contract is INHERITED, R3.1 — never a new RD interval parameter).
    pipeline_js = _static_module_text("views/pipeline.js")
    fn_start = pipeline_js.index("export function renderRdConfirmCard(")
    fn_end = pipeline_js.index("\nexport function renderRdRunningCard(", fn_start)
    body = pipeline_js[fn_start:fn_end]
    for field in ('data-pipeline-rd-field="rounds"', 'data-pipeline-rd-field="candidates_per_round"', 'data-pipeline-rd-field="objective"'):
        assert field in body, field
    # fixed_policy disclosure badge (rendered through THE canonical badge
    # renderer, not a hand-written span — single-renderer discipline D12) +
    # the R3.1 inheritance wording. The rendered class string is asserted by
    # the Node smoke that actually executes renderPipelineCard.
    assert "provenanceBadgeHtml({ source: 'fixed_policy' })" in body
    assert "继承因子评测设置" in body
    assert "无独立 interval 参数" in body
    # Cost preview: the card mounts #pipeline-rd-cost and fills it via the
    # rdCostText helper (whose 成本预告 label lives in that shared helper).
    assert "pipeline-rd-cost" in body
    assert "rdCostText(" in body
    assert "成本预告" in pipeline_js
    # No A→B auto-bridge: the card states RD is explicitly user-initiated.
    assert "无 A→B 自动桥" in body


def test_rd_leaderboard_card_is_terminal_and_points_to_the_audit_only_leaderboard() -> None:
    pipeline_js = _static_module_text("views/pipeline.js")
    fn_start = pipeline_js.index("export function renderRdLeaderboardCard(")
    fn_end = pipeline_js.index("\n}", fn_start)
    body = pipeline_js[fn_start:fn_end]
    assert "排行榜已生成" in body
    assert "审计" in body
    # F5: the leaderboard names the TRUTHFUL post-execution disposition, never
    # the old false "reused" (which implied "not rerun").
    assert "执行 / 执行后判定重复 / 执行前跳过" in body
    assert "reused" not in body
    # F2: the terminal card still discloses the attempt lineage.
    assert "renderRdAttemptDisclosure(pipeline)" in body
    # Terminal: no confirm/run action buttons on the leaderboard card.
    assert "data-pipeline-action=\"confirm\"" not in body
    assert "pipeline-actions" not in body


def test_pipeline_module_exposes_create_rd_pipeline_seeded_by_factor_id() -> None:
    # No A→B auto-bridge (spec §2.1): pipeline B is created ONLY through this
    # explicit, user-initiated call with a seed factor id.
    pipeline_js = _static_module_text("views/pipeline.js")
    assert "export async function createRdPipeline(" in pipeline_js
    create_start = pipeline_js.index("export async function createRdPipeline(")
    create_end = pipeline_js.index("\n}", create_start)
    body = pipeline_js[create_start:create_end]
    assert "kind: 'rd_optimize'" in body
    assert "seed_factor_id: seedFactorId" in body


def test_rd_confirm_reads_its_own_fields_and_server_revalidates_rounds() -> None:
    pipeline_js = _static_module_text("views/pipeline.js")
    assert "function parametersFromRdConfirm(" in pipeline_js
    # confirm dispatches to the RD field reader for rd_optimize.
    confirm_start = pipeline_js.index("export async function confirmCurrentPipeline(")
    confirm_end = pipeline_js.index("\n}", confirm_start)
    body = pipeline_js[confirm_start:confirm_end]
    assert "kind === 'rd_optimize'" in body
    assert "parametersFromRdConfirm()" in body


# ---------------------------------------------------------------------------
# research.js: leaderboard reuse — audit-only external OOS + dedup disposition
# ---------------------------------------------------------------------------


def test_research_comparison_labels_external_oos_audit_only() -> None:
    research_js = _static_module_text("views/research.js")
    # The external-OOS column header is labelled 审计, and the pre-existing
    # note keeps it out of winner selection (X4 / spec §5.4).
    assert "External OOS<br><span class=\"meta\">审计</span>" in research_js
    assert "external OOS 只用于审计展示，不参与 winner 选择" in research_js


def test_research_renders_truthful_post_execution_dedup_disposition() -> None:
    # F5 (OPTION B): the disposition is truthful about POST-execution dedup --
    # executed / executed-unique / duplicate-after-execution / skipped, disjoint,
    # never the old false "reused"/"复用" that implied "not rerun".
    research_js = _static_module_text("views/research.js")
    assert "export function dedupDisposition(" in research_js
    assert "export function renderDedupDisposition(" in research_js
    fn_start = research_js.index("export function dedupDisposition(")
    fn_end = research_js.index("\n}", fn_start)
    body = research_js[fn_start:fn_end]
    # All counts come from SERVER-authoritative fields, never invented.
    assert "payload.candidates" in body            # executed (all ran)
    assert "result_duplicates" in body             # duplicate-after-execution
    assert "formula_skipped" in body and "diversity_skipped" in body  # skipped (pre-execution)
    # The rendered panel says 执行后判定重复 and NEVER claims reused / not rerun.
    render_start = research_js.index("export function renderDedupDisposition(")
    render_end = research_js.index("\n}", render_start)
    render_body = research_js[render_start:render_end]
    assert "执行后判定重复" in render_body
    assert "reused" not in render_body and "复用" not in render_body
    assert "renderDedupDisposition(payload)" in research_js


# ---------------------------------------------------------------------------
# formula.js: editable-formula card module shape + honest disclosures
# ---------------------------------------------------------------------------


def test_formula_module_registered_in_expected_static_modules() -> None:
    from tests.test_web_static_frontend import EXPECTED_STATIC_MODULES

    assert "views/formula.js" in EXPECTED_STATIC_MODULES


def test_formula_module_pure_render_before_controller_no_fetch_no_dom() -> None:
    formula_js = _static_module_text("views/formula.js")
    controller_marker = formula_js.index("// [controller]")
    for render_fn in (
        "export function renderFormulaOverlay(",
        "export function renderPreValidationResult(",
        "export function renderFormulaCard(",
    ):
        assert render_fn in formula_js, render_fn
        assert formula_js.index(render_fn) < controller_marker, render_fn
    assert "fetch(" not in formula_js[:controller_marker]
    assert "document." not in formula_js[:controller_marker]


def test_formula_card_uses_the_canonical_dsl_highlighter_not_a_second_one() -> None:
    # FE-L2 / single-renderer discipline: the overlay defers to dsl.js's
    # formulaHtml; formula.js never tokenizes/colours a formula itself.
    formula_js = _static_module_text("views/formula.js")
    assert "from './dsl.js'" in formula_js
    assert "formulaHtml(" in formula_js
    assert "tokenizeFormula(" not in formula_js  # not re-implemented here


def test_formula_card_is_a_textarea_not_contenteditable() -> None:
    # spec §5.3: contenteditable is rejected (CN IME / undo / cursor / paste).
    # The card is a real <textarea>; no element uses the contenteditable
    # attribute (the module docstring may NAME it to explain the rejection, so
    # this pins the attribute usage, not the bare word).
    formula_js = _static_module_text("views/formula.js")
    assert 'id="formula-input"' in formula_js
    assert "<textarea" in formula_js
    assert "contenteditable=" not in formula_js


def test_formula_prevalidate_calls_the_read_only_endpoint() -> None:
    formula_js = _static_module_text("views/formula.js")
    assert "'/api/pipelines/pre-validate'" in formula_js


def test_formula_prevalidate_render_discloses_no_execute_no_persist() -> None:
    formula_js = _static_module_text("views/formula.js")
    fn_start = formula_js.index("export function renderPreValidationResult(")
    fn_end = formula_js.index("\n}", fn_start)
    body = formula_js[fn_start:fn_end]
    assert "executed=" in body
    assert "persisted=" in body
    # Unknown-operator branch surfaces the operator_drafts review packet and
    # states hot_executed=false verbatim (WORKORDER pin / X3).
    assert "hot_executed=" in body
    assert "operator_drafts" in body
    assert "绝不热执行" in body


# ---------------------------------------------------------------------------
# Node smoke: formula.js IME composition (no repaint mid-composition) + the
# pre-validation render, and the RD confirm/leaderboard + research renderers.
# ---------------------------------------------------------------------------


_FORMULA_SMOKE_HARNESS = r"""
function makeElement(id) {
  const attrs = new Map();
  const listeners = new Map();
  const el = {
    id, value: '', innerHTML: '', textContent: '', scrollTop: 0, scrollLeft: 0,
    dataset: {}, style: {},
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(fn); },
    dispatchEvent(evt) { (listeners.get(evt.type) || []).slice().forEach(fn => fn.call(el, evt)); return true; },
    setAttribute(name, value) { attrs.set(name, String(value)); },
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    removeAttribute(name) { attrs.delete(name); },
    hasAttribute(name) { return attrs.has(name); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, focus() {}, scrollIntoView() {},
    get hidden() { return attrs.has('hidden'); },
    set hidden(v) { if (v) attrs.set('hidden', ''); else attrs.delete('hidden'); }
  };
  return el;
}
const registry = new Map();
globalThis.document = {
  getElementById(id) { if (!registry.has(id)) registry.set(id, makeElement(id)); return registry.get(id); },
  querySelector() { return null; }, querySelectorAll() { return []; }, createElement() { return makeElement(''); }
};

const mod = await import(process.env.QF_FORMULA_URL);
const { initFormulaModule, openFormulaCard, renderPreValidationResult, renderFormulaOverlay } = mod;

let failed = 0;
function check(name, cond, detail) { if (cond) console.log('PASS ' + name); else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); } }

// IME pin: the aria-hidden overlay must NOT repaint mid-composition.
initFormulaModule();
openFormulaCard('rank(close)');
const input = document.getElementById('formula-input');
const overlay = document.getElementById('formula-overlay');
const baseline = overlay.innerHTML;
check('overlay_highlights_initial_formula', baseline.includes('rank') && baseline.includes('close'), baseline);

input.dispatchEvent({ type: 'compositionstart', target: input });
input.value = 'rank(close)中';              // an in-flight IME candidate
input.dispatchEvent({ type: 'input', target: input });
check('no_repaint_mid_composition', overlay.innerHTML === baseline, overlay.innerHTML);

input.dispatchEvent({ type: 'compositionend', target: input });
check('overlay_repaints_after_composition_ends', overlay.innerHTML !== baseline && overlay.innerHTML.includes('中'), overlay.innerHTML);

// An ordinary (non-composed) input still repaints immediately.
input.value = 'rank(open)';
input.dispatchEvent({ type: 'input', target: input });
check('non_composed_input_repaints', overlay.innerHTML.includes('open'), overlay.innerHTML);

// renderFormulaOverlay defers to the canonical dsl.js highlighter (dsl-fn span).
check('overlay_uses_dsl_highlighter', renderFormulaOverlay('rank(close)').includes('dsl-fn'));

// Pre-validation render: ready discloses no-execute/no-persist + fingerprint.
const ready = renderPreValidationResult({ status: 'ready', fingerprint: 'abc123', executed: false, persisted: false });
check('ready_shows_fingerprint', ready.includes('abc123'));
check('ready_discloses_not_executed', ready.includes('executed=false') && ready.includes('persisted=false'));

// Unknown operator: review packet, hot_executed=false, never executed.
const review = renderPreValidationResult({
  status: 'review_required', executed: false, persisted: false,
  review_packet: { channel: 'operator_drafts', unresolved_operators: ['ts_made_up'], hot_executed: false }
});
check('review_shows_operator_drafts', review.includes('operator_drafts'));
check('review_states_hot_executed_false', review.includes('hot_executed=false'));
check('review_lists_unknown_operator', review.includes('ts_made_up'));

console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_formula_ime_and_prevalidation_smoke(tmp_path) -> None:
    harness = tmp_path / "formula_smoke.mjs"
    harness.write_text(_FORMULA_SMOKE_HARNESS, encoding="utf-8")
    env = {"QF_FORMULA_URL": FORMULA_JS_PATH.resolve().as_uri()}
    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={**dict(os.environ), **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "SMOKE RESULT: 0 failed" in result.stdout
    for marker in (
        "PASS overlay_highlights_initial_formula",
        "PASS no_repaint_mid_composition",
        "PASS overlay_repaints_after_composition_ends",
        "PASS non_composed_input_repaints",
        "PASS overlay_uses_dsl_highlighter",
        "PASS ready_shows_fingerprint",
        "PASS ready_discloses_not_executed",
        "PASS review_shows_operator_drafts",
        "PASS review_states_hot_executed_false",
    ):
        assert marker in result.stdout, result.stdout


_RD_AND_RESEARCH_SMOKE_HARNESS = r"""
globalThis.document = { getElementById: () => null, createElement: () => ({}), querySelector: () => null, querySelectorAll: () => [] };

const pipeline = await import(process.env.QF_PIPELINE_URL);
const research = await import(process.env.QF_RESEARCH_URL);
const { renderPipelineCard } = pipeline;
const { renderComparisonTable, renderDedupDisposition, dedupDisposition, isRowScorable } = research;

let failed = 0;
function check(name, cond, detail) { if (cond) console.log('PASS ' + name); else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); } }

function rdStages(overrides) {
  return ['confirm', 'run', 'leaderboard'].map(id => ({ stage_id: id, status: (overrides[id] || 'pending'), child_job_id: null }));
}
const rdProvenance = [
  { field: 'rounds', value: 3, source: 'user_explicit' },
  { field: 'candidates_per_round', value: 4, source: 'user_explicit' },
  { field: 'objective', value: 'balanced', source: 'profile_default' }
];

// (a) rd_optimize confirm card is kind-dispatched off renderPipelineCard.
{
  const rd = {
    pipeline_id: 'PL_rd', kind: 'rd_optimize', status: 'awaiting_confirm',
    factor: { factor_id: 'FTR_DEMO_MOMENTUM' },
    parameters: { rounds: 3, candidates_per_round: 4, objective: 'balanced' },
    warnings: [], failure: null, confirmed_parameters: null,
    stages: rdStages({ confirm: 'active' })
  };
  const html = renderPipelineCard(rd, { provenance: rdProvenance });
  check('rd.confirm_card_dispatched', html.includes('确认 RD 优化'));
  check('rd.has_rounds_field', html.includes('data-pipeline-rd-field="rounds"'));
  check('rd.has_candidates_field', html.includes('data-pipeline-rd-field="candidates_per_round"'));
  check('rd.has_objective_field', html.includes('data-pipeline-rd-field="objective"'));
  check('rd.fixed_policy_disclosure', html.includes('provenance-badge--fixed_policy') && html.includes('继承因子评测设置'));
  check('rd.cost_preview', html.includes('成本预告') && html.includes('12'));   // 3 × 4
  check('rd.field_badges_present', html.includes('provenance-badge--user_explicit'));
  check('rd.stage_strip_rd_labels', html.includes('RD 确认') && html.includes('排行榜'));
  // Confirm card is NOT the factor_study 11-param grid.
  check('rd.no_factor_study_grid', !html.includes('data-pipeline-param-field='));
}

// (b) rd_optimize completed → terminal leaderboard card, no gate actions.
{
  const rd = {
    pipeline_id: 'PL_rd2', kind: 'rd_optimize', status: 'completed',
    factor: { factor_id: 'FTR_DEMO_MOMENTUM' }, parameters: {}, warnings: [], failure: null,
    confirmed_parameters: {}, stages: rdStages({ confirm: 'completed', run: 'completed', leaderboard: 'completed' })
  };
  const html = renderPipelineCard(rd, {});
  check('rd.leaderboard_terminal', html.includes('排行榜已生成'));
  check('rd.leaderboard_no_actions', !html.includes('pipeline-actions'));
}

// (c) F5 dedup disposition: result-signature duplicates are detected AFTER full
// execution — BOTH candidates ran; one is ALSO flagged duplicate-post-execution.
// Disjoint buckets (executed_unique + duplicate_after_execution === executed);
// the UI never claims "not rerun"/"复用". Driven from a real payload shape with
// a genuine duplicate-result trace (the second candidate carries the exact
// "duplicate result signature matches …" gate reason service.py records, and
// is STILL present in `candidates` because it executed).
{
  const payload = {
    candidates: [
      { factor: { factor_id: 'FTR_A', formula: 'rank(close)' }, gate_reasons: ['passed smoke research gate'] },
      { factor: { factor_id: 'FTR_B', formula: 'rank( close )' }, gate_reasons: ['duplicate result signature matches FTR_A'] }
    ],
    deduplication: { result_duplicates: 1, formula_skipped: 1, diversity_skipped: 1 }
  };
  const d = dedupDisposition(payload);
  check('dedup.executed_all_ran', d.executed === 2, JSON.stringify(d));
  check('dedup.executed_unique', d.executedUnique === 1, JSON.stringify(d));
  check('dedup.duplicate_after_execution', d.duplicateAfterExecution === 1, JSON.stringify(d));
  check('dedup.skipped_pre_execution', d.skipped === 2, JSON.stringify(d));
  check('dedup.disjoint', d.executedUnique + d.duplicateAfterExecution === d.executed, JSON.stringify(d));
  const html = renderDedupDisposition(payload);
  check('dedup.render_execution_after_dup', html.includes('执行后判定重复 1'));
  check('dedup.render_no_false_reused', !html.includes('reused') && !html.includes('复用'), html);
  check('dedup.render_states_both_ran', html.includes('之后') && html.includes('都已实际运行'));
}

// (d) F4: external OOS labelled 审计; a row whose rank_icir is
// status=insufficient_sample renders its status label (n/a), NEVER a 0.00
// scalar, and is EXCLUDED from the numeric ranking (isRowScorable === false).
{
  const payload = { comparison_rows: [
    { round: 1, factor_id: 'FTR_LOW', formula: 'rank(close)', selection_score: 0.10,
      selection_rank_ic: 0.02, selection_rank_ic_status: 'available',
      selection_icir: 0.5, selection_icir_status: 'available', gate_passed: true },
    { round: 1, factor_id: 'FTR_NA', formula: 'ts_mean(close, 5)', selection_score: 0.0,
      selection_rank_ic: 0.0, selection_rank_ic_status: 'insufficient_sample',
      selection_icir: 0.0, selection_icir_status: 'insufficient_sample', gate_passed: false }
  ] };
  const html = renderComparisonTable(payload);
  check('cmp.external_oos_audit_label', html.includes('审计'));
  // The withheld icir renders its status label, never the zero-filled scalar.
  check('cmp.status_label_rendered', html.includes('insufficient_sample'));
  check('cmp.status_not_zero_scalar', !html.includes('ICIR 0.00') && !html.includes('IC 0.0000'), html);
  // Exclusion from the numeric ranking is server-status-driven, not guessed.
  check('cmp.scorable_row_included', isRowScorable(payload.comparison_rows[0]) === true);
  check('cmp.status_row_excluded', isRowScorable(payload.comparison_rows[1]) === false);
  // Canonical dsl highlighter (F2c): the formula cell defers to formulaHtml.
  check('cmp.formula_uses_dsl_highlighter', html.includes('dsl-fn'));
}

// (e) F2: RD cards disclose the SERVER-authoritative attempt lineage +
// multiple-comparison honesty. failed_attempts is durable on the record — a
// retry clears `failure` but NOT this count, so "此前 N 次尝试失败" survives.
{
  const rd = {
    pipeline_id: 'PL_rd3', kind: 'rd_optimize', status: 'awaiting_confirm',
    factor: { factor_id: 'FTR_SEED' },
    parameters: { rounds: 2, candidates_per_round: 3, objective: 'balanced' },
    warnings: [], failure: null, confirmed_parameters: null,
    attempt: { number: 3, parent_run_id: 'PL_parent' }, failed_attempts: 2,
    input_hash: 'abcdef0123456789', stages: rdStages({ confirm: 'active' })
  };
  const html = renderPipelineCard(rd, { provenance: rdProvenance });
  check('rd.attempt_number_disclosed', html.includes('第 3 次尝试'));
  check('rd.failed_attempts_disclosed', html.includes('此前 2 次尝试失败'));
  check('rd.input_fingerprint_disclosed', html.includes('abcdef012345'));
  check('rd.parent_lineage_disclosed', html.includes('PL_parent'));
}

console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_rd_cards_and_research_leaderboard_smoke(tmp_path) -> None:
    harness = tmp_path / "rd_research_smoke.mjs"
    harness.write_text(_RD_AND_RESEARCH_SMOKE_HARNESS, encoding="utf-8")
    env = {
        "QF_PIPELINE_URL": PIPELINE_JS_PATH.resolve().as_uri(),
        "QF_RESEARCH_URL": RESEARCH_JS_PATH.resolve().as_uri(),
    }
    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={**dict(os.environ), **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "SMOKE RESULT: 0 failed" in result.stdout
    for marker in (
        "PASS rd.confirm_card_dispatched",
        "PASS rd.has_rounds_field",
        "PASS rd.fixed_policy_disclosure",
        "PASS rd.cost_preview",
        "PASS rd.stage_strip_rd_labels",
        "PASS rd.no_factor_study_grid",
        "PASS rd.leaderboard_terminal",
        "PASS rd.leaderboard_no_actions",
        "PASS dedup.executed_all_ran",
        "PASS dedup.executed_unique",
        "PASS dedup.duplicate_after_execution",
        "PASS dedup.skipped_pre_execution",
        "PASS dedup.disjoint",
        "PASS dedup.render_execution_after_dup",
        "PASS dedup.render_no_false_reused",
        "PASS cmp.external_oos_audit_label",
        "PASS cmp.status_label_rendered",
        "PASS cmp.status_row_excluded",
        "PASS cmp.formula_uses_dsl_highlighter",
        "PASS rd.attempt_number_disclosed",
        "PASS rd.failed_attempts_disclosed",
        "PASS rd.parent_lineage_disclosed",
    ):
        assert marker in result.stdout, result.stdout

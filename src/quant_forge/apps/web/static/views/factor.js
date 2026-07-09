/* Factor tape panels: parse result, validation result, staggered-entry
 * robustness backtest. Rendering moved 1:1 from the former inline script;
 * CP6-2 splits the report into section-level renderers (same output,
 * re-hosted under stable section ids for the Lab anchor navigation). */

import { esc, metricNum, metricPill, num, pct, valueOr } from '../metric.js';
import { sparklineSvg } from './spark.js';

const resultEl = document.getElementById('result');
const staggeredResultEl = document.getElementById('staggered-result');

export function profilePeriodText(profile) {
  const start = profile.test_period_start || 'full available data';
  const end = profile.test_period_end || 'latest available data';
  return `${start} -> ${end}`;
}

export function resetIdeaResult(title, message) {
  resultEl.innerHTML = `
    <div class="panel empty-state">
      <h3>${esc(title)}</h3>
      <p class="meta">${esc(message)}</p>
    </div>`;
}

export function resetStaggeredResult() {
  staggeredResultEl.innerHTML = '';
}

export function parserDefaultParameterMessage(parser) {
  const source = (parser && parser.source) || '';
  if (source.toLowerCase() === 'llm') {
    return 'LLM 已生成默认评测参数。确认或修改左侧参数后，点击“验证并评测”。';
  }
  return '解析器已生成默认评测参数。确认或修改左侧参数后，点击“验证并评测”。';
}

export function assumptionLabel(text) {
  if (text === 'rebalance_rate tracks component replacement per rebalance') {
    return '调仓率 = 相邻调仓的成分替换率';
  }
  if (text === 'turnover_rate estimates true portfolio weight turnover') {
    return '换手率 = 基于组合权重变化估算的真实换手率';
  }
  return text;
}

function warningPills(evaluation, backtest) {
  return [
    ...(evaluation.warning_codes || []),
    ...(backtest.warning_codes || []),
    ...(evaluation.warnings || []),
    ...(backtest.warnings || [])
  ].map(item =>
    `<span class="pill">${esc(item)}</span>`
  ).join(' ');
}

function cachePills(evaluation, backtest) {
  return [
    `eval ${evaluation.score_source || 'computed'} · cached ${evaluation.score_cached_rows || 0} · computed ${evaluation.score_computed_rows || 0}`,
    evaluation.factor_values_path ? `eval path ${evaluation.factor_values_path}` : '',
    `backtest ${backtest.score_source || 'computed'} · cached ${backtest.score_cached_rows || 0} · computed ${backtest.score_computed_rows || 0}`,
    backtest.factor_values_path ? `backtest path ${backtest.factor_values_path}` : ''
  ].filter(Boolean).map(item => `<span class="pill">${esc(item)}</span>`).join(' ');
}

export function renderReportHero(factor, parser, hero) {
  const metaLines = (hero.metaLines || []).map(line => `<p class="meta">${line}</p>`).join('\n        ');
  return `
    <div class="panel hero-panel report-section" id="report-hero">
      <div>
        <p class="eyebrow">Factor Report</p>
        <h3>${esc(factor.factor_id)} · ${esc(parser.source)} / ${esc(parser.provider)} / ${esc(parser.model)}</h3>
        <div class="formula">${esc(factor.formula)}</div>
        <p>${esc(factor.description || '')}</p>
        ${metaLines}
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${esc(hero.horizonDays)}<br>
        ${esc((factor.universe_filters || []).join(' · ') || 'FULL')}
      </div>
    </div>`;
}

export function renderPendingParams(parameters) {
  return `
    <div class="panel report-section" id="report-params">
      <h3>待确认参数</h3>
      <p>
        <span class="pill">holding ${esc(parameters.holding_days)}d</span>
        <span class="pill">decay ${esc(parameters.decay_days)}</span>
        <span class="pill">top ${esc(parameters.top_quantile)}</span>
        <span class="pill">delay ${esc(parameters.execution_delay_days)}d</span>
        <span class="pill">evaluation ${esc(profilePeriodText({test_period_start: parameters.evaluation_start, test_period_end: parameters.evaluation_end}))}</span>
        <span class="pill">backtest ${esc(profilePeriodText({test_period_start: parameters.backtest_start, test_period_end: parameters.backtest_end}))}</span>
        <span class="pill">commission ${esc(parameters.commission_bps)} bps</span>
        <span class="pill">slippage ${esc(parameters.slippage_bps)} bps</span>
        <span class="pill">short borrow ${esc(parameters.short_borrow_bps_annual)} bps/year</span>
      </p>
    </div>`;
}

export function renderEvaluationSection(evaluation, coverage, context) {
  return `
    <div class="panel report-section" id="report-evaluation">
      <h3>样本内研究评价</h3>
      <div class="grid">
        <div class="tile">Rank IC<b>${metricNum(evaluation.rank_ic_mean, evaluation.rank_ic_mean_status)}</b></div>
        <div class="tile">ICIR<b>${metricNum(evaluation.rank_icir, evaluation.rank_icir_status, 2)}</b></div>
        <div class="tile">HAC t-stat<b>${metricNum(evaluation.rank_ic_t_stat, evaluation.rank_ic_t_stat_status, 2)}</b></div>
        <div class="tile">IC Days<b>${esc(evaluation.ic_days)}</b></div>
        <div class="tile">Joint Coverage<b>${pct(valueOr(coverage.joint_coverage, evaluation.coverage))}</b></div>
        <div class="tile">Horizon / Delay<b>${esc(context.effectiveHoldingDays)}日 / ${esc(valueOr(context.evaluationProfile.execution_delay_days, context.profile.execution_delay_days))}日</b></div>
      </div>
      <p class="meta">research_evaluation · ${esc(profilePeriodText(context.evaluationProfile))}</p>
    </div>`;
}

export function renderInSampleSection(inSampleBacktest, inSampleProfile) {
  if (!inSampleBacktest) return '';
  return `
    <div class="panel report-section" id="report-insample">
      <h3>样本内组合回测</h3>
      <div class="grid">
        <div class="tile">毛累计收益<b>${pct(inSampleBacktest.gross_cumulative_return ?? inSampleBacktest.cumulative_return)}</b></div>
        <div class="tile">净累计收益<b>${pct(inSampleBacktest.net_cumulative_return)}</b></div>
        <div class="tile">完整持有期数<b>${esc(valueOr(inSampleBacktest.completed_periods, inSampleBacktest.periods))}</b></div>
        <div class="tile">Exposure Days<b>${num(inSampleBacktest.exposure_days, 0)}</b></div>
        <div class="tile">可报告净年化收益<b>${pct(inSampleBacktest.net_annualized_return)}</b></div>
        <div class="tile">年化Sharpe<b>${num(inSampleBacktest.net_long_short_sharpe ?? inSampleBacktest.long_short_sharpe, 2)}</b></div>
        <div class="tile">净值最大回撤<b>${pct(inSampleBacktest.net_max_drawdown ?? inSampleBacktest.max_drawdown)}</b></div>
        <div class="tile">Rebalance Turnover<b>${pct(inSampleBacktest.rebalance_turnover_mean ?? inSampleBacktest.turnover_rate)}</b></div>
      </div>
      <p class="meta">${esc(inSampleBacktest.sample_role || 'in_sample_backtest')} · ${esc(profilePeriodText(inSampleProfile))}</p>
    </div>`;
}

export function renderOosSection(backtest, profile, backtestProfile) {
  const singlePeriodWarning = Number(backtest.periods || 0) === 1
    ? `<div class="notice warn">外部样本外仅包含 1 个完整持有期。累计收益可计算；年化收益、波动率、Sharpe、再平衡率以及无日频净值支持的最大回撤不可报告。</div>`
    : '';
  return `
    <div class="panel report-section" id="report-oos">
      <h3>外部样本外组合评测</h3>
      ${singlePeriodWarning}
      <div class="grid">
        <div class="tile">毛累计收益<b>${pct(backtest.gross_cumulative_return ?? backtest.cumulative_return)}</b></div>
        <div class="tile">净累计收益<b>${pct(backtest.net_cumulative_return)}</b></div>
        <div class="tile">完整持有期数<b>${esc(valueOr(backtest.completed_periods, backtest.periods))}</b></div>
        <div class="tile">Exposure Days<b>${num(backtest.exposure_days, 0)}</b></div>
        <div class="tile">可报告毛年化收益<b>${pct(backtest.gross_annualized_return ?? backtest.annualized_return)}</b></div>
        <div class="tile">可报告净年化收益<b>${pct(backtest.net_annualized_return)}</b></div>
        <div class="tile">年化波动率<b>${pct(backtest.net_annualized_volatility ?? backtest.annualized_volatility)}</b></div>
        <div class="tile">年化Sharpe<b>${num(backtest.net_long_short_sharpe ?? backtest.long_short_sharpe, 2)}</b></div>
        <div class="tile">净值最大回撤<b>${pct(backtest.net_max_drawdown ?? backtest.max_drawdown)}</b></div>
        <div class="tile">Initial Build Turnover<b>${pct(backtest.initial_build_turnover)}</b></div>
        <div class="tile">Rebalance Turnover<b>${pct(backtest.rebalance_turnover_mean ?? backtest.turnover_rate)}</b></div>
        <div class="tile">Replacement Rate<b>${pct(backtest.replacement_rate_mean ?? backtest.rebalance_rate)}</b></div>
        <div class="tile">持有期<b>${esc(backtest.holding_days)}日</b></div>
        <div class="tile">Decay<b>${esc(valueOr(profile.decay_days, 0))}</b></div>
        <div class="tile">Top Quantile<b>${num(valueOr(profile.top_quantile, backtest.top_quantile), 2)}</b></div>
        <div class="tile">Delay<b>${esc(valueOr(profile.execution_delay_days, 1))}日</b></div>
      </div>
      <p class="meta">external_oos_backtest · ${esc(profilePeriodText(backtestProfile))}</p>
    </div>`;
}

export function renderDiagnosticsSection(evaluation, backtest) {
  const evaluationMetrics = evaluation.metrics || {};
  const backtestMetrics = backtest.metrics || {};
  const metricRows = [
    metricPill('HAC t-stat', evaluationMetrics.rank_ic_t_stat),
    metricPill('可报告毛年化收益', backtestMetrics.annualized_return),
    metricPill('可报告净年化收益', backtestMetrics.net_annualized_return),
    metricPill('净值最大回撤', backtestMetrics.max_drawdown),
    metricPill('再平衡换手', backtestMetrics.rebalance_turnover_mean || backtestMetrics.rebalance_rate)
  ].filter(Boolean).join(' ');
  const warningRows = warningPills(evaluation, backtest);
  const cacheRows = cachePills(evaluation, backtest);
  return `
    <div class="panel report-section" id="report-diagnostics">
      <h3>样本充分性与诊断</h3>
      <p>${metricRows || '<span class="pill">暂无指标状态</span>'}</p>
      <p>${warningRows || '<span class="pill">研究口径，不是生产交易口径</span>'}</p>
      <p>${cacheRows || '<span class="pill">computed</span>'}</p>
    </div>`;
}

export function renderEvidenceSection(evaluation, backtest) {
  const splitRows = (evaluation.split_metrics || []).map(metric =>
    `<span class="pill">${esc(metric.name)} ICIR ${metricNum(metric.rank_icir, metric.rank_icir_status, 2)} · HAC t ${metricNum(metric.rank_ic_t_stat, metric.rank_ic_t_stat_status, 2)} · days ${esc(metric.ic_days)}</span>`
  ).join(' ');
  const horizonRows = (evaluation.horizon_metrics || []).map(metric =>
    `<span class="pill">${esc(metric.horizon_days)}日 IC ${metricNum(metric.rank_ic_mean, metric.rank_ic_mean_status)} / ICIR ${metricNum(metric.rank_icir, metric.rank_icir_status, 2)} / HAC t ${metricNum(metric.rank_ic_t_stat, metric.rank_ic_t_stat_status, 2)}</span>`
  ).join(' ');
  const groupRows = (backtest.group_returns || []).map(metric =>
    `<span class="pill">${esc(metric.group)} ${pct(metric.mean_return)}</span>`
  ).join(' ');
  const segmentRows = (backtest.segment_metrics || []).map(metric =>
    `<span class="pill">${esc(metric.name)} net ann ${pct(metric.net_annualized_return)} · sharpe ${num(metric.net_long_short_sharpe, 2)}</span>`
  ).join(' ');
  const assumptionRows = (backtest.assumptions || []).map(item =>
    `<span class="pill">${esc(assumptionLabel(item))}</span>`
  ).join(' ');
  const warningRows = warningPills(evaluation, backtest);
  const cacheRows = cachePills(evaluation, backtest);
  return `
    <div class="evidence-grid report-section" id="report-evidence">
      <div class="panel">
        <h3>三段验证</h3>
        <p>${splitRows || '<span class="pill">暂无</span>'}</p>
        <h3>回测分段</h3>
        <p>${segmentRows || '<span class="pill">暂无</span>'}</p>
        <h3>多周期评价</h3>
        <p>${horizonRows || '<span class="pill">暂无</span>'}</p>
      </div>
      <div class="panel">
        <h3>分组收益</h3>
        <p>${groupRows || '<span class="pill">暂无</span>'}</p>
        <h3>风险提示</h3>
        <p>${warningRows || '<span class="pill">研究口径，不是生产交易口径</span>'}</p>
        <h3>口径说明</h3>
        <p>${assumptionRows || '<span class="pill">研究口径，不是生产交易口径</span>'}</p>
        <h3>因子值缓存</h3>
        <p>${cacheRows || '<span class="pill">computed</span>'}</p>
      </div>
    </div>`;
}

export function renderArtifactsSection(paths) {
  const lines = (paths || []).filter(path => path !== undefined && path !== null)
    .map(path => `<p class="meta">${esc(path)}</p>`).join('\n      ');
  return `
    <div class="panel report-section" id="report-artifacts">
      <h3>Artifacts</h3>
      ${lines}
    </div>`;
}

export function renderAnchorNav(sections) {
  const links = sections.map(section =>
    `<a href="#${section.id}">${esc(section.label)}</a>`
  ).join('');
  return `<nav class="anchor-nav" aria-label="报告章节">${links}</nav>`;
}

/* No-silent-fallback (F-010): the parse payload carries `warnings` whenever
 * the parser landed on the generic fallback formula. Render each one as a
 * design-system warn notice with a text label (never color alone), ahead of
 * the report hero so a fallback parse can never look like a confident one. */
export function renderParseWarnings(warnings) {
  return (warnings || []).map(item =>
    `<div class="notice warn"><span class="status-pill status-pill--running">警告</span> ${esc(item)}</div>`
  ).join('');
}

export function renderParsed(payload) {
  const factor = payload.factor;
  resultEl.innerHTML = renderParseWarnings(payload.warnings)
    + renderReportHero(factor, payload.parser, {
      horizonDays: factor.horizon_days,
      metaLines: [esc(parserDefaultParameterMessage(payload.parser))]
    }) + renderPendingParams(payload.parameters);
}

export function render(payload) {
  const factor = payload.factor;
  const evaluation = payload.evaluation;
  const inSampleBacktest = payload.in_sample_backtest || null;
  const backtest = payload.backtest;
  const effectiveHoldingDays = (payload.parameters && payload.parameters.holding_days) || backtest.holding_days || factor.horizon_days;
  const evaluationProfile = evaluation.simulation_profile || {};
  const inSampleProfile = inSampleBacktest ? (inSampleBacktest.simulation_profile || {}) : {};
  const backtestProfile = backtest.simulation_profile || {};
  const profile = Object.keys(backtestProfile).length ? backtestProfile : evaluationProfile;
  const coverage = evaluation.coverage_lineage || {};
  const anchorSections = [
    { id: 'report-hero', label: '概览' },
    { id: 'report-evaluation', label: '样本内评价' },
    ...(inSampleBacktest ? [{ id: 'report-insample', label: '样本内回测' }] : []),
    { id: 'report-oos', label: '样本外评测' },
    { id: 'report-diagnostics', label: '诊断' },
    { id: 'report-evidence', label: '研究证据' },
    { id: 'report-artifacts', label: 'Artifacts' }
  ];
  resultEl.innerHTML = renderAnchorNav(anchorSections)
    + renderReportHero(factor, payload.parser, {
      horizonDays: effectiveHoldingDays,
      metaLines: [
        `evaluation period: ${esc(profilePeriodText(evaluationProfile))}`,
        `backtest period: ${esc(profilePeriodText(backtestProfile))}`
      ]
    })
    + renderEvaluationSection(evaluation, coverage, { effectiveHoldingDays, evaluationProfile, profile })
    + renderInSampleSection(inSampleBacktest, inSampleProfile)
    + renderOosSection(backtest, profile, backtestProfile)
    + renderDiagnosticsSection(evaluation, backtest)
    + renderEvidenceSection(evaluation, backtest)
    + renderArtifactsSection([
      evaluation.artifact_path,
      ...(inSampleBacktest ? [inSampleBacktest.artifact_path] : []),
      backtest.artifact_path
    ]);
}

export function renderStaggeredRunning() {
  staggeredResultEl.innerHTML = `
    <div class="panel">
      <h3>首月逐日建仓稳健性回测运行中</h3>
      <p class="meta">完成后会显示 cohort、等权组合 NAV 和 artifact。</p>
    </div>`;
}

export function renderStaggered(payload) {
  const terminal = (payload.daily_nav || []).slice(-1)[0] || {};
  const cohortRows = (payload.cohorts || []).map(cohort =>
    `<span class="pill">${esc(cohort.signal_date)} · weight ${pct(cohort.capital_weight)} · net ${pct(cohort.net_cumulative_return)}</span>`
  ).join(' ');
  const navValues = (payload.daily_nav || []).map(day => day.net_nav);
  const spark = sparklineSvg(navValues, { label: `Staggered NAV · N=${navValues.length}` });
  staggeredResultEl.innerHTML = `
    <div class="panel report-section" id="report-staggered">
      <h3>首月逐日建仓稳健性回测</h3>
      <div class="grid">
        <div class="tile">Staggered 净累计收益<b>${pct(payload.strategy_cumulative_return)}</b></div>
        <div class="tile">基准累计收益<b>${pct(payload.benchmark_cumulative_return)}</b></div>
        <div class="tile">相对财富收益<b>${pct(payload.relative_wealth_excess_return)}</b></div>
        <div class="tile">Cohorts<b>${num(payload.cohort_count, 0)}</b></div>
        <div class="tile">Terminal NAV<b>${num(terminal.net_nav, 4)}</b></div>
        <div class="tile">Inactive Cash<b>${pct(terminal.inactive_cash_weight)}</b></div>
      </div>
      ${spark ? `<p class="sparkline-row">${spark}</p>` : ''}
      <p class="meta">${esc(payload.sample_role || 'staggered_entry_backtest')} · ${esc(payload.formation_window_mode || 'first_month')}</p>
      <p>${cohortRows || '<span class="pill">暂无 cohort 明细</span>'}</p>
      <p class="meta">${esc(payload.artifact_path || '')}</p>
    </div>`;
}

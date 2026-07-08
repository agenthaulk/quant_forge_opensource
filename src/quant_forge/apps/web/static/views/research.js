/* RD loop panel: candidate cards, iteration chain summary, comparison table.
 * Rendering moved 1:1 from the former inline script; CP6-2 splits the panel
 * into section-level renderers and upgrades gate markers to status pills
 * (text label always present — color is never the sole signal). */

import { esc, metricNum, num, pct, valueOr } from '../metric.js';
import { profilePeriodText } from './factor.js';

const rdResultEl = document.getElementById('rd-result');

export function optimizationStatusText(payload) {
  const status = payload.optimization_status || (payload.optimization_performed ? 'performed' : 'no_optimization_performed');
  if (status === 'performed') return 'performed';
  if (status === 'attempted_no_acceptance') return 'attempted_no_acceptance';
  return 'no_optimization_performed';
}

export function resetRdResult(title, message) {
  rdResultEl.innerHTML = `
    <div class="placeholder">
      <div class="panel">
        <h3>${esc(title)}</h3>
        <p class="meta">${esc(message)}</p>
      </div>
    </div>`;
}

export function comparisonRows(payload) {
  const chain = payload.iteration_chain || {};
  return payload.comparison_rows || chain.comparison_rows || [];
}

function gatePillHtml(gate) {
  const modifier = gate === 'pass' ? 'ok' : (gate === 'fail' ? 'fail' : 'neutral');
  return `<span class="status-pill status-pill--${modifier}">${esc(gate)}</span>`;
}

export function renderComparisonTable(payload) {
  const rows = comparisonRows(payload);
  const body = rows.map(row => {
    const gate = row.gate_passed === true ? 'pass' : (row.gate_passed === false ? 'fail' : 'n/a');
    return `
      <tr>
        <td>${esc(row.round || 1)}</td>
        <td>${esc(row.role || '')}</td>
        <td><code>${esc(row.factor_id || '')}</code><br><span class="meta">${esc(row.factor_status || '')}</span></td>
        <td><code>${esc(row.formula || '')}</code></td>
        <td>${num(row.selection_score, 4)}<br><span class="meta">IC ${num(row.selection_rank_ic, 4)} / ICIR ${num(row.selection_icir, 2)}</span></td>
        <td>${pct(row.selection_net_cumulative_return)}<br><span class="meta">ann ${pct(row.selection_net_annualized_return)} · periods ${esc(row.selection_completed_periods ?? row.selection_backtest_periods ?? '')}</span></td>
        <td>${pct(row.external_oos_net_cumulative_return)}<br><span class="meta">ann ${pct(row.external_oos_net_annualized_return)} · periods ${esc(row.external_oos_completed_periods ?? row.external_oos_periods ?? '')}</span></td>
        <td>${gatePillHtml(gate)}<br><span class="meta">${esc((row.gate_reasons || []).join('; '))}</span></td>
      </tr>`;
  }).join('');
  return `
    <div class="panel report-section" id="rd-comparison">
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
        <tbody>${body || '<tr><td colspan="8">暂无比较行</td></tr>'}</tbody>
      </table>
    </div>`;
}

export function renderRdSummary(payload) {
  const chain = payload.iteration_chain || {};
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
    ? `<div class="notice err">RD stopped at round ${esc(failedRoundIndex || '?')}: ${esc(chainError)}</div>`
    : '';
  const roundRows = rounds.map(item =>
    `<span class="pill">#${esc(item.round)} seed ${esc(item.seed_factor_id)} → ${esc(item.selected_next_seed_factor_id || item.top_candidate_id || 'stop')} · ${esc(item.selection_reason || 'completed')} · score ${item.top_score === null || item.top_score === undefined ? 'n/a' : num(item.top_score, 4)}</span>`
  ).join(' ');
  const reportRows = reportPaths.map(path => `<span class="pill">${esc(path)}</span>`).join(' ');
  return `
    <div class="panel report-section" id="rd-summary">
      <h3>${esc(payload.seed_factor_id)} · ${esc(payload.objective)}</h3>
      <p class="meta">workflow: ${esc(payload.workflow_type || payload.rd_stage || 'research')}</p>
      <p class="meta">iterations: ${esc(payload.iteration_count || 1)} / ${esc(payload.requested_iterations || 1)} · original seed ${esc(payload.original_seed_factor_id || payload.seed_factor_id)} · recommended factor ${esc(recommendedFactor)} (${esc(recommendationLabel)}) · last accepted ${esc(lastAcceptedFactor)} · last explored ${esc(lastExploredFactor)} · ${esc(payload.stopped_reason || 'completed')}</p>
      <p class="meta">next exploration seed: ${esc(explorationSeed)} · ${esc(explorationReason)} · ${esc(explorationGate)}</p>
      <p class="meta">optimization: ${esc(optimizationLabel)}${optimizationScope}</p>
      ${partialNotice}
      <p class="meta">accepted: ${esc(aggregateAccepted.join(', ') || 'none')}</p>
      <p class="meta">report: ${esc(payload.report_path || 'not generated')}</p>
      <p class="meta">round reports: ${reportRows || '<span class="pill">same as report</span>'}</p>
      <p>${roundRows || '<span class="pill">single round</span>'}</p>
    </div>`;
}

export function renderCandidateCard(candidate) {
  const factor = candidate.factor;
  const evaluation = candidate.evaluation;
  const backtest = candidate.backtest;
  const evaluationProfile = evaluation.simulation_profile || {};
  const backtestProfile = backtest.simulation_profile || {};
  const profile = Object.keys(backtestProfile).length ? backtestProfile : evaluationProfile;
  const gate = candidate.gate_passed
    ? '<span class="status-pill status-pill--ok">candidate · pass</span>'
    : '<span class="status-pill status-pill--neutral">draft · gate fail</span>';
  const cacheText = `${evaluation.score_source || 'computed'} / ${backtest.score_source || 'computed'} · cached ${evaluation.score_cached_rows || 0}/${backtest.score_cached_rows || 0} · computed ${evaluation.score_computed_rows || 0}/${backtest.score_computed_rows || 0}`;
  const cachePaths = [evaluation.factor_values_path, backtest.factor_values_path].filter(Boolean).join(' / ');
  const artifacts = [evaluation.artifact_path, backtest.artifact_path].filter(Boolean).join(' / ');
  const reviewWarnings = ((candidate.self_review && candidate.self_review.normalization_warnings) || []).join('; ');
  return `
      <div class="panel hero-panel candidate-card">
        <div>
          <h3>${esc(factor.factor_id)} · ${gate}</h3>
          <div class="formula">${esc(factor.formula)}</div>
          <p>${esc(candidate.hypothesis.text)}</p>
          <p class="meta">${esc(candidate.hypothesis.rationale)}</p>
          <p class="meta">evaluation period: ${esc(profilePeriodText(evaluationProfile))}</p>
          <p class="meta">backtest period: ${esc(profilePeriodText(backtestProfile))}</p>
          <p class="meta">研究口径，不是生产交易口径。</p>
        </div>
        <div class="formula-badge">
          score<br>${num(candidate.score, 4)}
        </div>
        <p>
          <span class="pill">score ${num(candidate.score, 4)}</span>
          <span class="pill">split ICIR ${num(candidate.split_weighted_icir, 2)}</span>
          <span class="pill">IC ${metricNum(evaluation.rank_ic_mean, evaluation.rank_ic_mean_status)}</span>
          <span class="pill">ICIR ${metricNum(evaluation.rank_icir, evaluation.rank_icir_status, 2)}</span>
          <span class="pill">HAC t-stat ${metricNum(evaluation.rank_ic_t_stat, evaluation.rank_ic_t_stat_status, 2)}</span>
          <span class="pill">decay ${esc(valueOr(profile.decay_days, 0))}</span>
          <span class="pill">top ${num(valueOr(profile.top_quantile, backtest.top_quantile), 2)}</span>
          <span class="pill">periods ${esc(backtest.periods)}</span>
          <span class="pill">net LS Sharpe ${num(backtest.net_long_short_sharpe ?? backtest.long_short_sharpe, 2)}</span>
          <span class="pill">gross ${pct(backtest.gross_annualized_return ?? backtest.annualized_return)}</span>
          <span class="pill">net ${pct(backtest.net_annualized_return)}</span>
          <span class="pill">rebalance rate ${pct(backtest.rebalance_rate)}</span>
          <span class="pill">turnover rate ${pct(backtest.turnover_rate)}</span>
          <span class="pill">factor cache ${esc(cacheText)}</span>
        </p>
        <p class="meta">${esc((candidate.self_review && candidate.self_review.summary) || '')}</p>
        <p class="meta">review normalization: ${esc(reviewWarnings || 'none')}</p>
        <p class="meta">factor_values: ${esc(cachePaths || 'none')}</p>
        <p class="meta">artifacts: ${esc(artifacts || 'not generated')}</p>
        <p class="meta">${esc((backtest.warnings || []).join('; ') || 'research semantics, not production trading semantics')}</p>
        <p class="meta">${esc((candidate.gate_reasons || []).join('; '))}</p>
      </div>`;
}

export function renderResearch(payload) {
  const candidates = payload.candidates || [];
  const cards = candidates.map(renderCandidateCard).join('');
  rdResultEl.innerHTML = renderRdSummary(payload)
    + (cards || '<div class="panel"><h3>无候选</h3></div>')
    + renderComparisonTable(payload);
}

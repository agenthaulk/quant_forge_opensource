# Configuration

Default public configuration lives in `configs/default.yaml`.

## Runtime Env Files

Quant Forge does not rely on macOS shell inheritance for local LLM keys. A
config may explicitly declare local env files:

```yaml
runtime:
  env_files:
    - default.local.env
```

Paths must be relative to the YAML config file and stay under that config
directory. These files must be ignored by git and must contain only plain
`KEY=value` lines with no whitespace or shell metacharacters in the value. The
loader does not execute shell syntax, does not scan parent directories, and does
not print loaded values.
For example, the local ignored file can define the variable name used below:

```env
DEEPSEEK_API_KEY=
```

Fill the value only in the ignored local file. Do not commit that file.

All runtime paths may be overridden through CLI flags:

```bash
qf init --workspace ./demo
qf eval-factor FTR_DEMO_SMALL_CAP --workspace ./demo --rd-config configs/rd.yaml
```

The repository does not store local absolute data paths. A user may pass an
absolute path at runtime, but it should not be committed to config, tests, or
docs.

Explicit root flags remain available for advanced workflows:

```bash
qf eval-factor FTR_DEMO_SMALL_CAP --data-root ./demo/data --factor-root ./demo/factor_root --artifact-root ./demo/artifacts --factor-values-root ./demo/factor_values
```

## Factor Value Cache

`paths.factor_values_root` is optional. When it is set, evaluation, backtest,
Workbench, Web, and RD first look for existing factor scores under that root.
If a factor has complete cached values for a trade date, Quant Forge reuses
those values and does not execute the formula for that date.

If only part of the requested panel is cached, Quant Forge computes the missing
dates only and writes them to `incremental/YYYY.parquet` sidecars inside the
matched factor directory. Existing canonical yearly files are not overwritten.
Quant Forge-owned incremental sidecars include a formula/filter signature, so
changing a local factor formula recomputes that sidecar instead of reusing stale
incremental values.

WorldQuant-style names are matched by aliases such as `WQ_ALPHA_003`,
`alpha_003`, and `worldquant_alpha_003`, so a configured WQ Alpha daily factor
library can be reused without recomputing those factors.

```yaml
paths:
  factor_values_root: factor_values
  factor_values_manifest_root: manifests/factor_values
```

## Local LLM Parsing

The local web adapter switches LLM access by the provider selected in the
front-end. `llm.provider` is the default selection, while `llm.providers`
declares every provider that may appear in the Web UI.

Store only environment variable names in configuration. For the local Web
workbench, the actual key should stay in a declared ignored local env file.
`api_key_env` is the name of the variable, not the API key value.

```yaml
llm:
  provider: deepseek
  timeout_seconds: 30
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: DEEPSEEK_API_KEY
    glm:
      model: glm-5.1
      base_url: https://open.bigmodel.cn/api/paas/v4
      api_key_env: GLM_API_KEY
    openai:
      model: gpt-4o-mini
      base_url: https://api.openai.com/v1
      api_key_env: OPENAI_API_KEY
    minimax:
      model: MiniMax-M2
      base_url: https://api.minimax.io/v1
      api_key_env: MINIMAX_API_KEY
    claude:
      model: claude-sonnet-4-5
      base_url: https://api.anthropic.com
      api_key_env: ANTHROPIC_API_KEY
```

Supported provider entries:

```yaml
# OpenAI Chat Completions
llm:
  provider: openai
  providers:
    openai:
      model: <set-current-openai-chat-model>
      base_url: https://api.openai.com/v1
      api_key_env: OPENAI_API_KEY

# GLM / Zhipu OpenAI-compatible Chat Completions
llm:
  provider: glm
  providers:
    glm:
      model: <set-current-glm-model>
      base_url: https://open.bigmodel.cn/api/paas/v4
      api_key_env: GLM_API_KEY

# Claude / Anthropic Messages API
llm:
  provider: claude
  providers:
    claude:
      model: <set-current-claude-model>
      base_url: https://api.anthropic.com
      api_key_env: ANTHROPIC_API_KEY

# DeepSeek OpenAI-compatible Chat Completions
llm:
  provider: deepseek
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: DEEPSEEK_API_KEY

# MiniMax OpenAI-compatible Chat Completions
llm:
  provider: minimax
  providers:
    minimax:
      model: <set-current-minimax-model>
      base_url: https://api.minimax.io/v1
      api_key_env: MINIMAX_API_KEY

# Custom OpenAI-compatible endpoint
llm:
  provider: openai_compatible
  providers:
    openai_compatible:
      provider: openai_compatible
      model: <model-name>
      base_url: <https://host.example/v1>
      api_key_env: OPENAI_COMPATIBLE_API_KEY
```

For configured providers, `model`, `base_url`, and `api_key_env` are required.
If one is missing, config loading fails with a provider-specific message such
as `llm.providers.deepseek.base_url`. If the selected provider's environment
variable is not set when parsing starts, the parser fails with a message like
`Missing API key for active LLM provider deepseek. Expected environment
variable: DEEPSEEK_API_KEY.`

Example local setup:

```bash
printf 'DEEPSEEK_API_KEY=<your-deepseek-api-key>\n' > configs/default.local.env
chmod 600 configs/default.local.env
qf web --config configs/default.local.yaml --rd-config configs/rd.yaml
```

## Research Loop

Default RD settings live in `configs/rd.yaml`. They are public, local, and
explicit:

```yaml
objective: balanced
default_max_candidates: 3
default_interval_days: 1
allowed_interval_days: [1, 5, 15, 30]
top_quantile: 0.3
simulation:
  execution_delay_days: 1
  top_quantile: 0.3
  nan_policy: drop
  neutralization: none
  truncation: null
  decay_days: 0
  test_period:
    start: null
    end: null
parameter_search:
  enabled: false
  method: successive_halving
  max_profile_variants: 6
  keep_ratio: 0.34
  min_survivors: 2
  quick_horizon_days_matrix: [5, 21]
  quick_sample_splits:
    - name: IS
      fraction: 1.0
      score_weight: 1.0
  top_quantile: [0.3]
  decay_days: [0]
horizon_days_matrix: [5, 10, 21, 63]
sample_splits:
  - name: IS
    fraction: 0.5
    score_weight: 0.5
  - name: OOS1
    fraction: 0.3
    score_weight: 0.3
  - name: OOS2
    fraction: 0.2
    score_weight: 0.2
gate:
  min_ic_days: 5
  min_coverage: 0.5
  min_score: 0.0
  min_backtest_periods: 1
  min_oos_net_annualized_return: null
  max_rebalance_rate: null
  max_turnover_rate: null
  min_net_return_retention: null
  max_oos_net_return_decay: null
transaction_costs:
  commission_bps: 0.0
  slippage_bps: 0.0
  short_borrow_bps_annual: 0.0
weights:
  weighted_split_icir: 0.4
  rank_ic_mean: 0.25
  rank_icir: 0.2
  annualized_return: 0.1
  max_drawdown: 0.05
weight_profiles:
  rank_ic:
    weighted_split_icir: 0.2
    rank_ic_mean: 0.6
    rank_icir: 0.1
    annualized_return: 0.1
    max_drawdown: 0.0
  rank_icir:
    weighted_split_icir: 0.5
    rank_ic_mean: 0.1
    rank_icir: 0.3
    annualized_return: 0.05
    max_drawdown: 0.05
  annualized_return:
    weighted_split_icir: 0.2
    rank_ic_mean: 0.15
    rank_icir: 0.15
    annualized_return: 0.4
    max_drawdown: 0.1
```

The default FactorLab-style evaluation runs a 5/10/21/63-day horizon matrix and
splits the usable dates chronologically into IS/OOS1/OOS2 at 50%/30%/20%.
`score_weight` controls the weighted split ICIR score. The default RD score
keeps IC and ICIR dominant by combining weighted split ICIR, whole-sample Rank
IC, whole-sample ICIR, net-of-cost backtest return, and net drawdown. The score
is for local research ordering only. It is not an investment recommendation and
it does not move factors to `active`.

Every RD run writes one Markdown research report under
`artifact_root/research_reports` and returns its `report_path` from CLI and web
JSON payloads. The report is deterministic local output: overview, SOTA/best
candidate, candidate comparison, iteration trace, split and horizon evidence,
group returns, conclusion notes, and risk notes. It is not LLM-authored by
default and is not an investment recommendation.

`weights` applies to the configured default `objective`. `weight_profiles`
applies when a user temporarily selects another objective from CLI or web, so
objective changes remain explicit and reproducible instead of falling back to
hidden code defaults.

`transaction_costs` configures lightweight research assumptions in basis
points. Commission and slippage are charged against estimated traded notional
from portfolio weight changes, while `short_borrow_bps_annual` is prorated by
the holding period. Backtests report gross and net metrics side by side.

Backtests expose only two turnover-style research metrics: `rebalance_rate`
for long/short membership changes per rebalance, and `turnover_rate` for the
true portfolio turnover estimate from weight changes.

Backtest segment metrics split returns, Sharpe, and drawdown across the same
configured IS/OOS sample split names. Optional gate fields can reject RD
candidates for weak OOS net return, excessive rebalance rate, excessive
turnover rate, low net/gross retention, or OOS net-return decay.

`simulation` is the effective profile shared by evaluation, backtesting, web
idea workflows, and RD. First-version score preparation applies `test_period`,
factor formula execution, universe filters, and EWMA `decay_days`; it supports
only `nan_policy: drop`, `neutralization: none`, and `truncation: null`.
Unsupported values fail fast.

`top_quantile` controls the long and short tail size. The legacy top-level key
is still accepted, but the canonical value is `simulation.top_quantile`. Pass
`--top-quantile` only when a single CLI backtest should override the configured
value.

`parameter_search` lets RD score generated formulas across a bounded grid of
simulation profile variants. The public default is disabled and keeps a single
pure daily profile. The first version supports only `top_quantile` and
`decay_days` variants.

When enabled with `method: successive_halving`, RD runs a quick stage over all
formula/profile trials using `quick_horizon_days_matrix` and
`quick_sample_splits`, keeps the top `ceil(total * keep_ratio)` trials subject
to `min_survivors`, and runs the full IS/OOS plus backtest path only for those
survivors. Quick-stage trials are recorded in the Markdown report but are not
eligible for factor promotion.

Use it from CLI or the local web adapter:

```bash
qf eval-factor FTR_DEMO_SMALL_CAP --workspace ./demo --rd-config configs/rd.yaml
qf run-backtest FTR_DEMO_SMALL_CAP --workspace ./demo --rd-config configs/rd.yaml
qf research run-once FTR_DEMO_SMALL_CAP --workspace ./demo --rd-config configs/rd.yaml
qf web --workspace ./demo --rd-config configs/rd.yaml
```

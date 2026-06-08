# Configuration

Default public configuration lives in `configs/default.yaml`. Use
`configs/mounted.draft.yaml` as the copyable template when the runtime database
lives on a mounted disk.

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
qf eval-factor FTR_DEMO_SMALL_CAP --data-root ./demo/data --factor-root ./demo/factor_root --artifact-root ./demo/artifacts --factor-values-root ./demo/factor_values --factor-values-manifest-root ./demo/manifests/factor_values
```

## Mounted Database Discovery

Quant Forge treats the configured roots as a portable local database. When the
same mounted drive is attached on another machine, point the local config at the
mounted roots and run `qf doctor` before starting Web or RD.

`paths.data_root` may point to a directory containing `panel.parquet`, to a
workspace directory containing `data/panel.parquet`, directly to a parquet panel
file, or to a mounted source snapshot root containing `price/` and
`daily_basic/`. The source snapshot adapter builds the lightweight public panel
from close, volume, and market-value fields; deeper PIT and provider-specific
ETL remain outside the lightweight core.

`paths.factor_root` remains the writable source of truth for user-created factor
definitions. `paths.factor_values_root` is read as an additional mounted factor
database. `qf factor list`, evaluation, backtest, Web, MCP catalog, and RD seed
loading merge both sources at read time without copying mounted factors into
`factor_root`.

Factor definitions are stored under category directories:

```text
factor_root/
  原始因子/{active_factors,inactive_factors}/<FACTOR_ID>/factor.yaml
  合成因子/{active_factors,inactive_factors}/<FACTOR_ID>/factor.yaml
```

`原始因子` contains imported/public formulas and external precomputed factors.
`合成因子` contains RD, campaign, parameter-search, or composite candidates.
Legacy flat paths such as `factor_root/inactive_factors/<FACTOR_ID>/factor.yaml`
remain readable. Run `qf factor normalize-root` to copy those legacy definitions
into the categorized layout without deleting the originals.

`paths.factor_values_root` may point directly at a canonical factor-value root,
or at a mounted data root containing `canonical/factor=cn_a`. Treat this root as
the read base for existing daily factor values. `paths.factor_values_overlay_root`
is an optional writable overlay for newly computed increments. The preferred
factor-value layout is one category directory plus one directory per registered
factor:

```text
factor_values_root/
  原始因子/factor_id=<FACTOR_ID>/
    2025.parquet
    <FACTOR_ID>.metadata.json
    incremental/
      2026.parquet
  合成因子/factor_id=<FACTOR_ID>/
    2025.parquet
    <FACTOR_ID>.metadata.json
    incremental/
      2026.parquet
```

Legacy directories such as `worldquant_alpha_003/2025.parquet`,
`alpha_003/2025.parquet`, or a factor-name directory remain readable for
mounted historical stores, but Quant Forge no longer treats provider or formula
family names as canonical storage paths. New incremental factor values are
written under
`factor_values_overlay_root/{原始因子,合成因子}/factor_id=<FACTOR_ID>/incremental/YYYY.parquet`
when an overlay is configured, otherwise they fall back to
`factor_values_root/{原始因子,合成因子}/factor_id=<FACTOR_ID>/incremental/YYYY.parquet`.

Discovered precomputed factors use a lightweight formula marker such as
`precomputed:factor_id=WQ_ALPHA_003`. They are cache-only: Quant Forge reads the
available values, leaves uncovered instruments as missing values, and does not
attempt to recompute external or complex DSL formulas in the public kernel.

To make mounted precomputed factors part of the local project registry, import
them explicitly:

```bash
qf factor import-precomputed --config configs/default.local.yaml --all
qf factor import-precomputed WQ_ALPHA_003 --config configs/default.local.yaml
```

The import writes `factor.yaml` files under `factor_root` with
`source: precomputed` and `formula: precomputed:<store_key>`. It does not store
mounted absolute paths in those factor definitions.

To normalize existing factor definitions without deleting legacy flat paths,
run:

```bash
qf factor normalize-root --config configs/default.local.yaml --dry-run
qf factor normalize-root --config configs/default.local.yaml
```

To normalize an existing mounted factor-value store without deleting legacy
directories, run:

```bash
qf factor normalize-store --config configs/default.local.yaml --dry-run
qf factor normalize-store --config configs/default.local.yaml --link-files
```

When a mounted disk contains previous factor-value roots outside the configured
canonical root, merge them by passing explicit sources or scanning the mounted
data tree:

```bash
qf factor normalize-store --config configs/default.local.yaml \
  --source-factor-values-root <MOUNT_ROOT>/QuantForgeData/facotrs/wq77_hs300_csi500_20250101_20251231/factor_values \
  --link-files

qf factor normalize-store --config configs/default.local.yaml \
  --scan-root <MOUNT_ROOT>/QuantForgeData \
  --link-files
```

The command creates or updates
`原始因子/factor_id=<FACTOR_ID>` or `合成因子/factor_id=<FACTOR_ID>` directories
and writes a portable metadata manifest when `paths.factor_values_manifest_root`
is configured. `--link-files` uses hardlinks when the mounted filesystem
supports them, falling back to normal copies if needed.

## Factor Value Cache

`paths.factor_values_root` is optional. When it is set, evaluation, backtest,
Workbench, Web, and RD first look for existing factor scores under that root.
If `paths.factor_values_overlay_root` is also set, Quant Forge reads the
canonical root first and the overlay second; overlay values win for duplicate
`trade_date/instrument` keys. If a factor has complete cached values for a trade
date, Quant Forge reuses those values and does not execute the formula for that
date.

If only part of the requested panel is cached, Quant Forge computes the missing
dates only and writes them to `incremental/YYYY.parquet` sidecars inside the
writable overlay when configured. Existing canonical yearly files are not
overwritten.
Quant Forge-owned incremental sidecars include a formula/filter signature, so
changing a local factor formula recomputes that sidecar instead of reusing stale
incremental values.

WorldQuant-style names are matched by aliases such as `WQ_ALPHA_003`,
`alpha_003`, and `worldquant_alpha_003`, so a configured WQ Alpha daily factor
library can be reused without recomputing those factors. Alias matching is a
legacy read-compatibility feature only; it does not change the canonical store
key exposed to the project.

```yaml
paths:
  factor_values_root: factor_values
  factor_values_overlay_root: factor_values_overlay
  factor_values_manifest_root: manifests/factor_values
```

## Local LLM Parsing

The local web adapter can use one active LLM provider for natural-language
factor parsing and optional RD LLM features. The public RD config is
local-first by default. Ordinary RD focuses on research ideas and bounded
hyper-parameter/profile search; set `llm.hypothesis_mode` and
`llm.review_mode` to `llm` in an ignored local RD config when you want RD idea
generation and self-review to reuse the same provider/key. Set
`llm.campaign_mode` only for the later factor-synthesis Campaign workflow.

`llm.providers` declares provider entries that may appear in the Web UI. Keep
the default config to one provider/key unless you intentionally want users to
choose among multiple providers.

Store only environment variable names in configuration. For the local Web
workbench, the actual key should stay in a declared ignored local env file.
`api_key_env` is the name of the variable, not the API key value.
Local rule parsing remains available as a separate, explicitly labeled mode;
LLM mode never silently falls back to rules. If a key is missing or a request
fails, the Web UI shows the LLM failure reason and asks before retrying with
local rule parsing.

```yaml
llm:
  provider: deepseek
  timeout_seconds: 30
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: DEEPSEEK_API_KEY
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

# Local OpenAI-compatible endpoint without authentication
llm:
  provider: openai_compatible
  providers:
    openai_compatible:
      provider: openai_compatible
      model: <local-model-name>
      base_url: http://127.0.0.1:11434/v1
      # set require_api_key to false
```

For cloud providers, `model`, `base_url`, and `api_key_env` are required. If
one is missing, config loading fails with a provider-specific message such as
`llm.providers.deepseek.base_url`. If the selected provider's environment
variable is not set when parsing starts, the parser fails with a message like
`Missing API key for active LLM provider deepseek. Expected environment
variable: DEEPSEEK_API_KEY.` For local providers with `require_api_key` set to `false`,
`api_key_env` is optional and no Authorization header is sent.

`qf web` starts even when the active cloud provider's key is not loaded yet, so
the user can inspect local status or choose rule parsing. The key is validated
when an LLM parse or LLM-backed RD action is actually requested. The public RD
config is local-first, so default RD smoke runs do not require a provider key.
If an ignored local RD config explicitly enables an LLM-backed RD mode and the
active provider cannot be used, that RD action fails with the LLM readiness
reason instead of silently switching to local rules.

Example local setup:

```bash
printf 'DEEPSEEK_API_KEY=<your-deepseek-api-key>\n' > configs/default.local.env
chmod 600 configs/default.local.env
qf web --config configs/default.local.yaml --rd-config configs/rd.yaml
```

## Research Loop

Default RD settings live in `configs/rd.yaml`. They are public, local, and
explicit:

RD hypothesis and review modes are `local` by default. For optional LLM-backed
ordinary RD work, copy `configs/rd.draft.yaml` to an ignored local RD config and
set `llm.hypothesis_mode` and `llm.review_mode` to `llm`. RD uses the main
`llm` config in that mode; do not create a separate RD API key for the same
provider. Keep one active `llm.provider` and one matching `api_key_env`.
`llm.campaign_mode` is reserved for factor-synthesis Campaign runs.

```yaml
objective: balanced
default_max_candidates: 3
default_interval_days: 1
allowed_interval_days: [1, 5, 15, 30]
top_quantile: 0.3
llm:
  hypothesis_mode: local
  review_mode: local
  # Use only when running factor-synthesis Campaign workflows.
  campaign_mode: local
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
JSON payloads. The report contains local evidence: overview, SOTA/best
candidate, candidate comparison, iteration trace, split and horizon evidence,
group returns, conclusion notes, and risk notes. When `llm.review_mode: llm`,
the bounded self-review text is LLM-generated from local evidence; evaluation,
backtest metrics, gates, and promotion decisions remain local and reproducible.
The report is not an investment recommendation.

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

When LLM-backed hypothesis generation is enabled, RD gives the model local
operator context, field constraints, effective prior ideas, and recent failure
feedback. The model should first propose executable operator-aware or financial
analysis hypotheses, including variations inspired by effective ideas. Only
when no better executable formula idea is available should it mark a parameter
search fallback; deterministic gating and local formula validation still decide
what can run.

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

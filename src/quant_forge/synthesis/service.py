"""A-priori composite core: standardize, direction, combine, coverage, pre-scan.

Phase P3 of the multi-factor backtest design
(``docs/design/multi_factor_portfolio_backtest.md`` §4.1-4.5, §12, §13 and the
CP0 amendments). Everything in this module is a PURE function over in-memory
frames: no endpoint, no materialization, no I/O. Later phases feed it the
per-member tidy score frames produced by ``prepare_factor_scores_result``
(columns ``[trade_date, instrument, score]``) and hand its composite output to
the materialization + engine drive steps.

Contract highlights implemented here:

- **Standardization (§4.2)** is cross-sectional per ``trade_date`` only —
  never pooled across dates. ``zscore`` maps a no-dispersion cross-section
  (std 0, or a single observation) to a 0.0 contribution for every observed
  name that date and records the date as degenerate for that factor;
  ``rank`` uses the deterministic tie policy ``method='first'`` over an
  instrument-sorted frame and maps percentile ranks onto ``[-1, 1]`` via
  ``2*r - 1`` (RB-3). Known asymmetry, disclosed: the design pins the
  no-dispersion -> 0 rule for ``zscore`` only; an all-equal cross-section
  under ``rank`` keeps its deterministic 'first' ordering and is caught at the
  composite level by RB-9 when it degenerates the whole cross-section.
- **Directions (§4.3)** are explicit ``+1``/``-1`` per member, applied AFTER
  standardization; ``-1`` exactly negates the standardized score. Directions
  are never derived from data.
- **A-priori combine (§4.4)** implements the literal formula
  ``composite = sum_f w_f * t_f`` with a missing factor at a
  ``(date, instrument)`` excluded from that name's sum (never imputed as a
  0 score). ``equal_weight`` uses the raw weight 1.0 per member (the design's
  "raw 1 before averaging" convention) so it equals ``weighted`` with a
  uniform declared vector; ``weighted`` uses the caller's raw weights and
  echoes them raw in ``weights_effective`` — never normalized for display.
  Under an opt-in ``min_factor_coverage < N`` rule, rows carried with partial
  coverage sum fewer terms and therefore lean toward the cross-sectional
  neutral value relative to full-coverage rows; this is the pinned formula's
  behavior and is disclosed here rather than silently rescaled.
- **Coverage (§4.5, FP-4)** reports per-factor ``rows_scored`` /
  ``rows_in_composite`` / ``coverage_ratio`` where an unobservable denominator
  yields a real ``None`` — never a fabricated 0. ``rows_required`` is the
  observed union index size and ``rows_full_coverage`` counts rows where every
  member is finite. Coverage is accounted at combine time; the RB-9 date
  erasure below is disclosed separately (its own dates + count), so the two
  diagnostics stay independently attributable.
- **Degenerate cross-sections (RB-9)** after combine: a date whose composite
  cross-section is all-NaN OR zero-variance (all finite values equal,
  including a single-name cross-section) becomes all-NaN so the engine drops
  the date instead of trading tie noise, and the date is counted under
  ``DEGENERATE_CROSS_SECTION``.
- **Skip pre-scan (RB-7, synthesis side)** classifies every rebalance signal
  date the shared ``rebalance_indices`` grid yields (RB-5: imported from the
  engine module, never re-derived) as ok / empty / thin against the composite
  coverage, emitting the same ``REBALANCE_SKIPPED_NO_COVERAGE`` /
  ``REBALANCE_SKIPPED_THIN`` code literals the engine stubs use. The scan is
  score-side: the engine can additionally skip a date on period-return
  coverage grounds, so the scan is a lower bound on realized skips.
- **Window preconditions (RB-2)** compute the realized non-overlapping period
  count ``N = floor((len(in_window_dates) - delay - 1) / holding) + 1`` and
  reject ``N < 2`` with the typed ``WindowTooShortError`` the workflow maps to
  a client error response.
- **Universe pinning (RB-6)** resolves ONE explicit ``universe_filters`` set
  for all members and rejects conflicting member declarations with the typed
  ``UniverseMismatchError`` — universes are never silently unioned.
- **Member-formula pinning (CP0 amendment)** captures each member's formula
  string into the fetch plan at plan-build time so provenance carries the
  formulas actually run, independent of later registry state.

Phase P4 (materialize + drive the shipped engine by id) adds:

- **Composite id (RB-10 / RF-1):** ``derive_composite_id`` hashes the
  canonical JSON of ALL run inputs — the ordered ``(factor_id, direction)``
  list, method, method_params, standardization, backtest window, decay,
  delay, top_quantile, coverage rule, min_factor_coverage, and the pinned
  universe — into ``COMPOSITE_<12 uppercase hex>``. Hashing every input is
  what stops ``_merge_score_updates`` retain-old-dates poisoning: any config
  change mints a fresh id. The hex is UPPERCASE by construction because
  ``FactorCatalog`` canonicalizes precomputed formulas to the uppercased id
  (``_canonical_factor_id``); an already-canonical id makes the write-time
  formula equal the read-time formula, so the value-store signature filter
  matches. The id is colon-free by construction (RF-1).
- **Materialization (RF-2 / RF-3):** ``materialize_composite`` writes the
  composite frame through the store's OWN layout resolution
  (``FactorValueStore._resolve_factor_paths`` + ``write_incremental_values``)
  — never a hand-built ``overlay_root/<id>`` directory — and registers the
  ``FactorDefinition`` via ``FactorRepository(factor_root).save`` (there is
  no ``register_factor`` symbol). The stored ``formula_signature`` is the
  store's own ``_formula_signature(factor_id, executable_formula,
  universe_filters)`` — the exact value ``prepare_scores`` filters by at
  read time; writing the raw formula string instead would silently read
  back zero rows. A catalog round-trip guard asserts the resolved
  definition carries the identical formula/filters before any engine run.
- **Engine drive (LA-1 / RB-5):** ``run_composite_backtest`` pins the
  engine-driving profile to ``decay_days=0`` structurally (members were
  already EWMA-decayed inside ``prepare_factor_scores_result`` under the
  shared profile; the combined composite must not be decayed a second
  time), drives ``run_factor_backtest`` by the composite id over a per-run
  overlay, and then asserts grid fidelity: the realized
  ``resolved_schedule`` signal dates must equal the shared
  ``rebalance_indices`` grid (after the engine's own disclosed D3
  final-partial exclusion) — a mismatch raises ``GridFidelityError``, never
  passes silently.
- **Failure cleanup:** any exception after materialization begins removes
  the registered definition (restoring a pre-existing one, mirroring
  ``_restore_factor_after_failed_validation``) and deletes the per-run
  overlay directory. On success the definition stays as a first-class
  factor; a retention/GC policy for accumulated successful composite
  definitions is a recorded deferred question.

Phase P6 (fitted methods, §4.4) adds the point-in-time IC/ICIR combination:

- **Anti-peek embargo (the load-bearing invariant):** at a rebalance date
  ``d`` the IC estimate uses ONLY periods ``s`` on the SHARED
  ``rebalance_indices`` grid with ``idx(s) + delay + holding <= idx(d)``
  and ``s >= start`` — periods whose forward-return window has fully
  closed on or before ``d``. A period closing exactly AT ``d`` is
  eligible; one closing at ``d + 1`` is not. Forward returns come from the
  engine's own ``_with_period_return`` close-to-close primitive (RB-5), so
  the IC that sets the weights and the return the engine realizes are the
  same object.
- **Weight rule (§4.4 pseudocode, NaN-hardened):** per-factor raw weight is
  the mean per-period cross-sectional Spearman rank IC (``ic_weighted``)
  or mean/std with an explicit ``std == 0`` guard (``icir_weighted``);
  negative and non-finite raws clip to 0 through an explicit finite check
  (never a bare ``max``, which is NaN-order-dependent); an all-zero vector
  falls back to per-date equal weight flagged ``IC_DEGENERATE_EQUAL_WEIGHT``
  rather than emptying the rebalance. Fewer than ``ic_min_periods`` closed
  periods runs the date equal-weight flagged ``WARM_UP_IC_UNFITTED``. Zero
  genuinely fitted rebalances in the whole window downgrades the run:
  ``is_fitted=False`` + ``NO_FITTED_PERIODS`` (RB-8) — the payload never
  advertises fitting on an all-fallback run.
- **Fold-in before materialization:** the stored composite frame carries
  already-combined values; each calendar date adopts the weight vector of
  the most recent grid rebalance at or before it (weights estimated from
  strictly older closed periods stay point-in-time valid for later dates),
  so the value the engine reads at a grid signal date is exactly
  ``sum_f w_{f,d} * t_{f,d,i}``. Per-date weights are exposed as
  diagnostics (``weights_path`` / ``fitted_weights_latest``), NEVER as
  ``weights_effective`` — that provenance field is an a-priori raw-declared
  claim (FP-1) and fitted runs omit it entirely.
- **Advisory redundancy diagnostic:** ``member_rank_ic_redundancy`` reports
  the pairwise correlation of the members' realized per-period rank-IC
  series over the window (crowding check). Advisory only — it never gates
  and never feeds weights — with real ``None`` cells when unobservable
  (FP-4).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

import numpy as np
import pandas as pd

from quant_forge.backtesting.service import (
    EXTERNAL_OOS_ROLE,
    REBALANCE_SKIPPED_NO_COVERAGE,
    REBALANCE_SKIPPED_THIN,
    _with_period_return,
    rebalance_indices,
    run_factor_backtest,
)
from quant_forge.core.contracts import (
    BacktestResult,
    FactorDefinition,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import apply_test_period
from quant_forge.factor_engine.value_store import FactorValueStore, _formula_signature
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.operator_registry.resolver import resolve_executable_formula

DEGENERATE_CROSS_SECTION = "DEGENERATE_CROSS_SECTION"
WINDOW_TOO_SHORT = "WINDOW_TOO_SHORT"
UNIVERSE_MISMATCH = "UNIVERSE_MISMATCH"
GRID_FIDELITY_MISMATCH = "GRID_FIDELITY_MISMATCH"
# RB-1 disclosure codes (design §1/§6): cadence == lifetime, so every run is a
# non-overlapping K=1 cohort sample of ~len/holding independent periods and is
# start-phase sensitive. Emitted on every composite run, with the realized
# period count carried in provenance and in a human-readable warning line.
NON_OVERLAPPING_COHORTS = "NON_OVERLAPPING_COHORTS"
PHASE_SENSITIVE_SMALL_SAMPLE = "PHASE_SENSITIVE_SMALL_SAMPLE"
# FP-2 degrade code: the same-window evaluation diagnostics slot could not be
# filled because the backtest window is shorter than the evaluation layer's
# 126-trading-day display floor (`MIN_DISPLAY_TRADING_DAYS`); the backtest
# itself still runs (its real gate is max(2, holding+delay+1), RF-4).
EVALUATION_WINDOW_TOO_SHORT = "EVALUATION_WINDOW_TOO_SHORT"
# §4.4 fitted-method codes (P6). WARM_UP_IC_UNFITTED: a rebalance with fewer
# than ic_min_periods fully-closed prior periods trades the per-date
# equal-weight fallback, flagged. IC_DEGENERATE_EQUAL_WEIGHT: a fittable
# rebalance whose clipped raw weight vector summed to zero (all ICs
# negative/non-finite/empty) trades the same fallback instead of collapsing
# the cross-section to NaN and silently emptying the rebalance.
# NO_FITTED_PERIODS: the RB-8 downgrade — ZERO rebalances in the window
# produced a genuinely fitted vector, so the run reports ``is_fitted=false``
# (it traded equal weight throughout and must never advertise fitting).
WARM_UP_IC_UNFITTED = "WARM_UP_IC_UNFITTED"
IC_DEGENERATE_EQUAL_WEIGHT = "IC_DEGENERATE_EQUAL_WEIGHT"
NO_FITTED_PERIODS = "NO_FITTED_PERIODS"

COMPOSITE_ID_PREFIX = "COMPOSITE_"
# RB-10: id = COMPOSITE_ + first 12 hex of a stable sha256 over ALL inputs.
_COMPOSITE_HASH_HEX_CHARS = 12
# Uppercase hex only: FactorCatalog canonicalizes precomputed ids/formulas to
# uppercase, so an uppercase id round-trips identically and the write-time
# value-store signature equals the read-time one (see module docstring).
_COMPOSITE_ID_RE = re.compile(r"COMPOSITE_[0-9A-F]{10,16}")

COVERAGE_RULE_ALL_FACTORS = "all_factors"
COVERAGE_RULE_MIN_FACTOR_COVERAGE = "min_factor_coverage"

APRIORI_METHODS: tuple[str, ...] = ("equal_weight", "weighted")
FITTED_METHODS: tuple[str, ...] = ("ic_weighted", "icir_weighted")
# §4.4/§9 catalog default for ic_min_periods; the web workflow resolves the
# request value through the catalog ParamSpec schema (which declares the same
# literal), so this constant only backs direct library callers.
DEFAULT_IC_MIN_PERIODS = 6

# RB-6 / Codex A-1: the cn_a formation default applied when NO member declares
# a universe — an empty pin would run every member fetch unfiltered and let an
# ST name scored by all members enter the book. Filters are predicate strings
# over panel columns (the same vocabulary FactorDefinition.universe_filters
# uses); a minimum-listing-age filter is not expressible on the current panel
# schema (no listing-date column) and is documented as such in the design doc.
DEFAULT_PINNED_UNIVERSE: tuple[str, ...] = ("is_st == false",)

SCAN_STATUS_OK = "ok"
SCAN_STATUS_EMPTY = "empty"
SCAN_STATUS_THIN = "thin"

_MATRIX_LEVELS: tuple[str, ...] = ("trade_date", "instrument")
_SCORE_COLUMNS: tuple[str, ...] = ("trade_date", "instrument", "score")
_ALLOWED_DIRECTIONS: tuple[int, ...] = (1, -1)

# Mirrors the engine's inline thin-cross-section threshold
# (`backtesting/service.py`: `len(merged) < max(4, group_count)`).
_MIN_THIN_CROSS_SECTION = 4


class SynthesisPreconditionError(ValueError):
    """Typed request-precondition failure the workflow maps to a 4xx response.

    Subclasses carry a stable ``code`` literal that lands in the error payload
    and in ``warning_codes`` vocabularies; subclassing ``ValueError`` keeps the
    existing web-layer invalid-request mapping working unchanged.
    """

    code = "SYNTHESIS_PRECONDITION"


class WindowTooShortError(SynthesisPreconditionError):
    """RB-2: the backtest window admits fewer than 2 non-overlapping periods."""

    code = WINDOW_TOO_SHORT


class UniverseMismatchError(SynthesisPreconditionError):
    """RB-6: member factors declare conflicting universe filter sets."""

    code = UNIVERSE_MISMATCH


class GridFidelityError(RuntimeError):
    """RB-5/RB-7: the engine's realized schedule diverged from the shared grid.

    Raised AFTER an engine run when ``resolved_schedule`` signal dates differ
    from ``dates[rebalance_indices(...)]`` (filtered only by the engine's own
    disclosed D3 final-partial exclusion). This is an internal-contract
    failure, not a request precondition, so it is a ``RuntimeError``: the run
    result must never be reported as if it traded the declared grid.
    """

    code = GRID_FIDELITY_MISMATCH


@dataclass(frozen=True)
class FactorCoverage:
    """Per-factor coverage row (§4.5): observed counts, never fabricated.

    ``coverage_ratio`` is a real ``None`` when ``rows_scored`` is 0 (the
    denominator is unobservable — FP-4); a genuine observed zero
    (``rows_scored > 0`` and ``rows_in_composite == 0``) is reported as 0.0.
    """

    factor_id: str
    rows_scored: int
    rows_in_composite: int
    coverage_ratio: float | None


@dataclass(frozen=True)
class CoverageAccounting:
    """Composite coverage provenance (§4.5).

    ``rows_required`` is the observed union index size (every
    ``(trade_date, instrument)`` row any member scored); ``rows_full_coverage``
    counts rows where ALL members are finite. Accounting happens at combine
    time — dates later erased by the RB-9 degenerate rule stay counted here
    and are disclosed separately via ``CompositeResult.degenerate_dates``.
    """

    coverage_rule: str
    min_factor_coverage: int
    rows_required: int
    rows_full_coverage: int
    per_factor: tuple[FactorCoverage, ...]


@dataclass(frozen=True)
class StandardizationOutcome:
    """Standardized matrix plus per-factor degenerate-date marks (§4.2)."""

    matrix: pd.DataFrame
    degenerate_dates_by_factor: Mapping[str, tuple[pd.Timestamp, ...]]


@dataclass(frozen=True)
class CompositeResult:
    """A-priori composite output: tidy frame + the §8 provenance claims.

    ``composite`` is tidy ``[trade_date, instrument, score]`` sorted by
    ``(trade_date, instrument)`` — the ``prepare_factor_scores_result`` output
    shape — with NaN scores preserved (the engine drops them per signal date;
    the pre-scan reads them as absence). ``weights_effective`` echoes the raw
    a-priori weights actually used in the sum, never normalized for display.
    """

    composite: pd.DataFrame
    method: str
    weights_effective: dict[str, float]
    coverage: CoverageAccounting
    degenerate_dates: tuple[pd.Timestamp, ...]
    warning_codes: tuple[str, ...]
    standardization: str | None = None
    degenerate_dates_by_factor: Mapping[str, tuple[pd.Timestamp, ...]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class RebalanceScanEntry:
    """One classified rebalance slot from the shared grid (RB-7 pre-scan)."""

    signal_index: int
    signal_date: pd.Timestamp
    status: str
    finite_count: int
    skip_code: str | None


@dataclass(frozen=True)
class RebalancePrescan:
    """Synthesis-side skip pre-scan result (RB-7).

    ``warning_codes`` carries each skip code at most once; the per-date
    detail lives in ``entries`` and the ``skipped_*_dates`` tuples.
    ``final_partial_excluded`` mirrors the engine's default D3 break: the
    trailing signal whose scheduled exit falls beyond the window is never
    classified because the engine never evaluates it.
    """

    entries: tuple[RebalanceScanEntry, ...]
    ok_count: int
    skipped_no_coverage_count: int
    skipped_thin_count: int
    skipped_no_coverage_dates: tuple[pd.Timestamp, ...]
    skipped_thin_dates: tuple[pd.Timestamp, ...]
    warning_codes: tuple[str, ...]
    final_partial_excluded: bool
    thin_threshold: int


@dataclass(frozen=True)
class MemberFetchSpec:
    """Per-member score-fetch plan entry with the formula pinned at run time.

    CP0 amendment: ``formula`` is captured when the plan is built so
    ``synthesis_provenance.factors[]`` reports the exact formula strings the
    run fetched, independent of any later registry edits. ``universe_filters``
    is the one pinned set (RB-6) every member fetch must use.
    """

    factor_id: str
    factor_name: str
    formula: str
    direction: int
    source: str
    universe_filters: tuple[str, ...]

    def provenance_entry(self) -> dict[str, object]:
        """The §8 ``synthesis_provenance.factors[]`` row (CP0: with formula)."""

        return {
            "factor_id": self.factor_id,
            "direction": self.direction,
            "source": self.source,
            "formula": self.formula,
        }


def _require_score_matrix(matrix: pd.DataFrame) -> None:
    if not isinstance(matrix, pd.DataFrame) or not isinstance(matrix.index, pd.MultiIndex):
        raise ValueError("score matrix must be a DataFrame indexed by (trade_date, instrument)")
    missing = [level for level in _MATRIX_LEVELS if level not in (matrix.index.names or ())]
    if missing:
        raise ValueError(f"score matrix index is missing levels: {missing}")


def _validate_direction(factor_id: str, direction: object) -> int:
    if isinstance(direction, bool) or not isinstance(direction, int):
        raise ValueError(f"direction must be the integer +1 or -1 for factor: {factor_id}")
    if direction not in _ALLOWED_DIRECTIONS:
        raise ValueError(f"direction must be +1 or -1 for factor: {factor_id}")
    return int(direction)


def build_score_matrix(member_scores: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Pivot per-member tidy frames into one (trade_date, instrument) matrix.

    Input frames use the ``prepare_factor_scores_result`` shape
    ``[trade_date, instrument, score]``. Column order preserves the mapping
    order (the request's factor order). Non-finite raw scores are treated as
    missing, never as values (FP-4). The result index is sorted by
    ``(trade_date, instrument)`` so downstream deterministic tie policies see
    an instrument-sorted frame (RB-3).
    """

    if not member_scores:
        raise ValueError("at least one member score frame is required")
    columns: dict[str, pd.Series] = {}
    for factor_id, frame in member_scores.items():
        if not str(factor_id).strip():
            raise ValueError("member factor_id must be a non-empty string")
        missing_columns = [column for column in _SCORE_COLUMNS if column not in frame.columns]
        if missing_columns:
            raise ValueError(
                f"score frame for factor {factor_id} is missing columns: {missing_columns}"
            )
        tidy = frame[list(_SCORE_COLUMNS)].copy()
        tidy["trade_date"] = pd.to_datetime(tidy["trade_date"])
        tidy["instrument"] = tidy["instrument"].astype(str)
        series = tidy.set_index(list(_MATRIX_LEVELS))["score"].astype("float64")
        if series.index.has_duplicates:
            raise ValueError(f"duplicate (trade_date, instrument) rows for factor: {factor_id}")
        columns[str(factor_id)] = series.where(np.isfinite(series.to_numpy()))
    matrix = pd.concat(columns, axis=1, join="outer")
    matrix.index = matrix.index.set_names(list(_MATRIX_LEVELS))
    return matrix.sort_index()


def _standardize_zscore(matrix: pd.DataFrame) -> StandardizationOutcome:
    grouped = matrix.groupby(level="trade_date")
    mean = grouped.transform("mean")
    std = grouped.transform("std")
    standardized = (matrix - mean) / std
    # §4.2: a cross-section with no dispersion (std 0, or a single observation
    # where the sample std is undefined) contributes 0 that date for every
    # observed name; unobserved names stay missing.
    no_dispersion = (std.isna() | (std == 0.0)) & matrix.notna()
    standardized = standardized.mask(no_dispersion, 0.0)

    finite_counts = grouped.count()
    per_date_std = grouped.std()
    degenerate = (finite_counts >= 1) & (per_date_std.isna() | (per_date_std == 0.0))
    degenerate_dates = {
        str(column): tuple(degenerate.index[degenerate[column]])
        for column in matrix.columns
    }
    return StandardizationOutcome(matrix=standardized, degenerate_dates_by_factor=degenerate_dates)


def _standardize_rank(matrix: pd.DataFrame) -> StandardizationOutcome:
    # RB-3 deterministic tie policy: method='first' assigns tied values by row
    # order, and build_score_matrix sorts the index by (trade_date, instrument),
    # so ties resolve by instrument id — reproducible across runs and input
    # orderings. NaN stays NaN and is excluded from the pct denominator.
    ranks = matrix.groupby(level="trade_date").rank(pct=True, method="first")
    standardized = 2.0 * ranks - 1.0
    # §4.2 no-dispersion symmetry with zscore: an all-tied cross-section has
    # no ordering information, and method='first' would otherwise fabricate a
    # deterministic instrument-ordered ladder the engine happily trades. Such
    # dates contribute 0.0 for every observed name (matching the zscore
    # convention) and are recorded as degenerate for the factor; if every
    # member degenerates, the combined cross-section is zero-variance and
    # RB-9 skips the date with DEGENERATE_CROSS_SECTION.
    grouped = matrix.groupby(level="trade_date")
    finite_counts = grouped.count()
    per_date_nunique = grouped.transform("nunique")
    no_dispersion = (per_date_nunique <= 1) & matrix.notna()
    standardized = standardized.mask(no_dispersion, 0.0)
    degenerate = (finite_counts >= 1) & (grouped.nunique() <= 1)
    degenerate_dates = {
        str(column): tuple(degenerate.index[degenerate[column]])
        for column in matrix.columns
    }
    return StandardizationOutcome(matrix=standardized, degenerate_dates_by_factor=degenerate_dates)


_STANDARDIZERS = {
    "zscore": _standardize_zscore,
    "rank": _standardize_rank,
}


def standardize_matrix(matrix: pd.DataFrame, *, standardization: str) -> StandardizationOutcome:
    """Cross-sectional per-date standardization (§4.2) — never pooled.

    ``zscore``: ``(s - mean_d) / std_d`` (sample std, ddof=1); a no-dispersion
    date contributes 0.0 for observed names and is recorded under
    ``degenerate_dates_by_factor`` for that factor. ``rank``: percentile rank
    with the deterministic ``method='first'`` tie policy over the
    instrument-sorted frame, mapped to ``[-1, 1]`` as ``2*r - 1``.
    """

    _require_score_matrix(matrix)
    standardizer = _STANDARDIZERS.get(standardization)
    if standardizer is None:
        raise ValueError(
            f"unknown standardization: {standardization}; expected one of {sorted(_STANDARDIZERS)}"
        )
    return standardizer(matrix.sort_index())


def apply_directions(matrix: pd.DataFrame, directions: Mapping[str, int]) -> pd.DataFrame:
    """Apply the declared ±1 directions AFTER standardization (§4.3).

    Every factor column requires an explicit direction (locked at request
    time); ``-1`` exactly negates the standardized score, ``+1`` is identity.
    Directions are never inferred, defaulted, or re-derived from data.
    """

    _require_score_matrix(matrix)
    declared = {str(key): value for key, value in directions.items()}
    columns = [str(column) for column in matrix.columns]
    missing = sorted(set(columns) - set(declared))
    if missing:
        raise ValueError(f"direction is required for every factor; missing: {missing}")
    unknown = sorted(set(declared) - set(columns))
    if unknown:
        raise ValueError(f"direction declared for unknown factors: {unknown}")
    multiplier = pd.Series(
        {column: float(_validate_direction(column, declared[column])) for column in columns},
        dtype="float64",
    )
    return matrix.mul(multiplier, axis=1)


def _resolve_apriori_weights(
    columns: Sequence[str],
    *,
    method: str,
    weights: Mapping[str, object] | None,
) -> dict[str, float]:
    if method == "equal_weight":
        if weights is not None:
            raise ValueError("equal_weight does not accept declared weights")
        # Design §4.4: raw weight 1 per member ("raw 1 before averaging"),
        # echoed as the a-priori claim; equal_weight is therefore exactly
        # `weighted` with a uniform raw vector of 1.0.
        return {column: 1.0 for column in columns}
    if weights is None:
        raise ValueError("weighted requires a weights mapping with one entry per factor")
    declared = {str(key): value for key, value in weights.items()}
    missing = sorted(set(columns) - set(declared))
    if missing:
        raise ValueError(f"weights are missing for factors: {missing}")
    unknown = sorted(set(declared) - set(columns))
    if unknown:
        raise ValueError(f"weights declared for unknown factors: {unknown}")
    resolved: dict[str, float] = {}
    for column in columns:
        value = declared[column]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"weight for factor {column} must be a number")
        weight = float(value)
        if not np.isfinite(weight):
            raise ValueError(f"weight for factor {column} must be finite")
        resolved[column] = weight
    if all(weight == 0.0 for weight in resolved.values()):
        raise ValueError("weights must not all be zero")
    return resolved


def _apply_coverage_rule(
    directed_matrix: pd.DataFrame,
    min_factor_coverage: int | None,
    *,
    row_weightable: pd.Series | None = None,
) -> tuple[pd.DataFrame, CoverageAccounting]:
    """Mask rows below the coverage requirement and account coverage (§4.5).

    Shared by the a-priori and fitted combiners so both branches carry
    identical coverage semantics: a row enters only with at least the
    required number of finite members (default: all), rows below the
    threshold are masked to NaN entirely (never summed into a fabricated
    value), and per-factor counts are observed — an unobservable ratio is a
    real ``None`` (FP-4), never 0. ``row_weightable``, when given (fitted
    branch), additionally excludes rows whose date has no defined weight
    vector (dates before the first grid rebalance under a non-zero start),
    so ``rows_in_composite`` never counts rows that could not enter the
    weighted sum; ``rows_scored`` stays a pure data-presence observation.
    """

    columns = [str(column) for column in directed_matrix.columns]
    total = len(columns)
    if min_factor_coverage is None:
        required = total
    else:
        if isinstance(min_factor_coverage, bool) or not isinstance(min_factor_coverage, int):
            raise ValueError("min_factor_coverage must be an integer")
        if not 1 <= min_factor_coverage <= total:
            raise ValueError(f"min_factor_coverage must be between 1 and {total}")
        required = min_factor_coverage
    coverage_rule = (
        COVERAGE_RULE_ALL_FACTORS if required == total else COVERAGE_RULE_MIN_FACTOR_COVERAGE
    )

    working = directed_matrix.sort_index()
    available = working.notna()
    row_counts = available.sum(axis=1)
    keep = row_counts >= required
    if row_weightable is not None:
        keep = keep & row_weightable
    masked = working.copy()
    if not bool(keep.all()):
        masked.loc[~keep, :] = np.nan

    in_composite = masked.notna()
    per_factor: list[FactorCoverage] = []
    for column in columns:
        rows_scored = int(available[column].sum())
        rows_in_composite = int(in_composite[column].sum())
        ratio = (rows_in_composite / rows_scored) if rows_scored > 0 else None
        per_factor.append(
            FactorCoverage(
                factor_id=column,
                rows_scored=rows_scored,
                rows_in_composite=rows_in_composite,
                coverage_ratio=ratio,
            )
        )
    coverage = CoverageAccounting(
        coverage_rule=coverage_rule,
        min_factor_coverage=required,
        rows_required=int(len(working.index)),
        rows_full_coverage=int((row_counts == total).sum()),
        per_factor=tuple(per_factor),
    )
    return masked, coverage


def _finalize_composite(
    composite: pd.Series,
) -> tuple[pd.DataFrame, tuple[pd.Timestamp, ...], tuple[str, ...]]:
    """RB-9 degenerate-date erasure + tidy output, shared by both combiners."""

    composite, degenerate_dates = _mask_degenerate_dates(composite)
    warning_codes = (DEGENERATE_CROSS_SECTION,) if degenerate_dates else ()
    tidy = (
        composite.rename("score")
        .reset_index()[list(_SCORE_COLUMNS)]
        .sort_values(["trade_date", "instrument"])
        .reset_index(drop=True)
    )
    return tidy, degenerate_dates, warning_codes


def _mask_degenerate_dates(
    composite: pd.Series,
) -> tuple[pd.Series, tuple[pd.Timestamp, ...]]:
    """RB-9: erase all-NaN / zero-variance composite dates, returning them.

    Both degenerate kinds converge on one explicit outcome: the whole date
    becomes NaN (the engine then drops it) and the date is reported so the
    caller counts it under ``DEGENERATE_CROSS_SECTION`` — never an arbitrary
    tie-noise long/short split, never a silent divergence between the two
    cases.
    """

    if composite.empty:
        return composite, ()
    grouped = composite.groupby(level="trade_date")
    finite_count = grouped.count()
    date_max = grouped.max()
    date_min = grouped.min()
    degenerate = (finite_count == 0) | ((finite_count >= 1) & (date_max == date_min))
    dates = tuple(degenerate.index[degenerate])
    if not dates:
        return composite, ()
    date_level = composite.index.get_level_values("trade_date")
    masked = composite.mask(pd.Index(date_level).isin(dates))
    return masked, dates


def combine_apriori(
    directed_matrix: pd.DataFrame,
    *,
    method: str,
    weights: Mapping[str, object] | None = None,
    min_factor_coverage: int | None = None,
) -> CompositeResult:
    """Combine standardized, direction-applied member scores (§4.4, a-priori).

    Implements the literal §4.4 sum ``composite = sum_f w_f * t_f`` with a
    missing factor at ``(date, instrument)`` excluded from that name's sum and
    tracked in coverage. The default coverage rule is ``all_factors``: a row
    enters only when every member is finite there; an explicit
    ``min_factor_coverage = k < N`` keeps rows with at least ``k`` finite
    members (the pinned universe still bounds membership — RB-6). Rows below
    the threshold are masked to NaN entirely, never summed into a fabricated
    value. After combining, RB-9 erases degenerate dates (all-NaN or
    zero-variance cross-sections) and flags ``DEGENERATE_CROSS_SECTION``.
    """

    _require_score_matrix(directed_matrix)
    if method not in APRIORI_METHODS:
        raise ValueError(f"unknown a-priori method: {method}; expected one of {APRIORI_METHODS}")
    columns = [str(column) for column in directed_matrix.columns]
    if len(columns) < 2:
        raise ValueError("a composite requires at least 2 member factors")
    weights_used = _resolve_apriori_weights(columns, method=method, weights=weights)

    masked, coverage = _apply_coverage_rule(directed_matrix, min_factor_coverage)
    weight_series = pd.Series(weights_used, dtype="float64")
    # skipna sum with min_count=1: available members contribute w_f * t_f,
    # missing members are excluded from the sum, and a fully-masked row stays
    # NaN instead of collapsing to a fabricated 0 (FP-4).
    composite = masked.mul(weight_series, axis=1).sum(axis=1, min_count=1)
    tidy, degenerate_dates, warning_codes = _finalize_composite(composite)
    return CompositeResult(
        composite=tidy,
        method=method,
        weights_effective=dict(weights_used),
        coverage=coverage,
        degenerate_dates=degenerate_dates,
        warning_codes=warning_codes,
    )


def build_apriori_composite(
    member_scores: Mapping[str, pd.DataFrame],
    *,
    directions: Mapping[str, int],
    standardization: str,
    method: str,
    weights: Mapping[str, object] | None = None,
    min_factor_coverage: int | None = None,
) -> CompositeResult:
    """Full pure a-priori pipeline: matrix -> standardize -> direction -> combine.

    The blessed P3 entry point later phases call once per run. Standardization
    degenerate marks (§4.2) are merged into the result next to the composite
    level RB-9 dates so provenance can report both.
    """

    matrix = build_score_matrix(member_scores)
    standardized = standardize_matrix(matrix, standardization=standardization)
    directed = apply_directions(standardized.matrix, directions)
    result = combine_apriori(
        directed,
        method=method,
        weights=weights,
        min_factor_coverage=min_factor_coverage,
    )
    return replace(
        result,
        standardization=standardization,
        degenerate_dates_by_factor=standardized.degenerate_dates_by_factor,
    )


# ---------------------------------------------------------------------------
# P6 — fitted combination (§4.4 point-in-time IC/ICIR on the shared grid)
# ---------------------------------------------------------------------------


_CLOSE_COLUMNS: tuple[str, ...] = ("trade_date", "instrument", "close")

# §4.4 ICIR std==0 guard, hardened for floating point: ``np.std`` over N
# bit-identical IC values returns summation noise (~1e-16), not exactly 0,
# and dividing the mean by that noise fabricates a near-infinite "perfectly
# stable" weight out of rounding error. A std at or below this RELATIVE
# scale of the mean magnitude is numerically indistinguishable from a
# constant series (real IC variation sits many orders of magnitude above
# machine epsilon), so it takes the std==0 branch: raw weight 0.
_ICIR_RELATIVE_STD_FLOOR = 1e-12


@dataclass(frozen=True)
class FittedWeightEntry:
    """One rebalance slot on the shared grid with its resolved weight vector.

    ``flag`` is ``None`` for a genuinely fitted vector, ``WARM_UP_IC_UNFITTED``
    when fewer than ``ic_min_periods`` prior periods had closed (the date
    trades the equal-weight fallback), or ``IC_DEGENERATE_EQUAL_WEIGHT`` when
    the clipped raw vector summed to zero and fell back to equal weight.
    ``eligible_period_count`` is the number of grid periods whose forward
    window had fully closed on or before this signal date — the §4.4 embargo
    set size, exposed so tests and provenance can audit the boundary.
    """

    signal_index: int
    signal_date: pd.Timestamp
    weights: dict[str, float]
    eligible_period_count: int
    flag: str | None


@dataclass(frozen=True)
class FittedCompositeResult:
    """Fitted composite output: combined frame + honest fit diagnostics.

    ``composite`` carries the ALREADY-COMBINED values (weights folded in
    before materialization — §4.4 materialization consequence), in the same
    tidy shape as :class:`CompositeResult`. There is deliberately NO
    ``weights_effective`` field: that provenance key is an a-priori
    raw-declared claim (FP-1) and fitted runs must omit it; the honest story
    lives in ``weights_path`` (per-signal-date diagnostic),
    ``fitted_weights_latest`` (last GENUINELY fitted vector, or ``None``),
    ``fitted_period_fraction`` and ``warmup_period_count``.

    ``is_fitted`` is the REALIZED truth: ``True`` only when at least one
    rebalance produced a genuinely fitted vector. When zero did (all warmup
    and/or all degenerate fallbacks) the run is downgraded per §3 RB-8: it
    traded equal weight throughout, ``is_fitted`` is ``False`` and
    ``warning_codes`` carries ``NO_FITTED_PERIODS``. ``method`` keeps the
    REQUESTED fitted method name — the §8 payload branch (fitted fields
    present, ``weights_effective`` absent) keys off the request while
    ``is_fitted`` reports what actually happened.
    """

    composite: pd.DataFrame
    method: str
    is_fitted: bool
    coverage: CoverageAccounting
    degenerate_dates: tuple[pd.Timestamp, ...]
    warning_codes: tuple[str, ...]
    weights_path: tuple[FittedWeightEntry, ...]
    fitted_weights_latest: dict[str, float] | None
    fitted_period_count: int
    warmup_period_count: int
    degenerate_weight_period_count: int
    fitted_period_fraction: float | None
    ic_min_periods: int
    standardization: str | None = None
    degenerate_dates_by_factor: Mapping[str, tuple[pd.Timestamp, ...]] = field(
        default_factory=dict
    )


def _validate_grid_params(delay: object, holding: object, start_signal_index: object) -> None:
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 1:
        raise ValueError("delay must be an integer >= 1")
    if isinstance(holding, bool) or not isinstance(holding, int) or holding < 1:
        raise ValueError("holding must be an integer >= 1")
    if (
        isinstance(start_signal_index, bool)
        or not isinstance(start_signal_index, int)
        or start_signal_index < 0
    ):
        raise ValueError("start_signal_index must be an integer >= 0")


def _validated_calendar(dates: Sequence[object]) -> list[pd.Timestamp]:
    calendar = [pd.Timestamp(value) for value in dates]
    if any(later <= earlier for earlier, later in zip(calendar, calendar[1:])):
        raise ValueError("dates must be strictly increasing trade dates")
    return calendar


def _require_matrix_dates_on_calendar(
    matrix: pd.DataFrame, calendar: Sequence[pd.Timestamp]
) -> None:
    known = set(calendar)
    outside = sorted(
        {date for date in matrix.index.get_level_values("trade_date") if date not in known}
    )
    if outside:
        raise ValueError(
            "score matrix trade dates are not on the provided calendar: "
            f"{[date.date().isoformat() for date in outside[:5]]}"
        )


def _validated_close_frame(close: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in _CLOSE_COLUMNS if column not in close.columns]
    if missing:
        raise ValueError(f"close frame is missing columns: {missing}")
    frame = close[list(_CLOSE_COLUMNS)].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["instrument"] = frame["instrument"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame


def _spearman_rank_ic(scores: pd.Series, forward_returns: pd.Series) -> float:
    """Cross-sectional Spearman rank IC over the common finite cross-section.

    Implemented as the Pearson correlation of average-method ranks — the
    EXACT primitive the shipped evaluation layer uses for its daily rank IC
    (``evaluation/service.py``: ``group["score"].rank().corr(group[
    "forward_return"].rank())``) — so the fitted weights and the reported
    evaluation diagnostics measure the same quantity, and no dependency
    beyond pandas/numpy is introduced. Returns NaN when undefined (fewer
    than 2 common names, or a zero-variance side) — callers exclude
    non-finite ICs per the §4.4 pseudocode's explicit finite check. Average
    ranks over ties are order-independent, keeping the estimate
    deterministic under permuted inputs (RB-3 posture).
    """

    frame = pd.concat({"score": scores, "fwd": forward_returns}, axis=1, join="inner")
    if frame.empty:
        return float("nan")
    values = frame.to_numpy(dtype="float64")
    frame = frame.loc[np.isfinite(values).all(axis=1)]
    if len(frame) < 2:
        return float("nan")
    score_ranks = frame["score"].rank()
    return_ranks = frame["fwd"].rank()
    if score_ranks.nunique() < 2 or return_ranks.nunique() < 2:
        return float("nan")
    return float(score_ranks.corr(return_ranks))


def _period_rank_ic_by_signal_index(
    directed_matrix: pd.DataFrame,
    *,
    close: pd.DataFrame,
    calendar: Sequence[pd.Timestamp],
    grid: Sequence[int],
    delay: int,
    holding: int,
) -> dict[int, dict[str, float]]:
    """Per-member realized rank IC for every grid period that closed in-window.

    The forward return of period ``s`` is the engine's own
    ``_with_period_return`` over ``[dates[s+delay], dates[s+delay+holding]]``
    (RB-5: same close-to-close primitive, same last-mark/survivorship fill),
    so the IC that drives the weights matches the return the engine realizes.
    Periods whose scheduled exit falls beyond the window are omitted — they
    can never satisfy the §4.4 embargo at any in-window rebalance.
    """

    columns = [str(column) for column in directed_matrix.columns]
    total = len(calendar)
    by_period: dict[int, dict[str, float]] = {}
    for signal_index in grid:
        exit_index = signal_index + delay + holding
        if exit_index >= total:
            continue
        forward = _with_period_return(close, calendar[signal_index + delay], calendar[exit_index])
        returns = forward.set_index("instrument")["period_return"].astype("float64")
        returns.index = returns.index.astype(str)
        returns = returns[np.isfinite(returns.to_numpy())]
        signal_date = calendar[signal_index]
        try:
            cross_section = directed_matrix.xs(signal_date, level="trade_date")
        except KeyError:
            cross_section = None
        ics: dict[str, float] = {}
        for column in columns:
            if cross_section is None:
                ics[column] = float("nan")
            else:
                ics[column] = _spearman_rank_ic(cross_section[column], returns)
        by_period[signal_index] = ics
    return by_period


def _normalize_ic_weights(
    raw: Mapping[str, float], factors: Sequence[str]
) -> tuple[dict[str, float], str | None]:
    """Clip + normalize a raw IC weight vector (§4.4 pseudocode, verbatim).

    Negative AND non-finite raw weights clip to 0 through an explicit
    ``isfinite`` check — never a bare ``max``, whose result is
    NaN-order-dependent (``max(nan, 0)`` is NaN while ``max(0, nan)`` is 0).
    An all-zero clipped vector falls back to per-date equal weight with the
    ``IC_DEGENERATE_EQUAL_WEIGHT`` flag rather than collapsing the whole
    cross-section to NaN and silently emptying the rebalance.
    """

    clipped = {
        str(factor): (
            float(raw[factor])
            if np.isfinite(raw[factor]) and float(raw[factor]) > 0.0
            else 0.0
        )
        for factor in factors
    }
    total = sum(clipped.values())
    if total <= 0.0:
        equal = 1.0 / len(factors)
        return {str(factor): equal for factor in factors}, IC_DEGENERATE_EQUAL_WEIGHT
    return {factor: value / total for factor, value in clipped.items()}, None


def fitted_weights_by_rebalance(
    directed_matrix: pd.DataFrame,
    *,
    close: pd.DataFrame,
    dates: Sequence[object],
    delay: int,
    holding: int,
    method: str,
    ic_min_periods: int = DEFAULT_IC_MIN_PERIODS,
    start_signal_index: int = 0,
) -> tuple[FittedWeightEntry, ...]:
    """§4.4 point-in-time weight vectors for EVERY shared-grid rebalance.

    The grid is ``rebalance_indices`` — the single source of truth the engine
    trades (RB-5), never a re-derived schedule. At rebalance index ``d`` the
    eligible periods are exactly the grid signals ``s`` with
    ``s + delay + holding <= d`` and ``s >= start_signal_index`` (forward
    window fully closed on/before ``d``, expanding from the window start):
    the anti-peek embargo. Per eligible period the cross-sectional Spearman
    rank IC of each member's standardized, direction-applied score against
    the ``_with_period_return`` realized forward return feeds the §4.4
    weight rule; the finite-check clip, the ICIR ``std == 0`` guard
    (population std, matching the pseudocode's ``np.std``), the all-zero
    equal-weight fallback and the warm-up fallback are implemented verbatim.
    """

    _require_score_matrix(directed_matrix)
    _validate_grid_params(delay, holding, start_signal_index)
    if method not in FITTED_METHODS:
        raise ValueError(f"unknown fitted method: {method}; expected one of {FITTED_METHODS}")
    if isinstance(ic_min_periods, bool) or not isinstance(ic_min_periods, int) or ic_min_periods < 1:
        raise ValueError("ic_min_periods must be an integer >= 1")
    factors = [str(column) for column in directed_matrix.columns]
    if len(factors) < 2:
        raise ValueError("a composite requires at least 2 member factors")
    calendar = _validated_calendar(dates)
    working = directed_matrix.sort_index()
    _require_matrix_dates_on_calendar(working, calendar)
    close_frame = _validated_close_frame(close)

    grid = rebalance_indices(
        calendar, delay=delay, holding=holding, start_signal_index=start_signal_index
    )
    ic_by_period = _period_rank_ic_by_signal_index(
        working, close=close_frame, calendar=calendar, grid=grid, delay=delay, holding=holding
    )
    equal = {factor: 1.0 / len(factors) for factor in factors}

    entries: list[FittedWeightEntry] = []
    for d_index in grid:
        # The §4.4 embargo, measured on the SAME grid the engine trades: a
        # period closing exactly AT d is eligible (<=); one closing at d+1
        # is not. Non-closed tail periods never appear in ic_by_period.
        # The `s >= start_signal_index` clause is structurally redundant
        # (`grid` already starts there) and is kept only to mirror the §4.4
        # eligibility expression verbatim.
        eligible = [
            s
            for s in grid
            if s + delay + holding <= d_index and s >= start_signal_index and s in ic_by_period
        ]
        if len(eligible) < ic_min_periods:
            entries.append(
                FittedWeightEntry(
                    signal_index=d_index,
                    signal_date=calendar[d_index],
                    weights=dict(equal),
                    eligible_period_count=len(eligible),
                    flag=WARM_UP_IC_UNFITTED,
                )
            )
            continue
        raw: dict[str, float] = {}
        for factor in factors:
            series = np.asarray(
                [
                    ic_by_period[s][factor]
                    for s in eligible
                    if np.isfinite(ic_by_period[s][factor])
                ],
                dtype=float,
            )
            if series.size == 0:
                raw[factor] = 0.0
            elif method == "ic_weighted":
                raw[factor] = float(np.mean(series))
            else:  # icir_weighted
                mean = float(np.mean(series))
                deviation = float(np.std(series))
                # Explicit std == 0 guard: a constant IC series carries no
                # stability signal, so the raw weight is 0 (clipped below),
                # never an inf/NaN division artifact. The comparison is
                # noise-hardened (see _ICIR_RELATIVE_STD_FLOOR): a series
                # constant to floating-point precision is the std==0 case,
                # not a license to divide by summation rounding error.
                if deviation <= abs(mean) * _ICIR_RELATIVE_STD_FLOOR:
                    raw[factor] = 0.0
                else:
                    raw[factor] = mean / deviation
        weights, flag = _normalize_ic_weights(raw, factors)
        entries.append(
            FittedWeightEntry(
                signal_index=d_index,
                signal_date=calendar[d_index],
                weights=weights,
                eligible_period_count=len(eligible),
                flag=flag,
            )
        )
    return tuple(entries)


def combine_fitted(
    directed_matrix: pd.DataFrame,
    *,
    method: str,
    close: pd.DataFrame,
    dates: Sequence[object],
    delay: int,
    holding: int,
    ic_min_periods: int = DEFAULT_IC_MIN_PERIODS,
    min_factor_coverage: int | None = None,
    start_signal_index: int = 0,
) -> FittedCompositeResult:
    """Combine with §4.4 point-in-time fitted weights, folded in pre-materialization.

    Weight vectors come from :func:`fitted_weights_by_rebalance` (one per
    shared-grid rebalance). Because the engine reads the freshest composite
    as-of each signal date, the stored frame folds the weights in per
    calendar date as a step function: every date adopts the vector of the
    most recent grid rebalance at or before it — weights estimated from
    strictly older closed periods remain point-in-time valid for later
    dates, so no date ever carries information from a period that had not
    closed by its own weight-fit date. At a grid signal date this is exactly
    ``sum_f w_{f,d} * t_{f,d,i}``, the value the engine trades. Dates before
    the first grid rebalance (possible only under a non-zero
    ``start_signal_index``) have no defined vector and stay out of the
    composite. Coverage and RB-9 degenerate-date handling are the SAME
    helpers the a-priori combiner uses.

    Downgrade semantics (§3 RB-8): when ZERO rebalances produced a genuinely
    fitted vector the run traded per-date equal weight throughout —
    ``is_fitted`` is ``False`` and ``NO_FITTED_PERIODS`` is flagged; the
    result never advertises fitting on an all-fallback run.
    """

    entries = fitted_weights_by_rebalance(
        directed_matrix,
        close=close,
        dates=dates,
        delay=delay,
        holding=holding,
        method=method,
        ic_min_periods=ic_min_periods,
        start_signal_index=start_signal_index,
    )
    calendar = _validated_calendar(dates)
    working = directed_matrix.sort_index()
    columns = [str(column) for column in working.columns]

    # Step-function fold-in: map every matrix date to the latest grid entry
    # at or before it (None before the first grid rebalance).
    date_positions = {date: position for position, date in enumerate(calendar)}
    row_dates = working.index.get_level_values("trade_date")
    unique_dates = row_dates.unique()
    entry_positions = [entry.signal_index for entry in entries]
    weights_for_date: dict[pd.Timestamp, dict[str, float] | None] = {}
    for date in unique_dates:
        slot = bisect_right(entry_positions, date_positions[date]) - 1
        weights_for_date[date] = entries[slot].weights if slot >= 0 else None

    per_date_weights = (
        pd.DataFrame.from_dict(
            {date: (weights_for_date[date] or {}) for date in unique_dates}, orient="index"
        )
        .reindex(columns=columns)
        .astype("float64")
    )
    weightable_by_date = pd.Series(
        {date: weights_for_date[date] is not None for date in unique_dates}
    )
    row_weightable = pd.Series(
        weightable_by_date.reindex(row_dates).to_numpy(dtype=bool), index=working.index
    )

    masked, coverage = _apply_coverage_rule(
        working, min_factor_coverage, row_weightable=row_weightable
    )
    expanded = per_date_weights.reindex(row_dates)
    expanded.index = masked.index
    # Same §4.4 sum as the a-priori combiner, with per-date weights: missing
    # members are excluded per name, fully-masked rows stay NaN (FP-4).
    composite = masked.mul(expanded).sum(axis=1, min_count=1)
    tidy, degenerate_dates, degenerate_codes = _finalize_composite(composite)

    warmup_count = sum(1 for entry in entries if entry.flag == WARM_UP_IC_UNFITTED)
    degenerate_weight_count = sum(
        1 for entry in entries if entry.flag == IC_DEGENERATE_EQUAL_WEIGHT
    )
    fitted_count = sum(1 for entry in entries if entry.flag is None)
    is_fitted = fitted_count >= 1
    fitted_weights_latest = next(
        (dict(entry.weights) for entry in reversed(entries) if entry.flag is None), None
    )
    fitted_period_fraction = (fitted_count / len(entries)) if entries else None

    warning_codes: list[str] = []
    if warmup_count:
        warning_codes.append(WARM_UP_IC_UNFITTED)
    if degenerate_weight_count:
        warning_codes.append(IC_DEGENERATE_EQUAL_WEIGHT)
    if not is_fitted:
        warning_codes.append(NO_FITTED_PERIODS)
    warning_codes.extend(degenerate_codes)

    return FittedCompositeResult(
        composite=tidy,
        method=method,
        is_fitted=is_fitted,
        coverage=coverage,
        degenerate_dates=degenerate_dates,
        warning_codes=tuple(warning_codes),
        weights_path=entries,
        fitted_weights_latest=fitted_weights_latest,
        fitted_period_count=fitted_count,
        warmup_period_count=warmup_count,
        degenerate_weight_period_count=degenerate_weight_count,
        fitted_period_fraction=fitted_period_fraction,
        ic_min_periods=ic_min_periods,
    )


def build_fitted_composite(
    member_scores: Mapping[str, pd.DataFrame],
    *,
    directions: Mapping[str, int],
    standardization: str,
    method: str,
    close: pd.DataFrame,
    dates: Sequence[object],
    delay: int,
    holding: int,
    ic_min_periods: int = DEFAULT_IC_MIN_PERIODS,
    min_factor_coverage: int | None = None,
    start_signal_index: int = 0,
) -> FittedCompositeResult:
    """Full pure fitted pipeline: matrix -> standardize -> direction -> PIT combine.

    The fitted analog of :func:`build_apriori_composite` and the blessed P6
    entry point. ``close`` must be the engine's working-panel close frame
    (``[trade_date, instrument, close]`` over the SAME in-window ``dates``)
    so the forward returns driving the IC fit are the returns the engine
    realizes (RB-5).
    """

    matrix = build_score_matrix(member_scores)
    standardized = standardize_matrix(matrix, standardization=standardization)
    directed = apply_directions(standardized.matrix, directions)
    result = combine_fitted(
        directed,
        method=method,
        close=close,
        dates=dates,
        delay=delay,
        holding=holding,
        ic_min_periods=ic_min_periods,
        min_factor_coverage=min_factor_coverage,
        start_signal_index=start_signal_index,
    )
    return replace(
        result,
        standardization=standardization,
        degenerate_dates_by_factor=standardized.degenerate_dates_by_factor,
    )


def member_rank_ic_redundancy(
    member_scores: Mapping[str, pd.DataFrame],
    *,
    directions: Mapping[str, int],
    standardization: str,
    close: pd.DataFrame,
    dates: Sequence[object],
    delay: int,
    holding: int,
    start_signal_index: int = 0,
) -> dict[str, object]:
    """Advisory rank-IC redundancy matrix across members (§4.5 crowding check).

    Pairwise Pearson correlation of the members' REALIZED per-period
    cross-sectional rank-IC series over every shared-grid period whose
    forward window closed inside the data window. Members whose IC series
    move together earn from the same states of the world — the crowding
    diagnostic QuantGPT computes across candidates. Advisory only: the
    matrix never gates a run and never feeds the fitted weights, and it is
    computed once over the whole realized window (a run-level diagnostic,
    not a point-in-time estimate). Unobservable cells — fewer than 2 common
    finite periods, or a zero-variance series — are real ``None`` (FP-4),
    never a fabricated number.
    """

    _validate_grid_params(delay, holding, start_signal_index)
    matrix = build_score_matrix(member_scores)
    standardized = standardize_matrix(matrix, standardization=standardization)
    directed = apply_directions(standardized.matrix, directions).sort_index()
    calendar = _validated_calendar(dates)
    _require_matrix_dates_on_calendar(directed, calendar)
    grid = rebalance_indices(
        calendar, delay=delay, holding=holding, start_signal_index=start_signal_index
    )
    ic_by_period = _period_rank_ic_by_signal_index(
        directed,
        close=_validated_close_frame(close),
        calendar=calendar,
        grid=grid,
        delay=delay,
        holding=holding,
    )
    factors = [str(column) for column in directed.columns]
    closed_periods = sorted(ic_by_period)
    series = {
        factor: np.asarray([ic_by_period[s][factor] for s in closed_periods], dtype=float)
        for factor in factors
    }
    matrix_rows: list[list[float | None]] = []
    for row_factor in factors:
        row: list[float | None] = []
        for column_factor in factors:
            both_finite = np.isfinite(series[row_factor]) & np.isfinite(series[column_factor])
            if int(both_finite.sum()) < 2:
                row.append(None)
                continue
            pair = np.corrcoef(
                series[row_factor][both_finite], series[column_factor][both_finite]
            )[0, 1]
            row.append(float(pair) if np.isfinite(pair) else None)
        matrix_rows.append(row)
    return {
        "advisory": True,
        "basis": "pearson_correlation_of_realized_period_rank_ic_series",
        "period_count": len(closed_periods),
        "factors": factors,
        "matrix": matrix_rows,
    }


def prescan_rebalance_coverage(
    composite: pd.DataFrame,
    dates: Sequence[object],
    *,
    delay: int,
    holding: int,
    start_signal_index: int = 0,
    group_count: int = 5,
    include_partial_final_period: bool = False,
) -> RebalancePrescan:
    """Classify every shared-grid rebalance date against composite coverage (RB-7).

    Walks exactly the ``rebalance_indices`` grid the engine trades (RB-5) and
    replicates the engine's period-completeness breaks: under the default D3
    behavior the trailing signal whose scheduled exit falls beyond the window
    is excluded (``final_partial_excluded``) rather than classified. A signal
    date with zero finite composite values is ``empty``
    (``REBALANCE_SKIPPED_NO_COVERAGE``); a non-empty cross-section thinner
    than ``max(4, group_count)`` — the engine's own threshold — is ``thin``
    (``REBALANCE_SKIPPED_THIN``). The scan reads the score side only, so the
    engine may skip additional dates on period-return coverage grounds.
    """

    missing_columns = [column for column in _SCORE_COLUMNS if column not in composite.columns]
    if missing_columns:
        raise ValueError(f"composite frame is missing columns: {missing_columns}")
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 1:
        raise ValueError("delay must be an integer >= 1")
    if isinstance(holding, bool) or not isinstance(holding, int) or holding < 1:
        raise ValueError("holding must be an integer >= 1")
    if (
        isinstance(start_signal_index, bool)
        or not isinstance(start_signal_index, int)
        or start_signal_index < 0
    ):
        raise ValueError("start_signal_index must be an integer >= 0")
    if isinstance(group_count, bool) or not isinstance(group_count, int) or group_count < 2:
        raise ValueError("group_count must be an integer >= 2")

    calendar = [pd.Timestamp(value) for value in dates]
    if any(later <= earlier for earlier, later in zip(calendar, calendar[1:])):
        raise ValueError("dates must be strictly increasing trade dates")

    frame = composite[list(_SCORE_COLUMNS)].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    known_dates = set(calendar)
    outside = sorted({date for date in frame["trade_date"] if date not in known_dates})
    if outside:
        raise ValueError(
            "composite trade dates are not on the provided calendar: "
            f"{[date.date().isoformat() for date in outside[:5]]}"
        )
    # Engine parity: the loop drops NaN scores per signal date
    # (`dropna(subset=['score'])`), so absence == NaN here.
    finite = frame.dropna(subset=["score"])
    counts_by_date = finite.groupby("trade_date")["score"].size().to_dict()

    thin_threshold = max(_MIN_THIN_CROSS_SECTION, group_count)
    entries: list[RebalanceScanEntry] = []
    skipped_no_coverage: list[pd.Timestamp] = []
    skipped_thin: list[pd.Timestamp] = []
    final_partial_excluded = False
    total_dates = len(calendar)
    for signal_index in rebalance_indices(
        calendar, delay=delay, holding=holding, start_signal_index=start_signal_index
    ):
        scheduled_exit_index = signal_index + delay + holding
        if scheduled_exit_index >= total_dates:
            if not include_partial_final_period:
                final_partial_excluded = True
                break
            actual_exit_index = total_dates - 1
            if actual_exit_index <= signal_index + delay:
                break
        signal_date = calendar[signal_index]
        finite_count = int(counts_by_date.get(signal_date, 0))
        if finite_count == 0:
            status, skip_code = SCAN_STATUS_EMPTY, REBALANCE_SKIPPED_NO_COVERAGE
            skipped_no_coverage.append(signal_date)
        elif finite_count < thin_threshold:
            status, skip_code = SCAN_STATUS_THIN, REBALANCE_SKIPPED_THIN
            skipped_thin.append(signal_date)
        else:
            status, skip_code = SCAN_STATUS_OK, None
        entries.append(
            RebalanceScanEntry(
                signal_index=signal_index,
                signal_date=signal_date,
                status=status,
                finite_count=finite_count,
                skip_code=skip_code,
            )
        )

    warning_codes: list[str] = []
    if skipped_no_coverage:
        warning_codes.append(REBALANCE_SKIPPED_NO_COVERAGE)
    if skipped_thin:
        warning_codes.append(REBALANCE_SKIPPED_THIN)
    ok_count = sum(1 for entry in entries if entry.status == SCAN_STATUS_OK)
    return RebalancePrescan(
        entries=tuple(entries),
        ok_count=ok_count,
        skipped_no_coverage_count=len(skipped_no_coverage),
        skipped_thin_count=len(skipped_thin),
        skipped_no_coverage_dates=tuple(skipped_no_coverage),
        skipped_thin_dates=tuple(skipped_thin),
        warning_codes=tuple(warning_codes),
        final_partial_excluded=final_partial_excluded,
        thin_threshold=thin_threshold,
    )


def count_non_overlapping_periods(date_count: int, *, delay: int, holding: int) -> int:
    """RB-2 realized non-overlapping period count.

    ``N = floor((len(in_window_dates) - delay - 1) / holding) + 1`` — the
    design §3 expression, computed verbatim. This is a precondition gate over
    the window, not the engine's realized ledger count (which additionally
    drops the excluded final partial period).
    """

    if isinstance(date_count, bool) or not isinstance(date_count, int) or date_count < 0:
        raise ValueError("date_count must be an integer >= 0")
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 1:
        raise ValueError("delay must be an integer >= 1")
    if isinstance(holding, bool) or not isinstance(holding, int) or holding < 1:
        raise ValueError("holding must be an integer >= 1")
    return (date_count - delay - 1) // holding + 1


def require_backtest_window(date_count: int, *, delay: int, holding: int) -> int:
    """RB-2 window precondition: reject windows with fewer than 2 periods.

    Raises the typed :class:`WindowTooShortError` (code ``WINDOW_TOO_SHORT``)
    the workflow maps to a client error; returns the realized ``N`` for
    provenance (`NON_OVERLAPPING_COHORTS`) on success.
    """

    period_count = count_non_overlapping_periods(date_count, delay=delay, holding=holding)
    if period_count < 2:
        raise WindowTooShortError(
            "backtest window is too short: realized non-overlapping period count "
            f"N={period_count} from {date_count} in-window trade dates with "
            f"delay={delay}, holding={holding}; at least 2 periods are required "
            f"({WINDOW_TOO_SHORT})"
        )
    return period_count


def _normalized_universe(filters: Sequence[str] | None) -> frozenset[str]:
    if filters is None:
        return frozenset()
    cleaned = {str(item).strip() for item in filters}
    return frozenset(item for item in cleaned if item)


def resolve_pinned_universe(
    members: Sequence[FactorDefinition],
    *,
    requested: Sequence[str] | None = None,
    default: Sequence[str] = (),
) -> tuple[str, ...]:
    """Resolve the ONE pinned ``universe_filters`` set for a composite (RB-6).

    Resolution order: an explicit ``requested`` set wins; otherwise the single
    universe the members unanimously declare; otherwise ``default``. A member
    with an empty declaration adopts the pin. Any two distinct non-empty
    declarations — or a non-empty declaration differing from the explicit
    ``requested`` set — are a conflict and raise the typed
    :class:`UniverseMismatchError` (code ``UNIVERSE_MISMATCH``); universes are
    never silently unioned or intersected. Comparison is order- and
    duplicate-insensitive; the returned pin is a canonical sorted tuple so
    every member fetch, the materialized composite, and the run digest all see
    one identical value.
    """

    if not members:
        raise ValueError("at least one member factor is required")
    declared = {
        str(member.factor_id): _normalized_universe(member.universe_filters)
        for member in members
    }
    if len(declared) != len(members):
        raise ValueError("member factor_ids must be unique")
    non_empty = {factor_id: filters for factor_id, filters in declared.items() if filters}

    if requested is not None:
        pinned = _normalized_universe(requested)
    elif non_empty:
        distinct = sorted({tuple(sorted(filters)) for filters in non_empty.values()})
        if len(distinct) > 1:
            conflicts = ", ".join(
                f"{factor_id}={sorted(filters)}" for factor_id, filters in sorted(non_empty.items())
            )
            raise UniverseMismatchError(
                f"member factors declare conflicting universe filters ({UNIVERSE_MISMATCH}): "
                f"{conflicts}"
            )
        pinned = frozenset(distinct[0])
    else:
        pinned = _normalized_universe(default)

    mismatched = sorted(
        factor_id for factor_id, filters in non_empty.items() if filters != pinned
    )
    if mismatched:
        detail = ", ".join(f"{factor_id}={sorted(non_empty[factor_id])}" for factor_id in mismatched)
        raise UniverseMismatchError(
            f"member universe filters conflict with the pinned set {sorted(pinned)} "
            f"({UNIVERSE_MISMATCH}): {detail}"
        )
    return tuple(sorted(pinned))


def build_member_fetch_plan(
    members: Sequence[FactorDefinition],
    *,
    directions: Mapping[str, int],
    universe_filters: Sequence[str],
) -> tuple[MemberFetchSpec, ...]:
    """Build the per-member score-fetch plan with formulas pinned at run time.

    CP0 amendment: each member's ``formula`` string is captured into the plan
    here, so the provenance the workflow later emits reflects exactly what was
    fetched even if the registry definition changes afterwards. Every member
    carries the same pinned ``universe_filters`` set (RB-6) and its declared
    ±1 direction (§4.3); the plan requires at least 2 unique members.
    """

    if len(members) < 2:
        raise ValueError("a composite requires at least 2 member factors")
    member_ids = [str(member.factor_id) for member in members]
    if len(set(member_ids)) != len(member_ids):
        raise ValueError("member factor_ids must be unique")
    declared = {str(key): value for key, value in directions.items()}
    missing = sorted(set(member_ids) - set(declared))
    if missing:
        raise ValueError(f"direction is required for every member factor; missing: {missing}")
    unknown = sorted(set(declared) - set(member_ids))
    if unknown:
        raise ValueError(f"direction declared for unknown factors: {unknown}")
    pinned_universe = tuple(sorted(_normalized_universe(universe_filters)))
    plan: list[MemberFetchSpec] = []
    for member in members:
        factor_id = str(member.factor_id)
        plan.append(
            MemberFetchSpec(
                factor_id=factor_id,
                factor_name=str(member.name),
                formula=str(member.formula),
                direction=_validate_direction(factor_id, declared[factor_id]),
                source=str(member.source),
                universe_filters=pinned_universe,
            )
        )
    return tuple(plan)


# ---------------------------------------------------------------------------
# P4 — composite id (RB-10), materialization (RF-2/RF-3), engine drive (LA-1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializedComposite:
    """Artifacts produced by :func:`materialize_composite`.

    ``values_dir`` is the store-resolved write directory (RF-3), never a
    hand-built path; ``definition_path`` is the ``factor.yaml`` the
    repository wrote into ``factor_root`` (RF-2). ``formula_signature`` is
    the exact signature stamped on the written rows — the value the read
    path must recompute identically for the engine to see any rows.
    """

    composite_id: str
    formula: str
    formula_signature: str
    definition: FactorDefinition
    definition_path: Path
    values_dir: Path
    overlay_root: Path
    rows_written: int


@dataclass(frozen=True)
class CompositeBacktestRun:
    """Result of :func:`run_composite_backtest` (P4 engine drive).

    ``engine_profile`` is the profile the engine actually ran with —
    ``decay_days`` pinned to 0 (LA-1) regardless of the shared profile's
    decay, because members were already decayed before combination.
    ``expected_signal_dates`` is the shared-grid expectation the realized
    schedule was verified against (RB-5).
    """

    composite_id: str
    result: BacktestResult
    materialized: MaterializedComposite
    engine_profile: SimulationProfile
    overlay_root: Path
    expected_signal_dates: tuple[str, ...]


def derive_composite_id(
    *,
    factor_refs: Sequence[tuple[str, int]],
    method: str,
    method_params: Mapping[str, object] | None,
    standardization: str,
    backtest_start: str | None,
    backtest_end: str | None,
    decay_days: int,
    execution_delay_days: int,
    top_quantile: float,
    coverage_rule: str,
    min_factor_coverage: int | None,
    universe_filters: Sequence[str],
    holding_days: int,
) -> str:
    """Derive the ``COMPOSITE_<hash>`` id from ALL run inputs (RB-10, RF-1).

    The digest covers every §11 input: the ORDERED ``(factor_id, direction)``
    list (member order is identity-bearing), method, method_params,
    standardization, the backtest window, decay, delay, top_quantile,
    coverage rule, min_factor_coverage, the pinned universe, and
    ``holding_days`` — the last is a CP0-review addition to the §11 list:
    fitted composite VALUES depend on holding via the rebalance grid, and the
    persisted definition's ``horizon_days`` mirrors it, so two runs differing
    only in holding must never share one registered identity. Any single
    change mints a fresh id, which — together with the per-run overlay — is
    what prevents ``_merge_score_updates`` from blending stale prior-run
    rows into a new run's read. The canonical JSON uses sorted keys, so
    mapping insertion order never changes the id; the member LIST order
    does, by design. The hex segment is UPPERCASE so the id is a fixed
    point of ``FactorCatalog`` canonicalization (write-time formula ==
    read-time formula == matching value-store signature) and colon-free by
    construction (RF-1).
    """

    if len(factor_refs) < 2:
        raise ValueError("a composite requires at least 2 (factor_id, direction) refs")
    ordered_refs: list[list[object]] = []
    for ref in factor_refs:
        factor_id, direction = ref
        factor_id = str(factor_id)
        if not factor_id.strip():
            raise ValueError("factor_id must be a non-empty string in factor_refs")
        ordered_refs.append([factor_id, _validate_direction(factor_id, direction)])
    if not str(method).strip():
        raise ValueError("method is required")
    if not str(standardization).strip():
        raise ValueError("standardization is required")
    if not str(coverage_rule).strip():
        raise ValueError("coverage_rule is required")
    if isinstance(decay_days, bool) or not isinstance(decay_days, int) or decay_days < 0:
        raise ValueError("decay_days must be an integer >= 0")
    if (
        isinstance(execution_delay_days, bool)
        or not isinstance(execution_delay_days, int)
        or execution_delay_days < 1
    ):
        raise ValueError("execution_delay_days must be an integer >= 1")
    top_quantile = float(top_quantile)
    if not 0.0 < top_quantile <= 0.5:
        raise ValueError("top_quantile must be in (0, 0.5]")
    if min_factor_coverage is not None and (
        isinstance(min_factor_coverage, bool) or not isinstance(min_factor_coverage, int)
    ):
        raise ValueError("min_factor_coverage must be an integer or None")
    payload = {
        "factor_refs": ordered_refs,
        "method": str(method),
        "method_params": {str(key): value for key, value in (method_params or {}).items()},
        "standardization": str(standardization),
        "backtest_start": str(backtest_start) if backtest_start is not None else None,
        "backtest_end": str(backtest_end) if backtest_end is not None else None,
        "decay_days": int(decay_days),
        "execution_delay_days": int(execution_delay_days),
        "top_quantile": top_quantile,
        "coverage_rule": str(coverage_rule),
        "min_factor_coverage": min_factor_coverage,
        "universe_filters": [str(item) for item in universe_filters],
        "holding_days": _validated_holding_for_digest(holding_days),
    }
    try:
        raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except TypeError as error:
        raise ValueError(f"composite id inputs must be JSON-serializable: {error}") from error
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_COMPOSITE_HASH_HEX_CHARS]
    return f"{COMPOSITE_ID_PREFIX}{digest.upper()}"


def composite_formula(composite_id: str) -> str:
    """The ``precomputed:`` formula for a validated composite id (RF-1)."""

    _require_composite_id(composite_id)
    return f"precomputed:factor_id={composite_id}"


def create_composite_overlay_root(artifact_root: Path, composite_id: str) -> Path:
    """Create the per-run factor-values overlay directory (RB-10).

    Every run gets a fresh directory under the run's artifact scope —
    ``<artifact_root>/synthesis_runs/<composite_id>_<token>`` — so even a
    hash collision cannot surface stale rows from a prior run. Non-reuse is
    structural: the token is random and creation refuses to adopt an
    existing directory.
    """

    _require_composite_id(composite_id)
    runs_root = Path(artifact_root).expanduser() / "synthesis_runs"
    for _ in range(3):
        candidate = runs_root / f"{composite_id}_{uuid.uuid4().hex[:12]}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a fresh per-run overlay directory")


def materialize_composite(
    composite_scores: pd.DataFrame,
    *,
    factor_root: Path,
    overlay_root: Path,
    composite_id: str,
    holding_days: int,
    universe_filters: Sequence[str],
    name: str | None = None,
) -> MaterializedComposite:
    """Materialize the composite as a synthetic ``precomputed:`` factor.

    Writes the tidy composite frame through the store's OWN path resolution
    (``_resolve_factor_paths`` -> ``write_incremental_values``, RF-3) into
    the per-run ``overlay_root``, stamped with the store's own
    ``_formula_signature`` over ``(factor_id, executable formula, pinned
    universe_filters)`` — exactly what the engine's read path recomputes, so
    the rows are actually readable back (writing the raw formula string, as
    an earlier sketch did, reads back zero rows). Registers the definition
    in ``factor_root`` via ``FactorRepository.save`` (RF-2; the engine
    resolves it through ``FactorCatalog(factor_root).get``) with
    ``source="synthesis"``, ``status="candidate"``,
    ``horizon_days=holding_days`` and the ONE pinned universe set (RB-6),
    then asserts the catalog round-trip returns the identical
    formula/filters — any canonicalization drift would silently change the
    read-time signature, so it is a hard error here instead.

    The caller owns failure cleanup (see :func:`run_composite_backtest`);
    NaN composite rows are written as-is: they encode "no signal at this
    date/name" (RB-9 degenerate-date erasure) and the engine drops them per
    signal date.
    """

    _require_composite_id(composite_id)
    _require_holding_days(holding_days)
    missing_columns = [column for column in _SCORE_COLUMNS if column not in composite_scores.columns]
    if missing_columns:
        raise ValueError(f"composite frame is missing columns: {missing_columns}")
    scores = composite_scores[list(_SCORE_COLUMNS)].copy()
    scores["trade_date"] = pd.to_datetime(scores["trade_date"])
    scores["instrument"] = scores["instrument"].astype(str)
    scores["score"] = pd.to_numeric(scores["score"], errors="coerce")
    if scores.empty:
        raise ValueError("composite frame has no rows to materialize")
    if int(scores["score"].notna().sum()) == 0:
        raise ValueError("composite frame has no finite values to materialize")

    pinned = tuple(str(item) for item in universe_filters)
    factor_name = str(name) if name else composite_id
    formula = composite_formula(composite_id)
    store = FactorValueStore(overlay_root, write_root=overlay_root)
    paths = store._resolve_factor_paths(  # RF-3: the store's own canonical layout
        factor_id=composite_id, factor_name=factor_name, formula=formula
    )
    if paths.write_dir is None:
        raise RuntimeError("factor value store did not resolve a write directory")
    # The signature the read path recomputes: resolve_executable_formula is a
    # pass-through for precomputed formulas, mirrored here literally so the
    # two sides can never drift apart.
    formula_signature = _formula_signature(
        composite_id, resolve_executable_formula(formula), pinned
    )
    store.write_incremental_values(
        paths.write_dir,
        factor_id=composite_id,
        factor_name=factor_name,
        formula_signature=formula_signature,
        scores=scores,
    )

    definition = FactorDefinition(
        factor_id=composite_id,
        name=factor_name,
        formula=formula,
        status="candidate",
        description="Synthesized multi-factor composite materialized as precomputed values.",
        horizon_days=holding_days,
        universe_filters=pinned,
        source="synthesis",
    )
    definition_path = FactorRepository(factor_root).save(definition)  # RF-2

    resolved = FactorCatalog(factor_root).get(composite_id)
    if (
        resolved.factor_id != composite_id
        or resolved.formula != formula
        or tuple(resolved.universe_filters) != pinned
    ):
        raise RuntimeError(
            "materialized composite does not round-trip through the catalog: "
            f"saved (factor_id={composite_id}, formula={formula}, universe_filters={list(pinned)}) "
            f"resolved (factor_id={resolved.factor_id}, formula={resolved.formula}, "
            f"universe_filters={list(resolved.universe_filters)}); the read-time value-store "
            "signature would differ from the written one and the engine would read zero rows"
        )
    return MaterializedComposite(
        composite_id=composite_id,
        formula=formula,
        formula_signature=formula_signature,
        definition=definition,
        definition_path=definition_path,
        values_dir=paths.write_dir,
        overlay_root=Path(overlay_root).expanduser(),
        rows_written=int(len(scores)),
    )


def expected_engine_signal_dates(
    dates: Sequence[object],
    *,
    delay: int,
    holding: int,
    start_signal_index: int = 0,
    include_partial_final_period: bool = False,
) -> tuple[pd.Timestamp, ...]:
    """Signal dates the engine must realize from the shared grid (RB-5).

    Walks ``rebalance_indices`` — the single grid source of truth — and
    applies ONLY the engine's own disclosed schedule breaks: the default D3
    exclusion of the trailing signal whose scheduled exit falls beyond the
    window, and (under the mark-to-market opt-in) the degenerate tail whose
    actual exit would not clear the entry bar. Skipped/thin rebalances are
    NOT filtered here: the engine keeps those slots visible as ledger stubs,
    so they must appear in the realized schedule too.
    """

    calendar = [pd.Timestamp(value) for value in dates]
    total = len(calendar)
    expected: list[pd.Timestamp] = []
    for signal_index in rebalance_indices(
        calendar, delay=delay, holding=holding, start_signal_index=start_signal_index
    ):
        scheduled_exit_index = signal_index + delay + holding
        if scheduled_exit_index < total:
            actual_exit_index = scheduled_exit_index
        elif include_partial_final_period:
            actual_exit_index = total - 1
        else:
            break
        if actual_exit_index <= signal_index + delay:
            break
        expected.append(calendar[signal_index])
    return tuple(expected)


def assert_engine_schedule_matches_grid(
    resolved_schedule: Sequence[Mapping[str, object]],
    dates: Sequence[object],
    *,
    delay: int,
    holding: int,
    start_signal_index: int = 0,
    include_partial_final_period: bool = False,
) -> tuple[str, ...]:
    """Hard-assert the realized schedule equals the shared grid (RB-5/RB-7).

    Compares the engine's ``resolved_schedule`` signal dates — including
    skipped-rebalance stubs, which stay visible by contract — against
    ``dates[rebalance_indices(...)]`` filtered only by the engine's own
    disclosed final-partial rule. Raises :class:`GridFidelityError`
    (``GRID_FIDELITY_MISMATCH``) on any divergence; returns the expected
    signal-date labels on success so callers can attach them to provenance.
    """

    expected = tuple(
        timestamp.date().isoformat()
        for timestamp in expected_engine_signal_dates(
            dates,
            delay=delay,
            holding=holding,
            start_signal_index=start_signal_index,
            include_partial_final_period=include_partial_final_period,
        )
    )
    realized = tuple(str(row["signal_date"]) for row in resolved_schedule)
    if realized != expected:
        raise GridFidelityError(
            "engine schedule diverged from the shared rebalance grid "
            f"({GRID_FIDELITY_MISMATCH}): expected signal dates {list(expected)}, "
            f"realized {list(realized)}"
        )
    return expected


def run_composite_backtest(
    composite_scores: pd.DataFrame,
    *,
    composite_id: str,
    factor_root: Path,
    data_root: Path,
    artifact_root: Path,
    holding_days: int,
    profile: SimulationProfile,
    universe_filters: Sequence[str] = (),
    transaction_costs: TransactionCostModel | None = None,
    composite_name: str | None = None,
    group_count: int = 5,
    overlay_root: Path | None = None,
    factor_values_root: Path | None = None,
    factor_values_manifest_root: Path | None = None,
    include_partial_final_period: bool = False,
    panel: pd.DataFrame | None = None,
) -> CompositeBacktestRun:
    """Materialize the composite and drive the shipped engine by id (P4).

    ``profile`` is the SHARED profile the members were fetched with (it may
    carry ``decay_days > 1``); the engine-driving profile is pinned to
    ``decay_days=0`` here, structurally (LA-1) — decay is a member-level
    transform applied exactly once before combination, and the precomputed
    read path applies the profile decay unconditionally, so a non-zero value
    would EWMA the composite a second time. ``holding_days`` is REQUIRED
    with no ``horizon_days`` fallback (RF-5). The run targets a fresh
    per-run overlay (created under ``artifact_root`` when not supplied;
    a supplied ``overlay_root`` must be dedicated to this run — it is
    deleted on failure). After the engine run the realized schedule is
    verified against the shared grid (RB-5) — a mismatch is a hard
    :class:`GridFidelityError`.

    On any exception after materialization begins, the registered
    definition is removed (or a pre-existing definition restored, mirroring
    the single-factor validation restore pattern) and the per-run overlay
    deleted. On success the definition stays in ``factor_root`` as a
    first-class factor; retention of accumulated composite definitions is a
    recorded deferred question. ``panel``, when provided, must be the
    ``data_root`` panel; it only feeds the grid-fidelity date calendar.
    """

    _require_composite_id(composite_id)
    _require_holding_days(holding_days)
    engine_profile = profile if profile.decay_days == 0 else replace(profile, decay_days=0)
    if overlay_root is None:
        run_overlay = create_composite_overlay_root(artifact_root, composite_id)
    else:
        # RB-10 hardening: a caller-supplied overlay must be fresh. Reusing a
        # populated directory would let _merge_score_updates retain prior-run
        # rows for dates absent from this run — a silent stale blend.
        run_overlay = Path(overlay_root).expanduser()
        if run_overlay.exists() and any(run_overlay.iterdir()):
            raise ValueError(
                "overlay_root must be a fresh (empty or nonexistent) directory; "
                "reusing a populated overlay can blend stale prior-run rows (RB-10)"
            )
        run_overlay.mkdir(parents=True, exist_ok=True)
    _require_disjoint_overlay(run_overlay, factor_root=factor_root, data_root=data_root)

    repository = FactorRepository(factor_root)
    previous_definition = _existing_definition(repository, composite_id)
    try:
        materialized = materialize_composite(
            composite_scores,
            factor_root=factor_root,
            overlay_root=run_overlay,
            composite_id=composite_id,
            holding_days=holding_days,
            universe_filters=universe_filters,
            name=composite_name,
        )
        result = run_factor_backtest(
            composite_id,
            factor_root=factor_root,
            data_root=data_root,
            artifact_root=artifact_root,
            holding_days=holding_days,
            group_count=group_count,
            simulation_profile=engine_profile,
            transaction_costs=transaction_costs,
            factor_values_root=factor_values_root,
            factor_values_overlay_root=run_overlay,
            factor_values_manifest_root=factor_values_manifest_root,
            sample_role=EXTERNAL_OOS_ROLE,
            include_partial_final_period=include_partial_final_period,
        )
        working_panel = apply_test_period(
            panel if panel is not None else LocalPanelDataProvider(data_root).load_panel(),
            engine_profile,
        )
        dates = sorted(working_panel["trade_date"].drop_duplicates())
        expected_signal_dates = assert_engine_schedule_matches_grid(
            result.resolved_schedule,
            dates,
            delay=engine_profile.execution_delay_days,
            holding=holding_days,
            include_partial_final_period=include_partial_final_period,
        )
    except Exception:
        _cleanup_composite_artifacts(
            repository,
            composite_id=composite_id,
            previous_definition=previous_definition,
            overlay_root=run_overlay,
        )
        raise
    return CompositeBacktestRun(
        composite_id=composite_id,
        result=result,
        materialized=materialized,
        engine_profile=engine_profile,
        overlay_root=run_overlay,
        expected_signal_dates=expected_signal_dates,
    )


def _require_composite_id(composite_id: str) -> str:
    if not isinstance(composite_id, str) or not _COMPOSITE_ID_RE.fullmatch(composite_id):
        raise ValueError(
            "composite_id must match COMPOSITE_<10-16 uppercase hex chars> "
            f"(colon-free, catalog-canonical — RF-1/RB-10); got: {composite_id!r}"
        )
    return composite_id


def _require_holding_days(value: object) -> int:
    """RF-5: ``holding_days`` is required — no ``horizon_days`` fallback."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            "holding_days is required and must be a positive integer for a "
            "composite backtest; there is no horizon_days fallback (RF-5)"
        )
    return value


def _validated_holding_for_digest(value: object) -> int:
    """Digest-side holding validation (same RF-5 rule, ValueError wording kept)."""

    return _require_holding_days(value)


def _require_disjoint_overlay(overlay_root: Path, *, factor_root: Path, data_root: Path) -> None:
    """Refuse overlay locations that would let failure cleanup touch source roots."""

    overlay = Path(overlay_root).expanduser().resolve()
    for label, root in (("factor_root", factor_root), ("data_root", data_root)):
        resolved = Path(root).expanduser().resolve()
        if overlay == resolved or overlay.is_relative_to(resolved) or resolved.is_relative_to(overlay):
            raise ValueError(
                f"overlay_root must be disjoint from {label}: overlay={overlay} {label}={resolved}"
            )


def _existing_definition(repository: FactorRepository, factor_id: str) -> FactorDefinition | None:
    try:
        return repository.get(factor_id)
    except FileNotFoundError:
        return None


def cleanup_composite_artifacts(
    factor_root: Path,
    *,
    composite_id: str,
    previous_definition: FactorDefinition | None,
    overlay_root: Path,
) -> None:
    """Public failure-cleanup entry point for workflow layers (P5).

    :func:`run_composite_backtest` cleans up after failures inside its own
    scope, but the web workflow keeps working with the materialized artifacts
    afterwards (same-window evaluation, payload assembly). When one of those
    later steps fails, the workflow calls this to apply the identical
    definition-restore + per-run-overlay removal so a failed run never leaves
    a half-registered composite behind.
    """

    _cleanup_composite_artifacts(
        FactorRepository(factor_root),
        composite_id=composite_id,
        previous_definition=previous_definition,
        overlay_root=overlay_root,
    )


def _cleanup_composite_artifacts(
    repository: FactorRepository,
    *,
    composite_id: str,
    previous_definition: FactorDefinition | None,
    overlay_root: Path,
) -> None:
    """Failure cleanup: remove the definition and the per-run overlay.

    Mirrors the single-factor ``_restore_factor_after_failed_validation``
    pattern: a pre-existing definition under the same id is restored, a
    fresh one is deleted. The per-run overlay is removed best-effort
    (``ignore_errors``) so cleanup never masks the original failure.
    """

    try:
        if previous_definition is None:
            repository.delete(composite_id)
        else:
            repository.save(previous_definition)
    finally:
        shutil.rmtree(overlay_root, ignore_errors=True)

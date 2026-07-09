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
"""

from __future__ import annotations

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
    degenerate_dates = {str(column): () for column in matrix.columns}
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
    masked = working.copy()
    if not bool(keep.all()):
        masked.loc[~keep, :] = np.nan

    weight_series = pd.Series(weights_used, dtype="float64")
    # skipna sum with min_count=1: available members contribute w_f * t_f,
    # missing members are excluded from the sum, and a fully-masked row stays
    # NaN instead of collapsing to a fabricated 0 (FP-4).
    composite = masked.mul(weight_series, axis=1).sum(axis=1, min_count=1)

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

    composite, degenerate_dates = _mask_degenerate_dates(composite)
    warning_codes = (DEGENERATE_CROSS_SECTION,) if degenerate_dates else ()

    tidy = (
        composite.rename("score")
        .reset_index()[list(_SCORE_COLUMNS)]
        .sort_values(["trade_date", "instrument"])
        .reset_index(drop=True)
    )
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
) -> str:
    """Derive the ``COMPOSITE_<hash>`` id from ALL run inputs (RB-10, RF-1).

    The digest covers every §11 input: the ORDERED ``(factor_id, direction)``
    list (member order is identity-bearing), method, method_params,
    standardization, the backtest window, decay, delay, top_quantile,
    coverage rule, min_factor_coverage, and the pinned universe. Any single
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
        run_overlay = Path(overlay_root).expanduser()
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

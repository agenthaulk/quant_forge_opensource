"""Local factor value store adapter for cache-first score preparation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat

import pandas as pd

from quant_forge.factor_library.classification import FACTOR_CATEGORY_DIRS, factor_category_from_parts
from quant_forge.factor_library.catalog import precomputed_formula_store_key, resolve_factor_values_root
from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.factor_engine.formula_parser import formula_lookback_rows


@dataclass(frozen=True)
class FactorScoreResult:
    scores: pd.DataFrame
    source: str
    cached_rows: int
    computed_rows: int
    factor_values_path: Path | None = None
    factor_values_write_path: Path | None = None
    compute_mode: str = "computed_formula"
    compute_reason: str = ""
    missing_rows: int = 0
    required_rows: int = 0
    missing_ratio: float = 0.0
    lookback_rows: int = 0
    context_rows: int = 0


@dataclass(frozen=True)
class ScoreComputationPlan:
    panel: pd.DataFrame
    mode: str
    reason: str
    missing_rows: int
    required_rows: int
    missing_ratio: float
    lookback_rows: int
    context_rows: int


@dataclass(frozen=True)
class _ResolvedFactorValuePaths:
    read_dirs: tuple[Path, ...]
    write_dir: Path | None
    primary_dir: Path


class FactorValueStore:
    def __init__(self, root: Path, *, write_root: Path | None = None) -> None:
        self.root = (resolve_factor_values_root(root) or root).expanduser()
        self.write_root = (resolve_factor_values_root(write_root) or write_root).expanduser() if write_root else None

    def prepare_scores(
        self,
        panel: pd.DataFrame,
        *,
        factor_id: str,
        factor_name: str,
        formula: str,
        universe_filters: tuple[str, ...],
        cache_only: bool = False,
        target_panel: pd.DataFrame | None = None,
    ) -> FactorScoreResult:
        from quant_forge.operator_registry.resolver import resolve_executable_formula

        executable_formula = resolve_executable_formula(formula)
        factor_paths = self._resolve_factor_paths(factor_id=factor_id, factor_name=factor_name, formula=formula)
        formula_signature = _formula_signature(factor_id, executable_formula, universe_filters)
        legacy_formula_signature = _formula_signature(factor_id, formula, universe_filters)
        cached = self.read_factor_values(
            factor_paths.read_dirs,
            factor_id=factor_id,
            formula_signature=formula_signature,
            allow_unsigned_root_values=cache_only,
        )
        if legacy_formula_signature != formula_signature:
            legacy_cached = self.read_factor_values(
                factor_paths.read_dirs,
                factor_id=factor_id,
                formula_signature=legacy_formula_signature,
                allow_unsigned_root_values=cache_only,
            )
            cached = _dedupe_scores(_concat_score_frames([legacy_cached, cached]))
        result_panel = target_panel if target_panel is not None else panel
        panel_keys = _score_keys(result_panel)
        context_keys = _score_keys(panel)
        required_keys = _required_score_keys(result_panel, universe_filters)
        cached_for_panel = _restrict_to_panel(cached, context_keys)
        if cache_only:
            combined = _apply_universe_filters(
                result_panel,
                _restrict_to_panel(cached_for_panel, panel_keys),
                universe_filters,
            )
            combined = combined.sort_values(["trade_date", "instrument"]).reset_index(drop=True)
            cached_rows = int(len(combined))
            return FactorScoreResult(
                scores=combined,
                source=_cache_only_source(cached_rows, int(len(required_keys))),
                cached_rows=cached_rows,
                computed_rows=0,
                factor_values_path=factor_paths.primary_dir,
                factor_values_write_path=factor_paths.write_dir,
                compute_mode="cache_only",
                compute_reason="cache_only requested for precomputed factor values",
                missing_rows=max(0, int(len(required_keys)) - cached_rows),
                required_rows=int(len(required_keys)),
                missing_ratio=_missing_ratio(max(0, int(len(required_keys)) - cached_rows), int(len(required_keys))),
                context_rows=0,
            )
        cached_available = _restrict_to_panel(_trusted_cached_scores(cached_for_panel, panel, executable_formula), required_keys)
        missing_keys = _missing_score_keys(required_keys, cached_available)
        cached_complete = cached_available
        result_missing_keys = _missing_score_keys(panel_keys, cached_complete)

        plan = _plan_score_computation(panel, required_keys=required_keys, missing_keys=missing_keys, formula=executable_formula)
        computed_context = (
            execute_factor_formula(plan.panel, executable_formula, universe_filters) if plan.missing_rows else _empty_scores()
        )
        computed = _restrict_to_panel(computed_context, missing_keys)
        computed_for_result = _restrict_to_panel(computed_context, result_missing_keys)
        if not computed.empty and factor_paths.write_dir is not None:
            self.write_incremental_values(
                factor_paths.write_dir,
                factor_id=factor_id,
                factor_name=factor_name,
                formula_signature=formula_signature,
                scores=computed,
            )

        combined = _concat_score_frames([cached_complete, computed_for_result])
        if combined.empty:
            combined = _empty_scores()
        else:
            combined = _restrict_to_panel(combined, panel_keys)
            combined = _apply_universe_filters(result_panel, combined, universe_filters)
            combined = combined.sort_values(["trade_date", "instrument"]).reset_index(drop=True)

        cached_rows = int(len(cached_complete))
        computed_rows = int(len(computed))
        source = _score_source(cached_rows, computed_rows)
        return FactorScoreResult(
            scores=combined,
            source=source,
            cached_rows=cached_rows,
            computed_rows=computed_rows,
            factor_values_path=factor_paths.primary_dir,
            factor_values_write_path=factor_paths.write_dir,
            compute_mode=plan.mode,
            compute_reason=plan.reason,
            missing_rows=plan.missing_rows,
            required_rows=plan.required_rows,
            missing_ratio=plan.missing_ratio,
            lookback_rows=plan.lookback_rows,
            context_rows=plan.context_rows,
        )

    def _resolve_factor_paths(
        self,
        *,
        factor_id: str,
        factor_name: str,
        formula: str,
    ) -> _ResolvedFactorValuePaths:
        candidates = _factor_dir_candidates(factor_id=factor_id, factor_name=factor_name, formula=formula)
        existing_read_dirs = _find_existing_factor_dirs(self.root, candidates)
        existing_write_dirs = _find_existing_factor_dirs(self.write_root, candidates) if self.write_root else ()
        category = factor_category_from_parts(factor_id=factor_id, factor_name=factor_name, formula=formula)
        write_dir = (
            self.write_root / FACTOR_CATEGORY_DIRS[category] / _canonical_factor_dir_name(factor_id or factor_name)
            if self.write_root
            else None
        )
        read_dirs = _unique_existing_dirs(
            (*existing_read_dirs, *existing_write_dirs)
        )
        primary_dir = (
            existing_write_dirs[-1]
            if existing_write_dirs
            else (existing_read_dirs[-1] if existing_read_dirs else (write_dir or self.root))
        )
        return _ResolvedFactorValuePaths(read_dirs=read_dirs, write_dir=write_dir, primary_dir=primary_dir)

    def has_stored_values(self, *, factor_id: str, factor_name: str, formula: str) -> bool:
        """Report whether ANY stored value file exists for this factor.

        This is a PRESENCE probe, not a readability guarantee: it matches
        candidate directories with the SAME two-strategy rules
        ``_resolve_factor_paths`` -> ``_find_existing_factor_dirs`` uses
        (direct category-dir candidates + a case-insensitive listing match;
        never a hand-built path — see ``_stored_value_dirs``) and returns
        True iff at least one ``*.parquet`` is found under any of them. The
        read path (``prepare_scores`` / ``read_factor_values``) still enforces
        the formula signature row by row, so a True result here does not
        promise that any rows will actually satisfy a given request — only
        that some value file exists on disk for this factor.

        Contract: this probe RAISES when presence is unobservable and
        returns ``False`` ONLY on a readable, confirmed absence. Unlike the
        read/write path's directory resolution (``_resolve_factor_paths`` ->
        ``_find_existing_factor_dirs``, which tolerates an inaccessible
        candidate by treating it as absent so an ordinary score computation
        can still fall back to computing from the formula), every
        filesystem call this probe makes is deliberately non-swallowing: a
        directory that structurally does not exist resolves to absence, but
        any OTHER ``OSError`` (permission denied, a stale/broken mount, a
        hardware read failure, ...) PROPAGATES instead of being silently
        read as confirmed absence — a caller mapping that to a hard refusal
        would otherwise be acting on a guess. Callers that need FP-4
        null-on-unobservable semantics (e.g. the registry probe) must catch
        around this call themselves; see ``_precomputed_values_present`` in
        ``apps/web/api.py``, which already maps any raised exception to
        ``None``.
        """

        candidates = _factor_dir_candidates(factor_id=factor_id, factor_name=factor_name, formula=formula)
        roots = (self.root, *((self.write_root,) if self.write_root else ()))
        read_dirs = _unique_existing_dirs(
            tuple(directory for root in roots for directory in _stored_value_dirs(root, candidates))
        )
        return any(_listed_parquet_files(read_dir) for read_dir in read_dirs)

    def read_factor_values(
        self,
        factor_dirs: tuple[Path, ...],
        *,
        factor_id: str,
        formula_signature: str,
        allow_unsigned_root_values: bool = False,
    ) -> pd.DataFrame:
        """Read cached factor values from canonical and overlay directories."""

        if not factor_dirs:
            return _empty_scores()
        frames: list[pd.DataFrame] = []
        for factor_dir in factor_dirs:
            for path in _factor_value_files(factor_dir):
                frame = _read_score_file(
                    path,
                    factor_id=factor_id,
                    formula_signature=formula_signature,
                    allow_unsigned_root_values=allow_unsigned_root_values,
                )
                if not frame.empty:
                    frames.append(frame)
        if not frames:
            return _empty_scores()
        return _dedupe_scores(pd.concat(frames, ignore_index=True))

    def write_incremental_values(
        self,
        factor_dir: Path,
        *,
        factor_id: str,
        factor_name: str,
        formula_signature: str,
        scores: pd.DataFrame,
    ) -> None:
        if scores.empty:
            return
        incremental_dir = factor_dir / "incremental"
        incremental_dir.mkdir(parents=True, exist_ok=True)
        normalized = _dedupe_scores(scores)
        for year, year_scores in normalized.groupby(normalized["trade_date"].dt.year):
            target = incremental_dir / f"{int(year)}.parquet"
            existing = (
                _read_score_file(target, factor_id=factor_id, formula_signature=formula_signature)
                if target.exists()
                else _empty_scores()
            )
            combined = _merge_score_updates(existing, year_scores)
            payload = combined.copy()
            payload["trade_date"] = payload["trade_date"].dt.strftime("%Y-%m-%d")
            payload["factor_id"] = factor_id
            payload["factor_name"] = factor_name
            payload["formula_signature"] = formula_signature
            payload = payload.rename(columns={"score": "factor_value"})
            payload[
                [
                    "trade_date",
                    "instrument",
                    "factor_id",
                    "factor_name",
                    "formula_signature",
                    "factor_value",
                ]
            ].to_parquet(
                target,
                index=False,
            )


def remove_stored_values_for_factor_id(root: Path | None, factor_id: str) -> int:
    """Remove every stored-value directory derivable from ``factor_id`` ALONE.

    Cleanup counterpart to ``_resolve_factor_paths`` for pipeline working
    rows (apps/web/pipeline.py): deletes the ID-derived candidate directory
    names (canonical ``factor_id=...`` form, the raw id, and its
    safe-dir-name form) under ``root`` itself and under each category
    subdirectory. Deliberately NARROWER than ``_factor_dir_candidates``: the
    name/formula-derived fallbacks are shared namespaces (two factors can
    share a display name or a precomputed formula key), so a cleanup keyed
    on them could destroy another factor's cache. Callers pass ids that are
    globally unique by construction (a pipeline's working id embeds its
    pipeline_id), which makes the id-derived forms safe to remove
    recursively. Returns the number of directories removed; a missing root
    removes nothing.
    """

    import shutil

    if root is None or not factor_id.strip():
        return 0
    base = Path(root).expanduser()
    if not base.is_dir():
        return 0
    candidates = list(
        dict.fromkeys(
            name
            for name in (_canonical_factor_dir_name(factor_id), factor_id, _safe_dir_name(factor_id))
            if name
        )
    )
    parents = [base, *(base / category_dir for category_dir in FACTOR_CATEGORY_DIRS.values())]
    removed = 0
    for parent in parents:
        for candidate in candidates:
            target = parent / candidate
            if target.is_dir():
                shutil.rmtree(target)
                removed += 1
    return removed


def _score_keys(panel: pd.DataFrame) -> pd.DataFrame:
    keys = panel[["trade_date", "instrument"]].copy()
    keys["trade_date"] = pd.to_datetime(keys["trade_date"])
    keys["instrument"] = keys["instrument"].astype(str)
    return keys.drop_duplicates().reset_index(drop=True)


def _missing_score_keys(target_keys: pd.DataFrame, cached: pd.DataFrame) -> pd.DataFrame:
    if target_keys.empty:
        return target_keys
    cached_keys = _score_keys(cached)
    marked = target_keys.merge(
        cached_keys.assign(_cached=True),
        on=["trade_date", "instrument"],
        how="left",
    )
    missing = marked[marked["_cached"].isna()][["trade_date", "instrument"]]
    return missing.reset_index(drop=True)


def _trusted_cached_scores(cached: pd.DataFrame, panel: pd.DataFrame, formula: str) -> pd.DataFrame:
    trusted = _dedupe_scores(cached)
    if trusted.empty:
        return trusted
    lookback = formula_lookback_rows(formula)
    if lookback <= 0:
        return trusted.dropna(subset=["score"])
    warmup_keys = _warmup_score_keys(panel, lookback)
    if warmup_keys.empty:
        return trusted.dropna(subset=["score"])
    normalized = trusted.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized["instrument"] = normalized["instrument"].astype(str)
    marked = normalized[["trade_date", "instrument"]].merge(
        warmup_keys.assign(_warmup=True),
        on=["trade_date", "instrument"],
        how="left",
    )
    keep = trusted["score"].notna().to_numpy() | marked["_warmup"].eq(True).to_numpy()
    return trusted.loc[keep].reset_index(drop=True)


def _warmup_score_keys(panel: pd.DataFrame, lookback: int) -> pd.DataFrame:
    if lookback <= 0 or panel.empty:
        return _score_keys(panel.iloc[0:0])
    ordered = _score_keys(panel).sort_values(["instrument", "trade_date"]).reset_index(drop=True)
    warmup = ordered.groupby("instrument", sort=False).cumcount() < lookback
    return ordered.loc[warmup, ["trade_date", "instrument"]].reset_index(drop=True)


def _panel_with_lookback(panel: pd.DataFrame, missing_keys: pd.DataFrame, formula: str) -> pd.DataFrame:
    return _plan_score_computation(panel, required_keys=_score_keys(panel), missing_keys=missing_keys, formula=formula).panel


def _plan_score_computation(
    panel: pd.DataFrame,
    *,
    required_keys: pd.DataFrame,
    missing_keys: pd.DataFrame,
    formula: str,
) -> ScoreComputationPlan:
    required_rows = int(len(required_keys))
    if missing_keys.empty:
        return ScoreComputationPlan(
            panel=panel.iloc[0:0],
            mode="cache_only",
            reason="all required factor values were available in cache",
            missing_rows=0,
            required_rows=required_rows,
            missing_ratio=0.0,
            lookback_rows=0,
            context_rows=0,
        )
    lookback = formula_lookback_rows(formula)
    normalized_panel = panel.copy()
    normalized_panel["trade_date"] = pd.to_datetime(normalized_panel["trade_date"])
    normalized_panel["instrument"] = normalized_panel["instrument"].astype(str)
    normalized_missing = _score_keys(missing_keys)
    missing_rows = int(len(normalized_missing))
    ratio = _missing_ratio(missing_rows, required_rows)
    context_dates = pd.DatetimeIndex(normalized_missing["trade_date"].drop_duplicates())
    if lookback <= 0:
        context_panel = panel[pd.to_datetime(panel["trade_date"]).isin(context_dates)]
        mode = "date_block_incremental" if ratio >= 0.05 else "sparse_incremental"
        return ScoreComputationPlan(
            panel=context_panel,
            mode=mode,
            reason=f"formula has no lookback; compute target missing dates only ({missing_rows} rows)",
            missing_rows=missing_rows,
            required_rows=required_rows,
            missing_ratio=ratio,
            lookback_rows=0,
            context_rows=int(len(context_panel)),
        )

    if ratio >= 0.5:
        return ScoreComputationPlan(
            panel=panel,
            mode="full_recompute",
            reason=f"dense lookback cache miss ratio {ratio:.2%}; full panel is cheaper than incremental context expansion",
            missing_rows=missing_rows,
            required_rows=required_rows,
            missing_ratio=ratio,
            lookback_rows=lookback,
            context_rows=int(len(panel)),
        )

    context_dates = context_dates.union(_lookback_context_dates(normalized_panel, normalized_missing, lookback))
    context_panel = panel[pd.to_datetime(panel["trade_date"]).isin(context_dates)]
    context_rows = int(len(context_panel))
    if context_rows >= int(len(panel) * 0.8):
        return ScoreComputationPlan(
            panel=panel,
            mode="full_recompute",
            reason=(
                f"lookback context expands to {context_rows} of {len(panel)} rows; "
                "full panel avoids fragmented incremental compute"
            ),
            missing_rows=missing_rows,
            required_rows=required_rows,
            missing_ratio=ratio,
            lookback_rows=lookback,
            context_rows=int(len(panel)),
        )
    mode = "date_block_incremental" if ratio >= 0.05 else "sparse_incremental"
    return ScoreComputationPlan(
        panel=context_panel,
        mode=mode,
        reason=f"lookback context planned with vectorized date expansion over {len(context_dates)} dates",
        missing_rows=missing_rows,
        required_rows=required_rows,
        missing_ratio=ratio,
        lookback_rows=lookback,
        context_rows=context_rows,
    )


def _missing_ratio(missing_rows: int, required_rows: int) -> float:
    if required_rows <= 0:
        return 0.0
    return float(missing_rows / required_rows)


def _lookback_context_dates(
    normalized_panel: pd.DataFrame,
    normalized_missing: pd.DataFrame,
    lookback: int,
) -> pd.DatetimeIndex:
    ordered = (
        normalized_panel[["trade_date", "instrument"]]
        .drop_duplicates()
        .sort_values(["instrument", "trade_date"])
        .reset_index(drop=True)
    )
    ordered["_qf_position"] = ordered.groupby("instrument", sort=False).cumcount()
    positions = normalized_missing.merge(
        ordered,
        on=["trade_date", "instrument"],
        how="inner",
    )[["instrument", "_qf_position"]]
    if positions.empty:
        return pd.DatetimeIndex([])
    date_chunks: list[pd.Series] = []
    ordered_by_instrument = {
        instrument: rows.set_index("_qf_position")["trade_date"]
        for instrument, rows in ordered.groupby("instrument", sort=False)
    }
    for instrument, group in positions.groupby("instrument", sort=False):
        instrument_dates = ordered_by_instrument.get(instrument)
        if instrument_dates is None:
            continue
        intervals = sorted(
            (max(0, int(position) - lookback), int(position))
            for position in group["_qf_position"].drop_duplicates()
        )
        for start, end in _merge_position_intervals(intervals):
            date_chunks.append(instrument_dates.loc[start:end])
    if not date_chunks:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(pd.concat(date_chunks, ignore_index=True).drop_duplicates())


def _merge_position_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[tuple[int, int]] = []
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end + 1:
            current_end = max(current_end, end)
            continue
        merged.append((current_start, current_end))
        current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _required_score_keys(panel: pd.DataFrame, universe_filters: tuple[str, ...]) -> pd.DataFrame:
    if not universe_filters:
        return _score_keys(panel)
    mask = execute_factor_formula(panel, "close", universe_filters)
    return _score_keys(mask[mask["score"].notna()])


def _empty_scores() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.Series(dtype="datetime64[ns]"),
            "instrument": pd.Series(dtype="object"),
            "score": pd.Series(dtype="float64"),
        }
    )


def _concat_score_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    # pandas warns when a concat operand is empty or all-NA because a future
    # release will stop excluding it from result-dtype inference; drop empty
    # frames before concatenating so the score-combining call sites never
    # depend on that inference and never trip the warning.
    non_empty = [frame for frame in frames if not frame.empty]
    if not non_empty:
        return _empty_scores()
    if len(non_empty) == 1:
        return non_empty[0]
    return pd.concat(non_empty, ignore_index=True)


def _factor_value_files(factor_dir: Path) -> list[Path]:
    files = [
        path
        for path in sorted(factor_dir.glob("*.parquet"))
        if _is_yearly_factor_value_file(path)
    ]
    incremental_dir = factor_dir / "incremental"
    if incremental_dir.exists():
        files.extend(
            path
            for path in sorted(incremental_dir.glob("*.parquet"))
            if _is_yearly_factor_value_file(path)
        )
    return files


def _is_yearly_factor_value_file(path: Path) -> bool:
    return not path.name.startswith("._") and re.fullmatch(r"\d{4}\.parquet", path.name) is not None


def _read_score_file(
    path: Path,
    *,
    factor_id: str | None = None,
    formula_signature: str | None = None,
    allow_unsigned_root_values: bool = False,
) -> pd.DataFrame:
    if not path.exists() or path.name.startswith("._"):
        return _empty_scores()
    raw = pd.read_parquet(path)
    if factor_id is not None and "factor_id" in raw.columns:
        expected = _factor_id_values(factor_id)
        actual = raw["factor_id"].astype(str).str.strip().str.lower().str.replace("-", "_", regex=False)
        raw = raw[actual.isin(expected)]
    if formula_signature is not None:
        if "formula_signature" in raw.columns:
            raw = raw[raw["formula_signature"].astype(str) == formula_signature]
        elif path.parent.name == "incremental" or not allow_unsigned_root_values:
            return _empty_scores()
    if raw.empty:
        return _empty_scores()
    date_column = _first_existing(raw, ("trade_date", "date"))
    instrument_column = _first_existing(raw, ("instrument", "instrument_id", "provider_symbol", "ts_code"))
    score_column = _first_existing(raw, ("score", "factor_value"))
    if date_column is None or instrument_column is None or score_column is None:
        return _empty_scores()
    result = pd.DataFrame(
        {
            "trade_date": _parse_trade_dates(raw[date_column]),
            "instrument": raw[instrument_column].astype(str),
            "score": pd.to_numeric(raw[score_column], errors="coerce"),
        }
    )
    return _dedupe_scores(result.dropna(subset=["trade_date", "instrument"]))


def _parse_trade_dates(values: pd.Series) -> pd.Series:
    if pd.api.types.is_integer_dtype(values) or pd.api.types.is_float_dtype(values):
        return pd.to_datetime(values.astype("Int64").astype(str), format="%Y%m%d", errors="coerce")
    as_text = values.astype(str)
    if as_text.str.fullmatch(r"\d{8}").all():
        return pd.to_datetime(as_text, format="%Y%m%d", errors="coerce")
    return pd.to_datetime(values, errors="coerce")


def _first_existing(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in frame.columns:
            return candidate
    return None


def _restrict_to_panel(scores: pd.DataFrame, target_keys: pd.DataFrame) -> pd.DataFrame:
    if scores.empty:
        return _empty_scores()
    normalized = _dedupe_scores(scores)
    return target_keys.merge(normalized, on=["trade_date", "instrument"], how="inner")


def _apply_universe_filters(
    panel: pd.DataFrame,
    scores: pd.DataFrame,
    universe_filters: tuple[str, ...],
) -> pd.DataFrame:
    if not universe_filters or scores.empty:
        return scores
    mask = execute_factor_formula(panel, "close", universe_filters)
    result = scores.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result["instrument"] = result["instrument"].astype(str)
    allowed = _score_keys(mask.loc[mask["score"].notna(), ["trade_date", "instrument"]])
    marked = result[["trade_date", "instrument"]].merge(
        allowed.assign(_allowed=True),
        on=["trade_date", "instrument"],
        how="left",
    )
    result.loc[~marked["_allowed"].eq(True).to_numpy(), "score"] = pd.NA
    return result


def _merge_score_updates(existing: pd.DataFrame, updates: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return _dedupe_scores(updates)
    return _dedupe_scores(pd.concat([existing, updates], ignore_index=True))


def _dedupe_scores(scores: pd.DataFrame) -> pd.DataFrame:
    if scores.empty:
        return _empty_scores()
    result = scores[["trade_date", "instrument", "score"]].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result["instrument"] = result["instrument"].astype(str)
    result["score"] = pd.to_numeric(result["score"], errors="coerce")
    return result.drop_duplicates(["trade_date", "instrument"], keep="last").reset_index(drop=True)


def _find_existing_factor_dirs(root: Path, candidates: list[str]) -> tuple[Path, ...]:
    if not _safe_is_dir(root):
        return ()
    matches: list[Path] = []
    for candidate in reversed(candidates):
        if not _is_child_name(candidate):
            continue
        for category_dir in FACTOR_CATEGORY_DIRS.values():
            direct = root / category_dir / candidate
            if _safe_is_dir(direct):
                matches.append(direct)
        direct = root / candidate
        if _safe_is_dir(direct):
            matches.append(direct)
    children: dict[str, Path] = {}
    for search_root in _factor_value_search_roots(root):
        children.update({child.name.lower(): child for child in _safe_child_dirs(search_root)})
    for candidate in reversed(candidates):
        match = children.get(candidate.lower())
        if match is not None:
            matches.append(match)
    return _unique_existing_dirs(tuple(matches))


def _factor_value_search_roots(root: Path) -> tuple[Path, ...]:
    roots = [root]
    for category_dir in FACTOR_CATEGORY_DIRS.values():
        category_root = root / category_dir
        if _safe_is_dir(category_root):
            roots.append(category_root)
    return tuple(roots)


def _safe_child_dirs(root: Path) -> list[Path]:
    directories: list[Path] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return directories
    for child in children:
        if _is_ignored_mount_entry(child):
            continue
        if _safe_is_dir(child):
            directories.append(child)
    return directories


def _safe_is_dir(path: Path) -> bool:
    if _is_ignored_mount_entry(path):
        return False
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_ignored_mount_entry(path: Path) -> bool:
    return path.name.startswith("._") or path.name in {".DS_Store", ".Spotlight-V100", ".Trashes", ".fseventsd"}


# ---------------------------------------------------------------------------
# Strict (non-swallowing) directory resolution — ``has_stored_values`` only.
#
# Everything above this line (``_find_existing_factor_dirs``,
# ``_factor_value_search_roots``, ``_safe_child_dirs``, ``_safe_is_dir``) is
# deliberately TOLERANT: the read/write path treats an inaccessible
# candidate directory as absent so an ordinary score computation can still
# fall back to computing from the formula instead of hard-failing on a
# filesystem hiccup. The presence probe (``has_stored_values``) needs the
# opposite contract — an unobservable directory must never be reported as
# confirmed absence (a downstream hard refusal would then be acting on a
# guess, not an observation) — so it uses this separate, strict set of
# helpers instead of the tolerant ones above. ``Path.is_dir()`` cannot be
# used for the strict form: on this stdlib (see ``genericpath.isdir``) it
# swallows EVERY ``OSError`` — including permission failures, not just a
# missing path — into ``False``, exactly the swallow this probe must not
# have. ``Path.stat()``/``Path.iterdir()`` are plain passthroughs to
# ``os.stat``/``os.scandir`` with no such swallow, so the strict helpers are
# built on those instead, narrowly treating only ``FileNotFoundError`` /
# ``NotADirectoryError`` (a path that structurally is not there — a normal,
# observable absence) as a negative result and letting every other
# ``OSError`` propagate.
# ---------------------------------------------------------------------------


def _is_dir_strict(path: Path) -> bool:
    """True if ``path`` is a directory; False ONLY on confirmed absence.

    ``FileNotFoundError``/``NotADirectoryError`` from ``Path.stat()`` mean
    the path structurally is not there; any OTHER ``OSError`` (permission
    denied, a stale/broken mount, a hardware read failure, ...) propagates.
    """

    if _is_ignored_mount_entry(path):
        return False
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISDIR(info.st_mode)


def _listed_dir_entries(path: Path) -> list[Path]:
    """Plain (non-swallowing) directory listing for the presence probe.

    ``FileNotFoundError``/``NotADirectoryError`` mean ``path`` simply is not
    there — a normal, confirmed absence — so they resolve to an empty list.
    Any OTHER ``OSError`` propagates: the listing genuinely failed and
    presence is UNOBSERVABLE, never silently "no entries".
    """

    try:
        return sorted(path.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []


def _stored_value_dirs(root: Path, candidates: list[str]) -> tuple[Path, ...]:
    """Presence-probe directory candidates for ``root`` (strict I/O).

    Mirrors ``_find_existing_factor_dirs``'s two matching strategies — direct
    candidate paths under each category directory, plus a case-insensitive
    child-name match against a listing of ``root`` and its category
    subdirectories — using ``_is_dir_strict``/``_listed_dir_entries`` in
    place of ``_safe_is_dir``/``_safe_child_dirs`` so an inaccessible
    directory raises instead of silently matching nothing.
    """

    if not _is_dir_strict(root):
        return ()
    matches: list[Path] = []
    for candidate in reversed(candidates):
        if not _is_child_name(candidate):
            continue
        for category_dir in FACTOR_CATEGORY_DIRS.values():
            direct = root / category_dir / candidate
            if _is_dir_strict(direct):
                matches.append(direct)
        direct = root / candidate
        if _is_dir_strict(direct):
            matches.append(direct)
    search_roots = [root]
    for category_dir in FACTOR_CATEGORY_DIRS.values():
        category_root = root / category_dir
        if _is_dir_strict(category_root):
            search_roots.append(category_root)
    children: dict[str, Path] = {}
    for search_root in search_roots:
        for child in _listed_dir_entries(search_root):
            if _is_dir_strict(child):
                children[child.name.lower()] = child
    for candidate in reversed(candidates):
        match = children.get(candidate.lower())
        if match is not None:
            matches.append(match)
    return _unique_existing_dirs(tuple(matches))


def _listed_parquet_files(factor_dir: Path) -> list[Path]:
    """``*.parquet`` value files under ``factor_dir`` (+ ``incremental/``).

    A non-swallowing replacement for the read path's ``Path.glob`` use
    (``_factor_value_files``): pathlib's ``glob`` documents that it silently
    skips directories it cannot read, including on ``PermissionError`` —
    exactly the swallow this probe must not have. Every entry is listed via
    ``_listed_dir_entries`` and matched by the same yearly-file name pattern
    ``_factor_value_files`` uses (``_is_yearly_factor_value_file``).
    """

    files = [entry for entry in _listed_dir_entries(factor_dir) if _is_yearly_factor_value_file(entry)]
    files.extend(
        entry
        for entry in _listed_dir_entries(factor_dir / "incremental")
        if _is_yearly_factor_value_file(entry)
    )
    return files


def _unique_existing_dirs(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _is_child_name(value: str) -> bool:
    if value in {".", ".."}:
        return False
    path = Path(value)
    return not path.is_absolute() and len(path.parts) == 1


def _factor_dir_candidates(*, factor_id: str, factor_name: str, formula: str) -> list[str]:
    canonical_candidates = [_canonical_factor_dir_name(factor_id)]
    id_candidates = [factor_id, _safe_dir_name(factor_id)]
    name_candidates = [factor_name, _safe_dir_name(factor_name)]
    store_key = _precomputed_store_key(formula)
    store_key_candidates = [store_key, _safe_dir_name(store_key)] if store_key else []
    aliases = _worldquant_aliases(factor_id)
    if aliases:
        canonical_candidates.append(_canonical_factor_dir_name(aliases[0]))
    legacy_candidates = (
        [*store_key_candidates, *aliases, *id_candidates, *name_candidates]
        if aliases
        else [*store_key_candidates, *id_candidates, *name_candidates]
    )
    return list(dict.fromkeys(candidate for candidate in [*canonical_candidates, *legacy_candidates] if candidate))


def _precomputed_store_key(formula: str) -> str:
    return precomputed_formula_store_key(formula)


def _canonical_factor_dir_name(value: str) -> str:
    normalized = value.strip().upper().replace("-", "_")
    match = re.fullmatch(r"(?:(?:WORLDQUANT|WQ)_)?ALPHA_?0*(\d+)", normalized)
    if match is not None:
        factor_id = f"WQ_ALPHA_{int(match.group(1)):03d}"
    else:
        factor_id = _safe_dir_name(normalized)
    return f"factor_id={factor_id}"


def _worldquant_aliases(value: str) -> list[str]:
    normalized = value.strip().lower().replace("-", "_")
    match = re.fullmatch(r"(?:(?:worldquant|wq)_)?alpha_?0*(\d+)", normalized)
    if match is None:
        return []
    number = int(match.group(1))
    padded = f"{number:03d}"
    return [f"worldquant_alpha_{padded}", f"wq_alpha_{padded}", f"alpha_{padded}"]


def _safe_dir_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.=-]+", "_", value.strip())
    normalized = normalized.strip("_")
    if normalized in {"", ".", ".."}:
        return "factor"
    return normalized


def _factor_id_values(value: str) -> set[str]:
    normalized = value.strip().lower().replace("-", "_")
    values = {normalized}
    values.update(_worldquant_aliases(normalized))
    return values


def _formula_signature(factor_id: str, formula: str, universe_filters: tuple[str, ...]) -> str:
    payload = {
        "factor_id": factor_id,
        "formula": formula.strip(),
        "universe_filters": list(universe_filters),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _score_source(cached_rows: int, computed_rows: int) -> str:
    if computed_rows:
        return "factor_values_incremental"
    if cached_rows:
        return "factor_values_cached"
    return "computed_formula"


def _cache_only_source(cached_rows: int, required_rows: int) -> str:
    if cached_rows <= 0:
        return "factor_values_missing"
    if required_rows > 0 and cached_rows < required_rows:
        return "factor_values_cached_partial"
    return "factor_values_cached"

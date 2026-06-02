"""Local factor value store adapter for cache-first score preparation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

import pandas as pd

from quant_forge.factor_engine.executor import execute_factor_formula


@dataclass(frozen=True)
class FactorScoreResult:
    scores: pd.DataFrame
    source: str
    cached_rows: int
    computed_rows: int
    factor_values_path: Path | None = None


class FactorValueStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()

    def prepare_scores(
        self,
        panel: pd.DataFrame,
        *,
        factor_id: str,
        factor_name: str,
        formula: str,
        universe_filters: tuple[str, ...],
    ) -> FactorScoreResult:
        factor_dir = self._resolve_factor_dir(factor_id=factor_id, factor_name=factor_name)
        formula_signature = _formula_signature(factor_id, formula, universe_filters)
        cached = self._load_cached_scores(
            factor_dir,
            factor_id=factor_id,
            formula_signature=formula_signature,
        )
        panel_keys = _score_keys(panel)
        required_keys = _required_score_keys(panel, universe_filters)
        cached_for_panel = _restrict_to_panel(cached, panel_keys)
        complete_dates = _complete_cached_dates(required_keys, cached_for_panel)
        cached_complete = cached_for_panel[cached_for_panel["trade_date"].isin(complete_dates)]
        missing_panel = panel[~panel["trade_date"].isin(complete_dates)]

        computed = (
            execute_factor_formula(missing_panel, formula, universe_filters)
            if not missing_panel.empty
            else _empty_scores()
        )
        if not computed.empty:
            self._write_incremental_scores(
                factor_dir,
                factor_id=factor_id,
                factor_name=factor_name,
                formula_signature=formula_signature,
                scores=computed,
            )

        combined = pd.concat([cached_complete, computed], ignore_index=True)
        if combined.empty:
            combined = _empty_scores()
        else:
            combined = _restrict_to_panel(combined, panel_keys)
            combined = _apply_universe_filters(panel, combined, universe_filters)
            combined = combined.sort_values(["trade_date", "instrument"]).reset_index(drop=True)

        cached_rows = int(len(cached_complete))
        computed_rows = int(len(computed))
        source = _score_source(cached_rows, computed_rows)
        return FactorScoreResult(
            scores=combined,
            source=source,
            cached_rows=cached_rows,
            computed_rows=computed_rows,
            factor_values_path=factor_dir,
        )

    def _resolve_factor_dir(self, *, factor_id: str, factor_name: str) -> Path:
        candidates = _factor_dir_candidates(factor_id=factor_id, factor_name=factor_name)
        existing = _find_existing_factor_dir(self.root, candidates)
        return existing or self.root / _safe_dir_name(factor_name or factor_id)

    def _load_cached_scores(self, factor_dir: Path, *, factor_id: str, formula_signature: str) -> pd.DataFrame:
        if not factor_dir.exists():
            return _empty_scores()
        frames: list[pd.DataFrame] = []
        for path in _factor_value_files(factor_dir):
            frame = _read_score_file(
                path,
                factor_id=factor_id,
                formula_signature=formula_signature,
            )
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return _empty_scores()
        return _dedupe_scores(pd.concat(frames, ignore_index=True))

    def _write_incremental_scores(
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


def _score_keys(panel: pd.DataFrame) -> pd.DataFrame:
    keys = panel[["trade_date", "instrument"]].copy()
    keys["trade_date"] = pd.to_datetime(keys["trade_date"])
    keys["instrument"] = keys["instrument"].astype(str)
    return keys.drop_duplicates().reset_index(drop=True)


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


def _factor_value_files(factor_dir: Path) -> list[Path]:
    files = [path for path in sorted(factor_dir.glob("*.parquet")) if not path.name.startswith("._")]
    incremental_dir = factor_dir / "incremental"
    if incremental_dir.exists():
        files.extend(path for path in sorted(incremental_dir.glob("*.parquet")) if not path.name.startswith("._"))
    return files


def _read_score_file(path: Path, *, factor_id: str | None = None, formula_signature: str | None = None) -> pd.DataFrame:
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
        elif path.parent.name == "incremental":
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


def _complete_cached_dates(target_keys: pd.DataFrame, cached: pd.DataFrame) -> set[pd.Timestamp]:
    if target_keys.empty or cached.empty:
        return set()
    required_by_date = _instrument_sets_by_date(target_keys)
    cached_non_null = _dedupe_scores(cached).dropna(subset=["score"])
    available_by_date = _instrument_sets_by_date(cached_non_null)
    return {
        date
        for date, required in required_by_date.items()
        if required.issubset(available_by_date.get(date, frozenset()))
    }


def _instrument_sets_by_date(keys: pd.DataFrame) -> pd.Series:
    if keys.empty:
        return pd.Series(dtype="object")
    normalized = _score_keys(keys)
    return normalized.groupby("trade_date")["instrument"].agg(frozenset)


def _apply_universe_filters(
    panel: pd.DataFrame,
    scores: pd.DataFrame,
    universe_filters: tuple[str, ...],
) -> pd.DataFrame:
    if not universe_filters or scores.empty:
        return scores
    mask = execute_factor_formula(panel, "close", universe_filters)
    allowed = set(
        zip(
            pd.to_datetime(mask.loc[mask["score"].notna(), "trade_date"]),
            mask.loc[mask["score"].notna(), "instrument"].astype(str),
            strict=True,
        )
    )
    result = scores.copy()
    keys = zip(result["trade_date"], result["instrument"].astype(str), strict=True)
    result.loc[[key not in allowed for key in keys], "score"] = pd.NA
    return result


def _merge_score_updates(existing: pd.DataFrame, updates: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return _dedupe_scores(updates)
    update_keys = set(zip(updates["trade_date"], updates["instrument"].astype(str), strict=True))
    keep = [
        key not in update_keys
        for key in zip(existing["trade_date"], existing["instrument"].astype(str), strict=True)
    ]
    return _dedupe_scores(pd.concat([existing.loc[keep], updates], ignore_index=True))


def _dedupe_scores(scores: pd.DataFrame) -> pd.DataFrame:
    if scores.empty:
        return _empty_scores()
    result = scores[["trade_date", "instrument", "score"]].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result["instrument"] = result["instrument"].astype(str)
    result["score"] = pd.to_numeric(result["score"], errors="coerce")
    return result.drop_duplicates(["trade_date", "instrument"], keep="last").reset_index(drop=True)


def _find_existing_factor_dir(root: Path, candidates: list[str]) -> Path | None:
    if not root.exists():
        return None
    for candidate in candidates:
        direct = root / candidate if _is_child_name(candidate) else None
        if direct is not None and direct.is_dir():
            return direct
    children = {child.name.lower(): child for child in root.iterdir() if child.is_dir()}
    for candidate in candidates:
        match = children.get(candidate.lower())
        if match is not None:
            return match
    return None


def _is_child_name(value: str) -> bool:
    path = Path(value)
    return not path.is_absolute() and len(path.parts) == 1


def _factor_dir_candidates(*, factor_id: str, factor_name: str) -> list[str]:
    raw_candidates = [factor_name, factor_id, _safe_dir_name(factor_name), _safe_dir_name(factor_id)]
    aliases: list[str] = []
    for value in raw_candidates:
        aliases.extend(_worldquant_aliases(value))
    candidates = aliases + raw_candidates
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _worldquant_aliases(value: str) -> list[str]:
    normalized = value.strip().lower().replace("-", "_")
    match = re.search(r"(?:worldquant_)?(?:wq_)?alpha_?0*(\d+)", normalized)
    if match is None:
        return []
    number = int(match.group(1))
    padded = f"{number:03d}"
    return [f"worldquant_alpha_{padded}", f"wq_alpha_{padded}", f"alpha_{padded}"]


def _safe_dir_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.=-]+", "_", value.strip())
    return normalized.strip("_") or "factor"


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

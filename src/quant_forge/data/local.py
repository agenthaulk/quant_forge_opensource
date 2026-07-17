"""Local parquet data provider, data catalog, and demo data generation.

Data catalog (CP5-1, owner decision D2)
---------------------------------------
``PANEL_FIELD_CATALOG`` is the single authoritative definition of the panel
schema this provider loads (FP-5). Everything else derives from it: the
required/optional column sets used by validation, the field surface advertised
by ``quant_forge.mcp.read_models.list_available_fields`` (and therefore what
the specs ValidationGate accepts), and the research metadata tags (CP5-2).

Field expansion path (CP5-3)
----------------------------
To add a new catalog field:

1. Append one ``CatalogField`` entry to ``PANEL_FIELD_CATALOG`` (declarative
   extension manifests may feed this the same way later, decision D7).
2. Nothing else is edited by hand: validation, the advertised MCP/LLM field
   surface, and the ValidationGate all consult the catalog at call time.
3. Provide the data: either the column exists in ``panel.parquet`` /
   the source snapshot, or — for loader-derived fields such as ``return_5d``
   — extend the loader derivation in this module.
4. Verify declared-vs-actually-available with
   ``catalog_field_availability(provider.validate())``: every declared field
   reports ``available``, ``missing`` or ``synthesized`` (statuses, never
   silent booleans — FP-7). ``undeclared_panel_columns`` covers the reverse
   direction (data carries a column the catalog does not declare).

Tests: ``tests/test_data_catalog_port.py``,
``tests/test_data_catalog_expansion.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from quant_forge.core.contracts import DataValidationResult, FactorDefinition
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.factor_library.research_tags import ResearchTags

PANEL_FILE = "panel.parquet"


@dataclass(frozen=True)
class CatalogField:
    """One declared panel field: schema role plus research metadata tags."""

    name: str
    description: str
    role: str  # "key" | "required" | "optional"
    tags: ResearchTags

    def __post_init__(self) -> None:
        if self.role not in ("key", "required", "optional"):
            raise ValueError(f"invalid catalog field role: {self.role}")
        if self.tags.subject_id != self.name or self.tags.subject_kind != "field":
            raise ValueError(f"catalog field tags must describe the field itself: {self.name}")


def _field_tags(
    name: str,
    *,
    themes: tuple[str, ...] = (),
    columns_required: tuple[str, ...] = (),
    min_warmup_bars: int | None = None,
    notes: str | None = None,
) -> ResearchTags:
    return ResearchTags(
        subject_kind="field",
        subject_id=name,
        themes=themes,
        columns_required=columns_required,
        frequency="daily",
        min_warmup_bars=min_warmup_bars,
        notes=notes,
        provenance="catalog",
    )


# The one authoritative panel field catalog (FP-5). Warmup bars are facts of
# the loader derivations below (pct_change / rolling windows), not estimates.
PANEL_FIELD_CATALOG: tuple[CatalogField, ...] = (
    CatalogField(
        name="trade_date",
        description="Trading date key column.",
        role="key",
        tags=_field_tags("trade_date", min_warmup_bars=1),
    ),
    CatalogField(
        name="instrument",
        description="Instrument identifier key column.",
        role="key",
        tags=_field_tags("instrument", min_warmup_bars=1),
    ),
    CatalogField(
        name="close",
        description="Adjusted close or close-like local demo price.",
        role="required",
        tags=_field_tags("close", themes=("price",), min_warmup_bars=1),
    ),
    CatalogField(
        name="open",
        description="Adjusted opening price on the same price basis as close.",
        role="optional",
        tags=_field_tags(
            "open",
            themes=("price",),
            min_warmup_bars=1,
            notes="Same adjusted price basis as close; do not mix with a raw-price close.",
        ),
    ),
    CatalogField(
        name="high",
        description="Adjusted intraday high on the same price basis as close.",
        role="optional",
        tags=_field_tags(
            "high",
            themes=("price",),
            min_warmup_bars=1,
            notes="Same adjusted price basis as close; do not mix with a raw-price close.",
        ),
    ),
    CatalogField(
        name="low",
        description="Adjusted intraday low on the same price basis as close.",
        role="optional",
        tags=_field_tags(
            "low",
            themes=("price",),
            min_warmup_bars=1,
            notes="Same adjusted price basis as close; do not mix with a raw-price close.",
        ),
    ),
    CatalogField(
        name="market_cap",
        description="Point-in-time market capitalization supplied by local data.",
        role="required",
        tags=_field_tags("market_cap", themes=("size",), min_warmup_bars=1),
    ),
    CatalogField(
        name="is_st",
        description="Boolean risk flag; use as a universe filter, not a numeric factor field.",
        role="required",
        tags=_field_tags(
            "is_st",
            themes=("risk_flag",),
            min_warmup_bars=1,
            notes="Universe-filter flag; not a numeric factor input.",
        ),
    ),
    CatalogField(
        name="volume",
        description="Local demo trading volume.",
        role="optional",
        tags=_field_tags("volume", themes=("liquidity",), min_warmup_bars=1),
    ),
    CatalogField(
        name="amount",
        description="Daily traded turnover (value); a VWAP proxy is amount / volume.",
        role="optional",
        tags=_field_tags(
            "amount",
            themes=("liquidity",),
            min_warmup_bars=1,
            notes="Turnover value in the same currency basis as price*volume.",
        ),
    ),
    CatalogField(
        name="return_1d",
        description="One-day trailing return derived from local close data.",
        role="optional",
        tags=_field_tags(
            "return_1d",
            themes=("returns",),
            columns_required=("close",),
            min_warmup_bars=2,
        ),
    ),
    CatalogField(
        name="return_5d",
        description="Five-day trailing return derived from local close data.",
        role="optional",
        tags=_field_tags(
            "return_5d",
            themes=("returns", "momentum"),
            columns_required=("close",),
            min_warmup_bars=6,
        ),
    ),
    CatalogField(
        name="volatility_5d",
        description="Five-day trailing return volatility.",
        role="optional",
        tags=_field_tags(
            "volatility_5d",
            themes=("volatility",),
            columns_required=("close",),
            min_warmup_bars=3,
        ),
    ),
)


def data_field_catalog() -> tuple[CatalogField, ...]:
    """The authoritative field catalog, read at call time (CP5-1).

    Callers (validation below, ``mcp.read_models``, the specs ValidationGate
    behind it) must go through this accessor so a catalog extension is visible
    everywhere at once and the advertised surface cannot drift from what the
    provider actually loads (FP-5).
    """

    return PANEL_FIELD_CATALOG


def _required_column_names() -> tuple[str, ...]:
    return tuple(item.name for item in data_field_catalog() if item.role in ("key", "required"))


def _optional_column_names() -> tuple[str, ...]:
    return tuple(item.name for item in data_field_catalog() if item.role == "optional")


# Import-time snapshots kept for external readers; the provider itself uses
# the call-time accessors above.
REQUIRED_COLUMNS = _required_column_names()
OPTIONAL_COLUMNS = _optional_column_names()
SOURCE_PRICE_COLUMNS = ("ts_code", "trade_date", "close", "vol")
SOURCE_DAILY_BASIC_COLUMNS = ("ts_code", "trade_date", "total_mv", "circ_mv")

FIELD_AVAILABLE = "available"
FIELD_MISSING = "missing"
FIELD_SYNTHESIZED = "synthesized"


@dataclass(frozen=True)
class FieldAvailability:
    """Declared catalog field vs what the data root actually provides (FP-7)."""

    name: str
    role: str
    status: str  # FIELD_AVAILABLE | FIELD_MISSING | FIELD_SYNTHESIZED


def catalog_field_availability(validation: DataValidationResult) -> tuple[FieldAvailability, ...]:
    """Compare declared non-key catalog fields against a validation result.

    ``synthesized`` means the loader fills the column without full source
    backing (see ``DataValidationResult.synthesized_columns``); ``missing``
    means the declared field is not provided at all. Availability is about
    presence — data-quality problems are already carried by
    ``DataValidationResult.ok`` / ``missing_columns`` tokens.
    """

    availability: list[FieldAvailability] = []
    synthesized = set(validation.synthesized_columns)
    missing = set(validation.missing_columns)
    optional_present = set(validation.optional_columns)
    for item in data_field_catalog():
        if item.role == "key":
            continue
        if item.name in synthesized:
            status = FIELD_SYNTHESIZED
        elif item.role == "required":
            status = FIELD_MISSING if item.name in missing or validation.rows == 0 else FIELD_AVAILABLE
        else:
            status = FIELD_AVAILABLE if item.name in optional_present else FIELD_MISSING
        availability.append(FieldAvailability(name=item.name, role=item.role, status=status))
    return tuple(availability)


def undeclared_panel_columns(columns: Iterable[str]) -> tuple[str, ...]:
    """Columns present in the data but not declared by the catalog (CP5-3)."""

    declared = {item.name for item in data_field_catalog()}
    return tuple(sorted(set(columns) - declared))


class LocalPanelDataProvider:
    """Read a local equity panel or a mounted source snapshot."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.expanduser()

    @property
    def panel_path(self) -> Path:
        return resolve_panel_path(self.data_root)

    def validate(self) -> DataValidationResult:
        if not self.panel_path.exists():
            source_root = resolve_source_snapshot_root(self.data_root)
            if source_root is not None:
                return _validate_source_snapshot(self.data_root, source_root)
            return DataValidationResult(
                data_root=self.data_root,
                ok=False,
                rows=0,
                instruments=0,
                date_count=0,
                missing_columns=_required_column_names(),
                panel_path=self.panel_path,
            )
        panel = pd.read_parquet(self.panel_path)
        missing = tuple(column for column in _required_column_names() if column not in panel.columns)
        dates = pd.to_datetime(panel["trade_date"]) if "trade_date" in panel.columns and not panel.empty else None
        problems = _panel_quality_problems(panel) if not missing and not panel.empty else ()
        return DataValidationResult(
            data_root=self.data_root,
            ok=not missing and not panel.empty and not problems,
            rows=len(panel),
            instruments=int(panel["instrument"].nunique()) if "instrument" in panel.columns else 0,
            date_count=int(panel["trade_date"].nunique()) if "trade_date" in panel.columns else 0,
            missing_columns=missing + problems,
            panel_path=self.panel_path,
            start_date=dates.min().date().isoformat() if dates is not None else "",
            end_date=dates.max().date().isoformat() if dates is not None else "",
            optional_columns=tuple(column for column in _optional_column_names() if column in panel.columns),
        )

    def load_panel(self) -> pd.DataFrame:
        if self.panel_path.exists():
            validation = self.validate()
            if not validation.ok:
                missing = ", ".join(validation.missing_columns) or "no rows"
                raise ValueError(f"invalid data_root {self.data_root}: missing {missing}")
            panel = pd.read_parquet(self.panel_path)
            panel = panel.copy()
            panel["trade_date"] = pd.to_datetime(panel["trade_date"])
            panel["is_st"] = panel["is_st"].astype(bool)
            return panel.sort_values(["trade_date", "instrument"]).reset_index(drop=True)
        source_root = resolve_source_snapshot_root(self.data_root)
        if source_root is None:
            validation = self.validate()
            missing = ", ".join(validation.missing_columns) or "no rows"
            raise ValueError(f"invalid data_root {self.data_root}: missing {missing}")
        return _load_source_snapshot_panel(source_root)


def _panel_quality_problems(panel: pd.DataFrame) -> tuple[str, ...]:
    """Detect data-quality issues that presence/non-empty checks miss.

    Returns problem tokens surfaced through ``missing_columns`` so that
    ``load_panel`` renders them and downstream callers treat the panel as
    invalid. Covers duplicate keys, NaNs in required columns, and dtype issues.
    Defensive: any check that raises is skipped rather than crashing validate.
    """

    problems: list[str] = []
    try:
        if panel.duplicated(subset=["trade_date", "instrument"]).any():
            problems.append("duplicate_keys")
    except Exception:  # pragma: no cover - defensive
        pass
    for column in _required_column_names():
        try:
            if panel[column].isna().any():
                problems.append(f"null:{column}")
        except Exception:  # pragma: no cover - defensive
            pass
    for numeric_column in ("close", "market_cap"):
        try:
            if not pd.api.types.is_numeric_dtype(panel[numeric_column]):
                problems.append(f"dtype:{numeric_column}")
        except Exception:  # pragma: no cover - defensive
            pass
    try:
        parsed_dates = pd.to_datetime(panel["trade_date"], errors="coerce")
        if parsed_dates.isna().any() and not panel["trade_date"].isna().any():
            problems.append("dtype:trade_date")
    except Exception:  # pragma: no cover - defensive
        pass
    return tuple(problems)


def validate_data_root(data_root: Path) -> DataValidationResult:
    return LocalPanelDataProvider(data_root).validate()


def resolve_panel_path(data_root: Path) -> Path:
    root = data_root.expanduser()
    if root.is_file():
        return root
    direct = root / PANEL_FILE
    if direct.exists():
        return direct
    nested_data = root / "data" / PANEL_FILE
    if nested_data.exists():
        return nested_data
    return direct


def resolve_source_snapshot_root(data_root: Path) -> Path | None:
    root = data_root.expanduser()
    candidates = (
        root,
        root / "source_snapshot" / "provider=tencent_cos_snapshot" / "market=cn_a",
    )
    for candidate in candidates:
        if _is_source_snapshot_root(candidate):
            return candidate
    source_root = root / "source_snapshot"
    if source_root.exists():
        for candidate in sorted(source_root.glob("provider=*/market=*")):
            if _is_source_snapshot_root(candidate):
                return candidate
    return None


def _is_source_snapshot_root(path: Path) -> bool:
    return (path / "price").is_dir() and (path / "daily_basic").is_dir()


def _validate_source_snapshot(data_root: Path, source_root: Path) -> DataValidationResult:
    try:
        price = _read_snapshot_files(source_root / "price", columns=SOURCE_PRICE_COLUMNS)
    except Exception as exc:
        return DataValidationResult(
            data_root=data_root,
            ok=False,
            rows=0,
            instruments=0,
            date_count=0,
            missing_columns=("price",),
            panel_path=source_root,
            optional_columns=(f"source_snapshot_error={exc}",),
        )
    try:
        daily_basic = _read_snapshot_files(source_root / "daily_basic", columns=SOURCE_DAILY_BASIC_COLUMNS)
    except Exception as exc:
        return DataValidationResult(
            data_root=data_root,
            ok=False,
            rows=0,
            instruments=0,
            date_count=0,
            missing_columns=("daily_basic",),
            panel_path=source_root,
            optional_columns=(f"source_snapshot_error={exc}",),
        )
    if price.empty:
        return DataValidationResult(
            data_root=data_root,
            ok=False,
            rows=0,
            instruments=0,
            date_count=0,
            missing_columns=("price",),
            panel_path=source_root,
        )
    price = price.drop_duplicates()
    daily_basic = daily_basic.drop_duplicates()
    duplicate_problems = tuple(
        f"duplicate_{label}_keys"
        for label, frame in (("price", price), ("daily_basic", daily_basic))
        if not frame.empty and bool(frame.duplicated(subset=["trade_date", "ts_code"], keep=False).any())
    )
    if duplicate_problems:
        return DataValidationResult(
            data_root=data_root,
            ok=False,
            rows=len(price),
            instruments=int(price["ts_code"].nunique()),
            date_count=int(price["trade_date"].nunique()),
            missing_columns=duplicate_problems,
            panel_path=source_root,
        )
    dates = pd.to_datetime(price["trade_date"].astype(str), errors="coerce")
    return DataValidationResult(
        data_root=data_root,
        ok=True,
        rows=len(price),
        instruments=int(price["ts_code"].nunique()),
        date_count=int(price["trade_date"].nunique()),
        missing_columns=(),
        panel_path=source_root,
        start_date=dates.min().date().isoformat() if not dates.empty else "",
        end_date=dates.max().date().isoformat() if not dates.empty else "",
        optional_columns=("source_snapshot", "volume", "return_1d", "return_5d", "volatility_5d"),
        synthesized_columns=_snapshot_synthesized_columns(price, daily_basic),
    )


def _snapshot_synthesized_columns(price: pd.DataFrame, daily_basic: pd.DataFrame) -> tuple[str, ...]:
    """Report panel columns the snapshot loader cannot fully back with source data.

    ``is_st`` is always synthesized: the snapshot schema carries no ST flag, so
    ``load_panel`` fills ``False`` for every row. ``market_cap`` is reported when
    ``total_mv``/``circ_mv`` coverage is incomplete for the price rows, in which
    case the loader leaves the affected rows as NaN instead of inventing a value.
    """

    if daily_basic.empty:
        return ("is_st", "market_cap")
    total_mv = pd.to_numeric(daily_basic["total_mv"], errors="coerce")
    circ_mv = pd.to_numeric(daily_basic["circ_mv"], errors="coerce")
    if total_mv.fillna(circ_mv).isna().any():
        return ("is_st", "market_cap")
    price_keys = pd.MultiIndex.from_frame(price[["trade_date", "ts_code"]].astype(str))
    basic_keys = pd.MultiIndex.from_frame(daily_basic[["trade_date", "ts_code"]].astype(str))
    if not price_keys.isin(basic_keys).all():
        return ("is_st", "market_cap")
    return ("is_st",)


def _reject_conflicting_snapshot_keys(frame: pd.DataFrame, label: str) -> None:
    if frame.empty:
        return
    # Exact duplicate rows were already dropped; anything still duplicated on
    # the key carries conflicting values, and picking one would fabricate data.
    if bool(frame.duplicated(subset=["trade_date", "ts_code"], keep=False).any()):
        raise ValueError(
            f"source snapshot {label} files contain conflicting duplicate (trade_date, ts_code) rows"
        )


def _load_source_snapshot_panel(source_root: Path) -> pd.DataFrame:
    price = _read_snapshot_files(source_root / "price", columns=SOURCE_PRICE_COLUMNS)
    daily_basic = _read_snapshot_files(
        source_root / "daily_basic",
        columns=SOURCE_DAILY_BASIC_COLUMNS,
    )
    if price.empty:
        raise ValueError(f"source snapshot has no price rows: {source_root}")
    price = price.drop_duplicates()
    daily_basic = daily_basic.drop_duplicates()
    _reject_conflicting_snapshot_keys(price, "price")
    _reject_conflicting_snapshot_keys(daily_basic, "daily_basic")
    panel = price.rename(columns={"ts_code": "instrument", "vol": "volume"}).copy()
    if not daily_basic.empty:
        fundamentals = daily_basic.rename(columns={"ts_code": "instrument"})
        panel = panel.merge(fundamentals, on=["trade_date", "instrument"], how="left")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"].astype(str), errors="coerce")
    panel["instrument"] = panel["instrument"].astype(str)
    panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
    panel["volume"] = pd.to_numeric(panel.get("volume", 0.0), errors="coerce").fillna(0.0)
    total_mv = pd.to_numeric(panel.get("total_mv"), errors="coerce") if "total_mv" in panel else pd.Series(index=panel.index)
    circ_mv = pd.to_numeric(panel.get("circ_mv"), errors="coerce") if "circ_mv" in panel else pd.Series(index=panel.index)
    panel["market_cap"] = total_mv.fillna(circ_mv)
    panel["is_st"] = False
    panel = panel.dropna(subset=["trade_date", "instrument", "close"])
    panel = panel.sort_values(["instrument", "trade_date"]).reset_index(drop=True)
    panel["return_1d"] = panel.groupby("instrument")["close"].pct_change()
    panel["return_5d"] = panel.groupby("instrument")["close"].pct_change(5)
    panel["volatility_5d"] = (
        panel.groupby("instrument")["return_1d"]
        .rolling(5, min_periods=2)
        .std()
        .reset_index(level=0, drop=True)
    )
    return panel[
        [
            "trade_date",
            "instrument",
            "close",
            "market_cap",
            "is_st",
            "volume",
            "return_1d",
            "return_5d",
            "volatility_5d",
        ]
    ].sort_values(["trade_date", "instrument"]).reset_index(drop=True)


def _read_snapshot_files(root: Path, *, columns: tuple[str, ...]) -> pd.DataFrame:
    files = [path for path in sorted(root.glob("*.parquet")) if not path.name.startswith("._")]
    frames = [pd.read_parquet(path, columns=list(columns)) for path in files]
    if not frames:
        return pd.DataFrame(columns=list(columns))
    return pd.concat(frames, ignore_index=True)


def create_demo_workspace(
    workspace: Path | None = None,
    *,
    data_root: Path | None = None,
    factor_root: Path | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Path]:
    """Create deterministic public demo data and factor definitions."""

    workspace = (workspace or Path("qf_demo")).expanduser()
    data_root = (data_root or workspace / "data").expanduser()
    factor_root = (factor_root or workspace / "factor_root").expanduser()
    artifact_root = (artifact_root or workspace / "artifacts").expanduser()
    data_root.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)

    panel = _build_demo_panel()
    panel.to_parquet(data_root / PANEL_FILE, index=False)

    repo = FactorRepository(factor_root)
    repo.ensure_layout()
    repo.save(
        FactorDefinition(
            factor_id="FTR_DEMO_SMALL_CAP",
            name="demo_small_cap",
            formula="-rank(market_cap)",
            status="candidate",
            description="Small market-cap stocks receive higher scores.",
            horizon_days=5,
            universe_filters=("is_st == false",),
            source="demo",
        )
    )
    repo.save(
        FactorDefinition(
            factor_id="FTR_DEMO_MOMENTUM",
            name="demo_momentum_5d",
            formula="rank(return_5d)",
            status="candidate",
            description="Five-day momentum receives higher scores.",
            horizon_days=5,
            source="demo",
        )
    )
    return {"workspace": workspace, "data_root": data_root, "factor_root": factor_root, "artifact_root": artifact_root}


def _build_demo_panel() -> pd.DataFrame:
    instruments = [f"STK{i:03d}" for i in range(1, 13)]
    dates = pd.bdate_range("2024-01-02", periods=160)
    rows: list[dict[str, object]] = []
    for instrument_index, instrument in enumerate(instruments):
        base_close = 10.0 + instrument_index
        base_cap = 5_000_000_000 + instrument_index * 750_000_000
        for day_index, trade_date in enumerate(dates):
            drift = 0.002 * day_index
            seasonal = np.sin((day_index + instrument_index) / 4.0) * 0.015
            small_cap_tilt = (len(instruments) - instrument_index) * 0.0008 * day_index
            close = base_close * (1.0 + drift + seasonal + small_cap_tilt)
            volume = float(1_000_000 + instrument_index * 50_000 + day_index * 2_000)
            # Intraday range and turnover on the SAME adjusted basis as close
            # (deterministic demo values, not estimates): high/low bracket close
            # and open sits inside [low, high]; amount is a price*volume turnover.
            high_gap = 0.012 + 0.006 * abs(float(np.sin((day_index + instrument_index) / 5.0)))
            low_gap = 0.012 + 0.006 * abs(float(np.cos((day_index - instrument_index) / 7.0)))
            high = close * (1.0 + high_gap)
            low = close * (1.0 - low_gap)
            open_fraction = 0.5 + 0.35 * float(np.sin((day_index * 2 + instrument_index) / 6.0))
            open_price = low + open_fraction * (high - low)
            rows.append(
                {
                    "trade_date": trade_date.date().isoformat(),
                    "instrument": instrument,
                    "open": round(float(open_price), 6),
                    "high": round(float(high), 6),
                    "low": round(float(low), 6),
                    "close": round(float(close), 6),
                    "market_cap": float(base_cap * (1.0 + seasonal)),
                    "is_st": bool(instrument_index in {1, 8} and day_index >= 10),
                    "volume": volume,
                    "amount": round(float(close) * volume, 2),
                }
            )
    panel = pd.DataFrame(rows)
    panel = panel.sort_values(["instrument", "trade_date"])
    # F-4: warmup rows stay NaN. Filling 0.0 would fabricate observations that
    # enter cross-sectional ranks as real data (FP-4); nan_policy=drop already
    # handles missing values downstream, matching the source-snapshot loader.
    panel["return_1d"] = panel.groupby("instrument")["close"].pct_change()
    panel["return_5d"] = panel.groupby("instrument")["close"].pct_change(5)
    panel["volatility_5d"] = (
        panel.groupby("instrument")["return_1d"]
        .rolling(5, min_periods=2)
        .std()
        .reset_index(level=0, drop=True)
    )
    return panel.sort_values(["trade_date", "instrument"]).reset_index(drop=True)

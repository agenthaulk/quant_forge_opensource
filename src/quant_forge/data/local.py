"""Local parquet data provider and demo data generation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from quant_forge.core.contracts import DataValidationResult, FactorDefinition
from quant_forge.factor_library.repository import FactorRepository

PANEL_FILE = "panel.parquet"
REQUIRED_COLUMNS = ("trade_date", "instrument", "close", "market_cap", "is_st")
OPTIONAL_COLUMNS = ("volume", "return_1d", "return_5d", "volatility_5d")
SOURCE_PRICE_COLUMNS = ("ts_code", "trade_date", "close", "vol")
SOURCE_DAILY_BASIC_COLUMNS = ("ts_code", "trade_date", "total_mv", "circ_mv")


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
                missing_columns=REQUIRED_COLUMNS,
                panel_path=self.panel_path,
            )
        panel = pd.read_parquet(self.panel_path)
        missing = tuple(column for column in REQUIRED_COLUMNS if column not in panel.columns)
        dates = pd.to_datetime(panel["trade_date"]) if "trade_date" in panel.columns and not panel.empty else None
        return DataValidationResult(
            data_root=self.data_root,
            ok=not missing and not panel.empty,
            rows=len(panel),
            instruments=int(panel["instrument"].nunique()) if "instrument" in panel.columns else 0,
            date_count=int(panel["trade_date"].nunique()) if "trade_date" in panel.columns else 0,
            missing_columns=missing,
            panel_path=self.panel_path,
            start_date=dates.min().date().isoformat() if dates is not None else "",
            end_date=dates.max().date().isoformat() if dates is not None else "",
            optional_columns=tuple(column for column in OPTIONAL_COLUMNS if column in panel.columns),
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
        _read_snapshot_files(source_root / "daily_basic", columns=SOURCE_DAILY_BASIC_COLUMNS)
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
    )


def _load_source_snapshot_panel(source_root: Path) -> pd.DataFrame:
    price = _read_snapshot_files(source_root / "price", columns=SOURCE_PRICE_COLUMNS)
    daily_basic = _read_snapshot_files(
        source_root / "daily_basic",
        columns=SOURCE_DAILY_BASIC_COLUMNS,
    )
    if price.empty:
        raise ValueError(f"source snapshot has no price rows: {source_root}")
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
    panel["market_cap"] = total_mv.fillna(circ_mv).fillna(1.0)
    panel["is_st"] = False
    panel = panel.dropna(subset=["trade_date", "instrument", "close"])
    panel = panel.sort_values(["instrument", "trade_date"]).reset_index(drop=True)
    panel["return_1d"] = panel.groupby("instrument")["close"].pct_change().fillna(0.0)
    panel["return_5d"] = panel.groupby("instrument")["close"].pct_change(5).fillna(0.0)
    panel["volatility_5d"] = (
        panel.groupby("instrument")["return_1d"]
        .rolling(5, min_periods=2)
        .std()
        .reset_index(level=0, drop=True)
        .fillna(0.0)
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
    dates = pd.bdate_range("2024-01-02", periods=32)
    rows: list[dict[str, object]] = []
    for instrument_index, instrument in enumerate(instruments):
        base_close = 10.0 + instrument_index
        base_cap = 5_000_000_000 + instrument_index * 750_000_000
        for day_index, trade_date in enumerate(dates):
            drift = 0.002 * day_index
            seasonal = np.sin((day_index + instrument_index) / 4.0) * 0.015
            small_cap_tilt = (len(instruments) - instrument_index) * 0.0008 * day_index
            close = base_close * (1.0 + drift + seasonal + small_cap_tilt)
            rows.append(
                {
                    "trade_date": trade_date.date().isoformat(),
                    "instrument": instrument,
                    "close": round(float(close), 6),
                    "market_cap": float(base_cap * (1.0 + seasonal)),
                    "is_st": bool(instrument_index in {1, 8} and day_index >= 10),
                    "volume": float(1_000_000 + instrument_index * 50_000 + day_index * 2_000),
                }
            )
    panel = pd.DataFrame(rows)
    panel = panel.sort_values(["instrument", "trade_date"])
    panel["return_1d"] = panel.groupby("instrument")["close"].pct_change().fillna(0.0)
    panel["return_5d"] = panel.groupby("instrument")["close"].pct_change(5).fillna(0.0)
    panel["volatility_5d"] = (
        panel.groupby("instrument")["return_1d"]
        .rolling(5, min_periods=2)
        .std()
        .reset_index(level=0, drop=True)
        .fillna(0.0)
    )
    return panel.sort_values(["trade_date", "instrument"]).reset_index(drop=True)

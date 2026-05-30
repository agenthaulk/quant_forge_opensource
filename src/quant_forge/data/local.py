"""Local parquet data provider and demo data generation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from quant_forge.core.contracts import DataValidationResult, FactorDefinition
from quant_forge.factor_library.repository import FactorRepository

PANEL_FILE = "panel.parquet"
REQUIRED_COLUMNS = ("trade_date", "instrument", "close", "market_cap", "is_st")


class LocalPanelDataProvider:
    """Read a simple local equity panel from `data_root/panel.parquet`."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.expanduser()

    @property
    def panel_path(self) -> Path:
        return self.data_root / PANEL_FILE

    def validate(self) -> DataValidationResult:
        if not self.panel_path.exists():
            return DataValidationResult(
                data_root=self.data_root,
                ok=False,
                rows=0,
                instruments=0,
                date_count=0,
                missing_columns=REQUIRED_COLUMNS,
            )
        panel = pd.read_parquet(self.panel_path)
        missing = tuple(column for column in REQUIRED_COLUMNS if column not in panel.columns)
        return DataValidationResult(
            data_root=self.data_root,
            ok=not missing and not panel.empty,
            rows=len(panel),
            instruments=int(panel["instrument"].nunique()) if "instrument" in panel.columns else 0,
            date_count=int(panel["trade_date"].nunique()) if "trade_date" in panel.columns else 0,
            missing_columns=missing,
        )

    def load_panel(self) -> pd.DataFrame:
        validation = self.validate()
        if not validation.ok:
            missing = ", ".join(validation.missing_columns) or "no rows"
            raise ValueError(f"invalid data_root {self.data_root}: missing {missing}")
        panel = pd.read_parquet(self.panel_path)
        panel = panel.copy()
        panel["trade_date"] = pd.to_datetime(panel["trade_date"])
        panel["is_st"] = panel["is_st"].astype(bool)
        return panel.sort_values(["trade_date", "instrument"]).reset_index(drop=True)


def validate_data_root(data_root: Path) -> DataValidationResult:
    return LocalPanelDataProvider(data_root).validate()


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

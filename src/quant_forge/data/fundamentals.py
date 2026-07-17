"""Point-in-time (PIT) fundamental data processing — public, path-free.

Reads locally-materialized financial-statement parquet in the **Tushare-Pro
public schema** (a widely documented open data specification) and expands each
report to a daily point-in-time value: for every ``(instrument, trade_date)``
the value is the latest report whose **announcement date** ``ann_date`` is
on-or-before ``trade_date``. There is no look-ahead — the report period
``end_date`` is NEVER treated as the availability date; only ``ann_date`` is.
A figure becomes usable the day it is announced, not the day the period ends.

Scope / policy: these are pure functions with no hardcoded paths, no provider
names, and no bundled network data provider or ingestion client (public-edition
data policy, AGENTS.md). The caller passes a local ``source_root`` whose actual
location lives in an ignored local config. Daily valuation ratios
(``daily_basic``) are already keyed by ``trade_date`` and need only an equality
merge; statement figures need the ``ann_date`` as-of expansion.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Tushare-Pro public schema key columns (documented open spec, not a secret).
SOURCE_INSTRUMENT_COL = "ts_code"
ANN_DATE_COL = "ann_date"
END_DATE_COL = "end_date"
TRADE_DATE_COL = "trade_date"

# Datasets whose rows are keyed by trade_date (already point-in-time) vs by
# announcement date (need the as-of expansion).
DAILY_DATASETS = ("daily_basic",)
STATEMENT_DATASETS = ("income", "balance", "cashflow", "financial")


@dataclass(frozen=True)
class FundamentalField:
    """One exposed fundamental field and where it comes from.

    ``pit_class`` is ``"daily"`` (trade_date-keyed, equality merge) or
    ``"statement"`` (ann_date-keyed, as-of expansion). ``direction`` records
    whether a higher value is the "good" tail (used only for the LLM formula
    hint / demo factor, never for silent sign flips).
    """

    name: str
    dataset: str
    pit_class: str
    theme: str
    description: str
    direction: str  # "high" (higher = stronger) | "low" (lower = stronger)


# The exposed fundamental field set (per-instrument, PIT-reliable). Names are
# the Tushare-Pro source column names verbatim so the contract is transparent
# and additional columns from the same tables extend this list unchanged.
FUNDAMENTAL_FIELDS: tuple[FundamentalField, ...] = (
    # -- growth (financial indicators, YoY %) --
    FundamentalField("netprofit_yoy", "financial", "statement", "growth", "净利润同比增速（%）。", "high"),
    FundamentalField("dt_netprofit_yoy", "financial", "statement", "growth", "扣非净利润同比增速（%）。", "high"),
    FundamentalField("or_yoy", "financial", "statement", "growth", "营业收入同比增速（%）。", "high"),
    FundamentalField("op_yoy", "financial", "statement", "growth", "营业利润同比增速（%）。", "high"),
    FundamentalField("q_sales_yoy", "financial", "statement", "growth", "单季度营收同比增速（%）。", "high"),
    # -- profitability / quality (financial indicators) --
    FundamentalField("roe", "financial", "statement", "profitability", "净资产收益率 ROE（%）。", "high"),
    FundamentalField("roa", "financial", "statement", "profitability", "总资产收益率 ROA（%）。", "high"),
    FundamentalField("grossprofit_margin", "financial", "statement", "profitability", "毛利率（%）。", "high"),
    FundamentalField("netprofit_margin", "financial", "statement", "profitability", "净利率（%）。", "high"),
    FundamentalField("eps", "financial", "statement", "profitability", "每股收益（元）。", "high"),
    FundamentalField("bps", "financial", "statement", "profitability", "每股净资产（元）。", "high"),
    FundamentalField("assets_turn", "financial", "statement", "quality", "总资产周转率。", "high"),
    # -- leverage (financial indicators) --
    FundamentalField("debt_to_assets", "financial", "statement", "leverage", "资产负债率（%）。", "low"),
    FundamentalField("current_ratio", "financial", "statement", "leverage", "流动比率。", "high"),
    # -- statement levels (income / cashflow) --
    FundamentalField("revenue", "income", "statement", "scale", "营业收入（元）。", "high"),
    FundamentalField("n_income", "income", "statement", "scale", "净利润（元）。", "high"),
    FundamentalField("n_cashflow_act", "cashflow", "statement", "quality", "经营活动现金流量净额（元）。", "high"),
    FundamentalField("free_cashflow", "cashflow", "statement", "quality", "自由现金流（元）。", "high"),
    # -- daily valuation (daily_basic, trade_date-keyed) --
    FundamentalField("pe_ttm", "daily_basic", "daily", "valuation", "市盈率 TTM。低表示便宜。", "low"),
    FundamentalField("pb", "daily_basic", "daily", "valuation", "市净率。低表示便宜。", "low"),
    FundamentalField("ps_ttm", "daily_basic", "daily", "valuation", "市销率 TTM。低表示便宜。", "low"),
    FundamentalField("dv_ttm", "daily_basic", "daily", "valuation", "股息率 TTM（%）。高表示分红丰厚。", "high"),
    FundamentalField("turnover_rate", "daily_basic", "daily", "liquidity", "换手率（%）。", "high"),
    FundamentalField("volume_ratio", "daily_basic", "daily", "liquidity", "量比。", "high"),
)

FUNDAMENTAL_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in FUNDAMENTAL_FIELDS)


def _read_parquet_dir(directory: Path, columns: list[str] | None = None) -> pd.DataFrame:
    """Concat every real parquet under ``directory`` (skipping macOS ``._*``
    AppleDouble resource-fork files, which are not valid parquet)."""

    directory = Path(directory)
    if not directory.exists():
        return pd.DataFrame()
    files = sorted(p for p in directory.rglob("*.parquet") if not p.name.startswith("._"))
    frames: list[pd.DataFrame] = []
    for path in files:
        available = pd.read_parquet(path).columns if columns else None
        use = [c for c in columns if c in available] if columns is not None else None
        frames.append(pd.read_parquet(path, columns=use))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def read_statement(source_root: Path, dataset: str, columns: list[str] | None = None) -> pd.DataFrame:
    """Read one source dataset (e.g. ``financial``) under ``source_root``."""

    return _read_parquet_dir(Path(source_root) / dataset, columns=columns)


def _parse_ymd(series: pd.Series) -> pd.Series:
    """Parse a Tushare ``YYYYMMDD`` string/int date column to datetime."""

    return pd.to_datetime(series.astype("string"), format="%Y%m%d", errors="coerce")


def dedup_announcements(reports: pd.DataFrame) -> pd.DataFrame:
    """One row per ``(instrument, ann_date)``: the most-recent report period
    (largest ``end_date``) announced on that day. Restatements of a period keep
    their own later ``ann_date`` row, so they correctly supersede only from the
    restatement's announcement date onward."""

    keys = ["instrument", ANN_DATE_COL]
    order = keys + ([END_DATE_COL] if END_DATE_COL in reports.columns else [])
    return reports.sort_values(order).drop_duplicates(keys, keep="last")


def as_of_expand(reports: pd.DataFrame, keys: pd.DataFrame, value_columns: list[str]) -> pd.DataFrame:
    """Point-in-time expansion of announcement-dated ``reports`` onto the daily
    ``keys`` (``[trade_date, instrument]``).

    For each ``(instrument, trade_date)`` the returned value is the latest
    report with ``ann_date <= trade_date`` (``pd.merge_asof`` backward), i.e. a
    figure is only visible from its announcement date onward — never from the
    report period end. Instruments/dates with no prior announcement stay NaN
    (FP-4: absence is NaN, never a silent 0).
    """

    reports = reports.dropna(subset=[ANN_DATE_COL, "instrument"])
    reports = dedup_announcements(reports)[["instrument", ANN_DATE_COL, *value_columns]]
    reports = reports.sort_values(ANN_DATE_COL, kind="mergesort")
    left = keys.sort_values(TRADE_DATE_COL, kind="mergesort")
    merged = pd.merge_asof(
        left,
        reports,
        by="instrument",
        left_on=TRADE_DATE_COL,
        right_on=ANN_DATE_COL,
        direction="backward",
    )
    return merged.drop(columns=[ANN_DATE_COL])


def build_fundamentals_overlay(
    source_root: Path,
    panel_keys: pd.DataFrame,
    fields: tuple[FundamentalField, ...] = FUNDAMENTAL_FIELDS,
) -> pd.DataFrame:
    """Assemble a daily ``[trade_date, instrument, <fundamental fields...>]``
    overlay from the mounted source layer, PIT-correct for statement fields.

    ``panel_keys`` provides the ``[trade_date, instrument]`` grid to expand onto
    (typically the trading panel's own keys). Only fields whose source column is
    actually present are produced; the rest are simply absent (reported later as
    ``missing`` by the catalog, never silently zero).
    """

    keys = panel_keys[[TRADE_DATE_COL, "instrument"]].drop_duplicates().copy()
    keys[TRADE_DATE_COL] = pd.to_datetime(keys[TRADE_DATE_COL])
    keys["instrument"] = keys["instrument"].astype("string")
    overlay = keys.copy()

    by_dataset: dict[str, list[FundamentalField]] = defaultdict(list)
    for field in fields:
        by_dataset[field.dataset].append(field)

    for dataset, dataset_fields in by_dataset.items():
        wanted = [f.name for f in dataset_fields]
        pit_class = dataset_fields[0].pit_class
        base_cols = [SOURCE_INSTRUMENT_COL]
        base_cols += [TRADE_DATE_COL] if pit_class == "daily" else [ANN_DATE_COL, END_DATE_COL]
        raw = read_statement(source_root, dataset, columns=base_cols + wanted)
        if raw.empty:
            continue
        raw = raw.rename(columns={SOURCE_INSTRUMENT_COL: "instrument"})
        raw["instrument"] = raw["instrument"].astype("string")
        present = [c for c in wanted if c in raw.columns]
        if not present:
            continue
        if pit_class == "daily":
            raw[TRADE_DATE_COL] = _parse_ymd(raw[TRADE_DATE_COL])
            daily = (
                raw[["instrument", TRADE_DATE_COL, *present]]
                .dropna(subset=[TRADE_DATE_COL])
                .drop_duplicates(["instrument", TRADE_DATE_COL], keep="last")
            )
            overlay = overlay.merge(daily, on=[TRADE_DATE_COL, "instrument"], how="left")
        else:
            raw[ANN_DATE_COL] = _parse_ymd(raw[ANN_DATE_COL])
            expanded = as_of_expand(raw, overlay[[TRADE_DATE_COL, "instrument"]], present)
            overlay = overlay.merge(expanded, on=[TRADE_DATE_COL, "instrument"], how="left")

    return overlay.sort_values([TRADE_DATE_COL, "instrument"]).reset_index(drop=True)

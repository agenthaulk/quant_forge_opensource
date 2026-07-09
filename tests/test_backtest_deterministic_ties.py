"""RB-3: deterministic ordering of tied scores in the backtest engine.

Composites make exact score ties common (tied ranks, equal-weight sums of rank
factors, zero-variance cross-sections). The engine sorts the cross-section with
a stable mergesort on ("score", "instrument"), so tied names land in the same
long/short legs and the same quantile groups on every run and for every input
row order. These tests lock both the stability property and the spec'd
tie-break itself (ascending instrument id within a tied score level).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import FactorRepository

FACTOR_ID = "FTR_TIE_PROBE"
HIGH_CAP = ("HHH1", "HHH2", "HHH3", "HHH4")
LOW_CAP = ("LLL1", "LLL2", "LLL3", "LLL4")


def _tie_workspace(root: Path, *, shuffle_seed: int | None = None) -> dict[str, Path]:
    """Panel with two market-cap levels: a 4-way rank tie inside each level."""

    data_root = root / "data"
    factor_root = root / "factor_root"
    data_root.mkdir(parents=True)
    dates = pd.bdate_range("2026-01-05", periods=24)
    rows: list[dict[str, object]] = []
    for index, instrument in enumerate((*HIGH_CAP, *LOW_CAP)):
        cap = 2_000_000_000.0 if instrument in HIGH_CAP else 1_000_000_000.0
        for day_index, trade_date in enumerate(dates):
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": instrument,
                    "close": 10.0 + index + 0.013 * day_index * (index + 1),
                    "market_cap": cap,
                    "is_st": False,
                }
            )
    frame = pd.DataFrame(rows)
    if shuffle_seed is not None:
        frame = frame.sample(frac=1.0, random_state=shuffle_seed).reset_index(drop=True)
    frame.to_parquet(data_root / "panel.parquet", index=False)
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id=FACTOR_ID,
            name="tie_probe",
            formula="-rank(market_cap)",
            horizon_days=5,
        )
    )
    return {"data_root": data_root, "factor_root": factor_root}


def _membership_fingerprint(artifact_path: Path) -> list[tuple[object, ...]]:
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    return [
        (
            row["signal_date"],
            tuple(row["long_instruments"]),
            tuple(row["short_instruments"]),
            tuple(sorted(row["group_returns"].items())),
            row["period_return"],
        )
        for row in payload["period_returns"]
    ]


def _run(paths: dict[str, Path], artifact_root: Path) -> Path:
    result = run_factor_backtest(
        FACTOR_ID,
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=artifact_root,
        holding_days=5,
        top_quantile=0.25,
    )
    return result.artifact_path


def test_tied_scores_resolve_to_instrument_tiebreak_membership(tmp_path: Path) -> None:
    paths = _tie_workspace(tmp_path / "base")
    payload = json.loads(_run(paths, tmp_path / "artifacts").read_text(encoding="utf-8"))

    assert payload["periods"] > 0
    for row in payload["period_returns"]:
        # score = -rank(market_cap): the high-cap level is the tied MOST
        # NEGATIVE block (short side head), the low-cap level the tied HIGHEST
        # block (long side tail). Within each tied block the stable mergesort
        # tie-break is ascending instrument id.
        assert row["short_instruments"] == ["HHH1", "HHH2"]
        assert row["long_instruments"] == ["LLL3", "LLL4"]


def test_tied_scores_are_byte_stable_across_repeated_runs(tmp_path: Path) -> None:
    paths = _tie_workspace(tmp_path / "base")
    first = _membership_fingerprint(_run(paths, tmp_path / "artifacts_one"))
    second = _membership_fingerprint(_run(paths, tmp_path / "artifacts_two"))

    assert first == second
    assert len(first) > 0


def test_tied_scores_are_stable_across_input_row_order_shuffles(tmp_path: Path) -> None:
    ordered = _tie_workspace(tmp_path / "ordered")
    baseline = _membership_fingerprint(_run(ordered, tmp_path / "artifacts_ordered"))

    for seed in (7, 23):
        shuffled = _tie_workspace(tmp_path / f"shuffled_{seed}", shuffle_seed=seed)
        shuffled_fingerprint = _membership_fingerprint(
            _run(shuffled, tmp_path / f"artifacts_shuffled_{seed}")
        )
        assert shuffled_fingerprint == baseline

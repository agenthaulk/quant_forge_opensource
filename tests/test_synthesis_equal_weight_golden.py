"""Equal-weight golden end-to-end on a synthetic fixture panel (P4).

Design contract (docs/design/multi_factor_portfolio_backtest.md §13 golden
e2e, scoped to the P4 materialize+drive slice): a two-member equal-weight
composite over a deterministic synthetic panel yields a STABLE colon-free
``COMPOSITE_...`` id, gross AND net metrics present with sane typed statuses,
a causal schedule (signal strictly before entry by ``delay``, exit exactly at
``entry + holding``), and turnover within [0, 2]. The D3 final-partial
exclusion stays disclosed, never silent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from quant_forge.backtesting.service import FINAL_PARTIAL_PERIOD_EXCLUDED
from quant_forge.core.contracts import SimulationProfile, TransactionCostModel
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import prepare_factor_scores_result
from quant_forge.synthesis.service import (
    build_apriori_composite,
    derive_composite_id,
    run_composite_backtest,
)

UNIVERSE = ("is_st == false",)
HOLDING_DAYS = 5
DELAY = 1
# Golden id for the fixed inputs below — must match tests/test_synthesis_
# composite_id.py's GOLDEN_BASE_ID; a change means the RB-10 canonical-JSON
# recipe changed and every existing id would be re-minted.
GOLDEN_COMPOSITE_ID = "COMPOSITE_6C843B794231"

_SANE_STATUSES = {"available", "insufficient_sample", "not_applicable"}
_METRIC_KEYS = (
    "annualized_return",
    "net_annualized_return",
    "annualized_volatility",
    "net_annualized_volatility",
    "long_short_sharpe",
    "net_long_short_sharpe",
    "max_drawdown",
    "net_max_drawdown",
    "rebalance_rate",
    "rebalance_turnover_mean",
)


def _write_panel(data_root: Path, *, periods: int = 40, instruments: int = 8) -> pd.DataFrame:
    data_root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2026-01-05", periods=periods)
    rows: list[dict[str, object]] = []
    for instrument_index in range(instruments):
        instrument = f"STK{instrument_index:03d}"
        for day_index, trade_date in enumerate(dates):
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": instrument,
                    "close": 10.0 + instrument_index + day_index * (0.03 + instrument_index * 0.002),
                    "market_cap": 1_000_000_000.0 + instrument_index * 150_000_000.0,
                    "is_st": False,
                    "volume": 1_000.0 + instrument_index * 25.0 + day_index * 5.0,
                    "return_5d": 0.01 * ((day_index + 2 * instrument_index) % 7) - 0.02,
                    "volatility_5d": 0.02 + 0.001 * ((day_index + instrument_index) % 5),
                }
            )
    pd.DataFrame(rows).to_parquet(data_root / "panel.parquet", index=False)
    return LocalPanelDataProvider(data_root).load_panel()


def test_equal_weight_golden_end_to_end(tmp_path: Path) -> None:
    panel = _write_panel(tmp_path / "data")
    profile = SimulationProfile(
        execution_delay_days=DELAY,
        top_quantile=0.3,
        decay_days=0,
        test_period_start="2026-01-01",
        test_period_end="2026-03-31",
    )
    members = {
        "F_MEM_ALPHA": prepare_factor_scores_result(
            panel, "rank(return_5d)", UNIVERSE, profile=profile
        ).scores,
        "F_MEM_BETA": prepare_factor_scores_result(
            panel, "rank(market_cap)", UNIVERSE, profile=profile
        ).scores,
    }
    composite = build_apriori_composite(
        members,
        directions={"F_MEM_ALPHA": 1, "F_MEM_BETA": -1},
        standardization="zscore",
        method="equal_weight",
    )
    assert composite.weights_effective == {"F_MEM_ALPHA": 1.0, "F_MEM_BETA": 1.0}

    id_inputs: dict[str, object] = {
        "factor_refs": (("F_MEM_ALPHA", 1), ("F_MEM_BETA", -1)),
        "method": "equal_weight",
        "method_params": None,
        "standardization": "zscore",
        "backtest_start": "2026-01-01",
        "backtest_end": "2026-03-31",
        "decay_days": 0,
        "execution_delay_days": DELAY,
        "top_quantile": 0.3,
        "coverage_rule": "all_factors",
        "min_factor_coverage": None,
        "universe_filters": UNIVERSE,
        "holding_days": HOLDING_DAYS,
    }
    composite_id = derive_composite_id(**id_inputs)  # type: ignore[arg-type]
    # Stable, colon-free composite id (RB-10 / RF-1 golden).
    assert composite_id == GOLDEN_COMPOSITE_ID
    assert composite_id == derive_composite_id(**id_inputs)  # type: ignore[arg-type]
    assert ":" not in composite_id

    run = run_composite_backtest(
        composite.composite,
        composite_id=composite_id,
        factor_root=tmp_path / "factor_root",
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        holding_days=HOLDING_DAYS,
        profile=profile,
        universe_filters=UNIVERSE,
        transaction_costs=TransactionCostModel(commission_bps=10.0, slippage_bps=5.0),
        composite_name="composite_equal_weight_golden",
        panel=panel,
    )
    result = run.result

    # Gross AND net metrics present, every typed status sane; an available
    # metric carries a finite value, an unavailable one never fakes a 0.
    assert result.periods == 7
    assert result.gross_cumulative_return is not None
    assert result.net_cumulative_return is not None
    assert result.net_cumulative_return < result.gross_cumulative_return  # costs charged
    for key in _METRIC_KEYS:
        metric = result.metrics[key]
        assert metric.status in _SANE_STATUSES, (key, metric.status)
        if metric.status == "available":
            assert metric.value is not None and np.isfinite(metric.value), key
        else:
            assert metric.value is None, key
    assert result.metrics["net_long_short_sharpe"].status == "available"
    assert result.group_returns and len(result.group_returns) == 5

    # Causal schedule: signal strictly before entry by `delay`, exit exactly
    # at entry + holding for every (complete) period.
    dates = sorted(panel["trade_date"].drop_duplicates())
    index_of = {date.date().isoformat(): position for position, date in enumerate(dates)}
    assert len(result.resolved_schedule) == 7
    for row in result.resolved_schedule:
        signal, entry = str(row["signal_date"]), str(row["entry_date"])
        exit_label = str(row["actual_exit_or_rebalance_date"])
        assert signal < entry
        assert index_of[entry] - index_of[signal] == DELAY
        assert row["is_complete_period"] is True
        assert index_of[exit_label] - index_of[entry] == HOLDING_DAYS

    # Turnover in [0, 2] for the initial build and every rebalance.
    assert result.initial_build_turnover is not None
    assert 0.0 <= result.initial_build_turnover <= 2.0
    ledger_turnovers = [
        float(row["rebalance_turnover"])
        for row in result.rebalance_ledger
        if row["rebalance_turnover"] is not None
    ]
    assert ledger_turnovers
    assert all(0.0 <= value <= 2.0 for value in ledger_turnovers)
    assert result.rebalance_turnover_mean is not None
    assert 0.0 <= result.rebalance_turnover_mean <= 2.0

    # The dropped scheduled tail stays disclosed (D3), never silent.
    assert FINAL_PARTIAL_PERIOD_EXCLUDED in result.warning_codes

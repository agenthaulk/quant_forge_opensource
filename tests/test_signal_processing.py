from __future__ import annotations

import pandas as pd

from quant_forge.core.contracts import SimulationProfile
from quant_forge.factor_engine.signal_processing import prepare_factor_scores


def test_prepare_factor_scores_applies_test_period_and_ewma_decay() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            "instrument": ["AAA", "AAA", "AAA"],
            "close": [1.0, 1.0, 1.0],
            "market_cap": [10.0, 20.0, 30.0],
            "is_st": [False, False, False],
        }
    )
    profile = SimulationProfile(decay_days=3, test_period_start="2025-01-02")

    scores = prepare_factor_scores(panel, "market_cap", profile=profile)

    assert list(scores["trade_date"].dt.strftime("%Y-%m-%d")) == ["2025-01-02", "2025-01-03"]
    assert list(scores["score"]) == [20.0, 25.0]


def test_prepare_factor_scores_preserves_universe_filter_missing_after_decay() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"]),
            "instrument": ["AAA", "AAA", "AAA"],
            "close": [1.0, 1.0, 1.0],
            "market_cap": [10.0, 20.0, 30.0],
            "is_st": [False, True, False],
        }
    )

    scores = prepare_factor_scores(panel, "market_cap", ("is_st == false",), profile=SimulationProfile(decay_days=3))

    assert pd.isna(scores.loc[1, "score"])
    assert scores.loc[2, "score"] > scores.loc[0, "score"]

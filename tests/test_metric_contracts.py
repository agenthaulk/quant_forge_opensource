from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest

from quant_forge.core.contracts import BacktestResult, MetricValue
from quant_forge.evaluation.service import DEGENERATE_IC_SERIES, _ic_summary
from quant_forge.research_loop.service import ResearchObjectiveWeights, score_candidate
from quant_forge.core.contracts import EvaluationResult


def test_metric_contract_distinguishes_zero_from_unavailable() -> None:
    zero = MetricValue(value=0.0, unit="return", status="available", observation_count=2)
    insufficient = MetricValue(
        value=None,
        unit="annualized_return",
        status="insufficient_sample",
        observation_count=1,
        minimum_required=2,
    )
    not_applicable = MetricValue(value=None, unit="turnover", status="not_applicable", observation_count=0)

    assert zero.value == 0.0
    assert zero.status == "available"
    assert insufficient.value is None
    assert insufficient.status == "insufficient_sample"
    assert not_applicable.value is None
    assert not_applicable.status == "not_applicable"


def test_json_serialization_preserves_null_metric_status(tmp_path: Path) -> None:
    metric = MetricValue(
        value=None,
        unit="annualized_return",
        status="insufficient_sample",
        observation_count=1,
        minimum_required=252,
        warning_codes=("INSUFFICIENT_ANNUALIZATION_HISTORY",),
    )
    path = tmp_path / "metric.json"
    path.write_text(json.dumps(asdict(metric), allow_nan=False), encoding="utf-8")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["value"] is None
    assert payload["status"] == "insufficient_sample"
    assert payload["warning_codes"] == ["INSUFFICIENT_ANNUALIZATION_HISTORY"]


def test_rd_score_does_not_treat_unavailable_as_zero_return() -> None:
    evaluation = EvaluationResult(
        factor_id="FTR_TEST",
        observations=10,
        coverage=1.0,
        rank_ic_mean=0.02,
        rank_ic_std=0.01,
        rank_icir=2.0,
        ic_days=10,
        artifact_path=Path("eval.json"),
    )
    backtest = BacktestResult(
        factor_id="FTR_TEST",
        periods=1,
        holding_days=21,
        cumulative_return=0.02,
        annualized_return=None,
        annualized_volatility=None,
        max_drawdown=None,
        artifact_path=Path("backtest.json"),
        net_annualized_return=None,
        net_max_drawdown=None,
        metrics={
            "net_annualized_return": MetricValue(
                value=None,
                unit="return",
                status="insufficient_sample",
                observation_count=1,
                warning_codes=("INSUFFICIENT_ANNUALIZATION_HISTORY",),
            )
        },
    )

    # BUG #006: the raised message is now a typed, parseable reason
    # (metric_unavailable:<name> (<status>: <warning codes>)) so a caller that
    # rejects the unscorable candidate/seed can record WHY (see
    # test_research_loop_structure.py's score_candidate/run_once coverage);
    # the "never silently treat missing as zero" contract this test pins is
    # otherwise unchanged.
    with pytest.raises(ValueError, match=r"metric_unavailable:net_annualized_return \(insufficient_sample"):
        score_candidate(
            evaluation,
            backtest,
            ResearchObjectiveWeights(
                weighted_split_icir=0.0,
                rank_ic_mean=0.0,
                rank_icir=0.0,
                annualized_return=1.0,
                max_drawdown=0.0,
            ),
        )


def _labeled(records: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(records, columns=["trade_date", "instrument", "score", "forward_return"])


def test_ic_summary_icir_metric_is_null_not_zero_on_single_day() -> None:
    # COR-4: a single IC day yields <2 observations, so ICIR (mean/std) is
    # insufficient_sample -> value None + status, NOT 0.0, in the v2 metrics map.
    labeled = _labeled(
        [
            {"trade_date": "2026-01-02", "instrument": "AAA", "score": 1.0, "forward_return": 0.01},
            {"trade_date": "2026-01-02", "instrument": "BBB", "score": 2.0, "forward_return": 0.02},
            {"trade_date": "2026-01-02", "instrument": "CCC", "score": 3.0, "forward_return": 0.03},
        ]
    )

    summary = _ic_summary(labeled, horizon_days=1, execution_delay_days=1)
    icir_metric = summary["metrics"]["rank_icir"]

    assert icir_metric.value is None
    assert icir_metric.status == "insufficient_sample"
    # The flat scalar field is unchanged (0.0) — only the v2 map carries null+status.
    assert summary["rank_icir"] == 0.0


def test_ic_summary_ic_mean_metric_is_null_not_zero_when_no_ic_days() -> None:
    # COR-4: all-constant scores mean every cross-section is skipped -> zero IC days
    # -> rank_ic_mean metric is insufficient_sample with value None, not 0.0.
    labeled = _labeled(
        [
            {"trade_date": "2026-01-02", "instrument": "AAA", "score": 1.0, "forward_return": 0.01},
            {"trade_date": "2026-01-02", "instrument": "BBB", "score": 1.0, "forward_return": 0.02},
            {"trade_date": "2026-01-03", "instrument": "AAA", "score": 1.0, "forward_return": 0.01},
            {"trade_date": "2026-01-03", "instrument": "BBB", "score": 1.0, "forward_return": 0.02},
        ]
    )

    summary = _ic_summary(labeled, horizon_days=1, execution_delay_days=1)
    ic_mean_metric = summary["metrics"]["rank_ic_mean"]

    assert summary["ic_days"] == 0
    assert ic_mean_metric.value is None
    assert ic_mean_metric.status == "insufficient_sample"


def test_ic_summary_near_constant_ic_does_not_explode() -> None:
    # COR-9: a near-constant IC series whose per-date std is ~6e-9 -- ABOVE the
    # absolute 1e-12 floor but far below abs(mean)*1e-6 -- previously made
    # rank_icir = mean/std explode (~1.5e8). The relative near-zero tolerance now
    # treats it as degenerate -> rank_icir 0.0, naive-t None.
    # Construct wide cross-sections: half the dates are perfectly rank-aligned
    # (IC == 1.0), the other half swap a single adjacent forward-return pair, which
    # nudges Spearman rho down by ~1e-8 -- a genuine tiny-but-nonzero spread.
    universe = 1000
    records: list[dict[str, object]] = []
    for day_index in range(6):
        date = f"2026-01-{day_index + 2:02d}"
        forward = [i / universe for i in range(universe)]
        if day_index % 2 == 1:
            forward[500], forward[501] = forward[501], forward[500]
        for i in range(universe):
            records.append(
                {
                    "trade_date": date,
                    "instrument": f"S{i:04d}",
                    "score": float(i),
                    "forward_return": forward[i],
                }
            )
    labeled = _labeled(records)

    summary = _ic_summary(labeled, horizon_days=1, execution_delay_days=1)

    assert summary["ic_days"] > 1
    # The raw per-date IC values genuinely differ (a real ~1e-8 spread exists),
    # so this exercises the RELATIVE floor, not the absolute 1e-12 one.
    raw_ics = {round(row["ic"], 12) for row in summary["ic_series"]}
    assert len(raw_ics) > 1
    # Near-constant IC: std is tiny relative to the mean -> flagged degenerate.
    assert DEGENERATE_IC_SERIES in summary["warning_codes"]
    assert summary["rank_icir"] == 0.0
    assert abs(summary["rank_icir"]) < 1e3  # bounded, not exploded to ~1e8
    assert summary["rank_ic_t_stat_naive"] is None
    # And the v2 ICIR metric is null-not-zero on the degenerate path.
    assert summary["metrics"]["rank_icir"].value is None
    assert summary["metrics"]["rank_icir"].status == "insufficient_sample"

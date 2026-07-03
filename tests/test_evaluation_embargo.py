"""QUANT-1 regression: purge/embargo gap between contiguous evaluation splits.

A signal date's forward-return label reaches (execution_delay + horizon) trading
days into the future. Without a gap, the tail of the IS split carries labels
realized inside the OOS1 window, so the IS metric borrows OOS data. These tests
pin the purge mechanism in `_split_dates`.
"""

from __future__ import annotations

import pandas as pd

from quant_forge.core.contracts import SampleSplitSpec
from quant_forge.evaluation.service import _split_dates


SPLITS = (
    SampleSplitSpec(name="IS", fraction=0.5, score_weight=0.5),
    SampleSplitSpec(name="OOS1", fraction=0.3, score_weight=0.3),
    SampleSplitSpec(name="OOS2", fraction=0.2, score_weight=0.2),
)


def _dates(n: int) -> list[pd.Timestamp]:
    return list(pd.date_range("2025-01-01", periods=n, freq="B"))


def test_split_dates_embargo_zero_is_legacy_partition() -> None:
    dates = _dates(20)
    is_dates, oos1, oos2 = _split_dates(dates, SPLITS, embargo=0)
    # 50/30/20 contiguous, nothing dropped.
    assert list(is_dates) == dates[0:10]
    assert list(oos1) == dates[10:16]
    assert list(oos2) == dates[16:20]


def test_split_dates_purges_trailing_dates_of_non_final_splits() -> None:
    dates = _dates(20)
    is_dates, oos1, oos2 = _split_dates(dates, SPLITS, embargo=3)
    # IS and OOS1 lose their last 3 dates; OOS2 (final split) is untouched.
    assert list(is_dates) == dates[0:7]
    assert list(oos1) == dates[10:13]
    assert list(oos2) == dates[16:20]
    # The gap guarantees IS's last label does not reach the next split's first date.
    is_last_index = dates.index(is_dates[-1])
    oos1_first_index = dates.index(oos1[0])
    assert oos1_first_index - is_last_index >= 3


def test_split_dates_fully_purges_split_shorter_than_embargo() -> None:
    dates = _dates(20)
    is_dates, oos1, oos2 = _split_dates(dates, SPLITS, embargo=8)
    # OOS1 has only 6 dates < embargo 8 -> fully purged; OOS2 (final) survives.
    assert list(is_dates) == dates[0:2]
    assert oos1 == ()
    assert list(oos2) == dates[16:20]

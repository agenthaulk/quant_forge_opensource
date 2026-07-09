"""RB-2 window preconditions: realized period count N and WINDOW_TOO_SHORT.

Design contract (docs/design/multi_factor_portfolio_backtest.md §3 RB-2/RB-8):
before materializing, the synthesis layer computes the realized
non-overlapping period count ``N = floor((len(in_window_dates) - delay - 1) /
holding) + 1`` and rejects ``N < 2`` with the typed ``WindowTooShortError``
(code ``WINDOW_TOO_SHORT``) that the workflow maps to a client-error response
— the engine's own tiny ``max(2, holding + delay + 1)`` gate is not the
synthesis precondition.
"""

from __future__ import annotations

import pytest
from quant_forge.synthesis.service import (
    WINDOW_TOO_SHORT,
    SynthesisPreconditionError,
    WindowTooShortError,
    count_non_overlapping_periods,
    require_backtest_window,
)


@pytest.mark.parametrize(
    ("date_count", "delay", "holding", "expected"),
    [
        (10, 1, 4, 3),
        (10, 1, 5, 2),
        (7, 1, 5, 2),
        (6, 1, 5, 1),
        (3, 1, 1, 2),
        (2, 1, 1, 1),
        (0, 1, 5, 0),
        (130, 1, 20, 7),
        (592, 2, 10, 59),
    ],
)
def test_period_count_formula_is_computed_verbatim(
    date_count: int, delay: int, holding: int, expected: int
) -> None:
    assert count_non_overlapping_periods(date_count, delay=delay, holding=holding) == expected
    assert expected == (date_count - delay - 1) // holding + 1


def test_require_backtest_window_returns_n_on_success() -> None:
    assert require_backtest_window(10, delay=1, holding=4) == 3
    # Boundary: exactly 2 periods is enough.
    assert require_backtest_window(3, delay=1, holding=1) == 2


@pytest.mark.parametrize(
    ("date_count", "delay", "holding"),
    [(6, 1, 5), (2, 1, 1), (0, 1, 5), (5, 3, 20)],
)
def test_too_short_windows_raise_the_typed_error(
    date_count: int, delay: int, holding: int
) -> None:
    with pytest.raises(WindowTooShortError) as excinfo:
        require_backtest_window(date_count, delay=delay, holding=holding)
    error = excinfo.value
    # Typed for the workflow's client-error mapping; still a ValueError so
    # existing invalid-request handling keeps working.
    assert isinstance(error, ValueError)
    assert isinstance(error, SynthesisPreconditionError)
    assert error.code == WINDOW_TOO_SHORT
    message = str(error)
    assert WINDOW_TOO_SHORT in message
    assert f"delay={delay}" in message
    assert f"holding={holding}" in message


@pytest.mark.parametrize(
    ("date_count", "delay", "holding"),
    [(-1, 1, 5), (10, 0, 5), (10, 1, 0), (True, 1, 5), (10, True, 5), (10, 1, True)],
)
def test_invalid_inputs_raise_plain_value_errors(
    date_count: object, delay: object, holding: object
) -> None:
    with pytest.raises(ValueError) as excinfo:
        count_non_overlapping_periods(date_count, delay=delay, holding=holding)
    # Input-shape problems are plain ValueError, not the typed
    # window-precondition outcome.
    assert not isinstance(excinfo.value, SynthesisPreconditionError)

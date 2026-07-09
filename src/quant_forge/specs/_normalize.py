"""Private normalization helpers shared by all spec modules."""

from __future__ import annotations

from typing import Any, TypeVar

_T = TypeVar("_T")


def set_tuple(instance: object, field_name: str) -> None:
    """Normalize a frozen-dataclass field to ``tuple[str, ...]`` in place."""

    value = getattr(instance, field_name)
    if value is None:
        normalized: tuple[Any, ...] = ()
    elif isinstance(value, tuple):
        normalized = value
    elif isinstance(value, list):
        normalized = tuple(value)
    else:
        normalized = (value,)
    object.__setattr__(instance, field_name, tuple(str(item) for item in normalized))


def coerce_component(cls: type[_T], value: Any, label: str) -> _T:
    """Build a kernel component from a payload, raising ValueError on bad shape.

    ``from_dict`` surfaces must fail with ValueError consistently; the raw
    ``cls(**payload)`` construction raises TypeError on unknown keys or a
    non-mapping payload, which is wrapped here.
    """

    if isinstance(value, cls):
        return value
    try:
        return cls(**dict(value))
    except TypeError as exc:
        raise ValueError(f"invalid {label} payload: {exc}") from exc

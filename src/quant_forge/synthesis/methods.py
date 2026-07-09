"""Synthesis method + standardization catalog and schema-driven validation (P1).

Single source of truth for ``GET /api/synthesis/methods`` (design doc
``docs/design/multi_factor_portfolio_backtest.md`` §9) and for the server-side
re-validation of method/standardization parameters that the run workflow
performs before touching any data. The web payload builder
(``apps/web/api.py:_synthesis_methods_payload``) serializes these constants;
nothing else defines the catalog.

Contract honesty rules baked in here:

- The wire payload is the design §9 literal JSON — the post-P6 end state:
  all four methods ship ``available: true`` now that the fitted
  implementation (point-in-time IC/ICIR, §4.4) has landed. The CP0 interim
  amendment (fitted methods reserved as 预留 ``available: false``) is
  retired; the frontend still renders any FUTURE reserved method as a
  disabled option generically, so the reserved mechanism stays supported.
- ``is_fitted`` describes each method's nature truthfully: the two a-priori
  methods never claim fitting, and the fitted methods' ``true`` is a
  catalog-level nature claim — the RUN-level ``is_fitted`` in provenance
  still downgrades to ``false`` when a window admits zero genuinely fitted
  rebalances (``NO_FITTED_PERIODS``, design §3 RB-8).
- Parameter validation is entirely schema-driven: every check reads only the
  declared :class:`ParamSpec` list, so a method added to the catalog is
  validated with zero per-method hardcoding — the same rule the shipped
  frontend form follows (``synthesis.js`` renders purely from ``params[]``).
  Declared defaults are resolved the same way (:func:`apply_param_defaults`),
  so what a run reports as its parameters is what it actually used.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any, Literal


ParamType = Literal["float", "int", "bool", "enum", "weights"]

PARAM_TYPES: tuple[str, ...] = ("float", "int", "bool", "enum", "weights")

_NUMERIC_PARAM_TYPES: tuple[str, ...] = ("float", "int", "weights")


@dataclass(frozen=True)
class ParamSpec:
    """One declared method/standardizer parameter (design §9 ``ParamSpec``).

    ``to_dict()`` emits exactly the JSON field set the shipped frontend form
    consumes (``synthesis.js`` ``renderParamSpecInputHtml``): the identity
    fields (``name`` / ``label`` / ``type`` / ``required`` / ``help``) are
    always present, and the optional constraint fields (``default`` /
    ``minimum`` / ``maximum`` / ``choices``) appear only when declared —
    matching the §9 literal payload key-for-key. The frontend treats a
    missing constraint and ``null`` identically, so omission loses nothing
    and keeps the wire shape pinned to the design doc.
    """

    name: str
    label: str
    type: ParamType
    required: bool = False
    default: float | int | bool | str | None = None
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("param name is required")
        if not self.label.strip():
            raise ValueError(f"param label is required for: {self.name}")
        if self.type not in PARAM_TYPES:
            raise ValueError(f"unsupported param type for {self.name}: {self.type}")
        if self.type == "enum" and not self.choices:
            raise ValueError(f"enum param requires choices: {self.name}")
        if self.type != "enum" and self.choices:
            raise ValueError(f"choices are only valid for enum params: {self.name}")
        if self.type not in _NUMERIC_PARAM_TYPES and (
            self.minimum is not None or self.maximum is not None
        ):
            raise ValueError(f"minimum/maximum are only valid for numeric params: {self.name}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"minimum must be <= maximum for param: {self.name}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        if self.choices:
            payload["choices"] = list(self.choices)
        payload["help"] = self.help
        return payload


ParamSchema = tuple[ParamSpec, ...]


@dataclass(frozen=True)
class MethodSpec:
    """One catalog method row (design §9 ``methods[]`` entry).

    ``required_standardization`` mirrors the wire contract exactly: JSON
    ``false`` means "the user picks a standardization" and a non-empty
    standardization name means "pinned by the method" (the frontend then
    renders the pinned choice disabled and omits the ``standardization``
    block from the run request). ``True`` and blank strings are meaningless
    on the wire, so construction rejects them.
    """

    name: str
    label: str
    available: bool
    is_fitted: bool
    params: ParamSchema = ()
    required_standardization: str | Literal[False] = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("method name is required")
        if not self.label.strip():
            raise ValueError(f"method label is required for: {self.name}")
        if self.required_standardization is not False and (
            not isinstance(self.required_standardization, str)
            or not self.required_standardization.strip()
        ):
            raise ValueError(
                f"required_standardization must be False or a standardization name for: {self.name}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available,
            "required_standardization": self.required_standardization,
            "is_fitted": self.is_fitted,
            "params": [param.to_dict() for param in self.params],
        }


@dataclass(frozen=True)
class StandardizationSpec:
    """One catalog standardization row (design §9 ``standardizations[]``).

    The wire payload carries ``name`` + ``label`` only (§9 literal — the
    frontend select reads nothing else and the run request always sends
    ``params: {}``). ``params`` stays server-side so the run workflow can
    re-validate the submitted block against a declared schema instead of
    trusting the empty-object convention.
    """

    name: str
    label: str
    params: ParamSchema = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("standardization name is required")
        if not self.label.strip():
            raise ValueError(f"standardization label is required for: {self.name}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label}


# Design §9 catalog, post-P6 end state: all four methods available. The
# fitted methods flipped ONLY their `available` flag when the §4.4
# implementation landed — labels, params, and `is_fitted` were the §9 end
# state from P1. Labels and help strings are §9 verbatim; the frontend
# escapes and renders them as-is.
SYNTHESIS_METHODS: tuple[MethodSpec, ...] = (
    MethodSpec(
        name="equal_weight",
        label="等权合成",
        available=True,
        is_fitted=False,
        params=(),
    ),
    MethodSpec(
        name="weighted",
        label="先验加权合成",
        available=True,
        is_fitted=False,
        params=(
            ParamSpec(
                name="weights",
                label="各因子权重",
                type="weights",
                required=True,
                help="为每个已选因子提供一个先验权重；原样回显，不归一化展示。",
            ),
        ),
    ),
    MethodSpec(
        name="ic_weighted",
        label="IC 加权合成（拟合）",
        available=True,
        is_fitted=True,
        params=(
            ParamSpec(
                name="ic_min_periods",
                label="IC 最小拟合期数",
                type="int",
                required=False,
                default=6,
                minimum=3,
                maximum=60,
                help=(
                    "点位时序拟合的最小已实现期数；不足则该期退化为等权并标注；"
                    "窗口内无任一可拟合期则整体退化为等权且 is_fitted=false（NO_FITTED_PERIODS）。"
                ),
            ),
        ),
    ),
    MethodSpec(
        name="icir_weighted",
        label="ICIR 加权合成（拟合）",
        available=True,
        is_fitted=True,
        params=(
            ParamSpec(
                name="ic_min_periods",
                label="ICIR 最小拟合期数",
                type="int",
                required=False,
                default=6,
                minimum=3,
                maximum=60,
                help=(
                    "以 IC 均值/IC 标准差作为权重；窗口内仅用已实现的前向收益，杜绝前视；"
                    "IC 标准差为 0 或权重非有限时该期退化为等权。"
                ),
            ),
        ),
    ),
)

STANDARDIZATIONS: tuple[StandardizationSpec, ...] = (
    StandardizationSpec(name="zscore", label="截面 Z-Score（按日）"),
    StandardizationSpec(name="rank", label="截面排序标准化（按日）"),
)


def method_catalog_payload() -> dict[str, Any]:
    """Build the ``GET /api/synthesis/methods`` response dict (design §9).

    Serialized fresh on every call (``to_dict`` allocates new dicts/lists),
    so callers can never mutate the catalog constants through the payload.
    """

    return {
        "methods": [method.to_dict() for method in SYNTHESIS_METHODS],
        "standardizations": [standardization.to_dict() for standardization in STANDARDIZATIONS],
    }


def apply_param_defaults(
    schema: Sequence[ParamSpec], params: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve declared ParamSpec defaults for absent optional params.

    Schema-driven like everything else in this module: only a spec that
    declares a ``default`` fills in, an explicitly supplied value always
    wins, and nothing is coerced. The run workflow applies this AFTER
    :func:`validate_params_against_schema`, so the parameters a run echoes
    into provenance (and hashes into its composite id) are the values it
    actually used — a fitted run submitted without ``ic_min_periods``
    truthfully reports the catalog default it ran with instead of an empty
    mapping.
    """

    resolved = {str(name): value for name, value in params.items()}
    for spec in schema:
        if spec.default is not None and spec.name not in resolved:
            resolved[spec.name] = spec.default
    return resolved


def _validate_numeric_bounds(spec: ParamSpec, value: float, *, owner: str, label: str) -> None:
    if spec.minimum is not None and value < spec.minimum:
        raise ValueError(f"{owner}: {label} must be >= {spec.minimum}; got {value}")
    if spec.maximum is not None and value > spec.maximum:
        raise ValueError(f"{owner}: {label} must be <= {spec.maximum}; got {value}")


def validate_params_against_schema(
    schema: Sequence[ParamSpec], params: Mapping[str, Any], *, owner: str = "params"
) -> dict[str, Any]:
    """Generic, schema-driven parameter validation (single enforcement source).

    The declared :class:`ParamSpec` schema is the enforcement truth for every
    method and standardizer: unknown names, required presence, per-type checks
    (``float`` / ``int`` / ``bool`` / ``enum`` / ``weights``),
    ``minimum``/``maximum`` bounds, enum membership, and the structural shape
    of ``weights`` mappings are all rejected here, driven entirely by the
    declared specs — zero per-method hardcoding, so a catalog addition cannot
    drift from its own validation. This is the server-side re-assertion of the
    client guards in ``synthesis.js`` ``buildRunRequest``; the run workflow
    calls it before any cross-field, data-dependent checks (for example
    "weights keys must equal the selected factor set", which needs the request
    context and stays in the workflow layer).

    Values are checked, never coerced: an ``int`` param rejects ``6.5`` and
    ``True`` alike instead of silently rounding or reinterpreting. Raises
    ``ValueError`` (mapped to HTTP 400 by the web layer) and returns a plain
    ``dict`` copy of the params on success.
    """

    if not isinstance(params, Mapping):
        raise ValueError(f"{owner}: params must be a mapping")
    specs = {spec.name: spec for spec in schema}
    unknown = sorted(set(map(str, params)) - set(specs))
    if unknown:
        raise ValueError(f"{owner}: unknown parameters: {unknown}")
    for name, spec in specs.items():
        if name not in params:
            if spec.required:
                raise ValueError(f"{owner}: missing required parameter: {name}")
            continue
        value = params[name]
        if spec.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{owner}: parameter {name} must be a boolean")
        elif spec.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{owner}: parameter {name} must be an integer")
            _validate_numeric_bounds(spec, value, owner=owner, label=f"parameter {name}")
        elif spec.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{owner}: parameter {name} must be a number")
            if not math.isfinite(float(value)):
                raise ValueError(f"{owner}: parameter {name} must be finite")
            _validate_numeric_bounds(spec, float(value), owner=owner, label=f"parameter {name}")
        elif spec.type == "enum":
            if not isinstance(value, str) or value not in spec.choices:
                raise ValueError(f"{owner}: parameter {name} must be one of {list(spec.choices)}")
        elif spec.type == "weights":
            if not isinstance(value, Mapping):
                raise ValueError(
                    f"{owner}: parameter {name} must be a mapping of factor_id to number"
                )
            for key, weight in value.items():
                if not str(key).strip():
                    raise ValueError(f"{owner}: parameter {name} has an empty factor_id key")
                if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                    raise ValueError(f"{owner}: parameter {name}[{key}] must be a number")
                if not math.isfinite(float(weight)):
                    raise ValueError(f"{owner}: parameter {name}[{key}] must be finite")
                _validate_numeric_bounds(
                    spec, float(weight), owner=owner, label=f"parameter {name}[{key}]"
                )
        else:  # pragma: no cover - ParamSpec.__post_init__ forbids other types
            raise ValueError(f"{owner}: unsupported param type for {name}: {spec.type}")
    return {str(name): value for name, value in params.items()}

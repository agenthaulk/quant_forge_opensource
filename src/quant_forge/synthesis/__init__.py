"""Multi-factor synthesis package (method catalog + schema-driven validation).

Backend of the shipped multi-factor frontend (``apps/web/static/views/
synthesis.js``). This package starts with the method/standardization catalog
served by ``GET /api/synthesis/methods`` and the schema layer the run
workflow uses for server-side parameter re-validation; the composite scoring
service lands in later phases of the same design
(``docs/design/multi_factor_portfolio_backtest.md``).
"""

from quant_forge.synthesis.methods import (
    PARAM_TYPES,
    STANDARDIZATIONS,
    SYNTHESIS_METHODS,
    MethodSpec,
    ParamSpec,
    StandardizationSpec,
    method_catalog_payload,
    validate_params_against_schema,
)

__all__ = [
    "PARAM_TYPES",
    "STANDARDIZATIONS",
    "SYNTHESIS_METHODS",
    "MethodSpec",
    "ParamSpec",
    "StandardizationSpec",
    "method_catalog_payload",
    "validate_params_against_schema",
]

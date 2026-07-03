from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.operator_registry import (
    canonical_formula_fingerprint,
    load_default_operator_registry,
    load_operator_registry,
    resolve_formula_operators,
)
from quant_forge.operator_registry.errors import OperatorRegistryError


def test_default_operator_registry_loads_all_supported_core_specs() -> None:
    registry = load_default_operator_registry()

    assert "stddev" in registry.operators
    assert "ts_stddev" in registry.aliases
    assert registry.operators["stddev"].execution_status == "implemented"
    assert registry.operators["stddev"].audit_status == "core_reviewed"
    assert registry.aliases["ts_stddev"].match_type == "canonical_equivalent"


def test_operator_registry_rejects_duplicate_aliases(tmp_path: Path) -> None:
    operators = tmp_path / "operators.yaml"
    aliases = tmp_path / "aliases.yaml"
    operators.write_text(
        """
schema_version: qf.operator_registry.v1
operators:
  - name: rank
    signature: rank(x)
    description: rank
    category: cross_sectional
    family: ranking
    args: [{name: x, type: series}]
    examples: [rank(close)]
""",
        encoding="utf-8",
    )
    aliases.write_text(
        """
schema_version: qf.operator_aliases.v1
aliases:
  - alias: a
    canonical: rank
    match_type: canonical_equivalent
    confidence: high
    behavior: rewrite_then_execute
  - alias: a
    canonical: rank
    match_type: canonical_equivalent
    confidence: high
    behavior: rewrite_then_execute
""",
        encoding="utf-8",
    )

    with pytest.raises(OperatorRegistryError, match="duplicate alias"):
        load_operator_registry(operators, aliases)


def test_operator_registry_rejects_paths_secrets_and_code(tmp_path: Path) -> None:
    operators = tmp_path / "operators.yaml"
    aliases = tmp_path / "aliases.yaml"
    aliases.write_text("schema_version: qf.operator_aliases.v1\naliases: []\n", encoding="utf-8")
    operators.write_text(
        """
schema_version: qf.operator_registry.v1
operators:
  - name: rank
    signature: rank(x)
    description: ../operators.py
    category: cross_sectional
    family: ranking
    args: [{name: x, type: series}]
    examples: [rank(close)]
""",
        encoding="utf-8",
    )

    with pytest.raises(OperatorRegistryError, match="local paths"):
        load_operator_registry(operators, aliases)

    operators.write_text(
        """
schema_version: qf.operator_registry.v1
operators:
  - name: rank
    signature: rank(x)
    description: secret marker fixture
    category: cross_sectional
    family: ranking
    args: [{name: x, type: series}]
    examples: [rank(close)]
""",
        encoding="utf-8",
    )
    with pytest.raises(OperatorRegistryError, match="secret-like"):
        load_operator_registry(operators, aliases)


def test_resolver_rewrites_ts_stddev_to_stddev() -> None:
    result = resolve_formula_operators("rank(-ts_stddev(return_1d, 20))")

    assert result.executable is True
    assert result.canonical_formula == "rank(-stddev(return_1d, 20))"
    assert any(item.status == "canonical_alias" for item in result.items)


def test_resolver_does_not_auto_execute_rolling_std() -> None:
    result = resolve_formula_operators("rolling_std(return_1d, 20)")

    assert result.executable is False
    assert result.requires_operator_draft_review is False
    assert "repair with canonical operator" in "; ".join(result.blocking_errors)


def test_unknown_operator_requires_draft_review() -> None:
    result = resolve_formula_operators("industry_neutralize(rank(return_1d), industry)")

    assert result.executable is False
    assert result.requires_operator_draft_review is True
    assert any(item.status == "unknown_requires_draft" for item in result.items)


def test_executor_rejects_alias_operator_directly() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "instrument": ["A", "A"],
            "return_1d": [0.1, 0.2],
        }
    )

    with pytest.raises(ValueError, match="unsupported factor operator"):
        execute_factor_formula(panel, "ts_stddev(return_1d, 2)")


def test_canonical_formula_fingerprint_treats_alias_equivalent_formulas_as_same() -> None:
    left = canonical_formula_fingerprint("rank(-ts_stddev(return_1d, 20))", 5, ())
    right = canonical_formula_fingerprint("rank(-stddev(return_1d, 20))", 5, ())

    assert left == right

"""Security regression tests for factor id validation and path hardening.

Covers SEC-1 (path traversal / glob injection via unvalidated factor_id) and
HARDEN-1 (bare '.'/'..' surviving _is_child_name / _safe_dir_name).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_engine.value_store import _is_child_name, _safe_dir_name
from quant_forge.factor_library.repository import FactorRepository


@pytest.mark.parametrize("bad_id", ["../x", "../../../../tmp/x", "*", "**", "..", ".", "", "a/b"])
def test_repository_get_rejects_traversal_and_wildcard_ids(tmp_path: Path, bad_id: str) -> None:
    repo = FactorRepository(tmp_path / "factor_root")
    with pytest.raises(ValueError):
        repo.get(bad_id)


@pytest.mark.parametrize("bad_id", ["../x", "*", "**", "..", ".", ""])
def test_repository_delete_rejects_traversal_and_wildcard_ids(tmp_path: Path, bad_id: str) -> None:
    repo = FactorRepository(tmp_path / "factor_root")
    with pytest.raises(ValueError):
        repo.delete(bad_id)


def test_repository_get_wellformed_missing_id_raises_file_not_found(tmp_path: Path) -> None:
    """Positive control: a conforming id reaches the not-found branch, not ValueError."""

    repo = FactorRepository(tmp_path / "factor_root")
    with pytest.raises(FileNotFoundError):
        repo.get("FTR_ABCD1234")


def test_repository_get_wellformed_existing_id_resolves(tmp_path: Path) -> None:
    repo = FactorRepository(tmp_path / "factor_root")
    repo.save(
        FactorDefinition(
            factor_id="WQ_ALPHA_003",
            name="worldquant_alpha_003",
            formula="rank(close)",
            status="candidate",
        )
    )
    assert repo.get("WQ_ALPHA_003").factor_id == "WQ_ALPHA_003"


def test_is_child_name_rejects_dot_segments() -> None:
    assert _is_child_name("..") is False
    assert _is_child_name(".") is False
    assert _is_child_name("WQ_ALPHA_003") is True


def test_safe_dir_name_never_yields_dot_segments() -> None:
    assert _safe_dir_name("..") == "factor"
    assert _safe_dir_name(".") == "factor"
    assert _safe_dir_name("WQ_ALPHA_003") == "WQ_ALPHA_003"

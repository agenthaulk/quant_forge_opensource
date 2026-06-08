"""Factor category helpers for portable mounted factor stores."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Literal

from quant_forge.core.contracts import FactorDefinition

FactorCategory = Literal["original", "synthetic"]

ORIGINAL_FACTOR_DIR = "原始因子"
SYNTHETIC_FACTOR_DIR = "合成因子"
FACTOR_CATEGORY_DIRS: dict[FactorCategory, str] = {
    "original": ORIGINAL_FACTOR_DIR,
    "synthetic": SYNTHETIC_FACTOR_DIR,
}
_DIR_TO_CATEGORY = {value: key for key, value in FACTOR_CATEGORY_DIRS.items()}

_SYNTHETIC_SOURCE_MARKERS = {
    "research_campaign",
    "campaign",
    "synthetic",
    "composite",
    "rd",
}
_SYNTHETIC_ID_PATTERNS = (
    re.compile(r"^FTR_CAMP(?:_|$)", re.IGNORECASE),
    re.compile(r"^GTJA_RD(?:_|$)", re.IGNORECASE),
    re.compile(r"^RD_(?:CAMP|SYN|TOP)(?:_|$)", re.IGNORECASE),
)


def factor_category(factor: FactorDefinition) -> FactorCategory:
    return factor_category_from_parts(
        factor_id=factor.factor_id,
        factor_name=factor.name,
        formula=factor.formula,
        source=factor.source,
    )


def factor_category_from_parts(
    *,
    factor_id: str,
    factor_name: str = "",
    formula: str = "",
    source: str = "",
) -> FactorCategory:
    """Classify factors into source/original factors or generated composites."""

    source_key = source.strip().lower()
    if source_key in _SYNTHETIC_SOURCE_MARKERS:
        return "synthetic"
    if any(pattern.search(factor_id.strip()) for pattern in _SYNTHETIC_ID_PATTERNS):
        return "synthetic"
    if any(pattern.search(factor_name.strip()) for pattern in _SYNTHETIC_ID_PATTERNS):
        return "synthetic"
    formula_store_key = _precomputed_store_key(formula)
    if any(pattern.search(formula_store_key) for pattern in _SYNTHETIC_ID_PATTERNS):
        return "synthetic"
    return "original"


def category_dir_name(category: FactorCategory) -> str:
    return FACTOR_CATEGORY_DIRS[category]


def category_from_dir_name(name: str) -> FactorCategory | None:
    return _DIR_TO_CATEGORY.get(name)


def categorized_dir(root: Path, category: FactorCategory) -> Path:
    return root / category_dir_name(category)


def is_factor_category_dir(path: Path) -> bool:
    return category_from_dir_name(path.name) is not None


def _precomputed_store_key(formula: str) -> str:
    stripped = formula.strip()
    if not stripped.lower().startswith("precomputed:"):
        return ""
    return stripped.split(":", 1)[1].strip()

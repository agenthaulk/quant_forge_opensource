"""Draft operator artifacts for RD hypotheses that exceed the public DSL."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from quant_forge.research_loop.contracts import FactorExperimentPlan

OPERATOR_DRAFT_SCHEMA_VERSION = "qf.operator_draft.v1"


@dataclass(frozen=True)
class OperatorDraftArtifacts:
    draft_id: str
    draft_root: str
    manifest_path: str
    semantics_request_path: str
    generated_tests_path: str
    audit_status_path: str
    review_path: str

    def to_refs(self) -> dict[str, str]:
        return asdict(self)


def write_operator_draft_artifacts(artifact_root: Path, plan: FactorExperimentPlan) -> OperatorDraftArtifacts | None:
    unknown = tuple(plan.operator_validation.get("unknown_operators", ()) or ())
    if plan.status != "requires_operator_draft_review" or not unknown:
        return None

    source_operator_name = str(unknown[0])
    draft_id = f"{_safe_identifier(source_operator_name)}_{_safe_identifier(plan.plan_id)}"
    root = artifact_root / "operator_drafts" / draft_id
    root.mkdir(parents=True, exist_ok=True)

    resolution = dict(plan.metadata.get("operator_resolution", {}) or {})
    resolution_items = resolution.get("items", []) if isinstance(resolution, dict) else []
    first_item = _resolution_item_for_operator(resolution_items, source_operator_name)
    manifest = {
        "schema_version": OPERATOR_DRAFT_SCHEMA_VERSION,
        "draft_id": draft_id,
        "operator_name": source_operator_name,
        "unknown_operator": source_operator_name,
        "status": "requires_codex_audit",
        "audit_status": "draft",
        "resolution_status": str(first_item.get("status", "unknown_requires_draft")),
        "source_plan_id": plan.plan_id,
        "source_hypothesis_id": plan.hypothesis_id,
        "source": str(plan.metadata.get("hypothesis_source", "rd_loop")),
        "raw_formula": plan.raw_formula_dsl or plan.formula_dsl,
        "canonical_formula": plan.canonical_formula_dsl or None,
        "formula_dsl": plan.formula_dsl,
        "near_matches": [],
        "operator_resolution": resolution,
        "blocking_reasons": list(plan.blocking_reasons),
        "security_boundary": "not_imported_not_executed_until_reviewed",
    }
    manifest_path = root / "manifest.json"
    semantics_request_path = root / "semantics_request.json"
    generated_tests_path = root / "generated_tests.json"
    audit_status_path = root / "audit_status.json"
    review_path = root / "review.md"

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    semantics_request_path.write_text(
        json.dumps(
            {
                "schema_version": OPERATOR_DRAFT_SCHEMA_VERSION,
                "operator_name": source_operator_name,
                "resolution_status": manifest["resolution_status"],
                "requested_semantics": "pending_codex_audit",
                "formula_dsl": plan.formula_dsl,
                "raw_formula": plan.raw_formula_dsl or plan.formula_dsl,
                "canonical_formula": plan.canonical_formula_dsl or None,
                "inputs": list(plan.inputs),
                "universe_filters": list(plan.universe_filters),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    generated_tests_path.write_text(
        json.dumps(
            {
                "schema_version": OPERATOR_DRAFT_SCHEMA_VERSION,
                "required_tests": [
                    f"operator {source_operator_name} rejects non-numeric inputs",
                    f"operator {source_operator_name} preserves trade_date/instrument alignment",
                    f"operator {source_operator_name} has no file/network/subprocess side effects",
                    f"operator {source_operator_name} is not executable until implemented in source code and registry",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    audit_status_path.write_text(
        json.dumps(
            {
                "schema_version": OPERATOR_DRAFT_SCHEMA_VERSION,
                "status": "pending_codex_audit",
                "approved_for_execution": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    review_path.write_text(
        "\n".join(
            [
                f"# Draft Operator Review: {source_operator_name}",
                "",
                "This artifact is metadata only. Quant Forge must not import or execute draft operators.",
                "",
                f"- Draft ID: `{draft_id}`",
                f"- Source plan: `{plan.plan_id}`",
                f"- Formula: `{plan.raw_formula_dsl or plan.formula_dsl}`",
                "- Promotion path: implement audited source code, add tests, then update the repo-shipped registry.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return OperatorDraftArtifacts(
        draft_id=draft_id,
        draft_root=str(root),
        manifest_path=str(manifest_path),
        semantics_request_path=str(semantics_request_path),
        generated_tests_path=str(generated_tests_path),
        audit_status_path=str(audit_status_path),
        review_path=str(review_path),
    )


def _safe_identifier(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    if not normalized or normalized[0].isdigit():
        normalized = f"op_{normalized}"
    return normalized[:80] or "operator"


def _resolution_item_for_operator(items: object, operator_name: str) -> dict[str, object]:
    if not isinstance(items, list):
        return {}
    for item in items:
        if isinstance(item, dict) and str(item.get("original_name", "")) == operator_name:
            return item
    for item in items:
        if isinstance(item, dict) and item.get("status") in {"unknown_operator", "unknown_requires_draft"}:
            return item
    return {}

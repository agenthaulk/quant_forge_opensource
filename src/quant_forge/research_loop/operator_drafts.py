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
    operator_path: str
    example_formula_path: str
    generated_tests_path: str
    audit_status_path: str

    def to_refs(self) -> dict[str, str]:
        return asdict(self)


def write_operator_draft_artifacts(artifact_root: Path, plan: FactorExperimentPlan) -> OperatorDraftArtifacts | None:
    unknown = tuple(plan.operator_validation.get("unknown_operators", ()) or ())
    if plan.status != "requires_operator_draft_review" or not unknown:
        return None

    source_operator_name = str(unknown[0])
    operator_name = _safe_identifier(source_operator_name)
    draft_id = f"{operator_name}_{_safe_identifier(plan.plan_id)}"
    root = artifact_root / "operator_drafts" / draft_id
    root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": OPERATOR_DRAFT_SCHEMA_VERSION,
        "draft_id": draft_id,
        "operator_name": source_operator_name,
        "status": "requires_codex_audit",
        "source_plan_id": plan.plan_id,
        "source_hypothesis_id": plan.hypothesis_id,
        "formula_dsl": plan.formula_dsl,
        "blocking_reasons": list(plan.blocking_reasons),
        "security_boundary": "not_imported_not_executed_until_reviewed",
    }
    manifest_path = root / "manifest.json"
    operator_path = root / "operator.py"
    example_formula_path = root / "example_formula.json"
    generated_tests_path = root / "generated_tests.json"
    audit_status_path = root / "audit_status.json"

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    operator_path.write_text(_operator_stub(source_operator_name, operator_name), encoding="utf-8")
    example_formula_path.write_text(
        json.dumps(
            {
                "schema_version": OPERATOR_DRAFT_SCHEMA_VERSION,
                "formula_dsl": plan.formula_dsl,
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
    return OperatorDraftArtifacts(
        draft_id=draft_id,
        draft_root=str(root),
        manifest_path=str(manifest_path),
        operator_path=str(operator_path),
        example_formula_path=str(example_formula_path),
        generated_tests_path=str(generated_tests_path),
        audit_status_path=str(audit_status_path),
    )


def _operator_stub(operator_name: str, function_name: str) -> str:
    return "\n".join(
        [
            f'"""Draft operator scaffold for {operator_name}.',
            "",
            "This file is generated for Codex review. Quant Forge does not import",
            "or execute draft operators until they are audited and promoted.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "",
            f"def {function_name}(*args, **kwargs):",
            '    raise NotImplementedError("draft operator requires Codex audit before execution")',
            "",
        ]
    )


def _safe_identifier(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    if not normalized or normalized[0].isdigit():
        normalized = f"op_{normalized}"
    return normalized[:80] or "operator"

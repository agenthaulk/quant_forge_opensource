"""Build compact RD context from public local catalogs and trace metadata."""

from __future__ import annotations

from pathlib import Path

from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.contracts import ResearchContext
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.trace_store import ResearchTraceStore

_MEMORY_CONTEXT_LIMIT = 5


class ResearchContextBuilder:
    def __init__(
        self,
        *,
        factor_root: Path,
        data_root: Path,
        factor_values_root: Path | None = None,
        factor_values_manifest_root: Path | None = None,
        trace_store: ResearchTraceStore | None = None,
        memory_store: ResearchMemoryStore | None = None,
        market: str = "cn_a",
    ) -> None:
        self.factor_root = factor_root
        self.data_root = data_root
        self.factor_values_root = factor_values_root
        self.factor_values_manifest_root = factor_values_manifest_root
        self.trace_store = trace_store
        self.memory_store = memory_store
        self.market = market

    def build(
        self,
        *,
        objective: str = "balanced",
        seed_factor_ids: tuple[str, ...] = (),
        run_date_range: str = "",
    ) -> ResearchContext:
        factors = FactorCatalog(
            self.factor_root,
            factor_values_root=self.factor_values_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        ).list()
        by_id = {factor.factor_id: factor for factor in factors}
        seeds = tuple(_factor_summary(by_id[factor_id]) for factor_id in seed_factor_ids if factor_id in by_id)
        effective = tuple(
            _factor_summary(factor) for factor in factors if factor.status in {"candidate", "active"}
        )[:20]
        recent = (
            self.trace_store.read_recent_entries(limit=20, phases={"experiment_result", "plan_blocked"})
            if self.trace_store is not None
            else []
        )
        terminal = tuple(item for item in recent if _is_terminal_trace(item))
        successes = tuple(item for item in terminal if _trace_passed(item))
        failures = tuple(item for item in terminal if not _trace_passed(item))
        memory_failures = self._memory_items("failure")
        memory_findings = self._memory_items("finding")
        field_catalog = tuple(dict(field) for field in list_available_fields())
        operator_catalog = tuple(dict(operator) for operator in list_available_operators())
        return ResearchContext(
            market=self.market,
            data_root="<configured:data_root>",
            factor_root="<configured:factor_root>",
            objective=objective,
            run_date_range=run_date_range,
            available_fields=tuple(str(field["name"]) for field in field_catalog),
            available_operators=tuple(str(operator["name"]) for operator in operator_catalog),
            field_catalog=field_catalog,
            operator_catalog=operator_catalog,
            available_filters=("is_st == false",),
            seed_factor_summary=seeds,
            effective_ideas=effective,
            recent_successes=successes[-5:] + memory_findings,
            recent_failures=failures[-5:] + memory_failures,
            next_focus_hints=_next_focus_hints(failures),
            prompt_context=_prompt_context(seeds, effective, failures),
        )

    def _memory_items(self, kind: str) -> tuple[dict[str, object], ...]:
        """Durable memory rows as context items: redacted statements only,
        marked with ``{"source": "research_memory"}`` so prompt assembly can
        distinguish them from same-run trace entries."""

        if self.memory_store is None:
            return ()
        return tuple(
            {
                "source": "research_memory",
                "kind": str(row.get("kind") or kind),
                "statement": str(row.get("statement") or ""),
                "observation_count": int(row.get("observation_count") or 0),
            }
            for row in self.memory_store.read_recent(kind, _MEMORY_CONTEXT_LIMIT)
        )


def _factor_summary(factor: object) -> dict[str, object]:
    formula = str(getattr(factor, "formula"))
    return {
        "factor_id": getattr(factor, "factor_id"),
        "name": getattr(factor, "name"),
        "formula": "<mounted_precomputed_reference_not_usable_in_formula>"
        if is_precomputed_formula(formula)
        else formula,
        "status": getattr(factor, "status"),
        "horizon_days": getattr(factor, "horizon_days"),
        "universe_filters": list(getattr(factor, "universe_filters")),
        "source": getattr(factor, "source"),
    }


def _trace_passed(entry: dict[str, object]) -> bool:
    decision = entry.get("gate_decision")
    return isinstance(decision, dict) and bool(decision.get("accepted") or decision.get("status") == "passed")


def _is_terminal_trace(entry: dict[str, object]) -> bool:
    return str(entry.get("phase") or "") in {"experiment_result", "plan_blocked"}


def _next_focus_hints(failures: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    hints: list[str] = []
    for entry in failures:
        hint = str(entry.get("next_hypothesis_hint") or "")
        if not hint and isinstance(entry.get("feedback"), dict):
            hint = str(entry["feedback"].get("next_hypothesis_hint") or "")  # type: ignore[index]
        if hint:
            hints.append(hint)
    return tuple(sorted(set(hints)))


def _prompt_context(
    seeds: tuple[dict[str, object], ...],
    effective: tuple[dict[str, object], ...],
    failures: tuple[dict[str, object], ...],
) -> str:
    parts: list[str] = []
    if seeds:
        parts.append(f"Seed factors: {len(seeds)} selected.")
    if effective:
        parts.append(f"Effective ideas available: {len(effective)} candidate/active factors.")
    if failures:
        parts.append(f"Recent blocked/failed traces: {len(failures)}.")
    return "\n".join(parts)

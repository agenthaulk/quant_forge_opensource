"""Build compact RD context from public local catalogs and trace metadata."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.contracts import ResearchContext
from quant_forge.research_loop.feedback_builder import NEXT_HYPOTHESIS_HINT_TEMPLATES
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.trace_store import ResearchTraceStore

_MEMORY_CONTEXT_LIMIT = 5

# SE-iv: the active_rules channel is capped independently of the plain
# memory-tier cap above so it stays a small, bounded steering signal even if
# a reviewer activates many rules at once.
_ACTIVE_RULES_LIMIT = 5

# Hints are producer-side fixed templates; rows read back from trace.jsonl
# must match the template set or they are silently skipped (P1/F4).
_KNOWN_FOCUS_HINTS = frozenset(NEXT_HYPOTHESIS_HINT_TEMPLATES)


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
        active_rules = self._active_rules()
        # Cross-tier dedup (SE-iv): a signature already steering as an active
        # rule is excluded from the passive finding/failure feed in the same
        # context so the same lesson never appears twice under two labels.
        active_rule_signatures = frozenset(str(row.get("signature") or "") for row in active_rules)
        memory_failures = self._memory_items("failure", exclude_signatures=active_rule_signatures)
        memory_findings = self._memory_items("finding", exclude_signatures=active_rule_signatures)
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
            active_rules=active_rules,
        )

    def _memory_items(
        self, kind: str, *, exclude_signatures: frozenset[str] = frozenset()
    ) -> tuple[dict[str, object], ...]:
        """Durable memory rows as context items: redacted statements only,
        marked with ``{"source": "research_memory"}`` so prompt assembly can
        distinguish them from same-run trace entries.

        Retired finding/failure signatures (SE-iii review events) and any
        signature already surfaced through ``exclude_signatures`` (the
        active_rules cross-tier dedup, SE-iv) never reach this feed.
        """

        if self.memory_store is None:
            return ()
        retired = self.memory_store.retired_signatures(kind) if kind in ("finding", "failure") else frozenset()
        excluded = retired | exclude_signatures
        return tuple(
            {
                "source": "research_memory",
                "kind": str(row.get("kind") or kind),
                "statement": str(row.get("statement") or ""),
                "observation_count": int(row.get("observation_count") or 0),
            }
            for row in self.memory_store.read_recent(kind, _MEMORY_CONTEXT_LIMIT)
            if str(row.get("signature") or "") not in excluded
        )

    def _active_rules(self) -> tuple[dict[str, object], ...]:
        """Bounded, human-activated steering rules (SE-iv).

        Only rule signatures whose LATEST review event is ``activate`` reach
        this feed (:meth:`ResearchMemoryStore.rule_activation_events` —
        promoted rule ROWS stay ``needs_human_review`` forever and are never
        consulted for activity, FP-2). Rows with an exact scope match to this
        builder's own scope context sort before global rows, then by
        activation recency (most recently activated first), then by a stable
        row key; the result is capped at ``_ACTIVE_RULES_LIMIT`` so this
        channel can never dominate the prompt. Zero activated rules is zero
        effect: an empty tuple, same as if the channel did not exist.
        """

        if self.memory_store is None:
            return ()
        activation_events = self.memory_store.rule_activation_events()
        activated = {
            signature: event for signature, event in activation_events.items() if event.get("action") == "activate"
        }
        if not activated:
            return ()
        rows = [row for row in self.memory_store.list_promoted("rule") if str(row.get("signature")) in activated]
        if not rows:
            return ()
        builder_scope = self._builder_scope_key()

        def _activated_at(row: dict[str, Any]) -> datetime:
            event = activated[str(row.get("signature"))]
            return _parse_iso_timestamp(str(event.get("decided_at") or ""))

        def _scope_rank(row: dict[str, Any]) -> int:
            row_scope = str(row.get("scope") or "global")
            return 0 if (builder_scope != "global" and row_scope == builder_scope) else 1

        # Stable multi-pass sort (least-significant key first): Python's
        # sorted() is stable, so applying passes in reverse priority order
        # yields a correct combined ordering even though activation-recency
        # must sort descending while scope-rank and the row-key tiebreak sort
        # ascending.
        ordered = sorted(rows, key=lambda row: str(row.get("entry_id") or ""))
        ordered = sorted(ordered, key=_activated_at, reverse=True)
        ordered = sorted(ordered, key=_scope_rank)
        return tuple(dict(row) for row in ordered[:_ACTIVE_RULES_LIMIT])

    def _builder_scope_key(self) -> str:
        """This builder's own scope context, rendered in the SAME
        ``key=value`` grammar :meth:`~quant_forge.research_loop.outcomes.
        OutcomeScope.scope_key` uses, so an active rule's stored ``scope``
        field can be compared for an exact match. Pre-generation context has
        no candidate-specific scope (no factor family/horizon/settings yet),
        only the configured market, which maps to the ``asset`` dimension.
        """

        market = (self.market or "").strip()
        return f"asset={market}" if market else "global"


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


def _parse_iso_timestamp(value: str) -> datetime:
    """Parse a tz-aware ISO timestamp for chronological sorting.

    Lexicographic string comparison is not reliable across differing UTC
    offsets, so activation-recency ordering parses into real ``datetime``
    values (mirroring the ``Z``-suffix normalization memory.py already uses
    for its own timestamps) rather than comparing raw strings.
    """

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _next_focus_hints(failures: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    hints: list[str] = []
    for entry in failures:
        hint = str(entry.get("next_hypothesis_hint") or "")
        if not hint and isinstance(entry.get("feedback"), dict):
            hint = str(entry["feedback"].get("next_hypothesis_hint") or "")  # type: ignore[index]
        if hint in _KNOWN_FOCUS_HINTS:
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

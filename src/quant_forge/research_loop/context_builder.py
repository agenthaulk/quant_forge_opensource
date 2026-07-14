"""Build compact RD context from public local catalogs and trace metadata."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Any

from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.contracts import ResearchContext
from quant_forge.research_loop.feedback_builder import NEXT_HYPOTHESIS_HINT_TEMPLATES
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.trace_store import ResearchTraceStore

logger = logging.getLogger(__name__)

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
        # Pre-activation silencing (P4a rework item 1) + full-set cross-tier
        # dedup (item 5): ANY rule-tier signature -- pending OR active, and
        # regardless of whether the bounded `active_rules` pipeline below
        # ends up DISPLAYING it after authentication/eligibility/cap -- is
        # excluded from the passive finding/failure feed. This is computed
        # from the store's UNBOUNDED rule-tier signature set, never from the
        # (possibly capped-out or authentication-dropped) `active_rules`
        # tuple, so a signature can never leak into the lower tiers just
        # because it lost a cap slot or failed template authentication.
        rule_tier_signatures = (
            self.memory_store.rule_tier_signatures() if self.memory_store is not None else frozenset()
        )
        memory_failures = self._memory_items("failure", exclude_signatures=rule_tier_signatures)
        memory_findings = self._memory_items("finding", exclude_signatures=rule_tier_signatures)
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
        rule-tier pre-activation-silencing + cross-tier dedup set, P4a
        rework items 1/5) never reach this feed.
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
        """Bounded, human-activated steering rules (SE-iv; P4a rework items
        4/5/6/11).

        Pipeline, in order (item 5 -- AUTH-BEFORE-CAP so a malformed or
        foreign row can never consume a cap slot a valid rule could have
        used, and dedup elsewhere in :meth:`build` is computed from the
        store's unbounded rule-tier signatures rather than this method's
        capped output):

        1. Effective activations: only rule signatures whose LATEST,
           ROW-BOUND review event is ``activate`` reach this feed at all
           (:meth:`ResearchMemoryStore.rule_activation_events` already
           applies the row-binding + file-append-order derivation; promoted
           rule ROWS stay ``needs_human_review`` forever and are never
           consulted for activity, FP-2).
        2. Statement + scope authentication: each candidate row's statement
           AND scope must pass :func:`~quant_forge.research_loop.llm.
           authenticate_active_rule_item` (the canonical closed-template
           parser, items 4/6) -- a malformed or foreign row is dropped here,
           logged, and never reaches ordering or the cap.
        3. Scope eligibility (item 4b): only a row whose scope is an EXACT
           match to this builder's own scope context, or ``"global"``,
           survives -- a mismatched scope (e.g. a ``asset=us`` rule steering
           a ``cn_a`` run) is DISCARDED here, not merely deprioritized.
        4. Ordering: exact-scope-match rows sort before global rows, then by
           activation recency (most recently activated first), then by a
           stable row key.
        5. Global-slot reservation (item 11): see :func:`_reserve_global_slot`.
        6. Cap at ``_ACTIVE_RULES_LIMIT``.

        Zero activated rules is zero effect: an empty tuple, same as if the
        channel did not exist.
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

        # Deferred import (not module-level): context_builder -> llm would
        # otherwise cycle back through llm -> service -> context_builder
        # (service.py imports ResearchContextBuilder). See
        # authenticate_active_rule_item's own docstring for the full
        # rationale; this is the SAME canonical parser llm.py's own prompt
        # gate calls, not an independently-coded copy.
        from quant_forge.research_loop.llm import authenticate_active_rule_item

        authenticated_rows: list[dict[str, Any]] = []
        for row in rows:
            statement = str(row.get("statement") or "")
            scope = str(row.get("scope") or "global")
            if authenticate_active_rule_item(statement, scope):
                authenticated_rows.append(row)
            else:
                logger.warning(
                    "dropping active rule entry_id=%s signature=%s: statement/scope failed the closed-"
                    "template authentication gate",
                    str(row.get("entry_id") or "")[:12],
                    row.get("signature"),
                )
        if not authenticated_rows:
            return ()

        builder_scope = self._builder_scope_key()
        eligible_rows = [
            row for row in authenticated_rows if str(row.get("scope") or "global") in ("global", builder_scope)
        ]
        if not eligible_rows:
            return ()

        def _activated_at(row: dict[str, Any]) -> datetime:
            event = activated[str(row.get("signature"))]
            return _parse_iso_timestamp(str(event.get("decided_at") or ""))

        def _scope_rank(row: dict[str, Any]) -> int:
            # Every surviving row's scope is already either an exact match
            # or "global" (the eligibility filter above discarded anything
            # else), so this only distinguishes those two buckets.
            return 0 if (builder_scope != "global" and str(row.get("scope") or "global") == builder_scope) else 1

        # Stable multi-pass sort (least-significant key first): Python's
        # sorted() is stable, so applying passes in reverse priority order
        # yields a correct combined ordering even though activation-recency
        # must sort descending while scope-rank and the row-key tiebreak sort
        # ascending.
        ordered = sorted(eligible_rows, key=lambda row: str(row.get("entry_id") or ""))
        ordered = sorted(ordered, key=_activated_at, reverse=True)
        ordered = sorted(ordered, key=_scope_rank)
        ordered = _reserve_global_slot(ordered, limit=_ACTIVE_RULES_LIMIT)
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


def _reserve_global_slot(ordered: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    """Global-safety-rule slot reservation (P4a rework item 11, Fable ruling
    on an opus review observation).

    When the top ``limit`` rows of ``ordered`` (already scope-rank-then-
    activation-recency sorted, so exact-scope-match rows sort before global
    ones) are ALL exact-scope matches -- i.e. enough narrower rules are
    active to fill the entire cap on their own -- a human-activated GLOBAL
    safety rule must not be silently starved just because it always sorts
    after exact matches. If no global row makes the natural top-``limit``
    cut, this reserves the LAST slot for the most recently activated global
    row instead (the first global entry in ``ordered``, since that ordering
    is already recency-descending within the global bucket). If a global
    row already appears naturally (fewer than ``limit`` exact-scope rows
    exist, or the cap was not even reached), nothing changes.
    """

    capped = ordered[:limit]
    if len(capped) < limit or any(row.get("scope") == "global" for row in capped):
        return capped
    global_rows = [row for row in ordered if row.get("scope") == "global"]
    if not global_rows:
        return capped  # no global rule exists among the eligible set at all
    return capped[: limit - 1] + [global_rows[0]]


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

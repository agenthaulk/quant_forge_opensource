"""Web payload/action module for the SE-P4b memory-review tab (SE-iii).

Pure orchestration over the already-reviewed, frozen engine-side contracts in
:mod:`quant_forge.research_loop.memory` and :mod:`quant_forge.research_loop.priors`
-- this module never hand-writes a JSONL row and never re-derives a state
this codebase already computes elsewhere. It only:

- shapes :meth:`ResearchMemoryStore.rule_review_snapshot`,
  :meth:`ResearchMemoryStore.list_promoted`, :meth:`ResearchMemoryStore.
  retired_signatures`, and :func:`quant_forge.research_loop.priors.
  compute_priors` into one JSON-serializable payload for the Web tab
  (:func:`memory_review_payload`);
- appends exactly one review event per action, through
  :meth:`ResearchMemoryStore.resolve_validate_append` (:func:`review_rule`,
  :func:`review_promoted`) -- the SAME atomic resolve+append path
  ``qf memory rules`` CLI parity commands use, closing the TOCTOU window a
  hand-chained ``resolve_signature_prefix`` + ``record_review_event`` would
  leave open.

Rule action vocabulary (owner ruling R3 / SE-iii): the frozen
``MemoryReviewEvent`` contract accepts ``activate``/``deactivate`` ONLY for
``target_kind="rule"`` and ``retire``/``unretire`` ONLY for ``target_kind`` in
``{"finding", "failure"}`` -- a rule can never be retired/unretired through
this store, and :meth:`ResearchMemoryStore.rule_review_snapshot`'s state
vocabulary (``active`` / ``never_reviewed`` / ``deactivated`` /
``lapsed_pending_re_review``) has no "retired" state. :func:`review_rule`
mirrors that closed set rather than the broader four-action list a rule row
might naively suggest.

No new steering (SE-iv): this module only reads memory and records review
events; nothing here composes a prompt or reaches
``research_loop.context_builder``/``llm.py``.

Actor is required non-empty for every action (fails fast, before any lock is
taken); ``rationale`` is optional. Both pass through the store's own
``redact_free_text`` on write -- no free text beyond those two fields is
accepted here.
"""

from __future__ import annotations

from typing import Any

from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.priors import compute_priors

MEMORY_REVIEW_PAYLOAD_SCHEMA_VERSION = "qf.web.memory_review.v1"

# Closed action sets per target kind (mirrors the frozen MemoryReviewEvent
# contract in research_loop/memory.py -- see the module docstring above).
RULE_REVIEW_ACTIONS = ("activate", "deactivate")
PROMOTED_REVIEW_ACTIONS = ("retire", "unretire")
PROMOTED_KINDS = ("finding", "failure")

# Sort priority for the rules table (workorder spec): needs-review states
# first (never_reviewed, lapsed_pending_re_review, tied), then active, then
# deactivated. Ties within a priority group are broken by last_seen desc,
# applied as a separate, earlier stable sort pass -- see _rules_payload.
_RULE_STATE_SORT_PRIORITY = {
    "never_reviewed": 0,
    "lapsed_pending_re_review": 0,
    "active": 1,
    "deactivated": 2,
}


def _require_actor(actor: str) -> None:
    if not actor or not actor.strip():
        raise ValueError("actor is required")


def _rules_payload(store: ResearchMemoryStore, *, include_actions: bool) -> list[dict[str, Any]]:
    """Every live rule row from :meth:`ResearchMemoryStore.rule_review_snapshot`,
    flattened to one dict per row with its 4-state label plus review-event
    display fields, sorted needs-review-first then by last_seen desc within
    each state group (two-pass stable sort: Python's ``sort`` is stable, so
    sorting by last_seen desc FIRST and state priority SECOND preserves the
    last_seen ordering as the tiebreak within each priority group).
    """

    snapshot = store.rule_review_snapshot()
    rows: list[dict[str, Any]] = []
    for info in snapshot.values():
        state = str(info["state"])
        entry: dict[str, Any] = {
            **info["row"],
            "state": state,
            "event_id": info["event_id"],
            "reviewed_entry_id": info["reviewed_entry_id"],
            "activation_seq": info["activation_seq"],
            "decided_at": info["decided_at"],
        }
        if include_actions:
            # Never offer a no-op decision on the row's own current state;
            # never_reviewed / lapsed_pending_re_review offer both (a first
            # or re-review decision is meaningful either way). Retire/
            # unretire are never offered for rules -- see the module
            # docstring: the frozen store rejects them for target_kind="rule".
            entry["can_activate"] = state != "active"
            entry["can_deactivate"] = state != "deactivated"
        rows.append(entry)
    rows.sort(key=lambda entry: str(entry.get("last_seen") or ""), reverse=True)
    rows.sort(key=lambda entry: _RULE_STATE_SORT_PRIORITY.get(entry["state"], 1))
    return rows


def _promoted_payload(store: ResearchMemoryStore, kind: str, *, include_actions: bool) -> list[dict[str, Any]]:
    """Every live ``kind`` row from :meth:`ResearchMemoryStore.list_promoted`
    (already newest-last_seen-first), with retirement joined in from
    :meth:`ResearchMemoryStore.retired_signatures` -- retirement is an event
    overlay, never a row mutation (SE-iii), so it is never present on the row
    itself and must be computed here.
    """

    retired = store.retired_signatures(kind)
    rows: list[dict[str, Any]] = []
    for row in store.list_promoted(kind):
        signature = str(row.get("signature") or "")
        is_retired = signature in retired
        entry: dict[str, Any] = {**row, "review_state": "retired" if is_retired else "active"}
        if include_actions:
            entry["can_retire"] = not is_retired
            entry["can_unretire"] = is_retired
        rows.append(entry)
    return rows


def _domain_payload(store: ResearchMemoryStore, *, include_actions: bool) -> dict[str, Any]:
    return {
        "rules": _rules_payload(store, include_actions=include_actions),
        "findings": _promoted_payload(store, "finding", include_actions=include_actions),
        "failures": _promoted_payload(store, "failure", include_actions=include_actions),
        # The WHOLE priors view, honesty counters included verbatim: as_of,
        # invalid_rows, oos_excluded, and each table's own unbucketed count
        # all ride inside to_dict() already -- nothing here drops a field.
        "priors": compute_priors(store).to_dict(),
    }


def memory_review_payload(
    store: ResearchMemoryStore, plugin_store: ResearchMemoryStore | None = None
) -> dict[str, Any]:
    """The full SE-P4b review-tab payload: main-store rules/findings/failures/
    priors WITH action-eligibility flags, plus an optional read-only plugin
    pane (R5) carrying the SAME shape with NO eligibility flags anywhere --
    the plugin domain is pure display, never an action target from this
    surface. ``plugin_store is None`` omits the ``"plugin"`` pane entirely
    (V1: no config hook resolves a plugin artifact root yet -- see routing
    wiring / the implementation report).
    """

    payload: dict[str, Any] = {
        "schema_version": MEMORY_REVIEW_PAYLOAD_SCHEMA_VERSION,
        **_domain_payload(store, include_actions=True),
        "plugin": None,
    }
    if plugin_store is not None:
        payload["plugin"] = _domain_payload(plugin_store, include_actions=False)
    return payload


def review_rule(
    store: ResearchMemoryStore,
    plugin_store: ResearchMemoryStore | None = None,
    *,
    signature_prefix: str,
    action: str,
    actor: str,
    rationale: str = "",
) -> dict[str, Any]:
    """Record one activate/deactivate review event for a rule signature and
    return the refreshed :func:`memory_review_payload`.

    ``signature_prefix`` is resolved (and the event appended) inside ONE
    atomic lock hold via :meth:`ResearchMemoryStore.resolve_validate_append`:
    an ambiguous or absent prefix raises ``ValueError`` naming every
    candidate (the R3 anti-fat-finger check) before anything is written.
    """

    _require_actor(actor)
    if action not in RULE_REVIEW_ACTIONS:
        raise ValueError(f"rule review action must be one of {RULE_REVIEW_ACTIONS}, got {action!r}")
    store.resolve_validate_append(
        target_kind="rule",
        prefix=signature_prefix,
        action=action,
        actor=actor,
        rationale=rationale,
    )
    return memory_review_payload(store, plugin_store)


def review_promoted(
    store: ResearchMemoryStore,
    plugin_store: ResearchMemoryStore | None = None,
    *,
    kind: str,
    signature_prefix: str,
    action: str,
    actor: str,
    rationale: str = "",
) -> dict[str, Any]:
    """Record one retire/unretire review event for a finding/failure
    signature and return the refreshed :func:`memory_review_payload`. Same
    atomic resolve+append discipline as :func:`review_rule`.
    """

    _require_actor(actor)
    if kind not in PROMOTED_KINDS:
        raise ValueError(f"kind must be one of {PROMOTED_KINDS}, got {kind!r}")
    if action not in PROMOTED_REVIEW_ACTIONS:
        raise ValueError(f"promoted review action must be one of {PROMOTED_REVIEW_ACTIONS}, got {action!r}")
    store.resolve_validate_append(
        target_kind=kind,
        prefix=signature_prefix,
        action=action,
        actor=actor,
        rationale=rationale,
    )
    return memory_review_payload(store, plugin_store)

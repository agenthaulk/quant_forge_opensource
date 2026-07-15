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


_PROMOTED_STABLE_READ_ATTEMPTS = 3


def _entry_id_mapping(rows: tuple[dict[str, Any], ...]) -> dict[str, str]:
    return {str(row.get("signature") or ""): str(row.get("entry_id") or "") for row in rows}


def _stable_promoted_rows_and_retired(
    store: ResearchMemoryStore, kind: str
) -> tuple[tuple[dict[str, Any], ...], frozenset[str]]:
    """(rows, retired_signatures) for ``kind``, read as one consistent
    snapshot -- without a single atomic store method to do it in one read
    (review finding P4B-F2).

    :meth:`ResearchMemoryStore.list_promoted` and :meth:`ResearchMemoryStore.
    retired_signatures` are each independently locked (the store takes and
    releases its advisory lock once per call), so calling them back to back
    is two separate snapshots of the store, not one: a ``promote_pending()``
    landing between them can supersede a row (new ``entry_id``) exactly when
    that signature's retire event was bound to the OLD ``entry_id`` -- the
    frozen contract (``_latest_events_by_signature_unlocked``) then makes
    that event lapse, so the signature is no longer retired, but a
    ``retired_signatures()`` read from BEFORE the supersession would still
    say it is. Pairing that stale flag with the NEW (post-supersession) row
    falsely labels fresh, never-reviewed content as retired.

    Unlike rules (:meth:`ResearchMemoryStore.rule_review_snapshot` already
    reads rows and events in ONE lock hold), there is no equivalent single
    public method for finding/failure retirement -- adding one would touch
    the frozen store contract, which is out of scope here. Instead this
    reads the SAME two public methods repeatedly and only trusts a pairing
    once it has evidence nothing moved underneath it: two consecutive
    ``list_promoted`` reads whose ``(signature -> entry_id)`` mapping is
    IDENTICAL bracket the ``retired_signatures`` read in between them, so
    nothing could have superseded a row inside that bracket without ALSO
    changing the second rows read -- the retired set read inside a stable
    bracket safely corresponds to the rows on either side of it. Bounded to
    :data:`_PROMOTED_STABLE_READ_ATTEMPTS` attempts; on persistent churn (a
    pathological continuously-promoting workload that never lets two
    consecutive reads agree) this returns the LATEST pair rather than
    blocking forever -- append-only, single-host-writer files (the store's
    own operating assumption) make convergence the overwhelmingly common
    case in practice, and an unbounded retry loop would be a new liveness
    risk this read-only surface should not own.
    """

    rows: tuple[dict[str, Any], ...] = ()
    retired: frozenset[str] = frozenset()
    previous_mapping: dict[str, str] | None = None
    for _ in range(_PROMOTED_STABLE_READ_ATTEMPTS):
        rows = store.list_promoted(kind)
        retired = store.retired_signatures(kind)
        mapping = _entry_id_mapping(rows)
        if previous_mapping is not None and mapping == previous_mapping:
            break
        previous_mapping = mapping
    return rows, retired


def _promoted_payload(store: ResearchMemoryStore, kind: str, *, include_actions: bool) -> list[dict[str, Any]]:
    """Every live ``kind`` row from :meth:`ResearchMemoryStore.list_promoted`
    (already newest-last_seen-first), with retirement joined in from
    :meth:`ResearchMemoryStore.retired_signatures` -- retirement is an event
    overlay, never a row mutation (SE-iii), so it is never present on the row
    itself and must be computed here. Rows and retirement are read together
    via :func:`_stable_promoted_rows_and_retired` (P4B-F2), never as two
    independently-racy calls.
    """

    rows, retired = _stable_promoted_rows_and_retired(store, kind)
    entries: list[dict[str, Any]] = []
    for row in rows:
        signature = str(row.get("signature") or "")
        is_retired = signature in retired
        entry: dict[str, Any] = {**row, "review_state": "retired" if is_retired else "active"}
        if include_actions:
            entry["can_retire"] = not is_retired
            entry["can_unretire"] = is_retired
        entries.append(entry)
    return entries


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
    (V1: no config/env hook anywhere in this repo resolves a plugin
    artifact root yet, so ``apps/web/routing.py`` never constructs one --
    see its ``GET /api/memory/review`` handler).
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

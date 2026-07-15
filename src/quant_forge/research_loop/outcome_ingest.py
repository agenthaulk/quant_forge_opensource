"""Shared ingress sink: ``ResearchOutcome`` -> ``ResearchMemoryStore`` (SE-P2).

The ONE place a :class:`~quant_forge.research_loop.outcomes.ResearchOutcome`
ever gets recorded into a :class:`~quant_forge.research_loop.memory.
ResearchMemoryStore`: append the outcome's canonical ledger envelope (exact
replay-drop by ``outcome_id``), turn the outcome into its trace-tier
``MemoryObservation`` rows via the pure ``outcomes.outcome_to_observations``
mapper and record them, then run ``promote_pending()`` so repeated evidence
promotes deterministically. Pure orchestration over the store's OWN locked
methods -- no new lock, no I/O of its own beyond calling them (DECISIONS.md
"2026-07-13 -- Self-evolution engine CP0", ruling SE-ix: "Memory-store
appends and promote/activate critical sections adopt the lineage
advisory-lock pattern" -- already true of every ``ResearchMemoryStore``
method this module calls; this module adds no second lock).

Store-agnostic by design (SE-i dual domain): the CALLER selects which store
instance to ingest into. ``service.py`` always passes the MAIN store (LOCAL
outcomes only -- SE-i cancelled main-store external ingress); a future
SE-P3 plugin-adapter refactor could reuse this sink for its OWN plugin-local
store instance (out of scope here -- ``worldquant/`` is untouched by this
module, and this module never imports it). This sink has no opinion on
outcome PRODUCTION either: it accepts an already-built ``ResearchOutcome``
and does not import ``local_outcomes`` or any producer.

Replay short-circuit + envelope-last ordering (crash-safety; SE-P2 review
finding P2-F5): an ``outcome_id`` already on the ledger appends NOTHING --
no envelope, no observations -- so administrative replays can never grow
``observations.jsonl`` or shift the pre-threshold representative row toward
a resend's payload. For a NEW outcome the observations are appended FIRST
and the envelope LAST, as the completion marker (the same
transactional-marker-last discipline the SE-P3 plugin ingest uses):

* crash after some observations, before the envelope -> the retry sees an
  unknown id and re-appends everything; the duplicated observation rows are
  bounded to real crash windows (not every replay) and stay scientifically
  inert -- ``memory.promote``'s evidence-unit cap (<=1 observation per
  ``(signature, run_id)``, where ``run_id`` is ``evidence_run_id()``) means
  a duplicate never inflates a promotion count;
* crash after the envelope, before promotion -> the retry short-circuits
  the appends but STILL runs ``promote_pending()`` (promotion is a pure,
  deterministic function of the full observation set, so this is the
  self-healing step, never a source of drift).
"""

from __future__ import annotations

from dataclasses import dataclass

from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.outcomes import ResearchOutcome, outcome_to_observations

__all__ = ["IngestReceipt", "ingest_outcome"]


@dataclass(frozen=True)
class IngestReceipt:
    """Result of one :func:`ingest_outcome` call."""

    outcome_id: str
    recorded: bool
    observation_count: int
    as_of: int


def ingest_outcome(store: ResearchMemoryStore, outcome: ResearchOutcome) -> IngestReceipt:
    """Ingest one outcome into ``store``: observations, envelope, promotion.

    ``recorded`` is False exactly when ``outcome.outcome_id()`` was already
    present on the ledger (an exact administrative replay); a replay appends
    NOTHING, so ``observation_count`` -- the number of :class:`~quant_forge.
    research_loop.memory.MemoryObservation` rows THIS call submitted -- is 0
    for a replay (P2-F5), and 0 for an ``unknown``/``not_applicable``
    verdict outcome, which mints none by design (see ``outcomes.
    outcome_to_observations``). ``promote_pending()`` runs on every call,
    replay included -- see the module docstring's crash-window walk-through.
    ``as_of`` is the store's outcomes-ledger revision AFTER this call
    (SE-P5's snapshot input): stable across a replay, strictly higher after
    a genuinely new outcome.
    """

    record = outcome.to_record()
    outcome_id = str(record["outcome_id"])
    # ONE store-level critical section (RV2-F3): the replay check, the
    # observation appends, and the envelope completion marker are a single
    # lock hold inside the store, so two concurrent ingests of the same
    # outcome cannot both pass the check and double-append.
    recorded, observation_count = store.ingest_outcome_rows(record, outcome_to_observations(outcome))
    store.promote_pending()
    return IngestReceipt(
        outcome_id=outcome_id,
        recorded=recorded,
        observation_count=observation_count,
        as_of=store.outcomes_revision(),
    )

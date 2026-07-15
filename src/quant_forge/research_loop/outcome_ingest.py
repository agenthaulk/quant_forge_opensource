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

Unconditional recording (crash-safety): steps 2 and 3 in
:func:`ingest_outcome` run REGARDLESS of whether step 1 recorded a new
ledger envelope or dropped an exact replay. This is deliberate, not
redundant: ``outcome_id`` excludes ``observed_at`` (an administrative resend
of the exact same measurement keeps the same id), so a process that crashed
between step 1 and step 2 on a PRIOR attempt would otherwise lose that
outcome's observations forever on retry if this sink skipped them whenever
``recorded`` came back False. Double-recording is harmless:
``memory.promote``'s own evidence-unit cap (<=1 observation per
``(signature, run_id)`` reaches the promotion thresholds, where ``run_id``
here is the outcome's ``evidence_run_id()``) absorbs the duplicate, so a
replay never inflates a promotion count.
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
    """Ingest one outcome into ``store``: envelope, observations, promotion.

    ``recorded`` is False exactly when ``outcome.outcome_id()`` was already
    present on the ledger (an exact administrative replay); ``observation_
    count`` is the number of :class:`~quant_forge.research_loop.memory.
    MemoryObservation` rows this call submitted (0 for an ``unknown``/
    ``not_applicable`` verdict outcome, which mints none by design -- see
    ``outcomes.outcome_to_observations``), independent of ``recorded``.
    ``as_of`` is the store's outcomes-ledger revision AFTER this call (SE-P5's
    snapshot input): stable across a replay, strictly higher after a
    genuinely new outcome. See the module docstring for why both the
    observation recording and the promotion step run unconditionally.
    """

    record = outcome.to_record()
    recorded = store.record_outcome_envelope(record)
    observations = outcome_to_observations(outcome)
    for observation in observations:
        store.record_observation(
            signature=observation.signature,
            statement=observation.statement,
            run_id=observation.run_id,
            data_window=observation.data_window,
            failure_class=observation.failure_class,
            evidence_ref=observation.evidence_ref,
            observed_at=observation.observed_at,
            scope=observation.scope,
        )
    store.promote_pending()
    return IngestReceipt(
        outcome_id=str(record["outcome_id"]),
        recorded=recorded,
        observation_count=len(observations),
        as_of=store.outcomes_revision(),
    )

"""The frozen ``planning_influence_snapshot`` contract (SE-P5, ruling SE-ix).

One snapshot answers, durably and hashably: *"exactly which learned
steering COULD have influenced the research launched from this
confirmation?"* It is captured at pipeline confirm time (the FE track
wires the capture in FE-P3; the reserved ``planning_influence_hash`` slot
already participates in the pipeline ``input_hash``), persisted alongside
the run artifacts, and rendered ONLY by the canonical disclosure
components. The sidecar gets no general memory-read tool -- this
run-local projection is the whole surface (SE-ix).

Contents (each field carries its cutoff semantics):

* ``as_of`` -- the outcomes-ledger revision the priors basis was computed
  from (one number; two snapshots with equal ``as_of`` + equal query
  fingerprint saw identical priors).
* ``rule_activation_seq_max`` -- the highest ``activation_seq`` among the
  eligible rules (the rule-side cutoff: the activations file is
  append-only, so this single integer pins which review events existed).
* ``active_rule_event_ids`` -- the content-identity event fingerprints of
  every rule that is BOTH effectively active (latest valid, row-bound
  ``activate`` event) AND re-authenticated through the same closed-
  template + scope check the prompt channel applies. Eligibility here is
  deliberately UNCAPPED: the prompt-time cap (5, exact-scope-first) is a
  runtime concern of the assembly step, while the snapshot must disclose
  everything that was ALLOWED to steer.
* ``rule_channel_stats`` -- ``{"total", "accepted", "dropped"}`` over the
  effectively-active set at capture time: the SE-P4a "silent drop"
  deferral lands here, so a rule that a human activated but that fails
  read-time authentication is VISIBLY dropped in the disclosure instead
  of quietly vanishing from prompts.
* ``priors_query_fingerprint`` / ``priors_dimensions`` -- the SE-ix
  "priors query fingerprints": the exact recipe of the priors view in
  force at capture.
* ``snapshot_hash`` -- ``canonical_fingerprint`` over the canonical
  payload WITHOUT the hash field itself; this is the value FE-P3 folds
  into the pipeline ``input_hash`` (same nonce => same search policy).

FREEZE: this module's serialized shape and hash derivation are the
cross-track contract. Extending it is a reviewed change; the golden
vector test (``tests/test_planning_influence.py``) pins the hash of a
fixed payload so any silent drift fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from quant_forge.lineage.store import canonical_fingerprint
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.priors import PriorsQuery, compute_priors

__all__ = [
    "PLANNING_INFLUENCE_SCHEMA_VERSION",
    "PlanningInfluenceSnapshot",
    "capture_planning_influence",
]

PLANNING_INFLUENCE_SCHEMA_VERSION = "qf.planning_influence.v1"


@dataclass(frozen=True)
class PlanningInfluenceSnapshot:
    as_of: int
    rule_activation_seq_max: int
    active_rule_event_ids: tuple[str, ...]
    rule_channel_stats: dict[str, int] = field(default_factory=dict)
    priors_query_fingerprint: str = ""
    priors_dimensions: tuple[str, ...] = ()
    schema_version: str = PLANNING_INFLUENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLANNING_INFLUENCE_SCHEMA_VERSION:
            raise ValueError(f"unsupported planning_influence schema_version: {self.schema_version}")
        if self.as_of < 0:
            raise ValueError("as_of must be >= 0")
        if self.rule_activation_seq_max < -1:
            raise ValueError("rule_activation_seq_max must be >= -1 (-1 == no events)")
        if tuple(sorted(self.active_rule_event_ids)) != self.active_rule_event_ids:
            raise ValueError("active_rule_event_ids must be sorted (canonical order)")
        expected_keys = {"total", "accepted", "dropped"}
        if set(self.rule_channel_stats) != expected_keys:
            raise ValueError(f"rule_channel_stats must carry exactly {sorted(expected_keys)}")

    def payload(self) -> dict[str, Any]:
        """The canonical hash input: everything EXCEPT the hash itself."""

        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of,
            "rule_activation_seq_max": self.rule_activation_seq_max,
            "active_rule_event_ids": list(self.active_rule_event_ids),
            "rule_channel_stats": {key: int(self.rule_channel_stats[key]) for key in sorted(self.rule_channel_stats)},
            "priors_query_fingerprint": self.priors_query_fingerprint,
            "priors_dimensions": list(self.priors_dimensions),
        }

    def snapshot_hash(self) -> str:
        return canonical_fingerprint(self.payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "snapshot_hash": self.snapshot_hash()}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "PlanningInfluenceSnapshot":
        snapshot = PlanningInfluenceSnapshot(
            schema_version=str(payload.get("schema_version", PLANNING_INFLUENCE_SCHEMA_VERSION)),
            as_of=int(payload["as_of"]),
            rule_activation_seq_max=int(payload.get("rule_activation_seq_max", -1)),
            active_rule_event_ids=tuple(str(item) for item in payload.get("active_rule_event_ids", ())),
            rule_channel_stats={key: int(value) for key, value in dict(payload.get("rule_channel_stats", {})).items()},
            priors_query_fingerprint=str(payload.get("priors_query_fingerprint", "")),
            priors_dimensions=tuple(str(item) for item in payload.get("priors_dimensions", ())),
        )
        recorded_hash = str(payload.get("snapshot_hash", ""))
        if recorded_hash and recorded_hash != snapshot.snapshot_hash():
            raise ValueError("planning_influence snapshot_hash does not match its payload (tampered or corrupt)")
        return snapshot


def capture_planning_influence(
    store: ResearchMemoryStore, *, priors_query: PriorsQuery | None = None
) -> PlanningInfluenceSnapshot:
    """Capture the snapshot from the store's CURRENT durable state.

    Deterministic for a given (outcomes ledger, activations file, rules
    rows, query): no clock, no randomness -- the same store state always
    mints the same ``snapshot_hash``, which is exactly what lets the FE
    fold it into ``input_hash`` without breaking confirm idempotency.
    """

    # Deferred import mirrors context_builder's own pattern: a module-level
    # import of llm here would pull the LLM client stack into every
    # priors/planning consumer.
    from quant_forge.research_loop.llm import authenticate_active_rule_item

    query = priors_query or PriorsQuery()
    priors = compute_priors(store, query)

    rules = store.effective_active_rules()
    accepted_event_ids: list[str] = []
    seq_max = -1
    dropped = 0
    for rule in rules:
        seq_max = max(seq_max, int(rule.get("activation_seq", -1)))
        statement = str(rule.get("statement", ""))
        scope = str(rule.get("scope", "global") or "global")
        if authenticate_active_rule_item(statement, scope):
            accepted_event_ids.append(str(rule.get("event_id", "")))
        else:
            dropped += 1

    return PlanningInfluenceSnapshot(
        as_of=priors.as_of,
        rule_activation_seq_max=seq_max,
        active_rule_event_ids=tuple(sorted(event_id for event_id in accepted_event_ids if event_id)),
        rule_channel_stats={"total": len(rules), "accepted": len(accepted_event_ids), "dropped": dropped},
        priors_query_fingerprint=query.fingerprint(),
        priors_dimensions=query.dimensions,
    )

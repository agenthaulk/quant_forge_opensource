"""The frozen ``planning_influence_snapshot`` contract (SE-P5, ruling SE-ix).

One snapshot answers, durably and hashably: *"exactly which learned
steering COULD have influenced the research launched from this
confirmation?"* It is captured at pipeline confirm time (the FE track
wires the capture in FE-P3; the reserved ``planning_influence_hash`` slot
already participates in the pipeline ``input_hash``), persisted alongside
the run artifacts, and rendered ONLY by the canonical disclosure
components. The sidecar gets no general memory-read tool -- this
run-local projection is the whole surface (SE-ix).

Atomicity (review round P5-F5): every input -- outcome envelopes, the
effective rule set, and the review-event revision -- is read in ONE
store-lock hold (``ResearchMemoryStore.planning_influence_inputs``), so a
snapshot always describes a single durable instant, never outcomes
revision N paired with a rule state that only existed after N.

Contents (each field carries its cutoff semantics):

* ``as_of`` -- the outcomes-ledger revision the priors basis was computed
  from (append-only, so this single integer pins the exact ledger prefix).
* ``review_events_revision`` -- the count of valid review events at
  capture (the activation log's OWN monotone revision; review round
  P5-F4: the previous max-activation_seq-of-active-rows regressed on
  deactivation and could not pin which events existed).
* ``active_rules`` -- one entry per rule that is BOTH effectively active
  (latest valid, row-bound ``activate`` event) AND re-authenticated
  through the same closed-template + scope check the prompt channel
  applies: ``{"event_id", "scope", "activation_seq"}``, ordered by
  ``activation_seq`` DESCENDING then ``event_id`` (the exact recency key
  the prompt channel ranks by). Recording ORDER and SCOPE makes the
  prompt-time projection -- exact-scope-first filtering, the reserved
  global slot, the cap -- a pure function of (this list, the run's own
  scope), so two states whose prompt top-N would differ can never share
  a hash (review round P5-F1, BLOCKING). Eligibility here is deliberately
  UNCAPPED: the snapshot must disclose everything ALLOWED to steer, while
  ``prompt_policy`` freezes the constants the runtime projection applies.
* ``prompt_policy`` -- the closed constants of the projection itself
  (cap, ordering rule, reserved global slots). A future policy change
  changes every snapshot hash, exactly like a weight change in the priors
  query fingerprint.
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
cross-track contract. Deserialization is FAIL-CLOSED (review round
P5-F3): the exact v1 key set is required, the hash must be present and
match, and unknown fields are rejected. Extending the shape is a reviewed
change; the golden vector test
(``tests/test_research_priors.py::test_snapshot_hash_golden_vector``)
pins the hash of a fixed payload so any silent drift fails loudly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from quant_forge.lineage.store import canonical_fingerprint
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.priors import PriorsQuery

__all__ = [
    "PLANNING_INFLUENCE_SCHEMA_VERSION",
    "PROMPT_POLICY",
    "PlanningInfluenceSnapshot",
    "capture_planning_influence",
]

PLANNING_INFLUENCE_SCHEMA_VERSION = "qf.planning_influence.v1"

# The prompt-projection constants in force for this schema version --
# mirrors context_builder._active_rules' documented pipeline (P4a items
# 4/5/11): exact-scope matches rank before global, recency = activation_seq
# descending (never decided_at), one global slot reserved, cap 5. Changing
# any of these is a policy change and MUST change every snapshot hash.
PROMPT_POLICY: Mapping[str, Any] = MappingProxyType(
    {
        "cap": 5,
        "ordering": "exact_scope_first,activation_seq_desc",
        "reserved_global_slots": 1,
    }
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "as_of",
        "review_events_revision",
        "active_rules",
        "prompt_policy",
        "rule_channel_stats",
        "priors_query_fingerprint",
        "priors_dimensions",
    }
)
_STATS_KEYS = frozenset({"total", "accepted", "dropped"})
_RULE_KEYS = frozenset({"event_id", "scope", "activation_seq"})


@dataclass(frozen=True)
class PlanningInfluenceSnapshot:
    as_of: int
    review_events_revision: int
    active_rules: tuple[Mapping[str, Any], ...]
    rule_channel_stats: Mapping[str, int] = field(default_factory=dict)
    priors_query_fingerprint: str = ""
    priors_dimensions: tuple[str, ...] = ()
    schema_version: str = PLANNING_INFLUENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLANNING_INFLUENCE_SCHEMA_VERSION:
            raise ValueError(f"unsupported planning_influence schema_version: {self.schema_version}")
        if self.as_of < 0:
            raise ValueError("as_of must be >= 0")
        if self.review_events_revision < 0:
            raise ValueError("review_events_revision must be >= 0")
        frozen_rules = []
        previous_key: tuple[int, str] | None = None
        for rule in self.active_rules:
            if set(rule) != _RULE_KEYS:
                raise ValueError(f"active_rules entries must carry exactly {sorted(_RULE_KEYS)}")
            event_id = str(rule["event_id"])
            if not _HEX64_RE.fullmatch(event_id):
                raise ValueError("active_rules event_id must be 64-hex")
            seq = int(rule["activation_seq"])
            if seq < 0:
                raise ValueError("active_rules activation_seq must be >= 0")
            key = (-seq, event_id)
            if previous_key is not None and key < previous_key:
                raise ValueError("active_rules must be ordered by activation_seq desc, then event_id")
            previous_key = key
            frozen_rules.append(
                MappingProxyType({"event_id": event_id, "scope": str(rule["scope"]), "activation_seq": seq})
            )
        object.__setattr__(self, "active_rules", tuple(frozen_rules))
        # RV2-F3: canonicalize + freeze the dimensions HERE -- a caller-owned
        # mutable list would let the hash change after construction.
        dimensions = tuple(str(item) for item in self.priors_dimensions)
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("priors_dimensions must be unique")
        object.__setattr__(self, "priors_dimensions", dimensions)
        if not (
            isinstance(self.priors_query_fingerprint, str)
            and (self.priors_query_fingerprint == "" or _HEX64_RE.fullmatch(self.priors_query_fingerprint))
        ):
            raise ValueError("priors_query_fingerprint must be empty or 64-hex")
        if set(self.rule_channel_stats) != _STATS_KEYS:
            raise ValueError(f"rule_channel_stats must carry exactly {sorted(_STATS_KEYS)}")
        stats = {key: int(self.rule_channel_stats[key]) for key in _STATS_KEYS}
        if any(value < 0 for value in stats.values()):
            raise ValueError("rule_channel_stats values must be >= 0")
        if stats["accepted"] + stats["dropped"] != stats["total"]:
            raise ValueError("rule_channel_stats must satisfy accepted + dropped == total")
        if stats["accepted"] != len(self.active_rules):
            raise ValueError("rule_channel_stats.accepted must equal len(active_rules)")
        object.__setattr__(self, "rule_channel_stats", MappingProxyType(stats))

    def payload(self) -> dict[str, Any]:
        """The canonical hash input: everything EXCEPT the hash itself."""

        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of,
            "review_events_revision": self.review_events_revision,
            "active_rules": [dict(rule) for rule in self.active_rules],
            "prompt_policy": dict(PROMPT_POLICY),
            "rule_channel_stats": {key: int(self.rule_channel_stats[key]) for key in sorted(_STATS_KEYS)},
            "priors_query_fingerprint": self.priors_query_fingerprint,
            "priors_dimensions": list(self.priors_dimensions),
        }

    def snapshot_hash(self) -> str:
        return canonical_fingerprint(self.payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "snapshot_hash": self.snapshot_hash()}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "PlanningInfluenceSnapshot":
        """FAIL-CLOSED deserialization (P5-F3): exact v1 key set, hash
        REQUIRED and matching, prompt_policy matching this build's
        constants -- a payload this build cannot re-mint bit-for-bit is
        rejected, never partially accepted with silent defaults."""

        keys = set(payload)
        expected = _PAYLOAD_KEYS | {"snapshot_hash"}
        if keys != expected:
            missing = sorted(expected - keys)
            extra = sorted(keys - expected)
            raise ValueError(f"planning_influence payload key mismatch (missing={missing}, extra={extra})")
        recorded_hash = str(payload["snapshot_hash"])
        if not _HEX64_RE.fullmatch(recorded_hash):
            raise ValueError("planning_influence snapshot_hash must be 64-hex")
        if dict(payload["prompt_policy"]) != dict(PROMPT_POLICY):
            raise ValueError("planning_influence prompt_policy does not match this build's frozen constants")
        snapshot = PlanningInfluenceSnapshot(
            schema_version=str(payload["schema_version"]),
            as_of=int(payload["as_of"]),
            review_events_revision=int(payload["review_events_revision"]),
            active_rules=tuple(dict(rule) for rule in payload["active_rules"]),
            rule_channel_stats={key: int(value) for key, value in dict(payload["rule_channel_stats"]).items()},
            priors_query_fingerprint=str(payload["priors_query_fingerprint"]),
            priors_dimensions=tuple(str(item) for item in payload["priors_dimensions"]),
        )
        # RV2-F4: the constructors above COERCE (int("5") == 5), so a
        # non-canonically-typed payload could round-trip to the same hash.
        # Canonical-serialization equality closes the whole class: the
        # incoming payload must be BIT-IDENTICAL to what this build would
        # re-mint, types included.
        incoming = {key: payload[key] for key in _PAYLOAD_KEYS}
        if incoming != snapshot.payload():
            raise ValueError("planning_influence payload is not in canonical serialized form")
        if recorded_hash != snapshot.snapshot_hash():
            raise ValueError("planning_influence snapshot_hash does not match its payload (tampered or corrupt)")
        return snapshot


def capture_planning_influence(
    store: ResearchMemoryStore, *, priors_query: PriorsQuery | None = None
) -> PlanningInfluenceSnapshot:
    """Capture the snapshot from ONE durable store instant.

    Deterministic for a given (outcomes ledger, activations file, rules
    rows, query): no clock, no randomness -- the same store state always
    mints the same ``snapshot_hash``, which is exactly what lets the FE
    fold it into ``input_hash`` without breaking confirm idempotency. All
    three inputs come from a single store-lock hold (P5-F5).
    """

    # Deferred import mirrors context_builder's own pattern: a module-level
    # import of llm here would pull the LLM client stack into every
    # priors/planning consumer.
    from quant_forge.research_loop.llm import authenticate_active_rule_item

    query = priors_query or PriorsQuery()
    records, rules, events_revision = store.planning_influence_inputs()
    as_of = len(records)

    accepted: list[dict[str, Any]] = []
    dropped = 0
    for rule in rules:
        statement = str(rule.get("statement", ""))
        scope = str(rule.get("scope", "global") or "global")
        event_id = str(rule.get("event_id", ""))
        if authenticate_active_rule_item(statement, scope) and _HEX64_RE.fullmatch(event_id):
            accepted.append(
                {"event_id": event_id, "scope": scope, "activation_seq": int(rule.get("activation_seq", -1))}
            )
        else:
            dropped += 1

    accepted.sort(key=lambda item: (-item["activation_seq"], item["event_id"]))
    return PlanningInfluenceSnapshot(
        as_of=as_of,
        review_events_revision=events_revision,
        active_rules=tuple(accepted),
        rule_channel_stats={"total": len(rules), "accepted": len(accepted), "dropped": dropped},
        priors_query_fingerprint=query.fingerprint(),
        priors_dimensions=query.dimensions,
    )

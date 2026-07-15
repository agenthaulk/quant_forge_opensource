"""Quantitative priors view over the outcomes ledger (SE-P5, Seam 3a).

A pure, COMPUTED, never-persisted read model (DECISIONS.md "2026-07-13 --
Self-evolution engine CP0", ruling SE-v): pass/fail structure by
generalization dimension, derived from the deduplicated ELIGIBLE outcome
envelopes on the ledger -- never from promoted findings (a finding is
already an aggregation; re-counting it would double-weight whatever
promotion emphasized), and never entering steering by itself (SE-iv: the
pre-generation context composition stays the ONLY steering owner; this
view exists for the human read surface and for the SE-ix
``planning_influence_snapshot`` fingerprint).

Evidence discipline (each rule carries its own reason):

* **Unit = evidence run, not row.** Within one bucket, all envelopes
  sharing an ``evidence_run_id`` collapse to the LATEST one in ledger
  order: a re-measured outcome (same factor x window x stage, new
  ``outcome_id``) supersedes its predecessor's verdict instead of voting
  beside it, mirroring ``memory.promote``'s own anti-gaming cap (<=1
  observation per (signature, run_id)).
* **Four verdicts counted separately** (SE-v). Only ``passed``/``blocked``
  are scientific answers, so ONLY they enter a rate denominator;
  ``unknown``/``not_applicable`` are reported as counts and nothing else
  (a pending submission is not evidence for either side -- FP-4).
* **OOS-role rows never steer** (FP-G / owner ruling R5-3's sample-role
  axis): an envelope whose ``sample_role`` is ``out_of_sample`` is
  excluded from every count and rate and tallied in ``oos_excluded`` --
  confirmatory holdout results must not become priors that steer the
  search that will later be judged on them.
* **Unknown dimension values do not unify** (R-F4's spirit): a row whose
  bucket dimension is ``""`` cannot honestly generalize along that
  dimension; it is tallied in that table's ``unbucketed`` counter, never
  in a cell.
* **Strength-weighted alongside raw** (owner ruling R5-3): each cell
  carries both raw verdict counts and a weighted pass rate using the
  closed ``EVIDENCE_STRENGTH_WEIGHTS`` tier (monotone in
  ``outcomes.EVIDENCE_STRENGTHS`` rank). Weights are reviewed constants,
  not configuration -- changing them is a contract change.
* **Thin cells fail honest** (FP-4): below ``min_cell_evidence_runs``,
  every rate is ``None`` and ``insufficient_sample`` is True. ``None`` is
  never 0.

``as_of`` is the outcomes-ledger revision THIS view was computed from --
by definition the count of valid ledger rows (``memory.
ResearchMemoryStore.outcomes_revision``), derived here from the SAME
locked read that produced the rows, so the stamp can never disagree with
the data. ``PriorsQuery.fingerprint()`` is the SE-ix "priors query
fingerprint": two snapshots carrying the same query fingerprint and the
same ``as_of`` are guaranteed to have seen identical priors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from quant_forge.lineage.store import canonical_fingerprint
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.outcomes import EVIDENCE_STRENGTHS, VERDICTS

__all__ = [
    "PRIORS_SCHEMA_VERSION",
    "PRIOR_DIMENSIONS",
    "EVIDENCE_STRENGTH_WEIGHTS",
    "DEFAULT_MIN_CELL_EVIDENCE_RUNS",
    "PriorsQuery",
    "PriorCell",
    "PriorsTable",
    "PriorsView",
    "compute_priors",
]

PRIORS_SCHEMA_VERSION = "qf.research_priors.v1"

# The generalization dimensions a cell may bucket by -- exactly the
# OutcomeScope dimensions that carry a promotion-relevant taxonomy today.
# horizon_bucket is deliberately absent: no producer emits it yet, so a
# table over it would be 100% unbucketed noise.
PRIOR_DIMENSIONS: tuple[str, ...] = ("factor_family", "settings_profile", "asset_class", "universe")

# Owner ruling R5-3: closed evidence tier, monotone weights. Rank-derived
# (0.25/0.5/0.75/1.0) so adding a tier to the closed vocabulary forces a
# deliberate weight decision here rather than a silent default.
EVIDENCE_STRENGTH_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {name: (rank + 1) / len(EVIDENCE_STRENGTHS) for rank, name in enumerate(EVIDENCE_STRENGTHS)}
)

DEFAULT_MIN_CELL_EVIDENCE_RUNS = 2


@dataclass(frozen=True)
class PriorsQuery:
    """The full recipe of a priors computation (SE-ix fingerprint input)."""

    dimensions: tuple[str, ...] = PRIOR_DIMENSIONS
    min_cell_evidence_runs: int = DEFAULT_MIN_CELL_EVIDENCE_RUNS
    schema_version: str = PRIORS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PRIORS_SCHEMA_VERSION:
            raise ValueError(f"unsupported priors schema_version: {self.schema_version}")
        if not self.dimensions:
            raise ValueError("priors query needs at least one dimension")
        unknown = set(self.dimensions) - set(PRIOR_DIMENSIONS)
        if unknown:
            raise ValueError(f"unknown priors dimension(s): {sorted(unknown)} (closed set: {PRIOR_DIMENSIONS})")
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("priors dimensions must be unique")
        if self.min_cell_evidence_runs < 1:
            raise ValueError("min_cell_evidence_runs must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dimensions": list(self.dimensions),
            "min_cell_evidence_runs": self.min_cell_evidence_runs,
            # The weights are part of the recipe: a future reviewed change
            # to the tier weighting must change every query fingerprint.
            "evidence_strength_weights": {name: EVIDENCE_STRENGTH_WEIGHTS[name] for name in EVIDENCE_STRENGTHS},
        }

    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_dict())


@dataclass(frozen=True)
class PriorCell:
    """One bucket of one dimension table. Rates are None below the floor."""

    dimension: str
    bucket: str
    evidence_runs: int
    verdict_counts: Mapping[str, int]
    weighted_passed: float
    weighted_blocked: float
    pass_rate: float | None
    weighted_pass_rate: float | None
    insufficient_sample: bool
    top_blocked_reasons: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "bucket": self.bucket,
            "evidence_runs": self.evidence_runs,
            "verdict_counts": {verdict: self.verdict_counts.get(verdict, 0) for verdict in VERDICTS},
            "weighted_passed": self.weighted_passed,
            "weighted_blocked": self.weighted_blocked,
            "pass_rate": self.pass_rate,
            "weighted_pass_rate": self.weighted_pass_rate,
            "insufficient_sample": self.insufficient_sample,
            "top_blocked_reasons": [[code, count] for code, count in self.top_blocked_reasons],
        }


@dataclass(frozen=True)
class PriorsTable:
    """All cells for one dimension plus that dimension's honesty counters."""

    dimension: str
    cells: tuple[PriorCell, ...]
    unbucketed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "cells": [cell.to_dict() for cell in self.cells],
            "unbucketed": self.unbucketed,
        }


@dataclass(frozen=True)
class PriorsView:
    """The computed view. Never persisted; recomputed per read (SE-v)."""

    query: PriorsQuery
    as_of: int
    total_envelopes: int
    total_evidence_runs: int
    oos_excluded: int
    tables: tuple[PriorsTable, ...] = field(default_factory=tuple)
    schema_version: str = PRIORS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query": self.query.to_dict(),
            "query_fingerprint": self.query.fingerprint(),
            "as_of": self.as_of,
            "total_envelopes": self.total_envelopes,
            "total_evidence_runs": self.total_evidence_runs,
            "oos_excluded": self.oos_excluded,
            "tables": [table.to_dict() for table in self.tables],
        }


def compute_priors(store: ResearchMemoryStore, query: PriorsQuery | None = None) -> PriorsView:
    """Compute the priors view from the store's outcomes ledger.

    Pure function of (ledger content, query): the single locked ledger read
    supplies both the rows and ``as_of`` (the valid-row count IS the
    revision), so the stamp cannot race the data.
    """

    query = query or PriorsQuery()
    records = store.outcome_records()
    as_of = len(records)

    eligible: list[dict[str, Any]] = []
    oos_excluded = 0
    # Latest envelope per evidence run wins (ledger order == append order).
    latest_by_run: dict[str, dict[str, Any]] = {}
    for record in records:
        outcome = record.get("outcome") or {}
        if str(outcome.get("sample_role", "")) == "out_of_sample":
            oos_excluded += 1
            continue
        run_id = str(record.get("evidence_run_id", ""))
        if not run_id:
            continue
        latest_by_run[run_id] = record
    eligible = list(latest_by_run.values())

    tables = tuple(_dimension_table(dimension, eligible, query) for dimension in query.dimensions)
    return PriorsView(
        query=query,
        as_of=as_of,
        total_envelopes=len(records),
        total_evidence_runs=len(latest_by_run),
        oos_excluded=oos_excluded,
        tables=tables,
    )


def _dimension_table(dimension: str, eligible: list[dict[str, Any]], query: PriorsQuery) -> PriorsTable:
    by_bucket: dict[str, list[dict[str, Any]]] = {}
    unbucketed = 0
    for record in eligible:
        outcome = record.get("outcome") or {}
        scope = outcome.get("scope") or {}
        bucket = str(scope.get(dimension, "") or "")
        if not bucket:
            unbucketed += 1
            continue
        by_bucket.setdefault(bucket, []).append(record)

    cells = tuple(
        _cell(dimension, bucket, records, query)
        for bucket, records in sorted(by_bucket.items(), key=lambda item: item[0])
    )
    return PriorsTable(dimension=dimension, cells=cells, unbucketed=unbucketed)


def _cell(dimension: str, bucket: str, records: list[dict[str, Any]], query: PriorsQuery) -> PriorCell:
    verdict_counts: dict[str, int] = {verdict: 0 for verdict in VERDICTS}
    weighted_passed = 0.0
    weighted_blocked = 0.0
    blocked_reasons: dict[str, int] = {}
    for record in records:
        outcome = record.get("outcome") or {}
        verdict = str(outcome.get("verdict", ""))
        if verdict not in verdict_counts:
            continue  # unrecognized rows never count toward anything
        verdict_counts[verdict] += 1
        weight = EVIDENCE_STRENGTH_WEIGHTS.get(str(record.get("evidence_strength", "")), 0.0)
        if verdict == "passed":
            weighted_passed += weight
        elif verdict == "blocked":
            weighted_blocked += weight
            for code in outcome.get("reason_codes") or ():
                code_text = str(code)
                blocked_reasons[code_text] = blocked_reasons.get(code_text, 0) + 1

    evidence_runs = len(records)
    scientific = verdict_counts["passed"] + verdict_counts["blocked"]
    insufficient = evidence_runs < query.min_cell_evidence_runs or scientific == 0
    if insufficient:
        pass_rate = None
        weighted_pass_rate = None
    else:
        pass_rate = verdict_counts["passed"] / scientific
        weighted_total = weighted_passed + weighted_blocked
        weighted_pass_rate = (weighted_passed / weighted_total) if weighted_total > 0 else None
    top_blocked = tuple(
        sorted(blocked_reasons.items(), key=lambda item: (-item[1], item[0]))[:5]
    )
    return PriorCell(
        dimension=dimension,
        bucket=bucket,
        evidence_runs=evidence_runs,
        verdict_counts=MappingProxyType(dict(verdict_counts)),
        weighted_passed=weighted_passed,
        weighted_blocked=weighted_blocked,
        pass_rate=pass_rate,
        weighted_pass_rate=weighted_pass_rate,
        insufficient_sample=insufficient,
        top_blocked_reasons=top_blocked,
    )

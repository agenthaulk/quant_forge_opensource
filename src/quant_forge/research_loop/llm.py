"""LLM adapters for public RD research loops.

Drop-stats surfacing beyond this module's own returned stats mapping (e.g.
persisting active_rules authentication counts into a queryable/disclosed
artifact) is deferred to SE-P5's ``planning_influence_snapshot`` disclosure
contract by design (P4a rework item 13); this module's job stops at making
the drop visible to its own caller, never at silently swallowing it.
"""

from __future__ import annotations

import json
import re
import warnings
from typing import Any

from quant_forge.config import LLMSettings
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition
from quant_forge.factor_library.catalog import is_precomputed_formula
from quant_forge.llm_client import extract_json_object, generate_chat_text
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.contracts import ResearchContext
from quant_forge.research_loop.llm_contracts import (
    RD_LLM_SCHEMA_VERSION,
    normalize_review_payload,
)
# Closed vocabularies (SE-i/SE-ii) imported, not hand-copied, so the active_rules
# statement-authentication grammar below can never silently drift from the
# outcomes contract it re-authenticates against.
from quant_forge.research_loop.outcomes import (
    EVIDENCE_STRENGTHS,
    ORIGINS,
    REASON_CODES,
    REASON_NONE,
    STAGE_EVIDENCE_STRENGTH,
    STAGES,
)
from quant_forge.research_loop.service import (
    ResearchGenerationMetadata,
    ResearchHypothesis,
    ResearchSelfReview,
)


CHINESE_RD_REPORT_PROMPT = "通过中文完成RD研究报告"


class LLMHypothesisGenerator:
    """Generate bounded RD hypotheses with the configured shared LLM."""

    def __init__(self, llm: LLMSettings, *, hypothesis_temperature: float = 0.0) -> None:
        self.llm = _require_non_rule_llm(llm, feature="RD LLM hypothesis generation")
        self._temperature = hypothesis_temperature
        self._metadata = ResearchGenerationMetadata(
            source="llm_hypothesis",
            provider=self.llm.provider,
            model=self.llm.model,
        )
        # SE-iv visibility requirement: the active_rules channel's closed-
        # template re-authentication must never silently drop a statement.
        # This is the runtime-visible counter (a returned stats mapping is
        # also independently available from `_active_rules_items_for_prompt`
        # for direct unit testing); it reflects the most recent generation
        # call only.
        self.last_active_rules_stats: dict[str, int] = dict(_EMPTY_ACTIVE_RULES_STATS)

    def metadata(self) -> ResearchGenerationMetadata:
        return self._metadata

    def generate(
        self,
        seed: FactorDefinition,
        *,
        objective: str,
        max_candidates: int,
    ) -> tuple[ResearchHypothesis, ...]:
        return self.generate_with_context(seed, context=None, objective=objective, max_candidates=max_candidates)

    def generate_with_context(
        self,
        seed: FactorDefinition,
        *,
        context: ResearchContext | None,
        objective: str,
        max_candidates: int,
    ) -> tuple[ResearchHypothesis, ...]:
        messages, active_rules_stats = _hypothesis_messages_and_stats(
            seed, context=context, objective=objective, max_candidates=max_candidates
        )
        self.last_active_rules_stats = active_rules_stats
        result = generate_chat_text(
            self.llm,
            messages,
            temperature=self._temperature,
            max_tokens=1200,
        )
        payload = extract_json_object(result.content)
        hypotheses = _hypotheses_from_payload(payload, max_candidates=max_candidates)
        self._metadata = ResearchGenerationMetadata(
            source="llm_hypothesis",
            provider=result.provider,
            model=result.model,
            raw_response=result.content,
        )
        return hypotheses

    def repair_invalid_hypothesis(
        self,
        seed: FactorDefinition,
        *,
        hypothesis: ResearchHypothesis,
        context: ResearchContext,
        objective: str,
        validation_error: str,
        attempt: int,
        max_attempts: int,
    ) -> ResearchHypothesis | None:
        repair_messages, active_rules_stats = _repair_messages_and_stats(
            seed=seed,
            hypothesis=hypothesis,
            context=context,
            objective=objective,
            validation_error=validation_error,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        self.last_active_rules_stats = active_rules_stats
        result = generate_chat_text(
            self.llm,
            repair_messages,
            temperature=0.1,
            max_tokens=900,
        )
        payload = extract_json_object(result.content)
        repaired = _hypotheses_from_payload(payload, max_candidates=1)
        self._metadata = ResearchGenerationMetadata(
            source="llm_formula_repair",
            provider=result.provider,
            model=result.model,
            raw_response=result.content,
        )
        return repaired[0] if repaired else None


class LLMResearchReviewGenerator:
    """Review RD candidate evidence with the configured shared LLM."""

    def __init__(self, llm: LLMSettings) -> None:
        self.llm = _require_non_rule_llm(llm, feature="RD LLM self-review")

    def review(
        self,
        *,
        seed: FactorDefinition,
        candidate: FactorDefinition,
        evaluation: EvaluationResult,
        backtest: BacktestResult,
        split_weighted_icir: float,
        score: float,
        gate_passed: bool,
        gate_reasons: tuple[str, ...],
    ) -> ResearchSelfReview:
        result = generate_chat_text(
            self.llm,
            _review_messages(
                seed=seed,
                candidate=candidate,
                evaluation=evaluation,
                backtest=backtest,
                split_weighted_icir=split_weighted_icir,
                score=score,
                gate_passed=gate_passed,
                gate_reasons=gate_reasons,
            ),
            temperature=0.1,
            max_tokens=1200,
            retry_timeouts=False,
        )
        payload = extract_json_object(result.content)
        fallback_summary = _fallback_review_summary(
            candidate=candidate,
            score=score,
            split_weighted_icir=split_weighted_icir,
            gate_passed=gate_passed,
        )
        normalized = normalize_review_payload(payload, fallback_summary=fallback_summary)
        payload = normalized.payload
        return ResearchSelfReview(
            source="llm_self_review",
            summary=str(payload["summary"]).strip(),
            strengths=_string_tuple(payload.get("strengths")),
            risks=_string_tuple(payload.get("risks")),
            next_hypotheses=_string_tuple(payload.get("next_hypotheses")),
            normalization_warnings=normalized.normalization_warnings,
        )


def _require_non_rule_llm(llm: LLMSettings, *, feature: str) -> LLMSettings:
    selected = llm.select_provider()
    if selected.provider.lower() in {"rule", "deterministic"}:
        raise RuntimeError(f"{feature} requires a configured LLM provider; selected provider is local rule.")
    return selected


def _hypothesis_messages(
    seed: FactorDefinition,
    *,
    context: ResearchContext | None = None,
    objective: str,
    max_candidates: int,
) -> list[dict[str, str]]:
    """Thin, signature-stable wrapper over :func:`_hypothesis_messages_and_stats`.

    Kept as a plain ``list[dict[str, str]]`` return (no stats tuple) because
    existing tests call it directly and expect exactly that shape.
    :meth:`LLMHypothesisGenerator.generate_with_context` calls
    :func:`_hypothesis_messages_and_stats` directly instead, when it needs
    the active_rules drop counter.
    """

    messages, _active_rules_stats = _hypothesis_messages_and_stats(
        seed, context=context, objective=objective, max_candidates=max_candidates
    )
    return messages


def _hypothesis_messages_and_stats(
    seed: FactorDefinition,
    *,
    context: ResearchContext | None = None,
    objective: str,
    max_candidates: int,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    field_catalog = _catalog_for_prompt(
        context.field_catalog if context is not None and context.field_catalog else tuple(list_available_fields())
    )
    operator_catalog = _catalog_for_prompt(
        context.operator_catalog
        if context is not None and context.operator_catalog
        else tuple(list_available_operators())
    )
    effective_ideas = list(context.effective_ideas)[:10] if context is not None else []
    next_hints = list(context.next_focus_hints)[:10] if context is not None else []
    # Durable research memory (already redacted and bounded upstream): only
    # statement + observation_count reach the prompt, max 5 items per tier.
    memory_failures = _memory_items_for_prompt(context.recent_failures) if context is not None else []
    memory_findings = _memory_items_for_prompt(context.recent_successes) if context is not None else []
    # SE-iv bounded active_rules channel: its OWN closed-template
    # re-authentication (never the plain memory gate above) and its OWN
    # visible drop counter (S1-F11 -- a template mismatch must never
    # silently vanish a human-activated rule without a trace).
    active_rules, active_rules_stats = _active_rules_items_for_prompt(
        context.active_rules if context is not None else ()
    )
    mechanism_guidance = _mechanism_guidance_for_prompt(seed)
    system = (
        "You are Quant Forge's RD hypothesis generator. Return one JSON object only. "
        "Generate bounded factor research hypotheses with executable formula_dsl. "
        "Use a mechanism-first research workflow inspired by R&D-Agent style loops: first state the "
        "research lane, then translate the lane into an executable formula, then make the hypothesis "
        "easy for local validation and feedback. "
        "Use financial analyst reasoning, effective-idea replay, and operator-aware variants. "
        "Every hypothesis must pick one lane: interaction_conjunction, stability_smoothing, "
        "relationship_consistency, horizon_retiming, or risk_cost_control. "
        "Do not merely append another rank term to a linear rank-sum seed unless additive exposure "
        "is the explicit research thesis. "
        "For small-cap/low-volatility/stable-return seeds, prefer non-additive interaction or "
        "stability transformations that require the concepts to jointly hold. "
        "Use covariance/correlation only for an interpretable co-movement or consistency thesis, "
        "not as a decorative wrapper around an existing score. "
        "Use only listed fields, listed operators, and safe arithmetic (+, -, *, /). "
        "Never include precomputed: factor references inside formula_dsl; the local system handles seed "
        "parameter search separately. Do not use placeholder fields such as seed, seed_score, or factor_score. "
        "Mention non-ST only when the seed uses an is_st filter. "
        "Do not invent data fields or unsupported formulas. Do not request parameter-search fallback; "
        "if no executable idea is available, return an empty hypotheses list. "
        "Active steering rules are human-approved research guidance: honor them and do not propose a "
        "candidate that contradicts one without naming the contradiction in the rationale."
    )
    user = (
        f"Seed factor:\n{json.dumps(_factor_summary(seed), ensure_ascii=False)}\n\n"
        f"Objective: {objective}\n"
        f"Available fields: {json.dumps(field_catalog, ensure_ascii=False)}\n"
        f"Available operators: {json.dumps(operator_catalog, ensure_ascii=False)}\n"
        f"Effective ideas: {json.dumps(effective_ideas, ensure_ascii=False)}\n"
        f"Recent failure hints: {json.dumps(next_hints, ensure_ascii=False)}\n"
        f"Research memory failures (avoid repeating): {json.dumps(memory_failures, ensure_ascii=False)}\n"
        f"Research memory findings (build on if relevant): {json.dumps(memory_findings, ensure_ascii=False)}\n"
        f"Active steering rules (human-activated): {json.dumps(active_rules, ensure_ascii=False)}\n"
        f"Mechanism guidance: {json.dumps(mechanism_guidance, ensure_ascii=False)}\n"
        f"Generate up to {max_candidates} distinct hypotheses. "
        "Each rationale must include: economic mechanism, formula transformation, expected failure mode, "
        "and how this candidate differs from the seed formula. "
        "Put the selected lane at the start of source_detail, for example "
        "\"interaction_conjunction: joint small-cap low-vol stability gate\". "
        "Return JSON shape: "
        "{\"schema_version\":\"qf.rd.llm.v1\",\"task_type\":\"rd_research_hypotheses\","
        "\"hypotheses\":[{\"text\":\"...\",\"rationale\":\"...\","
        "\"formula_dsl\":\"rank(delta(close, 5))\","
        "\"input_fields\":[\"close\"],\"expected_direction\":\"positive\","
        "\"universe_constraints\":[\"is_st == false\"],"
        "\"source\":\"financial_analyst|effective_idea|operator_mcp\","
        "\"source_detail\":\"...\",\"parameter_search_fallback\":false}]}. "
        "Always set parameter_search_fallback=false for LLM hypotheses."
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return messages, active_rules_stats


def _repair_messages(
    *,
    seed: FactorDefinition,
    hypothesis: ResearchHypothesis,
    context: ResearchContext,
    objective: str,
    validation_error: str,
    attempt: int,
    max_attempts: int,
) -> list[dict[str, str]]:
    """Thin, signature-stable wrapper over :func:`_repair_messages_and_stats`.

    Kept as a plain ``list[dict[str, str]]`` return (no stats tuple) because
    an existing test calls it directly and expects exactly that shape.
    :meth:`LLMHypothesisGenerator.repair_invalid_hypothesis` calls
    :func:`_repair_messages_and_stats` directly instead, when it needs the
    active_rules drop counter.
    """

    messages, _active_rules_stats = _repair_messages_and_stats(
        seed=seed,
        hypothesis=hypothesis,
        context=context,
        objective=objective,
        validation_error=validation_error,
        attempt=attempt,
        max_attempts=max_attempts,
    )
    return messages


def _repair_messages_and_stats(
    *,
    seed: FactorDefinition,
    hypothesis: ResearchHypothesis,
    context: ResearchContext,
    objective: str,
    validation_error: str,
    attempt: int,
    max_attempts: int,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    field_catalog = _catalog_for_prompt(context.field_catalog or tuple(list_available_fields()))
    operator_catalog = _catalog_for_prompt(context.operator_catalog or tuple(list_available_operators()))
    mechanism_guidance = _mechanism_guidance_for_prompt(seed)
    # SE-iv single steering point (P4a rework item 10): repair is still part
    # of the SAME research loop the hypothesis-generation channel steers, so
    # it retains the SAME authenticated active_rules block rather than a
    # second, unsteered generation path.
    active_rules, active_rules_stats = _active_rules_items_for_prompt(context.active_rules)
    system = (
        "You are Quant Forge's RD formula repair adapter. Return one JSON object only. "
        "Repair the given hypothesis so formula_dsl passes local validation. "
        "Use only listed fields, listed operators, numeric window arguments where required, and safe arithmetic. "
        "Preserve the selected research lane and economic mechanism whenever possible. "
        "If the exact formula cannot be repaired, choose the closest executable formula from the same lane. "
        "If validation_error says the formula already exists, return a materially different executable formula. "
        "If validation_error includes forbidden_formula_dsl, never return any listed formula_dsl. "
        "Do not change the research intent unless needed to make the formula executable. "
        "Do not request parameter-search fallback. If you cannot repair it, return an empty hypotheses list. "
        "Honor any active steering rules: a repaired formula must not contradict one."
    )
    user = json.dumps(
        {
            "seed": _factor_summary(seed),
            "objective": objective,
            "repair_attempt": attempt,
            "max_repair_attempts": max_attempts,
            "validation_error": validation_error,
            "invalid_hypothesis": {
                "text": hypothesis.text,
                "rationale": hypothesis.rationale,
                "formula_dsl": hypothesis.formula_dsl,
                "input_fields": hypothesis.input_fields,
                "expected_direction": hypothesis.expected_direction,
                "universe_constraints": hypothesis.universe_constraints,
                "source": hypothesis.source,
                "source_detail": hypothesis.source_detail,
            },
            "available_fields": field_catalog,
            "available_operators": operator_catalog,
            "active_rules": active_rules,
            "mechanism_guidance": mechanism_guidance,
            "return_shape": {
                "schema_version": RD_LLM_SCHEMA_VERSION,
                "task_type": "rd_research_hypotheses",
                "hypotheses": [
                    {
                        "text": "same research idea, repaired",
                        "rationale": "why this executable repair preserves the idea",
                        "formula_dsl": "rank(delta(close, 5))",
                        "input_fields": ["close"],
                        "expected_direction": "positive",
                        "universe_constraints": ["is_st == false"],
                        "source": "llm",
                        "source_detail": "interaction_conjunction: formula_repair",
                        "parameter_search_fallback": False,
                    }
                ],
            },
        },
        ensure_ascii=False,
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return messages, active_rules_stats


_MEMORY_PROMPT_ITEM_LIMIT = 5
# The service writes durable memory statements in exactly two fully-structured
# shapes (service._record_memory_observations):
#   "accepted candidate formula family {fp} passed the research gate"
#   "gate blocked candidate formula family {fp}: {families}"
# where {fp} is the leading 12 chars of an UPPERCASE SHA-1 hex fingerprint
# (service._hash_parts -> hashlib.sha1(...).hexdigest()[:16].upper()) and
# {families} is a comma-space-joined, sorted list of value-free gate-reason
# families (service._gate_reason_families): each is the leading word of a gate
# reason and, for every reason the shipped gate emits, is drawn from the
# [A-Za-z0-9_] identifier charset (metric names, INSUFFICIENT_* constants, and
# IS/OOS* split names). The prompt-side read gate authenticates the WHOLE
# collapsed statement against these anchored shapes, not merely an opening
# prefix, so a conforming prefix followed by an appended free-text payload is
# dropped instead of steering prompts (P1).
_MEMORY_FINGERPRINT_PATTERN = r"[0-9A-F]{12}"
_MEMORY_FAMILY_PATTERN = r"[A-Za-z0-9_]+"
_MEMORY_STATEMENT_PATTERNS = (
    re.compile(rf"^accepted candidate formula family {_MEMORY_FINGERPRINT_PATTERN} passed the research gate$"),
    re.compile(
        rf"^gate blocked candidate formula family {_MEMORY_FINGERPRINT_PATTERN}: "
        rf"{_MEMORY_FAMILY_PATTERN}(?:, {_MEMORY_FAMILY_PATTERN})*$"
    ),
)
# Defense-in-depth secondary bound only; the anchored templates above are the
# primary gate. Legitimate statements are far shorter than this.
_MEMORY_STATEMENT_MAX_CHARS = 300


def _memory_items_for_prompt(items: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bounded research-memory items for the hypothesis prompt.

    Only rows marked ``source == "research_memory"`` qualify (trace entries in
    the same tuples keep their existing prompt channels), and only the
    already-redacted statement plus observation_count are forwarded. Disk is
    not trusted at read time: after collapsing the statement to a single line it
    must FULLY match one of the accepted engine-minted shapes -- either a legacy
    service statement template (``_MEMORY_STATEMENT_PATTERNS``) OR the
    ResearchOutcome-derived grammar (``_authenticate_outcome_statement``, SE-i/
    SE-ii). This is the SAME union the active_rules channel authenticates
    (SE-iv): once SE-P2 migrated the local candidate-gate recorder to emit
    ResearchOutcome statements, promoted findings/failures carry the outcome
    grammar, so the passive recall gate must recognize it too or newly-promoted
    lessons silently stop steering hypothesis generation. A conforming prefix
    followed by an appended payload matches neither and is dropped, so free text
    written by any other same-host writer cannot steer prompts. Passive recall
    forwards only the statement text (no ``scope`` field), so there is no
    scope-spoofing surface here. ``_MEMORY_STATEMENT_MAX_CHARS`` is a secondary
    defense-in-depth length bound, not the primary gate.
    """

    bounded: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("source") != "research_memory":
            continue
        statement = " ".join(str(item.get("statement") or "").split())
        if len(statement) > _MEMORY_STATEMENT_MAX_CHARS:
            continue
        if not (
            any(pattern.match(statement) for pattern in _MEMORY_STATEMENT_PATTERNS)
            or _authenticate_outcome_statement(statement) is not None
        ):
            continue
        bounded.append(
            {
                "statement": statement,
                "observation_count": int(item.get("observation_count") or 0),
            }
        )
        if len(bounded) >= _MEMORY_PROMPT_ITEM_LIMIT:
            break
    return bounded


# ---------------------------------------------------------------------------
# SE-iv: active_rules bounded prompt channel with its OWN closed-template
# re-authentication. Rules can be promoted from EITHER the local candidate-
# gate statements above (_MEMORY_STATEMENT_PATTERNS) OR from a
# ResearchOutcome-derived observation (research_loop/outcomes.py, SE-i/SE-ii),
# so the accepted-template set here is the UNION of both shapes -- everything
# the engine can actually mint into a statement, nothing else.
#
# P4a rework items 4/5/6 (dual-phase review): a pure per-field regex cannot
# express the verdict<->reason AND-across-fields coherence rule ("passed"
# carries exactly NONE; "blocked" carries a real, non-NONE code), so this is
# a small canonical PARSER -- a structural regex capture followed by
# Python-level field validation -- not one monolithic alternation. It also
# authenticates the item's `scope` metadata field itself (not just the
# statement text): a forged scope value alongside an otherwise-genuine
# statement used to sail through unauthenticated (opus F1 probe: an
# "IGNORE ALL PRIOR..." payload injected via `scope` passed with
# dropped=0), because only `statement` was ever checked. Every candidate
# item's `scope` must now itself match the closed scope_key() grammar, and
# for an outcomes-grammar statement, `scope` must be EXACTLY the scope
# embedded in the statement text -- a mismatch is spoofing and is dropped.
# ---------------------------------------------------------------------------

_STATEMENT_STRUCTURE_RE = re.compile(
    r"^\[(?P<origin>[^/\]]+)/(?P<stage>[^\]]+)\] (?P<verdict>[^:]+): (?P<reason>[^;]+); "
    r"family=(?P<family>[^;]+); strength=(?P<strength>[^;]+); scope=(?P<scope>.+)$"
)

# Dimension-token grammar mirrors outcomes.OutcomeScope's own dimension regex
# (bounded lowercase/digit/underscore/dot/hyphen token, `_DIM_RE`). This is a
# STRUCTURAL grammar, not a closed string set, so it is reproduced here (with
# a pointer comment) rather than importing outcomes.py's module-private
# regex object.
_SCOPE_TOKEN_RE = re.compile(r"^[a-z0-9_.\-]{1,32}$")
# Fixed relative order outcomes.OutcomeScope.scope_key() always emits
# dimensions in; a scope string presenting them out of this order is not a
# shape the engine ever mints.
_SCOPE_DIMENSION_KEYS = ("asset", "universe", "family", "horizon", "settings")
# "unknown"/"global" are the RESERVED sentinels for "dimension absent" (R-F4
# in outcomes.py); accepting them as literal dimension VALUES would let
# unrelated scopes unify, so outcomes.OutcomeScope itself rejects them and
# this parser mirrors that rejection.
_SCOPE_RESERVED_VALUES = frozenset({"unknown", "global"})
_SCOPE_MAX_LENGTH = 160


def _parse_scope_grammar(scope: str) -> bool:
    """Structural validation of a scope string (item 4a/6): ``"global"``, or
    a ``;``-joined sequence of ``key=value`` segments with keys drawn from
    the closed 5-dimension set (each appearing at most once, in canonical
    order), values matching the outcomes dimension-token grammar, and
    neither ``"unknown"`` nor ``"global"`` accepted as a value.
    """

    if len(scope) > _SCOPE_MAX_LENGTH:
        return False
    if scope == "global":
        return True
    seen_keys: list[str] = []
    for segment in scope.split(";"):
        key, separator, value = segment.partition("=")
        if not separator or key not in _SCOPE_DIMENSION_KEYS or key in seen_keys:
            return False
        if value in _SCOPE_RESERVED_VALUES or not _SCOPE_TOKEN_RE.fullmatch(value):
            return False
        seen_keys.append(key)
    if not seen_keys:
        return False  # e.g. an empty string: neither "global" nor any real segment
    order_index = {key: index for index, key in enumerate(_SCOPE_DIMENSION_KEYS)}
    positions = [order_index[key] for key in seen_keys]
    return positions == sorted(positions)


def _family_token_valid(family: str) -> bool:
    if family == "unknown":
        return True
    if family in _SCOPE_RESERVED_VALUES:
        return False  # "global" is the scope-level sentinel, never a valid family value
    return bool(_SCOPE_TOKEN_RE.fullmatch(family))


def _reason_coherent_with_verdict(verdict: str, reason: str) -> bool:
    """Non-mintable shapes (item 6) fail here: outcomes.ResearchOutcome
    itself enforces "passed" <-> exactly NONE and "blocked" <-> a real,
    non-NONE reason (mirrored from its own ``__post_init__``), so
    ``"passed: VALIDATION_ERROR"`` or ``"blocked: NONE"`` were never
    representable and must not authenticate.
    """

    if reason not in REASON_CODES:
        return False
    if verdict == "passed":
        return reason == REASON_NONE
    if verdict == "blocked":
        return reason != REASON_NONE
    return False


def _authenticate_outcome_statement(statement: str) -> str | None:
    """Canonical structural parser for ``outcomes._statement_for``'s shape:
    ``"[origin/stage] verdict: REASON; family=...; strength=...; scope=..."``.

    Every closed-set check is driven by outcomes.py's imported vocabularies
    (never hand-copied), so this cannot silently drift from the contract it
    authenticates against (S1-F11). Returns the embedded scope substring on
    success (for the statement/scope equality cross-check in
    :func:`authenticate_active_rule_item`), or ``None`` if the statement is
    not a shape the engine can actually mint.
    """

    match = _STATEMENT_STRUCTURE_RE.match(statement)
    if match is None:
        return None
    origin = match.group("origin")
    stage = match.group("stage")
    verdict = match.group("verdict")
    reason = match.group("reason")
    family = match.group("family")
    strength = match.group("strength")
    scope = match.group("scope")

    if origin not in ORIGINS or stage not in STAGES:
        return None
    # Narrower than the full closed VERDICTS vocabulary on purpose:
    # outcome_to_observations() mints ZERO observations for "unknown"/
    # "not_applicable" verdicts (FP-4/R-F2), so a statement claiming either
    # could never have been genuinely minted.
    if verdict not in ("passed", "blocked"):
        return None
    if not _reason_coherent_with_verdict(verdict, reason):
        return None
    if not _family_token_valid(family):
        return None
    if strength not in EVIDENCE_STRENGTHS:
        return None
    # Stage/strength coherence (R2 rework item R2-4): outcomes.py derives
    # evidence_strength FROM the stage (owner ruling R5-3, "it can never
    # exceed the DECLARED stage") -- a statement is only mintable if its
    # strength is EXACTLY the one STAGE_EVIDENCE_STRENGTH maps that stage
    # to, not merely any closed-vocabulary value. This closes the gap where
    # a weak stage (e.g. "evaluate") paired with an inflated strength (e.g.
    # "submitted_live") would otherwise pass -- that combination can never
    # be genuinely minted by outcome_to_observations().
    if strength != STAGE_EVIDENCE_STRENGTH[stage]:
        return None
    if not _parse_scope_grammar(scope):
        return None
    return scope


_ACTIVE_RULES_PROMPT_ITEM_LIMIT = 5
_EMPTY_ACTIVE_RULES_STATS: dict[str, int] = {"total": 0, "accepted": 0, "dropped": 0}


def authenticate_active_rule_item(statement: str, scope: str) -> bool:
    """True iff ``(statement, scope)`` together pass the active_rules
    closed-template + scope grammar (P4a rework items 4/5/6): EITHER a local
    candidate-gate template (service.py's two accept/block statement
    shapes) with a well-formed standalone ``scope``, OR an outcomes-grammar
    statement (``outcomes._statement_for``) whose EMBEDDED scope matches
    ``scope`` exactly.

    Public (no leading underscore) and reached from
    ``context_builder._active_rules()`` via a DEFERRED (function-body-level)
    import: a module-level import there would create a cycle
    (context_builder -> llm -> service -> context_builder, since
    service.py imports ``ResearchContextBuilder``). This is the ONE
    canonical authentication implementation, invoked at two independent
    points in the data flow -- context_builder's auth-before-cap pipeline
    stage (item 5) and this module's own prompt-assembly gate -- not two
    independently coded copies that could silently drift apart.
    """

    collapsed_statement = " ".join(statement.split())
    if len(collapsed_statement) > _MEMORY_STATEMENT_MAX_CHARS:
        return False
    if not _parse_scope_grammar(scope):
        return False
    if any(pattern.match(collapsed_statement) for pattern in _MEMORY_STATEMENT_PATTERNS):
        return True  # local template: no embedded scope to cross-check
    embedded_scope = _authenticate_outcome_statement(collapsed_statement)
    if embedded_scope is None:
        return False
    return embedded_scope == scope


def _active_rules_items_for_prompt(
    items: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Bounded, closed-template-authenticated active_rules items (SE-iv).

    Mirrors :func:`_memory_items_for_prompt`'s read-time authentication
    discipline but calls the WIDER :func:`authenticate_active_rule_item`
    (statement AND scope). Unlike that passive memory-recall channel,
    active_rules are human-activated steering: a statement or scope failing
    authentication is DROPPED and counted in the returned stats mapping
    instead of vanishing without a trace (S1-F11) -- ``{"total": len(items),
    "accepted": forwarded count, "dropped": examined and rejected count}``.
    (``dropped`` only counts items actually examined: an input longer than
    the prompt cap that already found enough conforming statements stops
    early, same as the existing memory channel's cap.)

    Traceability, not prevention (R2 rework item R2-6): each accepted item
    also carries ``event_id``/``reviewed_entry_id`` straight through from
    ``ResearchMemoryStore.effective_active_rules()`` (via
    ``context.active_rules``), so any rule this channel forwards to the
    prompt is traceable to the exact review event that activated it in
    trace/debug output. See the module-docstring boundary note in
    ``memory.py`` for why this is traceability rather than an in-process-
    forgery defense.
    """

    total = len(items)
    bounded: list[dict[str, Any]] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        statement = " ".join(str(item.get("statement") or "").split())
        scope = str(item.get("scope") or "global")
        if not authenticate_active_rule_item(statement, scope):
            dropped += 1
            continue
        bounded.append(
            {
                "statement": statement,
                "scope": scope,
                "observation_count": int(item.get("observation_count") or 0),
                "event_id": str(item.get("event_id") or ""),
                "reviewed_entry_id": str(item.get("reviewed_entry_id") or ""),
            }
        )
        if len(bounded) >= _ACTIVE_RULES_PROMPT_ITEM_LIMIT:
            break
    stats = {"total": total, "accepted": len(bounded), "dropped": dropped}
    return bounded, stats


def _catalog_for_prompt(items: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        catalog.append(
            {
                "name": name,
                "description": str(item.get("description", "")).strip(),
            }
        )
    return catalog


def _review_messages(
    *,
    seed: FactorDefinition,
    candidate: FactorDefinition,
    evaluation: EvaluationResult,
    backtest: BacktestResult,
    split_weighted_icir: float,
    score: float,
    gate_passed: bool,
    gate_reasons: tuple[str, ...],
) -> list[dict[str, str]]:
    system = (
        "You are Quant Forge's RD reviewer. Return one JSON object only. "
        "Use the provided local evaluation/backtest evidence. Do not claim production readiness. "
        "Return exactly the requested schema keys. Do not wrap the JSON in markdown. "
        "Write summary, strengths, risks, and next_hypotheses in Chinese. "
        f"{CHINESE_RD_REPORT_PROMPT}"
    )
    user = json.dumps(
        {
            "seed": _factor_summary(seed),
            "candidate": _factor_summary(candidate),
            "evidence": {
                "rank_ic_mean": evaluation.rank_ic_mean,
                "rank_icir": evaluation.rank_icir,
                "coverage": evaluation.coverage,
                "ic_days": evaluation.ic_days,
                "split_weighted_icir": split_weighted_icir,
                "score": score,
                "net_annualized_return": backtest.net_annualized_return,
                "net_long_short_sharpe": backtest.net_long_short_sharpe,
                "turnover_rate": backtest.turnover_rate,
                "rebalance_rate": backtest.rebalance_rate,
                "max_drawdown": backtest.max_drawdown,
                "warnings": tuple(backtest.warnings),
                "gate_passed": gate_passed,
                "gate_reasons": gate_reasons,
            },
            "return_shape": {
                "schema_version": RD_LLM_SCHEMA_VERSION,
                "task_type": "rd_research_review",
                "summary": "one sentence",
                "strengths": ["short bullets"],
                "risks": ["short bullets"],
                "next_hypotheses": ["bounded next research ideas"],
                "normalization_warnings": [],
            },
        },
        ensure_ascii=False,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _factor_summary(factor: FactorDefinition) -> dict[str, Any]:
    return {
        "factor_id": factor.factor_id,
        "name": factor.name,
        "formula": _formula_for_prompt(factor.formula),
        "description": factor.description,
        "horizon_days": factor.horizon_days,
        "universe_filters": factor.universe_filters,
        "source": factor.source,
    }


def _formula_for_prompt(formula: str) -> str:
    if is_precomputed_formula(formula):
        return "<mounted_precomputed_reference_not_usable_in_formula>"
    return formula


def _mechanism_guidance_for_prompt(seed: FactorDefinition) -> dict[str, Any]:
    formula = _formula_for_prompt(seed.formula)
    seed_formula_shape = "linear_rank_sum" if _looks_like_linear_rank_sum(formula) else "other"
    return {
        "seed_formula_shape": seed_formula_shape,
        "research_loop": [
            "Research: choose a mechanism lane before proposing formula_dsl.",
            "Development: translate the mechanism into one executable formula using only available fields/operators.",
            "Feedback: design the candidate so local validation, duplicate checks, and evaluation can reject it clearly.",
        ],
        "must_avoid": [
            "Do not merely add another rank term to a linear rank-sum seed.",
            "Do not use covariance or correlation unless the rationale names the co-movement being measured.",
            "Do not return multiple candidates with the same formula family and only cosmetic wording changes.",
        ],
        "preferred_lanes": [
            {
                "name": "interaction_conjunction",
                "purpose": "Require the seed concepts to be jointly present instead of linearly adding exposures.",
                "use_when": "small-cap, low-volatility, quality, stability, or momentum ideas should work only together.",
                "example_formula": "rank((1 - rank(market_cap)) * (1 - rank(volatility_5d)) * rank(return_5d))",
            },
            {
                "name": "stability_smoothing",
                "purpose": "Replace raw recent return exposure with low dispersion or smoothed persistence.",
                "use_when": "a seed says stable returns, low volatility, or noisy short-term reversal.",
                "example_formula": "rank((1 - rank(market_cap)) * (1 - rank(volatility_5d)) * (1 - rank(stddev(return_5d, 20))))",
            },
            {
                "name": "relationship_consistency",
                "purpose": "Measure whether two interpretable components co-move persistently through time.",
                "use_when": "covariance/correlation expresses a real relationship such as size-volatility consistency.",
                "example_formula": "rank(covariance(1 - rank(market_cap), 1 - rank(volatility_5d), 20))",
            },
            {
                "name": "horizon_retiming",
                "purpose": "Change the lookback horizon to test whether the effect is short-lived or persistent.",
                "use_when": "feedback shows weak OOS, high turnover, or unstable IC.",
                "example_formula": "rank(ts_mean(return_5d, 10) - stddev(return_5d, 20))",
            },
            {
                "name": "risk_cost_control",
                "purpose": "Reduce candidates likely to fail after costs by penalizing noisy or high-turnover components.",
                "use_when": "feedback mentions high turnover, OOS decay, or cost sensitivity.",
                "example_formula": "rank(ts_mean(return_5d, 10) - stddev(return_1d, 20))",
            },
        ],
        "rationale_requirements": [
            "Name the economic thesis in one sentence.",
            "Explain why the formula shape is not a cosmetic variation of the seed.",
            "Name one expected failure mode such as OOS decay, crowding, turnover, or data sparsity.",
        ],
    }


def _looks_like_linear_rank_sum(formula: str) -> bool:
    if formula == "<mounted_precomputed_reference_not_usable_in_formula>":
        return False
    return formula.count("rank(") >= 2 and "+" in formula and "*" not in formula and "covariance(" not in formula


def _hypotheses_from_payload(payload: dict[str, Any], *, max_candidates: int) -> tuple[ResearchHypothesis, ...]:
    raw = payload.get("hypotheses")
    if not isinstance(raw, list):
        raise RuntimeError("RD LLM response must include hypotheses list")
    # Record (non-silently) any drift from the versioned RD hypothesis contract.
    # We coerce-and-warn rather than raise so already-lenient payloads keep
    # parsing, mirroring normalize_review_payload's behavior on the review path.
    if str(payload.get("schema_version", "")).strip() != RD_LLM_SCHEMA_VERSION:
        warnings.warn(
            f"RD LLM hypothesis payload schema_version_missing_or_mismatched "
            f"(expected {RD_LLM_SCHEMA_VERSION})",
            stacklevel=2,
        )
    if str(payload.get("task_type", "")).strip() != "rd_research_hypotheses":
        warnings.warn(
            "RD LLM hypothesis payload task_type_missing_or_mismatched "
            "(expected rd_research_hypotheses)",
            stacklevel=2,
        )
    parsed: list[ResearchHypothesis] = []
    provider_fallback_seen: list[bool] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("each RD LLM hypothesis must be an object")
        text = _required_str(item, "text")
        rationale = str(item.get("rationale", "")).strip()
        if text in seen:
            continue
        raw_source = item.get("source")
        formula_dsl = str(item.get("formula_dsl", "")).strip()
        provider_requested_parameter_search = bool(item.get("parameter_search_fallback", False)) or (
            str(raw_source or "").strip() == "parameter_search"
        )
        parsed.append(
            ResearchHypothesis(
                text=text,
                rationale=rationale,
                source=_hypothesis_source(raw_source),
                source_detail=str(item.get("source_detail", "")).strip(),
                parameter_search_fallback=False,
                formula_dsl=formula_dsl,
                input_fields=_string_tuple(item.get("input_fields")),
                expected_direction=_expected_direction(item.get("expected_direction")),
                universe_constraints=_string_tuple(item.get("universe_constraints")),
            )
        )
        provider_fallback_seen.append(provider_requested_parameter_search)
        seen.add(text)
    hypotheses = _drop_provider_fallbacks_when_regular_ideas_exist(parsed, provider_fallback_seen)[:max_candidates]
    return tuple(hypotheses)


def _drop_provider_fallbacks_when_regular_ideas_exist(
    hypotheses: list[ResearchHypothesis],
    provider_fallback_seen: list[bool],
) -> list[ResearchHypothesis]:
    if any(not is_fallback for is_fallback in provider_fallback_seen):
        return [item for item, is_fallback in zip(hypotheses, provider_fallback_seen, strict=True) if not is_fallback]
    return hypotheses


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise RuntimeError(f"LLM response missing required field: {key}")
    return value


def _fallback_review_summary(
    *,
    candidate: FactorDefinition,
    score: float,
    split_weighted_icir: float,
    gate_passed: bool,
) -> str:
    status = "passed" if gate_passed else "did not pass"
    return (
        f"{candidate.factor_id} {status} the research gate with score {score:.4f}; "
        f"weighted split ICIR is {split_weighted_icir:.4f}."
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeError("LLM response list field must be a list")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _hypothesis_source(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"financial_analyst", "effective_idea", "operator_mcp", "llm"}:
        return text
    return "llm"


def _expected_direction(value: Any) -> str:
    text = str(value or "positive").strip().lower()
    if text in {"positive", "negative", "unknown"}:
        return text
    return "positive"

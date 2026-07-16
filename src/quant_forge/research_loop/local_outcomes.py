"""Local RD-loop -> ``ResearchOutcome`` v2 producer (SE-P2).

Pure mapping from THIS repository's local research-loop candidate result
(:class:`~quant_forge.research_loop.service.ResearchCandidateResult`, the
smoke-gate bundle produced by ``ResearchLoopService._evaluate_final_trial``
-- conceptually the local analogue of a structured ``FactorExperimentResult``
+ ``GateDecision`` pair) into the provider-neutral
:class:`~quant_forge.research_loop.outcomes.ResearchOutcome` contract
(DECISIONS.md "2026-07-13 -- Self-evolution engine CP0", rulings
SE-i/SE-ii/SE-vii). Mirrors the mapping discipline of the SE-P3 external
producer (``worldquant/adapter/src/quant_forge_worldquant/
self_evo_producer.py``: closed reason-code table, allowlisted metric
snapshot, honest-unknown scope/window, no clock) WITHOUT importing it --
that module lives under a gitignored-local plugin tree this package must
never depend on.

origin / stage
---------------
Every outcome here carries ``origin="local"`` (SE-i: routes to the MAIN
kernel store, never a plugin-local one) and ``stage="gate"`` -- the local
candidate gate (``service.apply_gate_detailed``) is the producing stage, so
``evidence_strength`` derives to ``"local_backtest"`` (``STAGE_EVIDENCE_
STRENGTH["gate"]``), never inflated by this module (owner ruling R5-3).

verdict
-------
``result.gate_passed`` -> ``"passed"`` with reason_codes exactly
``(REASON_NONE,)``; ``False`` -> ``"blocked"`` with >=1 real reason code.
When NO gate-reason family maps to a closed code (administrative-only or
entirely unrecognized families), the mapper returns ``None`` -- no outcome,
no ledger row, no observation (the fail-closed contract; the pre-review
VALIDATION_ERROR fallback was rejected as fabricating a validation failure
-- SE-P2 review P2-F1/P2-F2, doc fix RV2-F5).

reason_codes mapping table (closed ``outcomes.REASON_CODES`` set; SE-ii)
-------------------------------------------------------------------------
Input is ``result.gate_reasons`` reduced to stable, value-free "families"
via :func:`_reason_family` (leading colon-segment, then leading
space-token). This rule originated as the pre-SE-P2 ``service.
_gate_reason_families`` memory-signature helper; that function was removed
from service.py as dead code once this seam migrated (it had no other
caller), so :func:`_reason_family` is now the sole copy, kept here (not
imported) specifically to avoid the service.py<->local_outcomes.py cycle --
see "circular-import notes" below.

Exact family -> reason code (checked first, case-insensitive)::

    ic_days                    -> INSUFFICIENT_SAMPLE  (too few IC days)
    backtest_periods           -> INSUFFICIENT_SAMPLE  (too few backtest periods)
    insufficient_oos_evidence  -> INSUFFICIENT_SAMPLE  (missing OOS evidence, not a threshold miss)
    insufficient_evidence      -> INSUFFICIENT_SAMPLE  (missing turnover/retention evidence)
    coverage                   -> DATA_UNAVAILABLE     (cross-sectional coverage below floor)
    rebalance_rate             -> TURNOVER_TOO_HIGH    (rebalance_rate above threshold)
    turnover_rate               -> TURNOVER_TOO_HIGH    (turnover_rate above threshold)
    net_return_retention        -> RETURNS_BELOW_GATE   (net/gross retention below threshold)
    score                       -> OBJECTIVE_SCORE_BELOW_GATE
                                    (blended research-objective composite below its configured
                                     gate; reviewed contract amendment, SE-P2 review 2026-07-14 --
                                     the pre-review VALIDATION_ERROR label claimed a validation
                                     failure that never happened)

ADMINISTRATIVE families (``duplicate``, ``existing``, ``passed``) are
workflow bookkeeping, not scientific evidence -- a duplicate-signature
rejection or a pre-existing-status conflict says nothing about the factor's
behavior, and the pass-marker string only leaks into ``gate_reasons`` on
the status-conflict flip path in ``service.run_once``. They map to NO
reason code: when they co-occur with real blockers they are simply omitted,
and a result blocked ONLY by administrative families produces NO outcome at
all (the mapper returns ``None`` and the caller log-skips) rather than a
fabricated failure lesson (SE-P2 review finding P2-F1).

Anchored variable-family fallback (checked when no exact family matches):
the ONLY variable family the local gate actually emits is an OOS segment
name -- ``oos_return_evidence`` and the decay clause admit exclusively
segments whose name starts with ``OOS`` (case-insensitive), producing
reasons like ``OOS net_annualized_return below threshold: ...`` /
``OOS1 ...`` / ``OOS2 ...`` (the shipped ``DEFAULT_SAMPLE_SPLITS`` names)
/ ``OOS_2024H2 ...`` / ``OOS net return decay exceeds ...`` whose
extracted family token is the segment name itself::

    ^oos[0-9]*([_.-][a-z0-9_.\\-]*)?$  -> RETURNS_BELOW_GATE  (OOS shortfall/decay)

Anything matching neither table FAILS CLOSED: the family maps to no code
(SE-P2 review finding P2-F2 -- the earlier broad substring fallback let
unrelated future tokens like ``returning_candidate`` silently classify as
RETURNS_BELOW_GATE, which is dishonest classification, and SE-ii forbids
inventing a new code on the fly). As with administrative families, a
result whose EVERY family fails closed produces no outcome at all. Multiple
gate reasons collapsing onto the same closed code count once (reason_codes
is a deduped, sorted tuple, matching ``ResearchOutcome``'s own identity
contract). ``TURNOVER_TOO_LOW``, ``VALIDATION_ERROR`` and
``EXECUTION_ERROR`` are valid, closed codes this table simply never emits
today: the local smoke gate has no minimum-turnover clause, no validation
stage of its own, and a caught evaluation/backtest exception aborts the
candidate before a ``ResearchCandidateResult`` (and hence this pure
mapper) is ever reached.

metric_snapshot (closed allowlist; SE-ii)
-------------------------------------------
Sourced from the STRUCTURED ``qf.metrics.v2`` mapping on the IN-SAMPLE
selection backtest (``BacktestResult.metrics``; see "sample_role" below),
never the legacy scalar attributes -- each reading is emitted ONLY where the
structured metric's ``status`` is ``"available"`` (SE-P2 review finding F4).
``sharpe``, ``annualized_return``, ``max_drawdown``, ``turnover`` are the
only keys this producer can source: each prefers the ``net_*`` (after-cost)
structured metric and falls back to the ``gross_*`` one, honestly labeled
via ``MetricReading.basis``. ``max_drawdown`` is converted to ``abs()`` of
the selected reading: the local backtester reports drawdown in its
negative-return convention, while the frozen contract (``outcomes.
METRIC_SPECS["max_drawdown"]``) defines a NON-NEGATIVE magnitude -- the sign
is a reporting convention, not information, so taking the magnitude is a
lossless unit conversion, never a rewrite of a measurement (P2-F3). A metric
with no available structured reading is OMITTED from the snapshot entirely,
never emitted as a fabricated 0 read off a scalar's 0.0 dataclass default:
a degenerate backtest (defaults only, empty ``metrics`` mapping) therefore
yields an EMPTY snapshot (verdict/reasons are computed elsewhere and are
unaffected). ``subwindow_sharpe``, ``self_correlation``, ``max_weight`` are
allowlisted in the target vocabulary but this local smoke-gate pipeline has
no structured source for any of the three (sub-window Sharpe and
self/redundancy correlation live only in the BRAIN-facing
``integrations/gate.py``, which this neutral module must never import -- see
"circular-import and neutrality notes"; no local backtest computes a
per-name weight at all), so they are simply never emitted. ``fitness``/
``icir``/``ic_mean``/``redundancy`` are never populated (outside the
design's allowlisted set for this producer; the local score is a blended
composite, not any one of these single metrics -- see the ``score``
reason-code note above).

sample_role (do-not-guess-OOS; explicit design constraint)
--------------------------------------------------------------
``"in_sample"`` ONLY when ``result.selection_backtest`` is present and
self-reports ``sample_role == quant_forge.backtesting.service.
IN_SAMPLE_ROLE`` (it always does for real pipeline results:
``service._score_trial`` always constructs it with
``sample_role=IN_SAMPLE_ROLE``) -- this is also the SAME backtest object the
local gate's non-OOS-specific blocking checks (score/coverage/ic_days/
backtest_periods/turnover) actually evaluated, so metric_snapshot stays
consistent with reason_codes. Every other case -- including
``result.backtest``, which is actually the EXTERNAL OOS backtest by
construction (``service._evaluate_final_trial`` passes
``backtest=external_oos_backtest``) -- stays ``"unspecified"``. This is a
deliberate, narrower rule than the sibling SE-P3 producer's (which DOES map
an analogous "external_oos_backtest" label straight to "out_of_sample" for
its own, separately-reviewed report shape): whether ``external_oos_backtest``
is a genuinely distinct holdout window depends on run configuration
(``evaluation_simulation_profile`` vs ``backtest_simulation_profile``) this
module cannot cheaply re-verify, and the design explicitly says "do not
guess OOS" for this producer -- an under-claimed "unspecified" is always
safe; an over-claimed "out_of_sample" is not.

factor_id / factor_fingerprint (identity; SE-ii)
------------------------------------------------
``factor_id = result.factor.factor_id`` when it satisfies the frozen
contract's identity charset (mirrored locally as ``_FACTOR_ID_RE`` --
``outcomes._ID_RE`` is module-private, and duplicating a short, stable regex
literal is safer than reaching into another module's underscore-prefixed
internals). ``FactorDefinition.factor_id`` additionally allows ``"="``
(parity with ``factor_library.repository._FACTOR_ID_RE``), which the
outcomes identity contract does not; a real but ``"="``-bearing seed
factor_id has NO representable identity in the neutral vocabulary and the
mapper returns ``None`` (log-skip at the caller) rather than stripping or
rewriting the character -- identity fields are REJECTED, never rewritten
(outcomes.py ``_require_clean_token``).

``factor_fingerprint`` prefers ``result.formula_fingerprint`` (populated by
every real pipeline result -- ``service._evaluate_final_trial`` always sets
it via ``factor_formula_fingerprint(candidate)``); the
``factor_formula_fingerprint(result.factor)`` fallback (for a hand-built
result that left the field blank) is imported LAZILY inside the function
body, again to avoid the circular import (see below). Either way the value
is ``.lower()``-cased before use: ``service._hash_parts`` emits UPPERCASE
hex (``hashlib...hexdigest()[:16].upper()``) while the frozen contract's
``_HEX_RE`` requires lowercase -- hex case carries no identity meaning
(sha1/sha256 digests are canonically lowercase; ``.upper()`` is a
service.py display convention only), so lower-casing is a lossless
normalization, not a rewrite of identity content.

window
------
:func:`quant_forge.workbench.service.evaluation_data_window` (the exact
primitive ``service._memory_data_window`` itself calls, imported directly
here to avoid a service.py<->local_outcomes.py cycle) over
``result.evaluation``; ``"available"`` only when both split-metric dates are
present, else honestly ``"unavailable"``. A malformed date (should never
happen for a real evaluation result) degrades to ``"unavailable"`` rather
than raising out of a pure mapper, mirroring the SE-P3 producer's
``_window_from_local_report``.

scope
-----
``asset_class``/``universe`` are read off ``result.evaluation.
simulation_profile`` (``instrument_type``/``universe`` -- always-populated,
per-candidate-accurate fields), lower-cased and validated against the
frozen contract's dimension grammar; a value that cannot satisfy that
grammar degrades to ``""``, an honest unknown, never a raise.

``horizon_bucket`` stays ``""`` (unknown): there is no established
horizon-bucketing scheme anywhere in this codebase to reuse, and inventing
domain thresholds (e.g. "short"/"medium"/"long") is not this module's call
to make. This is safe to leave unknown -- see the ``factor_family``/
``settings_profile`` paragraph below for why those two specifically cannot
follow the same path.

``factor_family`` is the FIXED LITERAL ``_FACTOR_FAMILY =
"rd_local_candidate"``, NOT ``""``, even though the local candidate shape
carries no real per-strategy-type taxonomy (``FactorDefinition`` has no
family field). This is deliberate, not a shortcut: ``outcomes.
OutcomeScope.signature_payloads()`` disambiguates EVERY signature by its
own ``evidence_run_id()`` whenever EITHER family or settings is empty
(R-F4), and ``evidence_run_id()`` is itself deterministic per
``(factor_fingerprint, window, stage)`` -- so an empty dimension would make
two DIFFERENT evidence runs (e.g. two distinct candidate factors
independently blocked for the same reason) permanently unable to share a
signature, and promotion (``>=2`` distinct ``run_id``s per signature) could
never fire for ANY local outcome. The fixed literal names the ONE coarse
cohort this V1 pipeline honestly has today (upheld by the SE-P2 review as
an explicitly-coarse-but-real cohort); a real per-strategy-type taxonomy
replacing it is future work, not a regression.

``settings_profile`` is DERIVED, never a fixed label: ``_settings_profile_
token(gate)`` = ``"rd_" + sha256(canonical JSON of the effective
ResearchGate's fields)[:10]``. The pre-review fixed ``"rd_default"``
literal merged evidence produced under materially different gate settings
(``run_once`` accepts arbitrary per-run ``ResearchGate`` overrides --
e.g. a turnover failure under ``max_turnover_rate=0.2`` and another under
``1.5`` shared a signature and could promote together despite different
standards; SE-P2 review finding P2-F4). The token is bounded (13 chars),
deterministic (sorted-key JSON over ``dataclasses.fields``, no floats
reformatted), matches the scope-dimension grammar, and is intentionally
OPAQUE rather than named: equal effective gates -- and only equal
effective gates -- unify, including the constructor-default gate (no
special "default" name that could silently become a lie if the defaults
ever change). Adding a field to ``ResearchGate`` shifts every token, which
is the conservative direction: evidence produced under a gate whose
semantics changed does not silently unify with old evidence.

observed_at (no clock; run_id-derived)
----------------------------------------
``ResearchCandidateResult`` and its nested ``EvaluationResult``/
``BacktestResult`` carry NO timestamp field anywhere in the current
contract, so "the result's timestamp" is never available; "the run's"
timestamp is parsed OUT of the ``run_id`` string's embedded UTC timestamp
segment (``service._research_run_id``: ``rd_<safe_seed>_<%Y%m%dT%H%M%S%fZ>_
<hex8>``) via a trailing structural regex (mirrors ``service.
_RUN_ID_SUFFIX_RE``'s own convention of parsing run_id structure, without
needing to know the seed-derived prefix boundary this function is not
given). This keeps the mapper clock-free (no ``datetime.now()`` call) while
still resolving a real, run-associated, tz-aware timestamp. If ``run_id``
does not carry a parseable timestamp (never true for a real
``service.run_once`` call; only reachable with a hand-crafted run_id), this
raises ``ValueError`` rather than fabricating a clock, per the design's
explicit "STOP and report" instruction for this one field (contrast with
window/scope above, which have a real "unknown" state to degrade into --
``observed_at`` does not).

circular-import and neutrality notes
--------------------------------------
``service.py`` imports THIS module (and ``outcome_ingest.py``) for the
migrated ``_record_memory_observations`` seam, so a module-level
``from quant_forge.research_loop.service import ...`` here would be a
genuine import cycle (whichever module loads first would hit the other's
top-level import before the needed name is defined). ``ResearchCandidateResult``
is therefore imported ONLY under ``TYPE_CHECKING`` (erased at runtime by
``from __future__ import annotations``); ``factor_formula_fingerprint`` is
imported LAZILY inside the one function body that needs its fallback path.
Neither this module nor ``outcome_ingest.py`` imports anything from
``integrations/`` or any provider/network module (SE-vii open-boundary /
neutrality test): ``subwindow_sharpe``/``self_correlation``/``max_weight``
are left honestly absent rather than reached for in ``integrations/
gate.py``, which is exactly the boundary this module must not cross.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from quant_forge.backtesting.service import IN_SAMPLE_ROLE
from quant_forge.core.contracts import BacktestResult, EvaluationResult
from quant_forge.research_loop.outcomes import (
    REASON_NONE,
    MetricReading,
    OutcomeScope,
    OutcomeWindow,
    ResearchOutcome,
)
from quant_forge.workbench.service import evaluation_data_window

if TYPE_CHECKING:
    from collections.abc import Mapping

    from quant_forge.core.contracts import MetricValue
    from quant_forge.research_loop.service import ResearchCandidateResult, ResearchGate

__all__ = ["experiment_result_to_outcome"]

# Mirrors outcomes._ID_RE (module-private in the frozen contract; duplicated
# here rather than imported -- see module docstring "factor_id" section).
_FACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
# Mirrors outcomes.OutcomeScope's own dimension-token grammar (outcomes._DIM_RE).
_SCOPE_DIM_RE = re.compile(r"^[a-z0-9_.\-]{0,32}$")
# Mirrors outcomes._RESERVED_DIM_SENTINELS.
_RESERVED_SCOPE_SENTINELS = frozenset({"unknown", "global"})
# service._research_run_id's trailing `<%Y%m%dT%H%M%S%fZ>_<hex8>` shape,
# anchored at the end of the string (mirrors service._RUN_ID_SUFFIX_RE's
# convention without needing the seed-derived prefix length this function is
# not given).
_RUN_ID_TIMESTAMP_RE = re.compile(r"(\d{8}T\d{12}Z)_[0-9a-f]{8}\Z")

# Fixed family literal (see module docstring "scope" section): required
# non-empty so outcomes.OutcomeScope.signature_payloads() does not
# per-evidence-run disambiguate every local signature, which would make
# promotion structurally unreachable (R-F4 interaction). settings_profile is
# DERIVED per effective gate -- see _settings_profile_token.
_FACTOR_FAMILY = "rd_local_candidate"
_SETTINGS_TOKEN_PREFIX = "rd_"
_SETTINGS_TOKEN_HEX_LEN = 10

# --- reason_codes mapping table (see module docstring for the full table) ---

_EXACT_FAMILY_REASON_CODES: dict[str, str] = {
    "ic_days": "INSUFFICIENT_SAMPLE",
    "backtest_periods": "INSUFFICIENT_SAMPLE",
    "insufficient_oos_evidence": "INSUFFICIENT_SAMPLE",
    "insufficient_evidence": "INSUFFICIENT_SAMPLE",
    "coverage": "DATA_UNAVAILABLE",
    "rebalance_rate": "TURNOVER_TOO_HIGH",
    "turnover_rate": "TURNOVER_TOO_HIGH",
    "net_return_retention": "RETURNS_BELOW_GATE",
    # Reviewed contract amendment (SE-P2 review 2026-07-14, P2-F1): the
    # blended objective composite is a real scientific blocker; the closed
    # vocabulary gained OBJECTIVE_SCORE_BELOW_GATE for exactly this shape.
    "score": "OBJECTIVE_SCORE_BELOW_GATE",
}

# Workflow bookkeeping, not scientific evidence (P2-F1): mapping these to any
# reason code would fabricate a failure lesson. They are omitted alongside
# real blockers; a result blocked ONLY by these produces no outcome at all.
_ADMINISTRATIVE_FAMILIES = frozenset({"duplicate", "existing", "passed"})

# The ONLY variable family the local gate emits is an OOS segment name
# (oos_return_evidence / the decay clause admit exclusively names starting
# with "OOS", case-insensitive). Anchored, not substring (P2-F2):
# "returning_candidate" or "no_return_path" must NOT classify as a returns
# shortfall. The optional digit run right after "oos" covers the SHIPPED
# default split names OOS1/OOS2 (evaluation.service.DEFAULT_SAMPLE_SPLITS --
# re-verify RV2-F1: the first anchored pattern required a separator and
# silently dropped exactly the segments the default configuration emits);
# a letter directly after "oos"/"oos<digits>" (e.g. "oosmalformed") still
# fails closed. Families matching nothing FAIL CLOSED (no code, no
# invention).
_VARIABLE_FAMILY_REASON_CODES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^oos[0-9]*(?:[_.\-][a-z0-9_.\-]*)?$"), "RETURNS_BELOW_GATE"),
)


def experiment_result_to_outcome(
    result: "ResearchCandidateResult", *, run_id: str, gate: "ResearchGate"
) -> ResearchOutcome | None:
    """Map one local candidate result to a ``ResearchOutcome`` (SE-P2 producer).

    ``gate`` is the effective ``ResearchGate`` that judged ``result`` (the
    same object ``service.run_once`` resolved); it feeds ONLY the derived
    ``settings_profile`` scope token (P2-F4) -- this mapper re-evaluates
    nothing.

    Returns ``None`` when the result has no representable outcome in the
    neutral vocabulary: either ``result.factor.factor_id`` fails the frozen
    identity charset (see module docstring "factor_id" section), or a
    blocked result's EVERY gate-reason family is administrative or unmapped
    (fail-closed; see the reason-table section -- emitting a fabricated
    reason code would be dishonest). The caller (``service._record_memory_
    observations``) is responsible for logging the skip -- this pure mapper
    performs no I/O of its own.
    """

    factor_id = result.factor.factor_id
    if not _FACTOR_ID_RE.fullmatch(factor_id):
        return None

    fingerprint = result.formula_fingerprint
    if not fingerprint:
        # Deferred import: breaks the service.py<->local_outcomes.py cycle
        # (see module docstring "circular-import notes"). By the time this
        # function is actually CALLED, service.py has always finished
        # executing its own top-level module body (it is the one calling
        # this function), so the name is guaranteed to exist.
        from quant_forge.research_loop.service import factor_formula_fingerprint

        fingerprint = factor_formula_fingerprint(result.factor)
    fingerprint = fingerprint.lower()

    observed_at = _observed_at_from_run_id(run_id)

    if result.gate_passed:
        verdict = "passed"
        reason_codes: tuple[str, ...] = (REASON_NONE,)
    else:
        verdict = "blocked"
        reason_codes = _blocked_reason_codes(result.gate_reasons)
        if not reason_codes:
            return None

    metric_backtest, sample_role = _sample_role_and_backtest(result)

    return ResearchOutcome(
        origin="local",
        stage="gate",
        verdict=verdict,
        factor_id=factor_id,
        factor_fingerprint=fingerprint,
        observed_at=observed_at,
        reason_codes=reason_codes,
        sample_role=sample_role,
        window=_outcome_window(result.evaluation),
        scope=_outcome_scope(result, gate),
        metric_snapshot=_metric_snapshot(metric_backtest),
    )


def _blocked_reason_codes(gate_reasons: tuple[str, ...]) -> tuple[str, ...]:
    """Deduped, sorted closed codes for the representable families only.

    Empty when every family is administrative or fails closed -- the caller
    then skips the whole outcome rather than minting a fabricated reason
    (P2-F1/P2-F2). The pre-review behavior (collapse anything unmapped onto
    VALIDATION_ERROR) is exactly what the review rejected.
    """

    codes = {
        code
        for code in (_reason_code_for_family(family) for family in _reason_families(gate_reasons))
        if code is not None
    }
    return tuple(sorted(codes))


def _reason_family(reason: str) -> str:
    """Stable, value-free "family" for one raw gate reason: leading
    colon-segment, then leading space-token, stripped.

    This rule originated as ``service._gate_reason_families`` (the pre-SE-P2
    memory-signature helper); that function was removed from service.py as
    dead code once the seam migrated here (it had no other caller), so this
    module now owns the SOLE copy of the rule. It stays a small, pure,
    inlined function rather than an import specifically to avoid the
    service.py<->local_outcomes.py cycle described in the module docstring's
    "circular-import notes" -- service.py imports THIS module for the
    migrated seam.
    """

    return str(reason).split(":", 1)[0].split(" ", 1)[0].strip()


def _reason_families(reasons: tuple[str, ...]) -> tuple[str, ...]:
    families = (_reason_family(reason) for reason in reasons)
    return tuple(sorted(dict.fromkeys(family for family in families if family)))


def _reason_code_for_family(family: str) -> str | None:
    """Closed code for one family, or ``None`` (administrative / fail-closed).

    ``None`` is NOT an error state: administrative families are deliberately
    unrepresentable, and an unrecognized family must not be guessed into a
    metric code (P2-F2 -- token-anchored matching only, no substring
    containment).
    """

    key = family.strip().lower()
    exact = _EXACT_FAMILY_REASON_CODES.get(key)
    if exact is not None:
        return exact
    if key in _ADMINISTRATIVE_FAMILIES:
        return None
    for pattern, code in _VARIABLE_FAMILY_REASON_CODES:
        if pattern.fullmatch(key):
            return code
    return None


def _observed_at_from_run_id(run_id: str) -> str:
    match = _RUN_ID_TIMESTAMP_RE.search(run_id)
    if match is not None:
        try:
            parsed = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ")
        except ValueError:
            pass
        else:
            return parsed.replace(tzinfo=timezone.utc).isoformat()
    raise ValueError(
        f"cannot derive observed_at: run_id {run_id!r} carries no parseable embedded "
        "timestamp and the result carries no timestamp of its own (refusing to "
        "fabricate a clock)"
    )


def _sample_role_and_backtest(result: "ResearchCandidateResult") -> tuple[BacktestResult, str]:
    selection = result.selection_backtest
    if selection is not None and selection.sample_role == IN_SAMPLE_ROLE:
        return selection, "in_sample"
    return result.backtest, "unspecified"


def _outcome_window(evaluation: EvaluationResult) -> OutcomeWindow:
    window = evaluation_data_window(evaluation)
    if str(window.get("status")) != "available":
        return OutcomeWindow()
    try:
        return OutcomeWindow(
            status="available",
            start_date=str(window["start_date"]),
            end_date=str(window["end_date"]),
        )
    except ValueError:
        # Malformed dates should never reach a real EvaluationResult; degrade
        # honestly rather than raise out of a pure mapper (mirrors the SE-P3
        # producer's _window_from_local_report).
        return OutcomeWindow()


def _outcome_scope(result: "ResearchCandidateResult", gate: "ResearchGate") -> OutcomeScope:
    profile = result.evaluation.simulation_profile
    return OutcomeScope(
        asset_class=_clean_scope_dim(profile.instrument_type),
        universe=_clean_scope_dim(profile.universe),
        factor_family=_FACTOR_FAMILY,
        settings_profile=_settings_profile_token(gate),
    )


def _settings_profile_token(gate: "ResearchGate") -> str:
    """Bounded deterministic settings token for the EFFECTIVE gate (P2-F4).

    ``"rd_" + sha256(sorted-key JSON of the gate's dataclass fields)[:10]``:
    equal effective gates -- and only equal effective gates -- share a
    token, so evidence produced under materially different thresholds can
    never unify into one signature. Uses ``dataclasses.fields`` reflection
    (duck-typed) rather than importing ``ResearchGate``, which would
    recreate the service.py<->local_outcomes.py cycle the module docstring
    describes. Output is 13 chars of ``[a-z0-9_]`` -- always a valid,
    non-reserved scope-dimension token.
    """

    payload = {
        field.name: _canonical_settings_value(getattr(gate, field.name)) for field in dataclasses.fields(gate)
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_SETTINGS_TOKEN_PREFIX}{digest[:_SETTINGS_TOKEN_HEX_LEN]}"


def _canonical_settings_value(value: object) -> object:
    """Numeric canonicalization so EQUAL gates hash equal -- and ONLY equal
    gates (RV2-F2 + RV3-F1).

    Raw JSON spelling splits values Python compares equal: ``0`` vs ``0.0``
    serialize differently, and ``-0.0 == 0.0`` while ``json.dumps`` spells
    them apart -- so equal dataclasses minted different tokens and could
    never promote together. The first fix (cast everything to float)
    over-corrected: distinct integers above 2**53 collapse in float, so two
    UNEQUAL gates could share a token. Canonical form is therefore INT for
    every integral value (ints kept exact; integral floats like ``5.0``
    normalized to ``5``; signed zero to ``0``) and the float itself for
    genuinely fractional values -- Python's ``==`` across int/float agrees
    with this normalization in both directions. bools/str/None pass
    through. Non-finite values never reach here --
    ``ResearchGate.__post_init__`` rejects them (RV2-F4).
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if isinstance(value, int):
        return value
    if value == 0.0:
        return 0
    if value.is_integer():
        return int(value)
    return value


def _clean_scope_dim(value: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate and candidate not in _RESERVED_SCOPE_SENTINELS and _SCOPE_DIM_RE.fullmatch(candidate):
        return candidate
    return ""


def _reading_from_metric(metric: MetricValue | None, *, basis: str) -> MetricReading | None:
    """A structured ``qf.metrics.v2`` reading -> outcome ``MetricReading``, or None.

    F4 (no fabricated zeros): ONLY a metric the backtester marked
    ``status == "available"`` (with a real value) becomes a reading. A missing
    key, a non-available status (insufficient_sample / not_applicable /
    invalid / ...), or a null value yields ``None`` so the caller omits the
    key -- a degenerate ``BacktestResult`` (empty ``metrics`` mapping) then
    produces NO readings instead of the ``value=0.0`` the old code read off the
    legacy scalar attributes' ``0.0`` dataclass defaults.
    """

    if metric is None or metric.status != "available" or metric.value is None:
        return None
    return MetricReading(value=metric.value, basis=basis)


def _net_first_reading(metrics: Mapping[str, MetricValue], net_key: str, gross_key: str) -> MetricReading | None:
    """Prefer the NET structured metric, fall back to GROSS, else ``None``.

    Preserves the historical net-first-then-gross preference while gating each
    candidate on ``status == "available"`` (F4).
    """

    return _reading_from_metric(metrics.get(net_key), basis="net") or _reading_from_metric(
        metrics.get(gross_key), basis="gross"
    )


def _magnitude(reading: MetricReading) -> MetricReading:
    """``abs()`` of a reading's value, basis preserved, ``None`` preserved.

    The frozen contract defines ``max_drawdown`` as a NON-NEGATIVE magnitude
    (``outcomes.METRIC_SPECS``); the local backtester reports it in a
    negative-return convention. The sign is convention, not information --
    dropping it is a unit conversion, not a measurement rewrite (P2-F3).
    """

    if reading.value is None:
        return reading
    return MetricReading(value=abs(reading.value), basis=reading.basis)


def _metric_snapshot(backtest: BacktestResult) -> dict[str, MetricReading]:
    """Outcome metric snapshot from the STRUCTURED ``qf.metrics.v2`` mapping.

    Sources every reading from ``BacktestResult.metrics`` (which carries a
    per-metric ``status``), emitting a key ONLY where ``status == "available"``
    (F4). The legacy scalar attributes default to ``0.0``, so the old snapshot
    fabricated a ``value=0.0`` reading for a degenerate result; sourcing from
    ``metrics`` (empty by default) makes such a result yield an EMPTY snapshot.
    Only ``sharpe``/``annualized_return``/``max_drawdown``/``turnover`` have a
    structured source here (see the module docstring's metric_snapshot note).
    """

    metrics = backtest.metrics
    snapshot: dict[str, MetricReading] = {}
    sharpe = _net_first_reading(metrics, "net_long_short_sharpe", "long_short_sharpe")
    if sharpe is not None:
        snapshot["sharpe"] = sharpe
    annualized_return = _net_first_reading(metrics, "net_annualized_return", "annualized_return")
    if annualized_return is not None:
        snapshot["annualized_return"] = annualized_return
    drawdown = _net_first_reading(metrics, "net_max_drawdown", "max_drawdown")
    if drawdown is not None:
        snapshot["max_drawdown"] = _magnitude(drawdown)
    turnover = _reading_from_metric(metrics.get("rebalance_turnover_mean"), basis="")
    if turnover is not None:
        snapshot["turnover"] = turnover
    return snapshot

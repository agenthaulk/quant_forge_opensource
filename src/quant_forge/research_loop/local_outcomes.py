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
``(REASON_NONE,)``; ``False`` -> ``"blocked"`` with >=1 real reason code
(never empty -- ``apply_gate``'s own invariant guarantees ``gate_reasons``
is non-empty whenever ``gate_passed`` is False, and the mapper falls back to
``VALIDATION_ERROR`` in the unreachable case it somehow were not).

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
    score                       -> VALIDATION_ERROR     (composite objective score; see note below)
    duplicate                   -> VALIDATION_ERROR     (duplicate result-signature rejection; workflow, not a metric)
    existing                    -> VALIDATION_ERROR     (pre-existing candidate status conflict; workflow, not a metric)
    passed                      -> VALIDATION_ERROR     (pass-marker string leaking into gate_reasons on the
                                                          status-conflict flip path in service.run_once; never a
                                                          real blocking reason on its own)

Substring fallback (checked when no exact family matches; first match
wins), covering shapes the current local gate does not yet emit but the
closed vocabulary anticipates (a future correlation/region/weight check),
plus the ``OOS ...`` / ``{segment} net_annualized_return ...`` shapes whose
family token IS the segment name, not a fixed word::

    sharpe                    -> SHARPE_BELOW_GATE
    self_correlation           -> SELF_CORRELATION_HIGH
    redundan(cy)                -> REDUNDANCY_HIGH
    drawdown                    -> DRAWDOWN_TOO_DEEP
    weight / concentration       -> WEIGHT_CONCENTRATION_HIGH
    turnover / rebalance         -> TURNOVER_TOO_HIGH
    region                       -> REGION_MISMATCH
    oos                          -> RETURNS_BELOW_GATE  (OOS segment shortfall/decay)
    return                       -> RETURNS_BELOW_GATE
    coverage / unavailable        -> DATA_UNAVAILABLE
    insufficient / sample / evidence -> INSUFFICIENT_SAMPLE

Anything matching neither table -> ``VALIDATION_ERROR`` (the documented,
closed-vocabulary fallback; SE-ii forbids inventing a new code). Multiple
gate reasons collapsing onto the same closed code count once (reason_codes
is a deduped, sorted tuple, matching ``ResearchOutcome``'s own identity
contract). ``TURNOVER_TOO_LOW`` and ``EXECUTION_ERROR`` are valid, closed
codes this table simply never emits today: the local smoke gate has no
minimum-turnover clause, and a caught evaluation/backtest exception aborts
the candidate before a ``ResearchCandidateResult`` (and hence this pure
mapper) is ever reached.

``score`` note: ``result.score`` is ``ResearchObjectiveWeights``'s blended
composite (``weighted_split_icir`` 0.4 + ``rank_ic_mean`` 0.25 +
``rank_icir`` 0.2 + ``annualized_return`` 0.1 + ``max_drawdown`` 0.05 by
default) -- it is neither "sharpe" nor purely "return", and the closed
vocabulary has no composite-objective code (deliberately: SE-ii excludes
provider composites, and a research-internal composite is no more honestly
representable by any single-metric code). ``VALIDATION_ERROR`` is the
documented fallback, not a claim that anything is broken.

metric_snapshot (closed allowlist; SE-ii)
-------------------------------------------
Only ``sharpe``, ``annualized_return``, ``max_drawdown``, ``turnover``
carry real numbers here -- read off the IN-SAMPLE selection backtest (see
"sample_role" below), preferring the ``net_*`` (after-cost) field and
falling back to the explicit ``gross_*`` field, honestly labeled either way
via ``MetricReading.basis``. ``subwindow_sharpe``, ``self_correlation``,
``max_weight`` are allowlisted in the target vocabulary but this local
smoke-gate pipeline has no numeric source for any of the three (sub-window
Sharpe and self/redundancy correlation live only in the BRAIN-facing
``integrations/gate.py``, which this neutral module must never import --
see "circular-import and neutrality notes"; no local backtest computes a
per-name weight at all) -- they are simply absent (``None``, never a
fabricated 0), not populated by guesswork. ``fitness``/``icir``/``ic_mean``/
``redundancy`` are never populated (outside the design's allowlisted set
for this producer; the local score is a blended composite, not any one of
these single metrics -- see the ``score`` reason-code note above).

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

``factor_family``/``settings_profile`` are FIXED LITERALS
(``_FACTOR_FAMILY = "rd_local_candidate"``, ``_SETTINGS_PROFILE =
"rd_default"``), NOT ``""``, even though the local candidate shape carries
no real per-strategy-type taxonomy or named tuning-profile axis
(``FactorDefinition`` has no family field; ``experiment_result_to_outcome``
is not even given the ``ResearchGate`` that produced ``result``, so no
settings signal is available beyond "the local smoke gate's own defaults").
This is deliberate, not a shortcut: ``outcomes.OutcomeScope.
signature_payloads()`` disambiguates EVERY signature by its own
``evidence_run_id()`` whenever EITHER dimension is empty (R-F4), and
``evidence_run_id()`` is itself deterministic per ``(factor_fingerprint,
window, stage)`` -- so an empty family/settings would make two DIFFERENT
evidence runs (e.g. two distinct candidate factors independently blocked
for the same reason) permanently unable to share a signature, and
promotion (``>=2`` distinct ``run_id``s per signature) could never fire for
ANY local outcome, no matter how many independent candidates recur with the
same verdict/reason/scope. That would silently break the "two runs still
promote a finding/failure" behavior this migration must preserve. Fixed
literals name the ONE family/profile this V1 pipeline honestly has today
(mirrors the SE-P3 producer's own fixed ``asset_class="us_eq"``/
``universe="top3000"``: "there is no other value this adapter could
honestly report" -- here, no more GRANULAR value); every locally-produced
outcome unifying under one coarse family/profile bucket is the INTENDED
SE-ii generalization (promote by reason-code family and scope, not by raw
per-factor fingerprint), not an accident. A real per-strategy-type
taxonomy replacing this fixed pair is future work, not a regression.

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
    from quant_forge.research_loop.service import ResearchCandidateResult

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

# Fixed scope literals (see module docstring "scope" section): required
# non-empty so outcomes.OutcomeScope.signature_payloads() does not
# per-evidence-run disambiguate every local signature, which would make
# promotion structurally unreachable (R-F4 interaction).
_FACTOR_FAMILY = "rd_local_candidate"
_SETTINGS_PROFILE = "rd_default"

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
    "score": "VALIDATION_ERROR",
    "duplicate": "VALIDATION_ERROR",
    "existing": "VALIDATION_ERROR",
    "passed": "VALIDATION_ERROR",
}

_SUBSTRING_FAMILY_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("sharpe", "SHARPE_BELOW_GATE"),
    ("self_correlation", "SELF_CORRELATION_HIGH"),
    ("redundan", "REDUNDANCY_HIGH"),
    ("drawdown", "DRAWDOWN_TOO_DEEP"),
    ("weight", "WEIGHT_CONCENTRATION_HIGH"),
    ("concentration", "WEIGHT_CONCENTRATION_HIGH"),
    ("turnover", "TURNOVER_TOO_HIGH"),
    ("rebalance", "TURNOVER_TOO_HIGH"),
    ("region", "REGION_MISMATCH"),
    ("oos", "RETURNS_BELOW_GATE"),
    ("return", "RETURNS_BELOW_GATE"),
    ("coverage", "DATA_UNAVAILABLE"),
    ("unavailable", "DATA_UNAVAILABLE"),
    ("insufficient", "INSUFFICIENT_SAMPLE"),
    ("sample", "INSUFFICIENT_SAMPLE"),
    ("evidence", "INSUFFICIENT_SAMPLE"),
)

_DEFAULT_REASON_CODE = "VALIDATION_ERROR"


def experiment_result_to_outcome(result: "ResearchCandidateResult", *, run_id: str) -> ResearchOutcome | None:
    """Map one local candidate result to a ``ResearchOutcome`` (SE-P2 producer).

    Returns ``None`` only when ``result.factor.factor_id`` has no
    representable identity in the neutral vocabulary (see module docstring
    "factor_id" section); the caller (``service._record_memory_
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
        scope=_outcome_scope(result),
        metric_snapshot=_metric_snapshot(metric_backtest),
    )


def _blocked_reason_codes(gate_reasons: tuple[str, ...]) -> tuple[str, ...]:
    codes = {_reason_code_for_family(family) for family in _reason_families(gate_reasons)}
    if not codes:
        # Unreachable for a real apply_gate() result (a blocked verdict
        # always carries >=1 raw reason -- see module docstring "verdict"
        # section), kept as an honest closed-vocabulary fallback rather than
        # an assertion so a future/hand-built caller shape cannot crash this
        # mapper.
        codes = {_DEFAULT_REASON_CODE}
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


def _reason_code_for_family(family: str) -> str:
    key = family.strip().lower()
    exact = _EXACT_FAMILY_REASON_CODES.get(key)
    if exact is not None:
        return exact
    for needle, code in _SUBSTRING_FAMILY_REASON_CODES:
        if needle in key:
            return code
    return _DEFAULT_REASON_CODE


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


def _outcome_scope(result: "ResearchCandidateResult") -> OutcomeScope:
    profile = result.evaluation.simulation_profile
    return OutcomeScope(
        asset_class=_clean_scope_dim(profile.instrument_type),
        universe=_clean_scope_dim(profile.universe),
        factor_family=_FACTOR_FAMILY,
        settings_profile=_SETTINGS_PROFILE,
    )


def _clean_scope_dim(value: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate and candidate not in _RESERVED_SCOPE_SENTINELS and _SCOPE_DIM_RE.fullmatch(candidate):
        return candidate
    return ""


def _net_or_gross(net: float | None, gross: float | None) -> MetricReading:
    if net is not None:
        return MetricReading(value=net, basis="net")
    if gross is not None:
        return MetricReading(value=gross, basis="gross")
    return MetricReading()


def _metric_snapshot(backtest: BacktestResult) -> dict[str, MetricReading]:
    return {
        "sharpe": _net_or_gross(backtest.net_long_short_sharpe, backtest.gross_long_short_sharpe),
        "annualized_return": _net_or_gross(backtest.net_annualized_return, backtest.gross_annualized_return),
        "max_drawdown": _net_or_gross(backtest.net_max_drawdown, backtest.gross_max_drawdown),
        "turnover": MetricReading(value=backtest.turnover_rate),
        "subwindow_sharpe": MetricReading(),
        "self_correlation": MetricReading(),
        "max_weight": MetricReading(),
    }

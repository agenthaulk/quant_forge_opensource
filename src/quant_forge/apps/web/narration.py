"""Server-side narration + clarify logic (agent_sidecar_frontend.md §5.5/§5.2/§5.6).

The pure narration/clarify TYPES live in :mod:`quant_forge.specs.narration`;
this module is the server-side behavior over them:

* **Ref resolution -- fail loud (pin).** A ``ref`` node must resolve to a
  currently-rendered, status-aware component and a known artifact.
  :func:`resolve_ref` raises :class:`UnresolvedNarrationRefError` otherwise --
  the sidecar can never point at a component that is not on screen.
* **Chat is never the sole carrier of a number (pin).**
  :func:`assert_chat_not_sole_number_carrier` re-validates a batch of narration
  (as persisted/journaled) through the schema, so a number can never have
  reached "chat" except via a ref the canonical renderer draws.
* **Clarify (tiered, blocking gate, supersede).** :class:`ClarifySession`
  records answers, keeps a superseded answer AND its replacement, projects
  answers onto provenance (``user_answer``), and refuses execution while a
  blocking question is unanswered (:func:`assert_clarify_unblocked`).
* **LLM readiness tri-state.** :func:`llm_readiness` returns
  ``unknown | unavailable | ready`` without ever pre-judging a token-redacted
  boot.
* **Journal replay.** :func:`replay_rendered_cards` reconstructs the exact
  NarrationNodes from a :class:`~quant_forge.apps.web.tools.SidecarJournal`
  so a replay reproduces the same rendered cards (spec §11 ship gate #1).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from quant_forge.apps.web.provenance import ProvenanceEntry
from quant_forge.specs.narration import (
    ClarifyAnswer,
    ClarifyQuestion,
    NarrationNode,
    NarrationRef,
    validate_clarify_questions,
)
from quant_forge.apps.web.tools import ACTION_TOOL_NAMES

__all__ = [
    "KNOWN_COMPONENT_IDS",
    "UnresolvedNarrationRefError",
    "ClarifyBlockedError",
    "resolve_ref",
    "active_component_ids_for",
    "assert_action_suggestion_allowlisted",
    "assert_chat_not_sole_number_carrier",
    "ClarifySession",
    "assert_clarify_unblocked",
    "llm_readiness",
    "SidecarSessionStore",
    "replay_rendered_cards",
]

# The canonical, status-aware components a narration ref may point at. Each is a
# real frontend surface backed by a canonical renderer (FE-L1/FE-L2): the
# pipeline card, the Factor Tape / report region, the benchmark comparison, the
# staggered-entry report, and the RD result. A ref to anything else is a bug
# (fail loud), never a silently-dropped node.
KNOWN_COMPONENT_IDS: frozenset[str] = frozenset(
    {"pipeline-card", "factor-tape", "report-comparison", "staggered-result", "rd-result"}
)


class UnresolvedNarrationRefError(ValueError):
    """A narration ref does not resolve to a currently-rendered component/artifact.

    Pin (WORKORDER P2): ref resolution failure = fail loud. The sidecar must
    never "say" a result by pointing at a component that is not on screen, or
    at an artifact the pipeline never produced.
    """


class ClarifyBlockedError(ValueError):
    """Execution was attempted while a blocking clarify question is unanswered.

    Pin (WORKORDER P2): blocking questions unanswered ⇒ no execution. Enforced
    server-side at the sidecar's confirm entrypoints (FE-L4 -- the UI is never
    the only gate).
    """


def active_component_ids_for(pipeline: dict[str, Any] | None) -> frozenset[str]:
    """Which components are currently rendered for a given pipeline snapshot.

    The pipeline card is present whenever a pipeline exists; the Factor Tape /
    benchmark comparison components come alive once the report exists (status
    ``completed``). This mirrors what ``static/views/pipeline.js`` /
    ``static/app.js`` actually render, so a server-validated ref can never
    outrun the DOM.
    """

    if not pipeline:
        return frozenset()
    active = {"pipeline-card"}
    if pipeline.get("status") == "completed":
        active.update({"factor-tape", "report-comparison"})
    return frozenset(active)


def resolve_ref(
    ref: NarrationRef,
    *,
    active_component_ids: frozenset[str],
    known_artifact_refs: frozenset[str] | None = None,
) -> NarrationRef:
    """Resolve a narration ref or raise :class:`UnresolvedNarrationRefError`.

    A ref resolves only when its ``component_id`` is a known canonical
    component AND is currently active, and (when the caller supplies the set)
    its ``artifact_ref`` is one the pipeline actually produced.
    """

    if ref.component_id not in KNOWN_COMPONENT_IDS:
        raise UnresolvedNarrationRefError(
            f"narration ref points at unknown component {ref.component_id!r} "
            f"(known: {sorted(KNOWN_COMPONENT_IDS)})"
        )
    if ref.component_id not in active_component_ids:
        raise UnresolvedNarrationRefError(
            f"narration ref points at component {ref.component_id!r} which is not currently rendered "
            f"(active: {sorted(active_component_ids)})"
        )
    if known_artifact_refs is not None and ref.artifact_ref not in known_artifact_refs:
        raise UnresolvedNarrationRefError(
            f"narration ref points at artifact {ref.artifact_ref!r} which this pipeline did not produce"
        )
    return ref


def assert_action_suggestion_allowlisted(node: NarrationNode) -> None:
    """An ``action_suggestion`` may only propose a tool in the closed action
    catalog (FE-X1/FE-X3): a model that names ``promote``/``submit``/anything
    off-list is rejected here, before it can be rendered as a clickable
    suggestion."""

    if node.kind != "action_suggestion":
        return
    if node.action not in ACTION_TOOL_NAMES:
        raise UnresolvedNarrationRefError(
            f"action_suggestion names {node.action!r}, which is not an allowlisted action tool "
            f"(allowed: {sorted(ACTION_TOOL_NAMES)})"
        )


def assert_chat_not_sole_number_carrier(narration: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> None:
    """Re-validate a batch of narration as persisted (journal + assertion, pin).

    Reconstructing each node through :meth:`NarrationNode.from_dict` re-runs the
    numeric-arg rejection, so a number can never have reached the narration
    plane except through a ``ref`` node (which carries only a component id + an
    artifact ref -- the canonical renderer, not the sidecar, draws the value).
    Raises :class:`~quant_forge.specs.narration.NumericNarrationArgError` if any
    node smuggled a numeric token into its args.
    """

    for node_dict in narration:
        node = NarrationNode.from_dict(node_dict)  # raises on numeric args / bad shape
        if node.kind == "action_suggestion":
            assert_action_suggestion_allowlisted(node)


# ---------------------------------------------------------------------------
# Clarify session (spec §5.2)
# ---------------------------------------------------------------------------


class ClarifySession:
    """The per-pipeline clarify interview state: tiered questions + answers.

    Answers are append-only; a later answer that replaces an earlier one for
    the same question links the earlier as ``superseded_by`` and BOTH stay in
    provenance ("what you clarified"). Execution is refused while any
    ``blocking`` question has no effective (non-superseded) answer.
    """

    def __init__(self, pipeline_id: str) -> None:
        self.pipeline_id = pipeline_id
        self._questions: list[ClarifyQuestion] = []
        self._answers: list[ClarifyAnswer] = []

    # -- questions ---------------------------------------------------------

    def pose(self, questions: list[ClarifyQuestion] | tuple[ClarifyQuestion, ...]) -> None:
        questions = tuple(questions)
        validate_clarify_questions(questions)
        self._questions = list(questions)

    @property
    def questions(self) -> tuple[ClarifyQuestion, ...]:
        return tuple(self._questions)

    def _question(self, question_key: str) -> ClarifyQuestion:
        for question in self._questions:
            if question.question_key == question_key:
                return question
        raise KeyError(f"unknown clarify question: {question_key!r}")

    # -- answers -----------------------------------------------------------

    def answer(self, question_key: str, chosen_option_id: str | None = None, *, skipped: bool = False) -> ClarifyAnswer:
        """Record an answer (or a skip = accept default, recorded).

        A prior effective answer for the same question is kept and linked as
        superseded by this one (spec §5.2: "a later answer that invalidates an
        earlier one keeps BOTH in provenance").
        """

        question = self._question(question_key)
        if skipped:
            chosen = question.default_option_id
        else:
            chosen = str(chosen_option_id or "")
            if chosen not in question.option_ids():
                raise ValueError(
                    f"clarify answer {chosen!r} is not an option of {question_key!r} "
                    f"(options: {sorted(question.option_ids())})"
                )
        # Supersede any prior effective answer for this question, recording both.
        new_answer = ClarifyAnswer(question_key=question_key, chosen_option_id=chosen, skipped=skipped)
        rebuilt: list[ClarifyAnswer] = []
        for prior in self._answers:
            if prior.question_key == question_key and prior.superseded_by is None:
                rebuilt.append(
                    ClarifyAnswer(
                        question_key=prior.question_key,
                        chosen_option_id=prior.chosen_option_id,
                        skipped=prior.skipped,
                        superseded_by=chosen,
                    )
                )
            else:
                rebuilt.append(prior)
        rebuilt.append(new_answer)
        self._answers = rebuilt
        return new_answer

    @property
    def answers(self) -> tuple[ClarifyAnswer, ...]:
        return tuple(self._answers)

    def effective_answers(self) -> dict[str, ClarifyAnswer]:
        """The latest, non-superseded answer per question_key."""

        return {a.question_key: a for a in self._answers if a.superseded_by is None}

    # -- gating ------------------------------------------------------------

    def blocking_unanswered(self) -> list[str]:
        effective = self.effective_answers()
        return [q.question_key for q in self._questions if q.tier == "blocking" and q.question_key not in effective]

    def is_executable(self) -> bool:
        return not self.blocking_unanswered()

    def assert_executable(self) -> None:
        pending = self.blocking_unanswered()
        if pending:
            raise ClarifyBlockedError(
                f"cannot execute: {len(pending)} blocking clarify question(s) unanswered: {pending}"
            )

    # -- provenance projection --------------------------------------------

    def provenance_entries(self) -> list[ProvenanceEntry]:
        """Project every answer (superseded ones included) onto provenance
        entries. Answers are ``user_answer``; a superseded answer keeps a
        ``superseded_by`` link so the report can show the full clarify trail."""

        entries: list[ProvenanceEntry] = []
        for answer in self._answers:
            entries.append(
                ProvenanceEntry(
                    field=f"clarify.{answer.question_key}",
                    value=answer.chosen_option_id,
                    source="user_answer",
                    superseded_by=answer.superseded_by,
                )
            )
        return entries

    # -- (de)serialization -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "questions": [q.to_dict() for q in self._questions],
            "answers": [a.to_dict() for a in self._answers],
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "ClarifySession":
        session = ClarifySession(str(payload["pipeline_id"]))
        session._questions = [ClarifyQuestion.from_dict(item) for item in payload.get("questions", ())]
        session._answers = [ClarifyAnswer.from_dict(item) for item in payload.get("answers", ())]
        return session


def assert_clarify_unblocked(session: ClarifySession | None) -> None:
    """Server-side gate used at every sidecar confirm entrypoint (FE-L4).

    A ``None`` session (no sidecar interview happened -- e.g. no-LLM
    degradation, direct form operation) trivially passes: there are no blocking
    questions. A session with an open blocking question raises
    :class:`ClarifyBlockedError`.
    """

    if session is None:
        return
    session.assert_executable()


class SidecarSessionStore:
    """Durable clarify-session snapshots under ``artifact_root/sidecar/``.

    One JSON snapshot per pipeline (``<id>.session.json``), rewritten
    atomically. Sits beside the append-only tool journal
    (:class:`~quant_forge.apps.web.tools.SidecarJournal`) in the same directory.
    """

    def __init__(self, artifact_root: Path) -> None:
        self._root = Path(artifact_root).expanduser() / "sidecar"
        self._lock = threading.Lock()

    def _path(self, pipeline_id: str) -> Path:
        return self._root / f"{pipeline_id}.session.json"

    def load(self, pipeline_id: str) -> ClarifySession | None:
        path = self._path(pipeline_id)
        if not path.exists():
            return None
        return ClarifySession.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, session: ClarifySession) -> None:
        path = self._path(session.pipeline_id)
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(session.to_dict(), ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            os.replace(tmp, path)


# ---------------------------------------------------------------------------
# LLM readiness tri-state (spec §5.6/§10)
# ---------------------------------------------------------------------------


def llm_readiness(config: Any, *, redacted: bool = False) -> str:
    """``unknown | unavailable | ready`` for the LLM sidecar.

    * ``unknown``     -- a token-redacted boot must not pre-judge (spec §5.6);
      the client re-checks with a token before deciding.
    * ``unavailable`` -- the active provider is the local rule parser (no
      semantic sidecar) OR a configured provider whose key/config is missing.
      The evidence plane is untouched; the simple landing degrades to the
      seeded guided form + rule parser (spec §10).
    * ``ready``       -- the active provider validates (including a local
      ``require_api_key=false`` endpoint).
    """

    if redacted:
        return "unknown"
    try:
        selected = config.llm.select_provider()
    except Exception:
        return "unavailable"
    if str(getattr(selected, "provider", "")).lower() in {"rule", "deterministic"}:
        return "unavailable"
    try:
        from quant_forge.config import validate_llm_runtime

        validate_llm_runtime(config.llm)
    except Exception:
        return "unavailable"
    return "ready"


def replay_rendered_cards(rows: list[dict[str, Any]]) -> list[NarrationNode]:
    """Reconstruct the exact rendered narration cards from journal rows.

    Spec §11 ship gate #1: "a replay reproduces the same rendered cards." Each
    row's ``narration`` list is rebuilt through :meth:`NarrationNode.from_dict`
    (which re-validates numeric-arg rejection and node shape), flattened in
    journal order. Equality against the originally-emitted nodes proves the
    journal is a faithful, replayable record.
    """

    cards: list[NarrationNode] = []
    for row in rows:
        for node_dict in row.get("narration", ()):
            cards.append(NarrationNode.from_dict(node_dict))
    return cards

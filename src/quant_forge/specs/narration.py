"""Typed narration AST + clarify interview types (agent_sidecar_frontend.md §5.5/§5.2).

Narration is STRUCTURED DATA, not free prose (spec §5.5). The LLM sidecar
emits ``NarrationNode``s; it never emits a metric, a formula, or any number.
This module is the pure type + validation layer -- like :mod:`quant_forge.specs.pipeline`
it is dataclasses + validation + (de)serialization only, with no filesystem,
clock, or ``apps/web`` dependency (``apps.web`` depends on ``specs``, never the
other way). The server-side ref-resolution, no-numeric-leaf assertion, clarify
gating, and journal/replay live in :mod:`quant_forge.apps.web.narration`.

Two iron laws are enforced HERE, at construction time, so a malformed node can
never be persisted or rendered:

* **FE-X2 / FE-L2 -- chat is never the sole carrier of a number.** ``args`` are
  non-numeric tokens only (statuses, labels, ids). A numeric literal -- an
  ``int``/``float``/``bool``, or a string that parses as a number/percentage --
  raises :class:`NumericNarrationArgError`. To "say" a result the sidecar emits
  a ``ref`` node and the canonical renderer shows the value; the number itself
  never travels inside narration text.
* **Stable message keys, not translated prose.** ``message_key`` is an
  enum-like i18n code (lowercase dotted snake); the Chinese label is resolved
  client-side (``static/views/narration.js``). LLM narration is never the
  translation catalog (spec §9), so an arbitrary free-text ``message_key`` is
  rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "NARRATION_KINDS",
    "CLARIFY_TIERS",
    "MAX_CLARIFY_QUESTIONS",
    "NarrationKind",
    "ClarifyTier",
    "NarrationError",
    "NumericNarrationArgError",
    "NarrationRefError",
    "ClarifyError",
    "NarrationRef",
    "NarrationOption",
    "NarrationNode",
    "ClarifyQuestion",
    "ClarifyAnswer",
    "is_numeric_token",
    "assert_non_numeric_args",
    "validate_clarify_questions",
]

NarrationKind = Literal["status", "question", "ref", "action_suggestion"]
NARRATION_KINDS: tuple[str, ...] = ("status", "question", "ref", "action_suggestion")

ClarifyTier = Literal["blocking", "semantic"]
# Blocking (execution-critical; unanswered ⇒ do not run) ranks above semantic
# (has a safe default; skippable). Spec §5.2.
CLARIFY_TIERS: tuple[str, ...] = ("blocking", "semantic")

# Cap ≤3 questions total (spec §5.2).
MAX_CLARIFY_QUESTIONS = 3

# Stable message-key grammar: lowercase, digits, and underscores in dotted
# segments (e.g. ``narration.parse.completed``, ``clarify.market_cap.basis``).
# Deliberately excludes spaces, uppercase, and CJK so an LLM cannot smuggle a
# rendered Chinese sentence in as the "key" (spec §9: narration is never the
# translation catalog).
_MESSAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")

# A token is "numeric" if it IS a number or reads as one: an optional sign, an
# integer/decimal/scientific magnitude, and an optional trailing percent. Ids
# that merely CONTAIN digits (``FTR_ABCD1234``, ``2025Q1_seed``) are not
# numbers and pass; a bare ``0.05`` / ``-1.2e3`` / ``30%`` does not.
_NUMERIC_TOKEN_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?%?$")


class NarrationError(ValueError):
    """A NarrationNode / clarify structure violates its schema."""


class NumericNarrationArgError(NarrationError):
    """A narration arg carries a number (FE-X2 / FE-L2 -- pin: schema rejects numeric args).

    Numbers become pixels ONLY through the canonical renderers via a ``ref``
    node; chat text is never the sole carrier of a numeric claim. Raised at
    construction so a numeric arg can never be persisted or rendered.
    """


class NarrationRefError(NarrationError):
    """A node's ``ref``/``options``/``action`` shape is illegal for its ``kind``."""


class ClarifyError(NarrationError):
    """A clarify question set violates the tiering/cap/default contract (spec §5.2)."""


def is_numeric_token(value: Any) -> bool:
    """Whether ``value`` is (or reads as) a number/percentage.

    ``bool`` is a numeric type in Python (``True == 1``); it is treated as
    numeric here so ``args=[True]`` cannot smuggle a 1/0 into chat text.
    """

    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(_NUMERIC_TOKEN_RE.match(value.strip()))
    return False


def assert_non_numeric_args(args: tuple[Any, ...]) -> tuple[str, ...]:
    """Validate + normalize narration args to a tuple of non-numeric string tokens.

    Every arg must be a string token that is not itself a number (FE-X2). A
    non-string, or a numeric-looking string, raises
    :class:`NumericNarrationArgError` -- the single enforcement point the
    ``NarrationNode`` constructor and any builder both go through.
    """

    normalized: list[str] = []
    for arg in args:
        if not isinstance(arg, str):
            raise NumericNarrationArgError(
                f"narration arg must be a non-numeric string token, got {type(arg).__name__}: {arg!r}"
            )
        if is_numeric_token(arg):
            raise NumericNarrationArgError(
                f"narration arg {arg!r} is numeric; a number must be carried by a ref node "
                "and rendered by a canonical renderer, never inline in chat (FE-L2)"
            )
        normalized.append(arg)
    return tuple(normalized)


@dataclass(frozen=True)
class NarrationRef:
    """A pointer to a currently-rendered canonical component + its artifact.

    ``component_id`` names a status-aware component the frontend actually
    renders (validated against the live component set in
    ``apps/web/narration.py``); ``artifact_ref`` is the canonical artifact the
    component shows. The narration never carries the artifact's CONTENTS -- the
    renderer re-fetches and displays it (FE-L2).
    """

    component_id: str
    artifact_ref: str

    def __post_init__(self) -> None:
        if not self.component_id.strip():
            raise NarrationRefError("ref.component_id is required")
        if not self.artifact_ref.strip():
            raise NarrationRefError("ref.artifact_ref is required")

    def to_dict(self) -> dict[str, Any]:
        return {"component_id": self.component_id, "artifact_ref": self.artifact_ref}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "NarrationRef":
        return NarrationRef(
            component_id=str(payload["component_id"]),
            artifact_ref=str(payload["artifact_ref"]),
        )


@dataclass(frozen=True)
class NarrationOption:
    """One choice on a ``question`` node (spec §5.5 ``options``)."""

    id: str
    label: str
    is_default: bool = False

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise NarrationError("option.id is required")
        if not self.label.strip():
            raise NarrationError("option.label is required")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "is_default": self.is_default}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "NarrationOption":
        return NarrationOption(
            id=str(payload["id"]),
            label=str(payload["label"]),
            is_default=bool(payload.get("is_default", False)),
        )


def _validate_options(options: tuple[NarrationOption, ...]) -> None:
    if len(options) < 2:
        raise NarrationRefError("a question node needs at least two options")
    ids = [option.id for option in options]
    if len(ids) != len(set(ids)):
        raise NarrationRefError("question options must have unique ids")
    defaults = [option for option in options if option.is_default]
    if len(defaults) != 1:
        raise NarrationRefError(
            f"a question node needs exactly one default option, got {len(defaults)} (spec §5.2)"
        )


@dataclass(frozen=True)
class NarrationNode:
    """One structured narration event (spec §5.5).

    ``kind`` fixes which optional payloads are legal:

    * ``status``          -- args only (a labelled status); no ref/options/action.
    * ``ref``             -- a ref REQUIRED (to "say" a rendered result); no options/action.
    * ``question``        -- options REQUIRED (≥2, exactly one default); no ref/action.
    * ``action_suggestion`` -- an ``action`` tool name REQUIRED; ref optional; no options.

    ``args`` are always non-numeric string tokens (:func:`assert_non_numeric_args`).
    """

    kind: str
    message_key: str
    args: tuple[str, ...] = field(default_factory=tuple)
    ref: NarrationRef | None = None
    options: tuple[NarrationOption, ...] = field(default_factory=tuple)
    # For ``action_suggestion`` nodes: the allowlisted action-tool name the
    # sidecar proposes. Membership in the closed tool catalog is checked in
    # ``apps/web/narration.py`` (this pure layer stays free of a tools import);
    # here it is validated as a non-empty token only.
    action: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in NARRATION_KINDS:
            raise NarrationError(f"invalid narration kind: {self.kind!r} (expected one of {NARRATION_KINDS})")
        if not _MESSAGE_KEY_RE.match(self.message_key or ""):
            raise NarrationError(
                f"invalid message_key {self.message_key!r}: must be a stable dotted-snake i18n code "
                "(spec §9 -- narration is never the translation catalog)"
            )
        # Normalize + enforce non-numeric args through the single choke point.
        object.__setattr__(self, "args", assert_non_numeric_args(tuple(self.args)))
        object.__setattr__(self, "options", tuple(self.options))

        if self.kind == "ref":
            if self.ref is None:
                raise NarrationRefError("a ref node requires a ref")
            if self.options:
                raise NarrationRefError("a ref node must not carry options")
            if self.action is not None:
                raise NarrationRefError("a ref node must not carry an action")
        elif self.kind == "question":
            if self.ref is not None:
                raise NarrationRefError("a question node must not carry a ref")
            if self.action is not None:
                raise NarrationRefError("a question node must not carry an action")
            _validate_options(self.options)
        elif self.kind == "status":
            if self.ref is not None or self.options or self.action is not None:
                raise NarrationRefError("a status node carries args only (no ref/options/action)")
        elif self.kind == "action_suggestion":
            if self.options:
                raise NarrationRefError("an action_suggestion node must not carry options")
            if not (self.action or "").strip():
                raise NarrationRefError("an action_suggestion node requires an action tool name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message_key": self.message_key,
            "args": list(self.args),
            "ref": self.ref.to_dict() if self.ref is not None else None,
            "options": [option.to_dict() for option in self.options],
            "action": self.action,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "NarrationNode":
        ref_payload = payload.get("ref")
        return NarrationNode(
            kind=str(payload["kind"]),
            message_key=str(payload["message_key"]),
            args=tuple(payload.get("args", ())),
            ref=NarrationRef.from_dict(ref_payload) if ref_payload else None,
            options=tuple(NarrationOption.from_dict(item) for item in payload.get("options", ())),
            action=payload.get("action"),
        )


@dataclass(frozen=True)
class ClarifyQuestion:
    """One tiered hypothesis-level clarify question (spec §5.2).

    Only hypothesis-level ambiguity is ever asked (market-cap basis, holding-
    horizon semantics, hard vs soft exclusions); parameter-level ambiguity is
    never a question (profile default + confirm-card disclosure). Every
    question carries a default option, so ``skip`` is always well-defined.
    """

    question_key: str
    tier: str
    options: tuple[NarrationOption, ...]
    prompt_args: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _MESSAGE_KEY_RE.match(self.question_key or ""):
            raise ClarifyError(f"invalid question_key {self.question_key!r}: must be a stable dotted-snake code")
        if self.tier not in CLARIFY_TIERS:
            raise ClarifyError(f"invalid clarify tier: {self.tier!r} (expected one of {CLARIFY_TIERS})")
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "prompt_args", assert_non_numeric_args(tuple(self.prompt_args)))
        _validate_options(self.options)

    @property
    def default_option_id(self) -> str:
        for option in self.options:
            if option.is_default:
                return option.id
        raise ClarifyError("clarify question has no default option")  # pragma: no cover - guarded by _validate_options

    def option_ids(self) -> frozenset[str]:
        return frozenset(option.id for option in self.options)

    def to_narration_node(self) -> NarrationNode:
        """Project onto a ``question`` NarrationNode for rendering."""

        return NarrationNode(
            kind="question",
            message_key=self.question_key,
            args=self.prompt_args,
            options=self.options,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_key": self.question_key,
            "tier": self.tier,
            "options": [option.to_dict() for option in self.options],
            "prompt_args": list(self.prompt_args),
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "ClarifyQuestion":
        return ClarifyQuestion(
            question_key=str(payload["question_key"]),
            tier=str(payload["tier"]),
            options=tuple(NarrationOption.from_dict(item) for item in payload["options"]),
            prompt_args=tuple(payload.get("prompt_args", ())),
        )


@dataclass(frozen=True)
class ClarifyAnswer:
    """A recorded answer to a clarify question (spec §5.2).

    ``skipped`` means the user accepted the default without choosing -- still a
    recorded decision, never a silent drop. ``superseded_by`` links an earlier
    answer to the later one that invalidated it; BOTH stay in provenance so the
    report can show "what you clarified".
    """

    question_key: str
    chosen_option_id: str
    skipped: bool = False
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not self.question_key.strip():
            raise ClarifyError("answer.question_key is required")
        if not self.chosen_option_id.strip():
            raise ClarifyError("answer.chosen_option_id is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_key": self.question_key,
            "chosen_option_id": self.chosen_option_id,
            "skipped": self.skipped,
            "superseded_by": self.superseded_by,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "ClarifyAnswer":
        return ClarifyAnswer(
            question_key=str(payload["question_key"]),
            chosen_option_id=str(payload["chosen_option_id"]),
            skipped=bool(payload.get("skipped", False)),
            superseded_by=payload.get("superseded_by"),
        )


def validate_clarify_questions(questions: tuple[ClarifyQuestion, ...]) -> None:
    """Enforce the clarify-set contract: ≤3 questions, unique keys (spec §5.2)."""

    if len(questions) > MAX_CLARIFY_QUESTIONS:
        raise ClarifyError(
            f"clarify asks at most {MAX_CLARIFY_QUESTIONS} questions, got {len(questions)} (spec §5.2)"
        )
    keys = [question.question_key for question in questions]
    if len(keys) != len(set(keys)):
        raise ClarifyError("clarify question_keys must be unique")

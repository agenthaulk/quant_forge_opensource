"""Static reviewed backend registry: the availability authority (D-iv/CP0).

Executable binding of an external factor backend is a reviewed constant plus
an explicit user opt-in — never discovery. Entry-point scanning was
considered and rejected because it would import whatever installed
distribution claims the group name; path-based loading and runtime exec stay
out per the unchanged D7 discipline. The import table below — not any
extension manifest — is the availability authority: manifests are metadata
only, and a manifest-declared backend id absent from this table stays
declared-but-unbound (see :func:`is_known_backend`).

Resolution order is load-bearing (CP0 amendment 4, anti-squatting): the
``QF_ENABLE_BACKEND_<ID>`` opt-in gate is checked before any import, so a
disabled backend is never imported — resolving a disabled id has zero import
side effects, and an unpublished module name can never be activated by an
unrelated installed package without the user's explicit opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
import re
from typing import Any

from quant_forge.integrations.contracts import (
    BACKEND_NOT_ENABLED,
    BACKEND_NOT_INSTALLED,
    UNKNOWN_BACKEND,
    BackendContractViolation,
    BackendDescriptor,
    FactorBackendPort,
)


# WHY this table is static (D-iv): each row is a code-reviewed public PR line
# mapping a backend id to one fixed module name, resolved only by a literal
# ``importlib.import_module`` on the constant below. Nothing else — no
# manifest, no entry point, no path — can bind executable code to an id.
# Adding a backend = adding one reviewed line here plus an installable
# package that exposes ``create_backend() -> FactorBackendPort``.
KNOWN_FACTOR_BACKENDS: dict[str, str] = {
    "worldquant": "quant_forge_worldquant",
}

BACKEND_STATUSES: tuple[str, ...] = ("available", "not_enabled", "not_installed", "unknown")

_ENV_COMPONENT_RE = re.compile(r"[^A-Z0-9]+")


def enable_env_var(backend_id: str) -> str:
    """The opt-in gate variable name for one backend id (CP0 amendment 4)."""

    return "QF_ENABLE_BACKEND_" + _ENV_COMPONENT_RE.sub("_", backend_id.upper())


def is_known_backend(backend_id: str) -> bool:
    """Whether an id is bound by the reviewed table.

    Extension manifests may declare ``integration.factor_backend``
    contributions for ids that are not (or not yet) in the table; those stay
    valid declarative metadata, surfaced as declared-but-unbound. This
    predicate is the one-line seam surfacing layers use for that distinction.
    """

    return isinstance(backend_id, str) and backend_id in KNOWN_FACTOR_BACKENDS


@dataclass(frozen=True)
class BackendResolution:
    """Outcome of resolving one backend id through the D-iv state machine.

    ``status`` is one of :data:`BACKEND_STATUSES`; ``warning_code`` is the
    matching closed-set code (``None`` only when available); ``port`` is set
    only when available. ``module`` and ``enable_env_var`` are populated for
    table-known ids so degradation messages can state the exact remediation.
    """

    backend_id: str
    status: str
    warning_code: str | None
    port: FactorBackendPort | None
    module: str | None
    enable_env_var: str | None


def resolve_backend(backend_id: str) -> BackendResolution:
    """Resolve one backend id: table membership, opt-in gate, then import.

    State machine (order is the contract):

    1. Id absent from the reviewed table -> ``unknown`` / ``UNKNOWN_BACKEND``.
    2. ``QF_ENABLE_BACKEND_<ID>`` not set to ``"1"`` -> ``not_enabled`` /
       ``BACKEND_NOT_ENABLED``, with **no import attempted** — the not-enabled
       answer deliberately beats not-installed so disabled backends have no
       import side effects.
    3. The fixed table module fails to import -> ``not_installed`` /
       ``BACKEND_NOT_INSTALLED`` (absence is a normal, honest state).
    4. The module's ``create_backend()`` must return a
       :class:`FactorBackendPort` whose descriptor ``backend_id`` matches the
       table key; any mismatch raises :class:`BackendContractViolation` —
       a misdeclared adapter is a loud defect, never mere unavailability.
    """

    if not isinstance(backend_id, str) or backend_id not in KNOWN_FACTOR_BACKENDS:
        return BackendResolution(
            backend_id=str(backend_id),
            status="unknown",
            warning_code=UNKNOWN_BACKEND,
            port=None,
            module=None,
            enable_env_var=None,
        )
    module_name = KNOWN_FACTOR_BACKENDS[backend_id]
    gate = enable_env_var(backend_id)
    if os.environ.get(gate, "") != "1":
        return BackendResolution(
            backend_id=backend_id,
            status="not_enabled",
            warning_code=BACKEND_NOT_ENABLED,
            port=None,
            module=module_name,
            enable_env_var=gate,
        )
    try:
        # The argument is always the fixed reviewed constant above, never
        # caller input. Codex B-5: only the TOP-LEVEL module being absent
        # means "not installed" — a nested import failure inside an installed
        # adapter is a real defect and must not masquerade as absence (the
        # user would be told to install something that is already installed).
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == module_name or module_name.startswith(missing + "."):
            return BackendResolution(
                backend_id=backend_id,
                status="not_installed",
                warning_code=BACKEND_NOT_INSTALLED,
                port=None,
                module=module_name,
                enable_env_var=gate,
            )
        raise BackendContractViolation(
            f"adapter '{module_name}' is installed but failed to import a "
            f"dependency: {exc}"
        ) from exc
    except ImportError as exc:
        raise BackendContractViolation(
            f"adapter '{module_name}' import failed: {exc}"
        ) from exc
    port = _create_port(backend_id, module_name, module)
    return BackendResolution(
        backend_id=backend_id,
        status="available",
        warning_code=None,
        port=port,
        module=module_name,
        enable_env_var=gate,
    )


def list_backends() -> list[dict[str, Any]]:
    """Per-id status rows for every table entry, in sorted id order.

    Reuses :func:`resolve_backend`, whose state-machine order guarantees
    disabled backends are never imported while listing. Rows are
    payload-ready (JSON-safe values only); descriptor fields appear only for
    available backends — never guessed for absent ones (FP-4).
    """

    rows: list[dict[str, Any]] = []
    for backend_id in sorted(KNOWN_FACTOR_BACKENDS):
        try:
            resolution = resolve_backend(backend_id)
        except BackendContractViolation as violation:
            # One misdeclared adapter must not hide every other backend's
            # status row. The violation stays loud — in its own row — while
            # direct resolve_backend() callers still get the raise.
            rows.append(
                {
                    "backend_id": backend_id,
                    "module": KNOWN_FACTOR_BACKENDS[backend_id],
                    "status": "contract_violation",
                    "warning_code": None,
                    "enable_env_var": enable_env_var(backend_id),
                    "violation": str(violation),
                }
            )
            continue
        row: dict[str, Any] = {
            "backend_id": backend_id,
            "module": resolution.module,
            "status": resolution.status,
            "warning_code": resolution.warning_code,
            "enable_env_var": resolution.enable_env_var,
        }
        if resolution.port is not None:
            descriptor = resolution.port.describe()
            row["label"] = descriptor.label
            row["regions"] = list(descriptor.regions)
            row["capabilities"] = sorted(descriptor.capabilities)
        rows.append(row)
    return rows


def _create_port(backend_id: str, module_name: str, module: object) -> FactorBackendPort:
    factory = getattr(module, "create_backend", None)
    if not callable(factory):
        raise BackendContractViolation(
            f"module '{module_name}' does not expose a callable create_backend()"
        )
    port = factory()
    if not isinstance(port, FactorBackendPort):
        raise BackendContractViolation(
            f"create_backend() in '{module_name}' must return a FactorBackendPort"
        )
    descriptor = port.describe()
    if not isinstance(descriptor, BackendDescriptor):
        raise BackendContractViolation(
            f"describe() in '{module_name}' must return a BackendDescriptor"
        )
    if descriptor.backend_id != backend_id:
        raise BackendContractViolation(
            "backend identity mismatch: table id "
            f"'{backend_id}' but the descriptor reports '{descriptor.backend_id}'"
        )
    return port

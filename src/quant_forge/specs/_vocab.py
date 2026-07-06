"""Closed vocabularies shared across the spec layer.

Each vocabulary mirrors kernel literals so that axiom-violating states are
unrepresentable at spec-construction time. The kernel remains the owner of
every literal; when the kernel adds one, extend the mirror here in the same
change.
"""

from __future__ import annotations

# Mirrors the kernel sample-role literals owned by `core.contracts`
# (sample_role defaults on evaluation/backtest contracts) and
# `backtesting.service` (EXTERNAL_OOS_ROLE, IN_SAMPLE_ROLE,
# STAGGERED_COHORT_ROLE, STAGGERED_AGGREGATE_ROLE).
SAMPLE_ROLES: frozenset[str] = frozenset(
    {
        "research_evaluation",
        "in_sample_backtest",
        "external_oos_backtest",
        "staggered_entry_cohort",
        "staggered_entry_backtest",
    }
)

# The spec families a run manifest can bind to. Closed on purpose: a
# manifest for an unknown kind of spec is unrepresentable.
SPEC_KINDS: frozenset[str] = frozenset({"factor", "strategy"})

# Typed, greppable sentinel for callers that genuinely lack provenance.
# Empty-string provenance is rejected at construction; a caller without a
# real fingerprint must say so explicitly, and promotion gates can (and
# must) treat this value as unverified.
UNVERIFIED_PROVENANCE = "unverified"

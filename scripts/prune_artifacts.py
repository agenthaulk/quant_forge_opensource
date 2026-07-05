#!/usr/bin/env python3
"""Retention / cleanup for locally-generated artifacts and outputs.

Context (AGENT-1, Choice B): the agent/CLI/Web `evaluate_factor` path legitimately
writes evaluation JSON under `artifact_root`, and backtests/RD runs write under
`artifact_root` / `output_root`. These accumulate over time. This tool bounds that
growth with a simple age-based retention policy.

These directories are local and git-ignored (`/artifacts/`, `/outputs/`), so
cleanup is a LOCAL maintenance task, not a CI job — CI never sees them.

Usage:
    python scripts/prune_artifacts.py                       # dry-run, 30-day default
    python scripts/prune_artifacts.py --apply               # actually delete
    python scripts/prune_artifacts.py --older-than-days 7 --apply
    python scripts/prune_artifacts.py --root artifacts --root outputs --apply

Retention triggers (pick what fits — this script does not install any of them):
  - Manual: run it when convenient.
  - Scheduled (macOS launchd / cron), e.g. daily at 03:00:
        0 3 * * *  cd /path/to/quant_forge_opensource && \
                   python scripts/prune_artifacts.py --older-than-days 30 --apply
  - Post-merge git hook (.git/hooks/post-merge):
        #!/bin/sh
        python scripts/prune_artifacts.py --older-than-days 30 --apply

By default nothing is deleted (dry-run). Pass --apply to remove files. Empty
directories left behind by pruning are removed too (except the given roots).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


DEFAULT_ROOTS = ("artifacts", "outputs")
DEFAULT_RETENTION_DAYS = 30


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def prune_root(root: Path, cutoff_epoch: float, *, apply: bool) -> tuple[int, int]:
    """Return (removed_count, removed_bytes) for files older than the cutoff."""
    removed_count = 0
    removed_bytes = 0
    if not root.exists():
        return (0, 0)
    for path in _iter_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime >= cutoff_epoch:
            continue
        removed_count += 1
        removed_bytes += stat.st_size
        action = "delete" if apply else "would delete"
        print(f"  {action}: {path}  ({stat.st_size} bytes)")
        if apply:
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - filesystem edge
                print(f"  ! failed to delete {path}: {exc}", file=sys.stderr)
    if apply:
        _remove_empty_dirs(root)
    return (removed_count, removed_bytes)


def _remove_empty_dirs(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            try:
                path.rmdir()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune old local artifacts/outputs by age.")
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help=f"Directory to prune (repeatable). Default: {', '.join(DEFAULT_ROOTS)}.",
    )
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Delete files whose mtime is older than this many days (default {DEFAULT_RETENTION_DAYS}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this flag the run is a dry-run.",
    )
    args = parser.parse_args(argv)

    if args.older_than_days < 0:
        parser.error("--older-than-days must be non-negative")

    roots = [Path(r) for r in (args.roots or DEFAULT_ROOTS)]
    cutoff_epoch = time.time() - args.older_than_days * 86400.0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] pruning files older than {args.older_than_days} days from: {', '.join(str(r) for r in roots)}")

    total_count = 0
    total_bytes = 0
    for root in roots:
        print(f"- {root}:")
        count, size = prune_root(root, cutoff_epoch, apply=args.apply)
        total_count += count
        total_bytes += size

    verb = "removed" if args.apply else "would remove"
    print(f"[{mode}] {verb} {total_count} file(s), {total_bytes / 1_048_576:.2f} MiB total.")
    if not args.apply and total_count:
        print("Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

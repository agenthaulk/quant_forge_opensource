"""Concurrent-writer safety for the shared file-IO helpers (CP7 residual AW).

Pre-CP7, ``quant_forge.utils._atomic_write`` staged EVERY writer of a given
path into the same temp file (``.{name}.tmp``). Two concurrent writers then
raced in two ways:

- writer B re-creates the shared temp file while writer A is between its
  staging write and its ``os.replace``, so A can publish bytes B is still
  writing (torn content visible at the final path);
- whichever writer renames second hits ``FileNotFoundError`` because the
  shared temp name was already renamed away.

The fixed contract (documented on ``_atomic_write``): each writer stages a
private, uniquely named temp file and atomically renames it over the target.
Concurrent writers are safe; the surviving content is that of the writer
whose rename lands last (last-writer-wins); readers only ever observe one
writer's complete payload.
"""

from __future__ import annotations

import threading
from pathlib import Path

from quant_forge.utils import write_text

WRITER_COUNT = 8
ROUNDS = 40


def _payload(index: int) -> str:
    # Distinct lengths per writer so torn or interleaved writes cannot
    # masquerade as a valid payload.
    return f"writer-{index}\n" + (f"line-{index}-" + "x" * (50 + 13 * index) + "\n") * (3 + index)


def test_concurrent_writers_do_not_raise_and_publish_only_whole_payloads(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    payloads = {index: _payload(index) for index in range(WRITER_COUNT)}
    valid = set(payloads.values())
    barrier = threading.Barrier(WRITER_COUNT)
    errors: list[str] = []
    torn_reads: list[str] = []
    stop_reading = threading.Event()

    def writer(index: int) -> None:
        for _ in range(ROUNDS):
            try:
                barrier.wait(timeout=60)
            except threading.BrokenBarrierError:
                return
            try:
                write_text(target, payloads[index])
            except Exception as exc:  # noqa: BLE001 - the raised race IS the finding
                errors.append(f"writer {index}: {exc!r}")

    def reader() -> None:
        while not stop_reading.is_set():
            try:
                content = target.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                continue
            if content and content not in valid:
                torn_reads.append(content[:120])

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(WRITER_COUNT)]
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    stop_reading.set()
    reader_thread.join(timeout=30)

    # A timed-out join returns silently; prove every worker actually
    # terminated before asserting against the shared state it mutates.
    for thread in threads:
        assert not thread.is_alive(), "writer thread did not terminate"
    assert not reader_thread.is_alive(), "reader thread did not terminate"

    assert errors == []
    assert torn_reads == []
    # Last-writer-wins: exactly one writer's complete payload survives.
    assert target.read_text(encoding="utf-8") in valid
    # Every successful writer renamed its private staging file away.
    assert [item.name for item in tmp_path.iterdir()] == ["artifact.txt"]


def test_single_writer_roundtrip_leaves_no_staging_files(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "artifact.txt"
    write_text(target, "hello\n")
    write_text(target, "world\n")
    assert target.read_text(encoding="utf-8") == "world\n"
    assert [item.name for item in (tmp_path / "nested").iterdir()] == ["artifact.txt"]

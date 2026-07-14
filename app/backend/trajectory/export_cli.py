"""``make export-trajectories`` CLI: run the PII-safe trajectory export.

The operator entry point for the in-VPC export boundary shipped in
:mod:`app.backend.trajectory.export`. ``run_export`` is the governance-gated
function that turns captured ``(chosen, rejected, critique)`` records into a
local JSONL training file; this module is the one-command wrapper that an
operator runs once data-governance / PII review has approved an export, mirroring
the ``make calibrate`` ergonomics.

Design
------
* **Pure core, thin I/O.** Thread-id assembly (:func:`collect_thread_ids` /
  :func:`load_thread_ids_from_file`) is pure over plain strings. The store read
  and the JSONL write happen only inside
  :func:`~app.backend.trajectory.export.run_export` (or, for ``--dry-run``, the
  pure ``export_training_examples`` path), so this module imports with no DB
  driver present and its tests run fully offline.
* **Operator-supplied threads only.** The export seam is append + read per
  thread, never an unbounded ``read all``; the CLI enforces that at least one
  thread id is supplied (positional and/or ``--threads-file``).
* **Honest about the gate.** ``run_export`` is a hard no-op unless
  ``SAISEI_TRAJECTORY_EXPORT_ENABLED`` and ``SAISEI_TRAJECTORY_EXPORT_DIR`` are
  set. The CLI reports WHY nothing was written and which env knobs to set,
  rather than silently producing an empty result.
* **Offline preview.** ``--dry-run`` counts what WOULD be exported (via the pure
  redaction path) without touching the filesystem, so an operator can preview
  against an in-memory / configured store with no side effects.

No ``print()`` is used: the project's ruff config bans T20 in app code, so all
output goes through ``sys.stdout`` / ``sys.stderr`` explicitly.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from app.backend.trajectory.export import (
    export_training_examples,
    iter_records,
    run_export,
)
from app.backend.trajectory.store import TrajectoryStore, get_trajectory_store
from app.shared.settings import Settings, get_settings

__all__ = [
    "collect_thread_ids",
    "load_thread_ids_from_file",
    "main",
]


def collect_thread_ids(*sources: Iterable[str]) -> list[str]:
    """Merge thread ids from several sources, de-duplicated, order-preserving.

    Each source is an iterable of raw strings (CLI positionals, file lines).
    Whitespace is stripped; blank lines and ``#`` comments are skipped; the
    first occurrence of each id wins so the output order is stable.

    Args:
        *sources: One or more iterables of candidate thread-id strings.

    Returns:
        The de-duplicated list of thread ids in first-seen order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for source in sources:
        for raw in source:
            tid = raw.strip()
            if not tid or tid.startswith("#"):
                continue
            if tid not in seen:
                seen.add(tid)
                result.append(tid)
    return result


def load_thread_ids_from_file(path: Path) -> list[str]:
    """Load candidate thread ids from a text file (one id per line).

    Blank lines and ``#`` comment lines are ignored by
    :func:`collect_thread_ids`; this function only reads and splits.

    Args:
        path: Path to the thread-id list file.

    Returns:
        The raw, un-deduplicated lines (stripping is left to the collector).

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    return path.read_text(encoding="utf-8").splitlines()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export-trajectories",
        description=(
            "Run the PII-safe, in-VPC trajectory training-data export for the "
            "given thread ids. Gated by SAISEI_TRAJECTORY_EXPORT_ENABLED + "
            "SAISEI_TRAJECTORY_EXPORT_DIR; writes local JSONL only, no network."
        ),
    )
    parser.add_argument(
        "thread_ids",
        nargs="*",
        help="Thread ids to export (space-separated).",
    )
    parser.add_argument(
        "--threads-file",
        type=Path,
        default=None,
        help=(
            "Read additional thread ids from a file (one per line; blank lines "
            "and '#' comments ignored)."
        ),
    )
    parser.add_argument(
        "--filename",
        default="trajectory_export.jsonl",
        help=(
            "Output file name within SAISEI_TRAJECTORY_EXPORT_DIR "
            "(default: trajectory_export.jsonl)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Count what WOULD be exported via the pure redaction path without "
            "writing any file (offline preview)."
        ),
    )
    return parser


def _dry_run(thread_ids: list[str], cfg: Settings, store: TrajectoryStore) -> int:
    """Count exportable examples without touching the filesystem."""
    records = list(iter_records(store, thread_ids))
    examples = export_training_examples(records, cfg)
    sys.stdout.write(
        f"dry-run: {len(examples)} example(s) would be exported from "
        f"{len(thread_ids)} thread(s); no file written.\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code.

    Exit codes:
        0 - export written, dry-run completed, or export cleanly disabled.
        2 - usage error (no thread ids, or unreadable threads file).
        3 - export is enabled but misconfigured (no export dir), so nothing
            was written.
    """
    args = _build_parser().parse_args(argv)

    file_lines: list[str] = []
    if args.threads_file is not None:
        try:
            file_lines = load_thread_ids_from_file(args.threads_file)
        except FileNotFoundError:
            sys.stderr.write(f"error: file not found: {args.threads_file}\n")
            return 2

    thread_ids = collect_thread_ids(args.thread_ids, file_lines)
    if not thread_ids:
        sys.stderr.write(
            "error: no thread ids supplied. Pass them as arguments and/or via "
            "--threads-file (one per line).\n"
        )
        return 2

    cfg = get_settings()

    if args.dry_run:
        return _dry_run(thread_ids, cfg, get_trajectory_store(cfg))

    enabled = bool(getattr(cfg, "trajectory_export_enabled", False))
    export_dir = (getattr(cfg, "trajectory_export_dir", "") or "").strip()
    if not enabled:
        sys.stderr.write(
            "export disabled: set SAISEI_TRAJECTORY_EXPORT_ENABLED=true (and "
            "SAISEI_TRAJECTORY_EXPORT_DIR) after PII review. Nothing written.\n"
            "hint: use --dry-run to preview the export offline.\n"
        )
        return 0
    if not export_dir:
        sys.stderr.write(
            "error: export enabled but SAISEI_TRAJECTORY_EXPORT_DIR is unset; nothing written.\n"
        )
        return 3

    written = run_export(thread_ids, filename=args.filename, settings=cfg)
    destination = Path(export_dir) / args.filename
    sys.stdout.write(
        f"exported {written} example(s) from {len(thread_ids)} thread(s) to {destination}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin runtime shim
    raise SystemExit(main())

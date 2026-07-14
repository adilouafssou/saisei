"""``make calibrate`` CLI: print the advisory threshold-calibration report.

Loads the captured ``reconciliation_outcomes`` corpus and prints the advisory
:class:`~app.backend.analysis.threshold_calibration.CalibrationReport` produced
by !3's pure analysis. This is the one-command operational check that lets an
operator run the calibration against real persisted data.

Design
------
* **Pure core, thin I/O.** All corpus-assembly logic
  (``collect_outcomes`` / ``load_outcomes_from_json`` / ``format_report``) is a
  pure function over plain dicts. The Postgres read is a lazily-imported wrapper
  invoked only at runtime, so this module imports (and its tests run) with no DB
  driver present.
* **Advisory only.** Prints a report; it never edits the constant, gates,
  routes, or calls an LLM — consistent with the rest of the stack.
* **Offline-safe for CI.** ``--json-file`` reads outcomes from a JSON array or a
  dumped state object, so the tool runs with no database.

No ``print()`` is used: the project's ruff config bans T20 in app code, so all
output goes through ``sys.stdout`` / ``sys.stderr`` explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from app.backend.analysis.threshold_calibration import (
    CalibrationReport,
    calibrate_reconciliation_threshold,
)

__all__ = [
    "collect_outcomes",
    "load_outcomes_from_json",
    "format_report",
    "main",
]

#: The state channel that holds the append-only who-was-right corpus.
_OUTCOMES_KEY = "reconciliation_outcomes"


def _run_key(outcomes: list[dict[str, Any]]) -> str:
    """Return a stable identity key for one run's outcome list.

    Used only to deduplicate growing snapshots of the SAME run: an append-only
    corpus means a later snapshot is a superset of an earlier one, so keeping
    the longest list per key avoids double-counting.

    The key is the JSON of the FIRST outcome (the run's oldest, stable entry).
    LangGraph checkpoint values can contain non-JSON-serialisable objects (e.g.
    ``datetime``), so serialisation falls back to ``default=str`` and then to
    ``repr`` — a key is only ever compared for equality, never parsed back, so a
    lossy-but-stable string is sufficient and must never raise.
    """
    if not outcomes:
        return ""
    first = outcomes[0]
    try:
        return json.dumps(first, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(sorted(first.items())) if isinstance(first, dict) else repr(first)


def collect_outcomes(
    channel_values: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble the deduplicated outcome corpus from many state snapshots.

    Pure function over a stream of ``channel_values`` dicts (each the persisted
    state of one checkpoint). For every distinct run it keeps the LONGEST
    outcome list seen (append-only => longest is most complete), so growing
    snapshots of the same thread do not double-count.

    Args:
        channel_values: An iterable of state dicts; each may or may not contain
            the ``reconciliation_outcomes`` key.

    Returns:
        The concatenated, deduplicated list of outcome dicts across all runs.
    """
    longest_by_run: dict[str, list[dict[str, Any]]] = {}
    for values in channel_values:
        if not isinstance(values, dict):
            continue
        outcomes = values.get(_OUTCOMES_KEY)
        if not isinstance(outcomes, list) or not outcomes:
            continue
        clean = [o for o in outcomes if isinstance(o, dict)]
        if not clean:
            continue
        key = _run_key(clean)
        if key not in longest_by_run or len(clean) > len(longest_by_run[key]):
            longest_by_run[key] = clean

    corpus: list[dict[str, Any]] = []
    for key in sorted(longest_by_run):
        corpus.extend(longest_by_run[key])
    return corpus


def load_outcomes_from_json(path: Path) -> list[dict[str, Any]]:
    """Load outcomes from a JSON file (an array, or a dumped state object).

    Accepts either:
    * a JSON array of outcome dicts, or
    * a JSON object with a ``reconciliation_outcomes`` array.

    Args:
        path: Path to the JSON file.

    Returns:
        The list of outcome dicts.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the JSON shape is not a supported outcomes container.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        outcomes = data.get(_OUTCOMES_KEY, [])
    elif isinstance(data, list):
        outcomes = data
    else:
        raise ValueError(
            f"JSON must be an array of outcomes or an object with a {_OUTCOMES_KEY!r} array."
        )
    if not isinstance(outcomes, list):
        raise ValueError(f"{_OUTCOMES_KEY!r} must be a JSON array.")
    return [o for o in outcomes if isinstance(o, dict)]


def format_report(report: CalibrationReport) -> str:
    """Render the report as a human-readable table (no I/O)."""
    lines: list[str] = []
    lines.append("RECONCILIATION_BAND_DISTANCE calibration report")
    lines.append("=" * 52)
    lines.append(
        f"outcomes: {report.total_outcomes} (skipped malformed: {report.skipped_outcomes})"
    )
    lines.append(f"target precision: {report.target_precision}   min samples: {report.min_samples}")
    lines.append("")
    header = (
        f"{'dist':>4}  {'total':>6}  {'labelled':>8}  {'useful':>6}  {'precision':>9}  {'meets':>5}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for s in report.per_distance:
        precision = "—" if s.precision is None else f"{s.precision:.4f}"
        lines.append(
            f"{s.band_distance:>4}  {s.total:>6}  {s.labelled:>8}  "
            f"{s.useful:>6}  {precision:>9}  {('yes' if s.meets_target else 'no'):>5}"
        )
    lines.append("")
    rec = report.recommended_band_distance
    lines.append(f"recommendation: {'(keep current constant)' if rec is None else rec}")
    lines.append(f"rationale: {report.rationale}")
    return "\n".join(lines)


def _iter_checkpoint_channel_values() -> Iterator[dict[str, Any]]:
    """Yield ``channel_values`` for every persisted checkpoint (Postgres path).

    Lazily imports the graph's Postgres checkpointer so this module (and its
    test suite) imports cleanly with no DB driver. Assumes the standard
    LangGraph ``PostgresSaver.list()`` shape
    (``CheckpointTuple.checkpoint['channel_values']``); isolated here and
    wrapped by the caller so a version mismatch is the single line to adjust
    without touching the pure ``--json-file`` path.
    """
    from app.backend.graph import postgres_checkpointer

    with postgres_checkpointer() as cp:
        for tuple_ in cp.list(None):
            checkpoint = getattr(tuple_, "checkpoint", None) or {}
            values = checkpoint.get("channel_values", {})
            if isinstance(values, dict):
                yield values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calibrate",
        description=(
            "Print the advisory RECONCILIATION_BAND_DISTANCE calibration report "
            "over the captured reconciliation_outcomes corpus."
        ),
    )
    parser.add_argument(
        "--json-file",
        type=Path,
        default=None,
        help=(
            "Read outcomes from a JSON file (array, or object with a "
            "reconciliation_outcomes array) instead of Postgres."
        ),
    )
    parser.add_argument(
        "--target-precision",
        type=float,
        default=None,
        help="Precision floor a band distance must clear (default 0.70).",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=None,
        help="Minimum labelled outcomes required (default 10).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of a table.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)

    if args.json_file is not None:
        try:
            outcomes = load_outcomes_from_json(args.json_file)
        except FileNotFoundError:
            sys.stderr.write(f"error: file not found: {args.json_file}\n")
            return 2
        except ValueError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 2
    else:
        try:
            outcomes = collect_outcomes(_iter_checkpoint_channel_values())
        except Exception as exc:  # noqa: BLE001 - surface DB errors as a clean message
            sys.stderr.write(
                f"error: could not read outcomes from Postgres: {exc}\n"
                "hint: use --json-file to run without a database.\n"
            )
            return 2

    kwargs: dict[str, Any] = {}
    if args.target_precision is not None:
        kwargs["target_precision"] = args.target_precision
    if args.min_samples is not None:
        kwargs["min_samples"] = args.min_samples

    report = calibrate_reconciliation_threshold(outcomes, **kwargs)

    if args.json:
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(format_report(report) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin runtime shim
    raise SystemExit(main())

"""Trajectory training-data export boundary (Feature 3 — the in-VPC export side).

The capture seam (``recorder`` + ``store``) persists
:class:`~app.backend.trajectory.record.TrajectoryRecord`s. This module is the
EXPORT side: it turns those records into the ``(chosen, rejected, critique)``
training examples a preference-optimisation run consumes — under a strict
data-governance boundary, because handing captured negotiations to a training
run is a higher-bar decision than merely capturing them.

Governance boundary (the whole point of this module)
----------------------------------------------------
* **Financial data never leaves the bank's VPC.** The export writer touches ONLY
  the local filesystem (:func:`write_training_jsonl`) and makes NO network call.
  There is deliberately no "upload" / remote-sink path here — the boundary is
  structural, not a config flag.
* **Separate, explicit opt-in.** Export is gated by its OWN flag
  (``trajectory_export_enabled``) and a local destination dir, distinct from the
  capture flag (``trajectory_dsn``). With either unset, :func:`run_export` is a
  hard no-op.
* **No direct identifiers.** :func:`redact_for_export` NEVER emits ``tdb_code``,
  ``hojin_bango``, or ``actor``. Borrower grouping uses a salted pseudonymous
  key (:func:`borrower_key`) so the same borrower's records stay linkable in the
  corpus without exposing who they are.
* **Free text off by default.** Banker revision notes and Keikakusho drafts can
  carry borrower-identifying prose, so they are DROPPED unless the bank opts in
  (``trajectory_export_include_free_text``) after PII review.

The transform is pure and deterministic; only :func:`write_training_jsonl` /
:func:`run_export` touch the filesystem. Offline-safe: importing this module
pulls in no DB driver and makes no network/file access until an export is
explicitly run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from app.backend.secrets import resolve_secret
from app.backend.trajectory.record import TrajectoryRecord
from app.backend.trajectory.store import TrajectoryStore, get_trajectory_store
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "borrower_key",
    "redact_for_export",
    "to_training_example",
    "export_training_examples",
    "iter_records",
    "write_training_jsonl",
    "run_export",
]

_log = get_logger(__name__)

#: Strategy dict keys that are SAFE to export (deterministic, non-PII): the
#: title, rationale, and the expected ordinary-profit uplift the strategist
#: computed. Any other key (should the model grow) is dropped by default so a
#: new field can never silently leak into the training corpus.
_SAFE_STRATEGY_KEYS: frozenset[str] = frozenset({"title", "rationale", "expected_keijo_uplift"})

#: input_summary keys that are SAFE to export (deterministic figures / classes).
_SAFE_INPUT_KEYS: frozenset[str] = frozenset(
    {"fsa_classification", "ews_score", "working_capital_gap", "revision_count"}
)


def borrower_key(tdb_code: str, salt: str) -> str:
    """Return a pseudonymous, stable grouping key for a borrower.

    HMAC-SHA256 of the ``tdb_code`` under the secret ``salt`` so the same
    borrower's records stay linkable in the corpus WITHOUT emitting the real
    code, and a salt prevents trivial rainbow-table re-identification (the TDB
    code space is small). With an empty salt this degrades to a plain SHA-256
    (still pseudonymous and linkable, but weaker) rather than failing, so an
    unconfigured-salt export is possible but logged as weaker by the caller.

    Args:
        tdb_code: The 7-digit borrower code (never itself exported).
        salt: The secret salt (already resolved through the secret seam).

    Returns:
        A 64-char lowercase hex digest.
    """
    code = (tdb_code or "").encode("utf-8")
    if salt:
        return hmac.new(salt.encode("utf-8"), code, hashlib.sha256).hexdigest()
    return hashlib.sha256(code).hexdigest()


def _safe_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    """Return a strategy dict whitelisted to the safe, non-PII keys."""
    return {k: strategy[k] for k in _SAFE_STRATEGY_KEYS if k in strategy}


def redact_for_export(
    record: TrajectoryRecord, *, salt: str, include_free_text: bool
) -> dict[str, Any]:
    """Project one trajectory record to its PII-safe training surface.

    The export contract, applied here as a WHITELIST (anything not explicitly
    allowed is dropped, so a new record field cannot silently leak):

    * direct identifiers (``tdb_code`` / ``hojin_bango`` / ``actor``) are never
      emitted; borrower grouping is the pseudonymous :func:`borrower_key`;
    * ``input_summary`` and each strategy are whitelisted to deterministic,
      non-PII keys;
    * the preference framing (chosen / rejected) comes from the record's own
      :meth:`~app.backend.trajectory.record.TrajectoryRecord.preference_pair`;
    * the decision label and data_version (an opaque hash) are kept;
    * free text (``critique`` / revision note, ``keikakusho_draft``) is included
      ONLY when ``include_free_text`` is True.

    Args:
        record: The captured trajectory record.
        salt: Secret salt for the borrower key (already secret-seam-resolved).
        include_free_text: Whether to include borrower-identifying free text.

    Returns:
        A JSON-serialisable, PII-safe training example dict.
    """
    pair = record.preference_pair()
    example: dict[str, Any] = {
        "borrower_key": borrower_key(record.tdb_code, salt),
        "decision": record.decision.value,
        "data_version": record.data_version,
        "inputs": {
            k: record.input_summary[k] for k in _SAFE_INPUT_KEYS if k in record.input_summary
        },
        "chosen": _safe_strategy(pair.chosen) if pair.chosen else None,
        "rejected": [_safe_strategy(s) for s in pair.rejected],
    }
    if include_free_text:
        example["critique"] = pair.critique
        example["keikakusho_draft"] = record.keikakusho_draft
    return example


def to_training_example(
    record: TrajectoryRecord, settings: Settings | None = None
) -> dict[str, Any]:
    """Redact one record to a training example using the configured policy.

    Convenience over :func:`redact_for_export` that reads the salt (through the
    secret seam) and the free-text policy from settings.
    """
    cfg = settings or get_settings()
    salt = resolve_secret(getattr(cfg, "trajectory_export_salt", "") or "")
    return redact_for_export(
        record,
        salt=salt,
        include_free_text=bool(getattr(cfg, "trajectory_export_include_free_text", False)),
    )


def export_training_examples(
    records: Iterable[TrajectoryRecord], settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Project a stream of records to PII-safe training examples (pure)."""
    cfg = settings or get_settings()
    return [to_training_example(record, cfg) for record in records]


def iter_records(store: TrajectoryStore, thread_ids: Iterable[str]) -> Iterator[TrajectoryRecord]:
    """Yield every record for the given thread_ids in order (export enumeration).

    The store seam is intentionally per-thread (append + read only); an export
    enumerates the thread_ids the operator supplies (e.g. from an audit/portfolio
    listing). This keeps the export explicit about WHICH books it touches rather
    than implying an unbounded ``read all`` the append-only seam does not offer.

    Args:
        store: The trajectory store to read from.
        thread_ids: The thread_ids to enumerate.

    Yields:
        Each thread's records, in write order.
    """
    for thread_id in thread_ids:
        yield from store.read(thread_id)


def write_training_jsonl(examples: Iterable[dict[str, Any]], destination: Path) -> int:
    """Write training examples as JSONL to a LOCAL file (no network).

    Writes one JSON object per line (``ensure_ascii=False`` so CJK stays
    readable). Creates the parent directory if needed. This is the ONLY function
    that performs output, and it writes strictly to the local filesystem — the
    in-VPC boundary is structural: there is no remote-sink code path.

    Args:
        examples: The PII-safe training examples to write.
        destination: The local file path to write.

    Returns:
        The number of examples written.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            count += 1
    return count


def run_export(
    thread_ids: Iterable[str],
    *,
    filename: str = "trajectory_export.jsonl",
    settings: Settings | None = None,
    store: TrajectoryStore | None = None,
) -> int:
    """Export the given threads' trajectories to a local JSONL training file.

    The governance-gated entry point. It is a HARD NO-OP (returns ``0`` without
    touching the filesystem) unless BOTH:

    * ``trajectory_export_enabled`` is True (the explicit export decision), AND
    * ``trajectory_export_dir`` is a non-empty local directory path.

    When enabled, it reads the records for ``thread_ids`` from the store,
    redacts each to its PII-safe training surface, and writes them as JSONL to
    ``<trajectory_export_dir>/<filename>`` — a LOCAL write, no network. Returns
    the number of examples written.

    Args:
        thread_ids: The thread_ids whose trajectories to export.
        filename: Output file name within the export dir.
        settings: Optional settings override.
        store: Optional store override (defaults to the configured store).

    Returns:
        The number of examples written (``0`` when export is disabled/unconfigured).
    """
    cfg = settings or get_settings()
    if not getattr(cfg, "trajectory_export_enabled", False):
        _log.info("trajectory.export.disabled", reason="export_not_enabled")
        return 0
    export_dir = (getattr(cfg, "trajectory_export_dir", "") or "").strip()
    if not export_dir:
        _log.warning("trajectory.export.disabled", reason="no_export_dir")
        return 0
    if not getattr(cfg, "trajectory_export_salt", ""):
        _log.warning("trajectory.export.weak_pseudonymisation", reason="no_salt")

    active_store = store or get_trajectory_store(cfg)
    records = list(iter_records(active_store, thread_ids))
    examples = export_training_examples(records, cfg)
    destination = Path(export_dir) / filename
    written = write_training_jsonl(examples, destination)
    _log.info(
        "trajectory.export.written",
        examples=written,
        destination=str(destination),
        include_free_text=bool(getattr(cfg, "trajectory_export_include_free_text", False)),
    )
    return written

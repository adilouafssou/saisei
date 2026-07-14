"""Offline coverage for the ``make export-trajectories`` CLI.

Pins the operator-facing contract of
:mod:`app.backend.trajectory.export_cli` without any database or network:

* thread-id assembly de-duplicates, preserves order, and skips blanks/comments;
* ``--threads-file`` is read and merged with positional ids;
* a run with no thread ids is a usage error (exit 2);
* ``--dry-run`` counts exportable examples via an in-memory store and writes
  nothing;
* the governance gate is reported honestly: disabled -> exit 0, enabled but no
  export dir -> exit 3, and a configured run writes the JSONL and exits 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.backend.trajectory import export_cli
from app.backend.trajectory.export_cli import (
    collect_thread_ids,
    load_thread_ids_from_file,
    main,
)
from app.backend.trajectory.record import TrajectoryDecision, TrajectoryRecord
from app.backend.trajectory.store import InMemoryTrajectoryStore


def _record(thread_id: str, tdb_code: str = "1234567") -> TrajectoryRecord:
    """Build a minimal record whose preference pair has a chosen strategy."""
    strategies = [
        {"title": "A", "rationale": "r", "expected_keijo_uplift": 1.0},
        {"title": "B", "rationale": "r2", "expected_keijo_uplift": 0.5},
    ]
    return TrajectoryRecord(
        trajectory_id=f"tid-{thread_id}",
        thread_id=thread_id,
        tdb_code=tdb_code,
        created_at="2026-06-20T00:00:00+00:00",
        decision=TrajectoryDecision.APPROVE,
        revision_note="note",
        data_version="v0",
        input_summary={"fsa_classification": "normal", "ews_score": 12},
        proposed_strategies=strategies,
        approved_strategy=strategies[0],
        keikakusho_draft="draft",
    )


class _Cfg:
    """Minimal settings stand-in for the export knobs the CLI reads."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        export_dir: str = "",
        salt: str = "s",
        include_free_text: bool = False,
    ) -> None:
        self.trajectory_export_enabled = enabled
        self.trajectory_export_dir = export_dir
        self.trajectory_export_salt = salt
        self.trajectory_export_include_free_text = include_free_text
        self.trajectory_dsn = ""


def test_collect_thread_ids_dedupes_preserves_order_skips_noise() -> None:
    ids = collect_thread_ids(["t1", " t2 ", "t1"], ["", "# comment", "t3", "t2"])
    assert ids == ["t1", "t2", "t3"]


def test_load_thread_ids_from_file(tmp_path: Path) -> None:
    f = tmp_path / "threads.txt"
    f.write_text("t1\n# skip me\n\nt2\n", encoding="utf-8")
    assert collect_thread_ids(load_thread_ids_from_file(f)) == ["t1", "t2"]


def test_missing_threads_file_is_usage_error(tmp_path: Path) -> None:
    code = main(["--threads-file", str(tmp_path / "nope.txt"), "t1"])
    assert code == 2


def test_no_thread_ids_is_usage_error() -> None:
    assert main([]) == 2


def test_dry_run_counts_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryTrajectoryStore()
    store.append(_record("t1"))
    store.append(_record("t1"))
    cfg = _Cfg(enabled=False)
    monkeypatch.setattr(export_cli, "get_settings", lambda: cfg)
    monkeypatch.setattr(export_cli, "get_trajectory_store", lambda _cfg: store)

    code = main(["--dry-run", "t1"])

    assert code == 0
    assert "dry-run: 2 example(s)" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())  # nothing written


def test_disabled_gate_reports_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(export_cli, "get_settings", lambda: _Cfg(enabled=False))
    code = main(["t1"])
    assert code == 0
    assert "export disabled" in capsys.readouterr().err


def test_enabled_without_dir_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(export_cli, "get_settings", lambda: _Cfg(enabled=True, export_dir=""))
    code = main(["t1"])
    assert code == 3
    assert "SAISEI_TRAJECTORY_EXPORT_DIR is unset" in capsys.readouterr().err


def test_configured_run_writes_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryTrajectoryStore()
    store.append(_record("t1"))
    cfg = _Cfg(enabled=True, export_dir=str(tmp_path))
    monkeypatch.setattr(export_cli, "get_settings", lambda: cfg)
    # run_export resolves its own store via get_trajectory_store(cfg); point it
    # at the in-memory store in the export module's namespace.
    from app.backend.trajectory import export as export_mod

    monkeypatch.setattr(export_mod, "get_trajectory_store", lambda _cfg: store)

    code = main(["t1"])

    assert code == 0
    out = capsys.readouterr().out
    assert "exported 1 example(s)" in out
    written = (tmp_path / "trajectory_export.jsonl").read_text(encoding="utf-8")
    line = json.loads(written.splitlines()[0])
    # PII-safe surface: pseudonymous key, no raw identifiers, free text dropped.
    assert "borrower_key" in line
    assert "tdb_code" not in line
    assert "critique" not in line
    assert line["chosen"]["title"] == "A"

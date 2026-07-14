"""Verifier for the trajectory training-data export boundary (Feature 3).

The export side's whole value is the GOVERNANCE BOUNDARY, so these offline
checks pin exactly that (pure transform + the local-only, gated writer; no DB,
no network):

- direct identifiers (tdb_code / hojin_bango / actor) are NEVER emitted; the
  borrower key is a salted pseudonymous hash that is stable per borrower and
  changes with the salt;
- inputs and strategies are whitelisted to deterministic, non-PII keys (a new/
  unknown field cannot leak);
- the (chosen, rejected) framing matches the record's preference_pair;
- free text (critique / draft) is dropped by default and included only on the
  explicit opt-in;
- run_export is a HARD no-op (no file written) unless BOTH the enable flag and a
  local export dir are set; when enabled it writes local JSONL only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.backend.trajectory.export import (
    borrower_key,
    export_training_examples,
    redact_for_export,
    run_export,
    to_training_example,
)
from app.backend.trajectory.record import TrajectoryDecision, TrajectoryRecord
from app.backend.trajectory.store import InMemoryTrajectoryStore
from app.shared.settings import Settings


def _record(
    *,
    thread_id: str = "t1",
    tdb_code: str = "1234567",
    decision: TrajectoryDecision = TrajectoryDecision.APPROVE,
    revision_note: str = "borrower Yamada-san asked for more time",
) -> TrajectoryRecord:
    proposed = [
        {
            "title": "Price pass-through",
            "rationale": "recover margin",
            "expected_keijo_uplift": 1000,
            "secret_internal": "LEAK",
        },
        {"title": "COGS reduction", "rationale": "yield", "expected_keijo_uplift": 500},
    ]
    return TrajectoryRecord(
        trajectory_id="x1",
        thread_id=thread_id,
        tdb_code=tdb_code,
        hojin_bango="1234567890123",
        created_at="2026-03-01T00:00:00+00:00",
        actor="banker-jane",
        decision=decision,
        revision_note=revision_note,
        data_version="deadbeef",
        input_summary={
            "fsa_classification": "\u8981\u6ce8\u610f\u5148",
            "ews_score": 42,
            "working_capital_gap": -3_000_000,
            "revision_count": 0,
            "company_name": "\u5c71\u7530\u88fd\u4f5c\u6240",  # PII -> must be dropped
        },
        proposed_strategies=proposed,
        approved_strategy=proposed[0],
    ).with_content_hash()


class TestRedaction:
    def test_direct_identifiers_never_emitted(self) -> None:
        ex = redact_for_export(_record(), salt="s", include_free_text=True)
        flat = json.dumps(ex, ensure_ascii=False)
        assert "1234567" not in flat  # tdb_code
        assert "1234567890123" not in flat  # hojin_bango
        assert "banker-jane" not in flat  # actor
        assert "tdb_code" not in ex and "hojin_bango" not in ex and "actor" not in ex

    def test_borrower_key_is_salted_and_stable(self) -> None:
        a = borrower_key("1234567", "salt-A")
        again = borrower_key("1234567", "salt-A")
        other_salt = borrower_key("1234567", "salt-B")
        assert a == again  # stable per (code, salt)
        assert a != other_salt  # salt changes the key
        assert a != "1234567"

    def test_strategy_keys_whitelisted(self) -> None:
        ex = redact_for_export(_record(), salt="s", include_free_text=False)
        assert ex["chosen"] == {
            "title": "Price pass-through",
            "rationale": "recover margin",
            "expected_keijo_uplift": 1000,
        }
        # The unknown 'secret_internal' key is dropped, not leaked.
        assert "secret_internal" not in json.dumps(ex)

    def test_input_keys_whitelisted_drops_pii(self) -> None:
        ex = redact_for_export(_record(), salt="s", include_free_text=False)
        assert set(ex["inputs"]) == {
            "fsa_classification",
            "ews_score",
            "working_capital_gap",
            "revision_count",
        }
        assert "company_name" not in ex["inputs"]

    def test_preference_framing_matches_record(self) -> None:
        ex = redact_for_export(_record(), salt="s", include_free_text=False)
        assert ex["chosen"]["title"] == "Price pass-through"
        assert [s["title"] for s in ex["rejected"]] == ["COGS reduction"]

    def test_free_text_dropped_by_default(self) -> None:
        ex = redact_for_export(_record(), salt="s", include_free_text=False)
        assert "critique" not in ex
        assert "Yamada" not in json.dumps(ex, ensure_ascii=False)

    def test_free_text_included_on_opt_in(self) -> None:
        ex = redact_for_export(_record(), salt="s", include_free_text=True)
        assert ex["critique"] == "borrower Yamada-san asked for more time"


class _Cfg:
    """Minimal settings stand-in for the export policy."""

    def __init__(self, **kw: object) -> None:
        self.trajectory_export_enabled = kw.get("enabled", False)
        self.trajectory_export_dir = kw.get("dir", "")
        self.trajectory_export_salt = kw.get("salt", "salt")
        self.trajectory_export_include_free_text = kw.get("free_text", False)
        self.trajectory_dsn = ""


class TestRunExportGate:
    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        store = InMemoryTrajectoryStore()
        store.append(_record())
        cfg = _Cfg(enabled=False, dir=str(tmp_path))
        assert run_export(["t1"], settings=cast("Settings", cfg), store=store) == 0
        assert list(tmp_path.iterdir()) == []  # nothing written

    def test_noop_when_no_dir(self, tmp_path: Path) -> None:
        store = InMemoryTrajectoryStore()
        store.append(_record())
        cfg = _Cfg(enabled=True, dir="")
        assert run_export(["t1"], settings=cast("Settings", cfg), store=store) == 0

    def test_writes_local_jsonl_when_enabled(self, tmp_path: Path) -> None:
        store = InMemoryTrajectoryStore()
        store.append(_record(thread_id="t1"))
        store.append(_record(thread_id="t1", decision=TrajectoryDecision.REVISE))
        cfg = _Cfg(enabled=True, dir=str(tmp_path), salt="s")
        written = run_export(["t1"], settings=cast("Settings", cfg), store=store)
        assert written == 2
        out = tmp_path / "trajectory_export.jsonl"
        assert out.exists()
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert "borrower_key" in first and "1234567" not in lines[0]


def test_to_training_example_uses_settings_policy() -> None:
    cfg = _Cfg(enabled=True, salt="s", free_text=False)
    ex = to_training_example(_record(), cast("Settings", cfg))
    assert "critique" not in ex
    assert export_training_examples([_record()], cast("Settings", cfg))[0]["decision"] == "approve"

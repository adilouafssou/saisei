"""Offline tests for the agent-trajectory data flywheel (Feature 3).

The trajectory store is a side-record that mirrors the audit ledger: capturing a
trajectory must be deterministic, append-only, and a strict no-op offline. These
tests pin:

- the record's deterministic content_hash + integrity check;
- the preference_pair() framing (chosen / rejected / critique);
- the recorder building a sealed record into an in-memory store;
- the strict offline no-op (NullTrajectoryStore) contract.

Fully offline: stdlib + pydantic + the in-memory store only; no network.
"""

from __future__ import annotations

from app.backend.state import SaiseiState, Strategy
from app.backend.trajectory.record import (
    TrajectoryDecision,
    TrajectoryRecord,
    compute_content_hash,
)
from app.backend.trajectory.recorder import build_input_summary, record_trajectory
from app.backend.trajectory.store import (
    InMemoryTrajectoryStore,
    NullTrajectoryStore,
    get_trajectory_store,
)
from app.shared.settings import Settings

_OFFLINE = Settings(trajectory_dsn="")


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="r", expected_keijo_uplift=uplift)


def _state() -> SaiseiState:
    return SaiseiState(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        ews_score=62.0,
        working_capital_gap=-5_000_000,
        proposed_strategies=[
            _strategy("price", 43_920_000),
            _strategy("cogs", 30_960_000),
        ],
    )


# ---------------------------------------------------------------------------
# Record model + hashing
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> TrajectoryRecord:
    base: dict[str, object] = {
        "trajectory_id": "t-1",
        "thread_id": "thread-1",
        "tdb_code": "1234567",
        "created_at": "2026-01-01T00:00:00+00:00",
        "decision": TrajectoryDecision.APPROVE,
        "proposed_strategies": [{"title": "price"}, {"title": "cogs"}],
        "approved_strategy": {"title": "price"},
    }
    base.update(overrides)
    return TrajectoryRecord(**base)


class TestRecordHashing:
    def test_content_hash_is_deterministic(self) -> None:
        r = _record()
        assert compute_content_hash(r) == compute_content_hash(r)
        assert len(compute_content_hash(r)) == 64

    def test_with_content_hash_sets_a_valid_hash(self) -> None:
        sealed = _record().with_content_hash()
        assert sealed.content_hash
        assert sealed.hash_is_valid()

    def test_hash_excludes_itself(self) -> None:
        # Two records identical but for content_hash hash the same.
        a = _record(content_hash="")
        b = _record(content_hash="deadbeef")
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_tamper_is_detectable(self) -> None:
        sealed = _record().with_content_hash()
        tampered = sealed.model_copy(update={"tdb_code": "9999999"})
        assert not tampered.hash_is_valid()


# ---------------------------------------------------------------------------
# Preference framing
# ---------------------------------------------------------------------------


class TestPreferencePair:
    def test_approve_yields_chosen_and_rejected(self) -> None:
        pair = _record(decision=TrajectoryDecision.APPROVE).preference_pair()
        assert pair.has_chosen
        assert pair.chosen == {"title": "price"}
        assert pair.rejected == [{"title": "cogs"}]

    def test_revise_has_no_chosen_and_all_rejected(self) -> None:
        pair = _record(
            decision=TrajectoryDecision.REVISE,
            approved_strategy=None,
            revision_note="increase price further",
        ).preference_pair()
        assert not pair.has_chosen
        assert pair.chosen is None
        assert pair.rejected == [{"title": "price"}, {"title": "cogs"}]
        assert pair.critique == "increase price further"


# ---------------------------------------------------------------------------
# Input summary + recorder
# ---------------------------------------------------------------------------


def test_build_input_summary_is_compact_and_serialisable() -> None:
    summary = build_input_summary(_state())
    assert summary["ews_score"] == 62.0
    assert summary["working_capital_gap"] == -5_000_000
    assert summary["revision_count"] == 0
    # fsa_classification is None on this fixture (not yet classified).
    assert summary["fsa_classification"] is None


def test_recorder_appends_sealed_record_to_store() -> None:
    store = InMemoryTrajectoryStore()
    state = _state()
    record_trajectory(
        state=state,
        decision=TrajectoryDecision.APPROVE,
        thread_id="thread-1",
        actor="banker-7",
        approved_strategy=state.proposed_strategies[0],
        store=store,
    )
    records = store.read("thread-1")
    assert len(records) == 1
    rec = records[0]
    assert rec.hash_is_valid()
    assert rec.decision is TrajectoryDecision.APPROVE
    assert rec.actor == "banker-7"
    assert rec.approved_strategy == {
        "title": "price",
        "rationale": "r",
        "expected_keijo_uplift": 43_920_000,
    }
    assert rec.preference_pair().rejected == [
        {"title": "cogs", "rationale": "r", "expected_keijo_uplift": 30_960_000}
    ]


def test_recorder_accepts_string_decision() -> None:
    store = InMemoryTrajectoryStore()
    record_trajectory(
        state=_state(),
        decision="revise",
        thread_id="thread-2",
        revision_note="raise the price target",
        store=store,
    )
    rec = store.read("thread-2")[0]
    assert rec.decision is TrajectoryDecision.REVISE
    assert rec.revision_note == "raise the price target"
    assert rec.approved_strategy is None


def test_recorder_never_raises_on_bad_input() -> None:
    # A bogus decision string would raise inside; the recorder must swallow it.
    store = InMemoryTrajectoryStore()
    record_trajectory(state=_state(), decision="not_a_decision", store=store)
    assert store.read("") == []


# ---------------------------------------------------------------------------
# Store selection / offline no-op
# ---------------------------------------------------------------------------


class TestStoreSelection:
    def test_unconfigured_returns_null_store(self) -> None:
        assert isinstance(get_trajectory_store(_OFFLINE), NullTrajectoryStore)

    def test_null_store_is_a_noop(self) -> None:
        store = NullTrajectoryStore()
        store.append(_record().with_content_hash())
        assert store.read("thread-1") == []

    def test_recorder_with_default_store_is_offline_noop(self) -> None:
        # No store passed + offline settings -> NullTrajectoryStore -> no error.
        record_trajectory(state=_state(), decision=TrajectoryDecision.APPROVE, settings=_OFFLINE)

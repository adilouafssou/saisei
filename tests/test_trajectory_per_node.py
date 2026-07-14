"""Offline tests for the full per-node trajectory capture (Feature 3.1).

Extends the Feature 3 flywheel: the captured record now carries the FULL agentic
path (ordered per-node output digests) plus the raw HITL interrupt payload, not
just the negotiation summary. These tests pin:

- NodeSnapshot hashing determinism (canonical JSON, order-insensitive);
- build_node_trajectory contents + graph ordering from accumulated state;
- the record round-tripping + sealing the new fields, and tamper detection
  over them;
- byte-stable hashing when the new fields are left empty (no regression for
  pre-Feature-3.1 records);
- the strict offline no-op contract with the new params.

Fully offline: stdlib + pydantic + the in-memory store only; no network.
"""

from __future__ import annotations

import datetime as dt

from app.backend.state import SaiseiState, Strategy
from app.backend.trajectory.record import (
    NodeSnapshot,
    TrajectoryDecision,
    TrajectoryRecord,
    compute_content_hash,
)
from app.backend.trajectory.recorder import (
    build_node_trajectory,
    record_trajectory,
)
from app.backend.trajectory.store import InMemoryTrajectoryStore
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass
from app.shared.settings import Settings

_OFFLINE = Settings(trajectory_dsn="")


def _strategy(title: str, uplift: int) -> Strategy:
    return Strategy(title=title, rationale="r", expected_keijo_uplift=uplift)


def _state() -> SaiseiState:
    return SaiseiState(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        ews_score=62.0,
        ews_breakdown=[{"key": "sales", "points": 20.0}],
        working_capital_gap=-5_000_000,
        fsa_classification=FsaClass.YOCHUISAKI,
        special_attention=True,
        classification_reason="EWS >= substandard floor",
        net_worth=10_000_000,
        hosho_kaijo_score=55.0,
        hosho_kaijo_eligible=False,
        succession_ready=True,
        shisanhyo=[
            TrialBalance(
                period=dt.date(2025, 5, 31),
                uriage=138_000_000,
                uriage_genka=115_000_000,
                hanbaihi=21_000_000,
            )
        ],
        proposed_strategies=[
            _strategy("price", 43_920_000),
            _strategy("cogs", 30_960_000),
        ],
        feasibility_notes=[{"strategy_title": "price", "achievability": "medium"}],
        reconciliation_required=False,
        negotiation_status="approved",
    )


# ---------------------------------------------------------------------------
# NodeSnapshot hashing
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


class TestNodeSnapshotHashing:
    def test_snapshot_digest_is_order_insensitive(self) -> None:
        a = _record(node_trajectory=[NodeSnapshot(node="ews", output={"a": 1, "b": 2})])
        b = _record(node_trajectory=[NodeSnapshot(node="ews", output={"b": 2, "a": 1})])
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_record_with_trajectory_seals_and_validates(self) -> None:
        sealed = _record(
            node_trajectory=build_node_trajectory(_state()),
            interrupt_payload={"prompt": "decide", "ews_score": 62.0},
        ).with_content_hash()
        assert sealed.hash_is_valid()
        assert sealed.interrupt_payload["ews_score"] == 62.0
        assert [s.node for s in sealed.node_trajectory][0] == "intake"


# ---------------------------------------------------------------------------
# build_node_trajectory
# ---------------------------------------------------------------------------


class TestBuildNodeTrajectory:
    def test_graph_order_and_node_names(self) -> None:
        traj = build_node_trajectory(_state())
        assert [s.node for s in traj] == [
            "intake",
            "ews",
            "macro",
            "classifier",
            "keieisha_hosho",
            "strategist",
            "feasibility_critic",
            "critics",
            "lead_arranger",
        ]

    def test_node_outputs_carry_expected_values(self) -> None:
        by_node = {s.node: s.output for s in build_node_trajectory(_state())}
        assert by_node["intake"]["tdb_code"] == "1234567"
        assert by_node["ews"]["ews_score"] == 62.0
        assert by_node["macro"]["working_capital_gap"] == -5_000_000
        assert by_node["classifier"]["special_attention"] is True
        assert by_node["keieisha_hosho"]["hosho_kaijo_score"] == 55.0
        assert len(by_node["strategist"]["proposed_strategies"]) == 2
        assert by_node["lead_arranger"]["negotiation_status"] == "approved"

    def test_is_deterministic(self) -> None:
        assert build_node_trajectory(_state()) == build_node_trajectory(_state())

    def test_empty_state_yields_stable_shape(self) -> None:
        # A barely-populated state still yields all nine snapshots (stable shape).
        traj = build_node_trajectory(SaiseiState(tdb_code="7654321"))
        assert len(traj) == 9
        assert traj[1].output["ews_score"] is None


# ---------------------------------------------------------------------------
# Tamper detection over the new fields
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_mutating_a_snapshot_breaks_the_hash(self) -> None:
        sealed = _record(
            node_trajectory=[NodeSnapshot(node="ews", output={"ews_score": 62.0})]
        ).with_content_hash()
        tampered = sealed.model_copy(
            update={"node_trajectory": [NodeSnapshot(node="ews", output={"ews_score": 10.0})]}
        )
        assert not tampered.hash_is_valid()

    def test_mutating_the_interrupt_payload_breaks_the_hash(self) -> None:
        sealed = _record(interrupt_payload={"ews_score": 62.0}).with_content_hash()
        tampered = sealed.model_copy(update={"interrupt_payload": {"ews_score": 1.0}})
        assert not tampered.hash_is_valid()


# ---------------------------------------------------------------------------
# Byte-stable empty default (no regression for pre-Feature-3.1 records)
# ---------------------------------------------------------------------------


def test_empty_new_fields_hash_is_byte_identical() -> None:
    # A record built WITHOUT the new fields must hash identically to one that
    # sets them to their empty defaults explicitly — proving the additive fields
    # do not change the hash of an existing-shaped record.
    without = _record()
    with_empty = _record(node_trajectory=[], interrupt_payload={})
    assert compute_content_hash(without) == compute_content_hash(with_empty)


# ---------------------------------------------------------------------------
# Recorder seals the new fields / offline no-op
# ---------------------------------------------------------------------------


def test_recorder_seals_node_trajectory_and_interrupt_payload() -> None:
    store = InMemoryTrajectoryStore()
    state = _state()
    record_trajectory(
        state=state,
        decision=TrajectoryDecision.APPROVE,
        thread_id="thread-1",
        approved_strategy=state.proposed_strategies[0],
        node_trajectory=build_node_trajectory(state),
        interrupt_payload={"prompt": "decide", "ews_score": 62.0},
        store=store,
    )
    rec = store.read("thread-1")[0]
    assert rec.hash_is_valid()
    assert [s.node for s in rec.node_trajectory][0] == "intake"
    assert len(rec.node_trajectory) == 9
    assert rec.interrupt_payload["ews_score"] == 62.0


def test_recorder_with_new_params_is_offline_noop() -> None:
    # No store passed + offline settings -> NullTrajectoryStore -> no error,
    # even when the new params are supplied.
    record_trajectory(
        state=_state(),
        decision=TrajectoryDecision.APPROVE,
        node_trajectory=build_node_trajectory(_state()),
        interrupt_payload={"prompt": "decide"},
        settings=_OFFLINE,
    )

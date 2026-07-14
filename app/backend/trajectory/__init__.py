"""Agent-trajectory data flywheel (Feature 3).

Every banker decision (approve / revise / reject + the note) is a high-quality
human preference label. Captured well, the stream of trajectories becomes the
training signal that makes the strategist measurably better over time.

This package is a **side-record**, exactly like the audit ledger and LangSmith
capture: persisting a trajectory NEVER changes a gate, route, score, figure, or
the deterministic verdict. It is offline-safe (a no-op NullTrajectoryStore by
default) and best-effort (a storage failure can never break the workflow).

What is captured (per negotiation):
- the input state digest + identity,
- the proposed strategies (candidates),
- the banker's decision and revision note,
- the approved strategy (the *chosen* option), and the rejected alternatives,
  framed as a preference pair for DPO/ORPO-style offline training,
- the final plan, when one was written.

The append-only store (Null / InMemory now; Postgres later) mirrors the audit
sink seam, so a real backend drops in with no call-site changes.
"""

from __future__ import annotations

from app.backend.trajectory.record import (
    PreferencePair,
    TrajectoryDecision,
    TrajectoryRecord,
)
from app.backend.trajectory.recorder import record_trajectory
from app.backend.trajectory.store import (
    InMemoryTrajectoryStore,
    NullTrajectoryStore,
    TrajectoryStore,
    get_trajectory_store,
)

__all__ = [
    "PreferencePair",
    "TrajectoryDecision",
    "TrajectoryRecord",
    "record_trajectory",
    "InMemoryTrajectoryStore",
    "NullTrajectoryStore",
    "TrajectoryStore",
    "get_trajectory_store",
]

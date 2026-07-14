"""Tests for the continuous book-monitoring planner (Feature 8.1 / V2).

plan_refresh: which persisted borrowers are due for re-assessment, ordered
              most-overdue first; unknown-age rows surfaced (never skipped).
detect_crossings: which borrowers crossed the 要注意 floor UPWARD since the last
              book; new rows (no baseline) are not crossings; ordered worst-first.

Pure, deterministic, offline; imports only from ``app.*`` + stdlib.
"""

from __future__ import annotations

import datetime as dt

from app.backend.portfolio.monitor import detect_crossings, plan_refresh
from app.backend.portfolio.store import PortfolioSnapshot
from app.shared.constants import EWS_SUBSTANDARD

_NOW = dt.datetime(2026, 6, 19, 12, 0, 0, tzinfo=dt.UTC)


def _snap(
    tdb_code: str,
    *,
    ews: float = 30.0,
    updated_at: str = "",
    name: str = "",
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        tenant_id="t",
        tdb_code=tdb_code,
        company_name=name,
        ews=ews,
        fsa_kanji="",
        ews_series="",
        updated_at=updated_at,
    )


def _iso(days_ago: int) -> str:
    return (_NOW - dt.timedelta(days=days_ago)).isoformat()


class TestPlanRefresh:
    def test_only_stale_snapshots_are_due(self) -> None:
        snaps = [
            _snap("1111111", updated_at=_iso(40)),  # stale (>31d)
            _snap("2222222", updated_at=_iso(5)),  # fresh
        ]
        due = plan_refresh(snaps, now=_NOW)
        assert [i.tdb_code for i in due] == ["1111111"]
        assert due[0].reason == "stale"
        assert due[0].age_days == 40

    def test_ordered_most_overdue_first(self) -> None:
        snaps = [
            _snap("1111111", updated_at=_iso(35)),
            _snap("2222222", updated_at=_iso(90)),
            _snap("3333333", updated_at=_iso(60)),
        ]
        due = plan_refresh(snaps, now=_NOW)
        assert [i.tdb_code for i in due] == ["2222222", "3333333", "1111111"]

    def test_unknown_age_is_surfaced_last(self) -> None:
        snaps = [
            _snap("1111111", updated_at=_iso(90)),
            _snap("2222222", updated_at=""),  # no timestamp
        ]
        due = plan_refresh(snaps, now=_NOW)
        assert [i.tdb_code for i in due] == ["1111111", "2222222"]
        assert due[-1].reason == "unknown_age"
        assert due[-1].age_days is None

    def test_custom_horizon(self) -> None:
        snaps = [_snap("1111111", updated_at=_iso(10))]
        # 7-day horizon -> the 10-day-old snapshot is now due.
        due = plan_refresh(snaps, now=_NOW, max_age=dt.timedelta(days=7))
        assert [i.tdb_code for i in due] == ["1111111"]

    def test_empty_book(self) -> None:
        assert plan_refresh([], now=_NOW) == []


class TestDetectCrossings:
    def test_upward_crossing_detected(self) -> None:
        prev = [_snap("1111111", ews=EWS_SUBSTANDARD - 5)]
        curr = [_snap("1111111", ews=EWS_SUBSTANDARD + 5)]
        alerts = detect_crossings(prev, curr)
        assert len(alerts) == 1
        assert alerts[0].tdb_code == "1111111"
        assert alerts[0].prev_ews == EWS_SUBSTANDARD - 5
        assert alerts[0].new_ews == EWS_SUBSTANDARD + 5

    def test_no_crossing_when_already_above(self) -> None:
        prev = [_snap("1111111", ews=EWS_SUBSTANDARD + 1)]
        curr = [_snap("1111111", ews=EWS_SUBSTANDARD + 10)]
        assert detect_crossings(prev, curr) == []

    def test_no_crossing_on_improvement(self) -> None:
        prev = [_snap("1111111", ews=EWS_SUBSTANDARD + 5)]
        curr = [_snap("1111111", ews=EWS_SUBSTANDARD - 5)]
        assert detect_crossings(prev, curr) == []

    def test_new_borrower_is_not_a_crossing(self) -> None:
        # No baseline in `previous` -> not a crossing (already a new watchlist row).
        curr = [_snap("9999999", ews=EWS_SUBSTANDARD + 20)]
        assert detect_crossings([], curr) == []

    def test_ordered_worst_first(self) -> None:
        prev = [
            _snap("1111111", ews=10.0),
            _snap("2222222", ews=10.0),
        ]
        curr = [
            _snap("1111111", ews=EWS_SUBSTANDARD + 5),
            _snap("2222222", ews=EWS_SUBSTANDARD + 30),
        ]
        alerts = detect_crossings(prev, curr)
        assert [a.tdb_code for a in alerts] == ["2222222", "1111111"]

    def test_exact_floor_counts_as_crossing(self) -> None:
        prev = [_snap("1111111", ews=EWS_SUBSTANDARD - 1)]
        curr = [_snap("1111111", ews=EWS_SUBSTANDARD)]  # exactly at the floor
        assert len(detect_crossings(prev, curr)) == 1

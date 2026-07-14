"""Tests for the opt-in Portfolio watchlist recorder (Feature 8.1).

Mirrors tests/test_audit_record.py for the watchlist seam:

- never fatal: a store whose upsert raises does NOT propagate out of
  record_snapshot (it logs + swallows);
- offline no-op: the default NullPortfolioStore persists nothing;
- round-trip: with an InMemoryPortfolioStore a snapshot is upserted and
  readable, scoped to its tenant;
- upsert semantics: re-recording the same borrower replaces (does not append);
- deterministic EWS series: build_ews_series reads the real trial balances;
- production guard: auth_required + the placeholder tenant skips persistence.

Offline, deterministic; imports only from ``app.*`` + stdlib.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.backend.portfolio.recorder import (
    build_ews_series,
    loan_status_kanji,
    record_origination_snapshot,
    record_snapshot,
)
from app.backend.portfolio.store import (
    InMemoryPortfolioStore,
    NullPortfolioStore,
    PortfolioSnapshot,
)
from app.backend.state import SaiseiState
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass
from app.shared.models.loan import LoanEvent, LoanStatus
from app.shared.settings import Settings


def _tb(period: dt.date, keijo: int) -> TrialBalance:
    # keijo_rieki is a computed field (operating profit + non-op income - non-op
    # expense). Operating profit here is 100M - 78M - 18M = 4M, so set the
    # non-operating items to land ordinary profit exactly on ``keijo``.
    return TrialBalance(
        period=period,
        uriage=100_000_000,
        uriage_genka=78_000_000,
        hanbaihi=18_000_000,
        eigai_shueki=keijo,
        eigai_hiyo=4_000_000,
    )


def _state(**overrides: object) -> SaiseiState:
    base: dict[str, object] = {
        "tdb_code": "1234567",
        "hojin_bango": "1234567890123",
        "tdb_score": 41,
        "working_capital_gap": -5_000_000,
        "ews_score": 62.5,
        "fsa_classification": FsaClass.YOCHUISAKI,
        "shisanhyo": [
            _tb(dt.date(2025, 4, 30), 4_000_000),
            _tb(dt.date(2025, 5, 31), 2_000_000),
            _tb(dt.date(2025, 6, 30), -1_000_000),
        ],
    }
    base.update(overrides)
    return SaiseiState(**base)


def _settings(**overrides: object) -> Settings:
    base: dict[str, Any] = {
        "portfolio_dsn": "",
        "portfolio_tenant_default": "default",
        "audit_actor_default": "banker",
        "auth_required": False,
    }
    base.update(overrides)
    return Settings(**base)


class _BoomStore(NullPortfolioStore):
    """A store whose upsert always raises (to prove record_snapshot swallows it)."""

    def upsert(self, snapshot: PortfolioSnapshot) -> None:  # noqa: D102
        raise RuntimeError("backend down")


class TestRecordSnapshotNeverFatal:
    def test_upsert_failure_is_swallowed(self) -> None:
        # Must not raise even though the store's upsert blows up.
        record_snapshot(state=_state(), settings=_settings(), store=_BoomStore())


class TestOfflineNoOp:
    def test_null_store_persists_nothing(self) -> None:
        store = NullPortfolioStore()
        record_snapshot(state=_state(), settings=_settings(), store=store)
        assert store.read("default") == []


class TestRoundTrip:
    def test_snapshot_is_upserted_and_readable(self) -> None:
        store = InMemoryPortfolioStore()
        record_snapshot(state=_state(), settings=_settings(), store=store)

        rows = store.read("default")
        assert len(rows) == 1
        snap = rows[0]
        assert snap.tenant_id == "default"
        assert snap.tdb_code == "1234567"
        assert snap.ews == 62.5
        assert snap.fsa_kanji == FsaClass.YOCHUISAKI.kanji
        assert snap.ews_series == "4000000,2000000,-1000000"
        assert snap.updated_at != ""

    def test_tenant_isolation(self) -> None:
        store = InMemoryPortfolioStore()
        record_snapshot(state=_state(), settings=_settings(), store=store, tenant_id="bank-a")
        assert len(store.read("bank-a")) == 1
        assert store.read("bank-b") == []


class TestUpsertSemantics:
    def test_re_record_replaces_not_appends(self) -> None:
        store = InMemoryPortfolioStore()
        record_snapshot(state=_state(ews_score=62.5), settings=_settings(), store=store)
        # Same borrower, newer assessment -> replaces the single row.
        record_snapshot(state=_state(ews_score=70.0), settings=_settings(), store=store)

        rows = store.read("default")
        assert len(rows) == 1
        assert rows[0].ews == 70.0


class TestBuildEwsSeries:
    def test_series_is_deterministic_from_trial_balances(self) -> None:
        assert build_ews_series(_state()) == "4000000,2000000,-1000000"

    def test_empty_history_yields_empty_series(self) -> None:
        assert build_ews_series(_state(shisanhyo=[])) == ""


def _loan_log(*statuses: LoanStatus) -> list[dict[str, object]]:
    """Build a JSON-safe loan-event log from a status sequence."""
    at = dt.datetime(2025, 4, 1, 9, 0, 0, tzinfo=dt.UTC)
    return [LoanEvent(status=s, at=at, actor="system").model_dump(mode="json") for s in statuses]


class TestLoanStatusKanji:
    """loan_status_kanji derives the current lifecycle label from the log."""

    def test_empty_log_yields_empty_label(self) -> None:
        assert loan_status_kanji(_state(loan_events=[])) == ""

    def test_current_status_is_the_last_event(self) -> None:
        state = _state(
            loan_id="L-1",
            loan_events=_loan_log(
                LoanStatus.APPLIED,
                LoanStatus.UNDER_REVIEW,
                LoanStatus.APPROVED,
                LoanStatus.DISBURSED,
            ),
        )
        assert loan_status_kanji(state) == LoanStatus.DISBURSED.kanji  # 実行

    def test_malformed_log_yields_empty_label(self) -> None:
        # A non-LoanEvent entry must never raise -- display derivation is safe.
        assert loan_status_kanji(_state(loan_events=[{"bogus": 1}])) == ""


class TestRecordSnapshotEnrichesLoanStatus:
    """The assessment snapshot now carries the facility's lifecycle status."""

    def test_snapshot_carries_loan_status(self) -> None:
        store = InMemoryPortfolioStore()
        state = _state(
            loan_id="L-1",
            loan_events=_loan_log(
                LoanStatus.APPLIED,
                LoanStatus.UNDER_REVIEW,
                LoanStatus.APPROVED,
                LoanStatus.DISBURSED,
                LoanStatus.PERFORMING,
            ),
        )
        record_snapshot(state=state, settings=_settings(), store=store)
        rows = store.read("default")
        assert len(rows) == 1
        assert rows[0].loan_status == LoanStatus.PERFORMING.kanji  # 正常

    def test_snapshot_loan_status_empty_without_facility(self) -> None:
        store = InMemoryPortfolioStore()
        record_snapshot(state=_state(), settings=_settings(), store=store)
        assert store.read("default")[0].loan_status == ""


class TestRecordOriginationSnapshot:
    """An originated facility lands in the SAME book carrying its status."""

    def test_originated_facility_is_recorded_with_status(self) -> None:
        store = InMemoryPortfolioStore()
        state = _state(
            loan_id="L-1",
            loan_events=_loan_log(
                LoanStatus.APPLIED,
                LoanStatus.UNDER_REVIEW,
                LoanStatus.APPROVED,
                LoanStatus.DISBURSED,
            ),
        )
        record_origination_snapshot(state=state, settings=_settings(), store=store)
        rows = store.read("default")
        assert len(rows) == 1
        snap = rows[0]
        assert snap.tdb_code == "1234567"
        assert snap.loan_status == LoanStatus.DISBURSED.kanji  # 実行
        # An applicant has no distress assessment yet: EWS / FSA stay empty.
        assert snap.ews == 0.0
        assert snap.fsa_kanji == ""
        assert snap.ews_series == ""

    def test_origination_then_assessment_share_one_row(self) -> None:
        """Origination + a later assessment upsert the SAME borrower row.

        The unified-lifecycle payoff: a facility recorded at origination (実行)
        and the same borrower later assessed (要注意先 / a distress EWS) are ONE
        row in the book, not two -- the assessment upsert replaces the
        origination snapshot for that borrower.
        """
        store = InMemoryPortfolioStore()
        originated = _state(
            loan_id="L-1",
            loan_events=_loan_log(
                LoanStatus.APPLIED,
                LoanStatus.UNDER_REVIEW,
                LoanStatus.APPROVED,
                LoanStatus.DISBURSED,
            ),
        )
        record_origination_snapshot(state=originated, settings=_settings(), store=store)
        # Later: the same borrower is assessed (distress EWS + FSA class).
        record_snapshot(state=_state(), settings=_settings(), store=store)

        rows = store.read("default")
        assert len(rows) == 1, "origination + assessment must be one row"
        assert rows[0].ews == 62.5  # the assessment figures now populate it

    def test_offline_null_store_is_noop(self) -> None:
        store = NullPortfolioStore()
        record_origination_snapshot(
            state=_state(loan_id="L-1", loan_events=_loan_log(LoanStatus.APPLIED)),
            settings=_settings(),
            store=store,
        )
        assert store.read("default") == []

    def test_never_fatal_on_store_failure(self) -> None:
        # Must not raise even though the store's upsert blows up.
        record_origination_snapshot(
            state=_state(loan_id="L-1", loan_events=_loan_log(LoanStatus.APPLIED)),
            settings=_settings(),
            store=_BoomStore(),
        )


class TestProductionGuard:
    """The auth guard flows through the identity seam (require_persistable).

    It fires only on the normal path where the tenant is RESOLVED from identity
    (no explicit tenant_id). Passing an explicit tenant_id is the
    already-established-tenant path and deliberately bypasses the guard.
    """

    def test_auth_required_with_placeholder_identity_skips_persistence(self) -> None:
        store = InMemoryPortfolioStore()
        # No explicit tenant_id -> resolve_identity returns the placeholder
        # (authenticated=False) -> require_persistable raises under auth_required
        # -> the best-effort guard swallows it and nothing is written.
        record_snapshot(
            state=_state(),
            settings=_settings(auth_required=True, portfolio_tenant_default="default"),
            store=store,
        )
        assert store.read("default") == []

    def test_auth_required_with_explicit_tenant_persists(self) -> None:
        store = InMemoryPortfolioStore()
        record_snapshot(
            state=_state(),
            settings=_settings(auth_required=True),
            store=store,
            tenant_id="bank-a",
        )
        assert len(store.read("bank-a")) == 1

    def test_default_posture_persists_under_placeholder(self) -> None:
        # auth_required=False (the demo default) -> placeholder identity is
        # permitted and the snapshot lands under the resolved 'default' tenant.
        store = InMemoryPortfolioStore()
        record_snapshot(state=_state(), settings=_settings(), store=store)
        assert len(store.read("default")) == 1

"""End-to-end verifier for the origination run/decision HTTP API.

The origination counterpart to ``tests/test_api_runs.py``: it exercises the
*real* FastAPI surface (``app.app.create_app``) via Starlette's ``TestClient``,
against the *real* origination graph (``app.backend.graph_origination``), in
fully OFFLINE, SYNCHRONOUS mode. Nothing is mocked except the runtime
configuration:

* ``use_mocks=True``            -> the deterministic mock data engine (no network).
* ``persist_checkpoints=False`` -> the process-wide ``MemorySaver`` singleton, so
  a run started by one request is readable / resumable by the next (the
  start -> get -> decision durability contract the API promises).
* ``run_async=False``           -> the graph runs to the 稟議 interrupt (or to a
  terminal state) before responding, so the snapshot is returned inline.
* ``auth_required=False``       -> the placeholder identity is permitted.

The Settings are forced offline exactly as the assessment-surface test does, by
constructing one cached ``Settings`` instance and patching ``get_settings`` at
every name bound at import time (``graph``, ``identity``, and the canonical
``settings`` module), so the checkpointer selection, the 稟議 ``interrupt()``,
and the identity seam all run their real production code paths.

Origination authority boundary under test
-----------------------------------------
The HTTP ``start`` payload carries only the ``tdb_code``. The origination intake
node now resolves the applicant on the shared data seam (``provider.credit_report``
/ ``provider.shisanhyo``), so the *deterministic* 稟議 recommendation reflects the
applicant's real TDB score: a sub-floor applicant is recommended ``decline``, a
creditworthy one ``approve`` with a provisional facility ceiling. The
recommendation remains ADVISORY ONLY and grounded by construction; the banker is
the sole decider, so the approve/decline lifecycle is still driven by the API
decision, not the recommendation. The tests pin BOTH the applicant-specific
recommendation AND the decision-driven lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import app.backend.graph as graph_module
import app.backend.identity as identity_module
import app.shared.settings as settings_module
import pytest
from app.app import create_app
from app.shared.models.loan import LoanEvent, LoanStatus, current_status
from app.shared.settings import Settings
from fastapi.testclient import TestClient

# --- Applicant fixtures (deterministic mock TDB codes) ----------------------

#: A deteriorating SME (aichi_manufacturer): TDB score 41, below the origination
#: approve floor (60) -> the deterministic 稟議 recommendation is ``decline``.
#: Anti-social check is clear, so the decline is purely the sub-floor score.
SUBFLOOR_CODE = "1234567"

#: A healthy service company (normal_service_co): TDB score 75, at/above the
#: approve floor, anti-social clear -> the recommendation is ``approve`` with a
#: provisional facility ceiling (it carries sales).
CREDITWORTHY_CODE = "2000001"

#: A thin-margin wholesaler (thin_margin_trading_co): TDB score 65 (clears the
#: approve floor) but razor-thin ordinary profit. The size-anchored facility
#: ceiling (a flat sales multiple) far exceeds what the firm's demonstrated
#: 経常利益 can service, so the advisory debt-capacity band is ``over_capacity``
#: even though the credit recommendation is APPROVE -- the exact case where the
#: !1 check warns the banker that a creditworthy-by-score applicant is being
#: offered more than its P&L can carry.
THIN_MARGIN_CODE = "6000001"

#: Default applicant used where the recommendation direction is irrelevant
#: (lifecycle / validation / idempotency tests). The sub-floor code is fine: the
#: banker's decision drives the lifecycle regardless of the recommendation.
APPLICANT_CODE = SUBFLOOR_CODE


# ---------------------------------------------------------------------------
# Offline-settings plumbing (mirrors tests/test_api_runs.py)
# ---------------------------------------------------------------------------


def _offline_settings(**overrides: object) -> Settings:
    """Build a Settings instance forced fully offline + synchronous.

    ``use_mocks`` + ``persist_checkpoints=False`` give the deterministic engine
    and the shared in-process MemorySaver (state durable across TestClient
    requests). ``run_async=False`` keeps the graph on the request path so the
    snapshot is returned inline. ``auth_required=False`` accepts the placeholder
    identity.
    """
    base: dict[str, object] = {
        "use_mocks": True,
        "persist_checkpoints": False,
        "run_async": False,
        "auth_required": False,
        # Belt-and-braces: keep every live integration unconfigured so a stray
        # client can never reach out over the network from within a test.
        "llm_api_key": "",
        "tdb_api_key": "",
        "audit_dsn": "",
        "portfolio_dsn": "",
        "trajectory_dsn": "",
        "langsmith_tracing": False,
        "ui_meeting_pace_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _install_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Patch ``get_settings`` to return ``settings`` at every bound call site.

    ``app.backend.graph`` and ``app.backend.identity`` import ``get_settings``
    by name (a bound reference captured at import); the API modules call it
    through them. Patch all three references plus clear the canonical lru_cache
    so nothing falls through to a real environment-derived Settings. (The
    origination router reads settings via ``settings_module.get_settings``, so
    patching the canonical name covers it.)
    """
    settings_module.get_settings.cache_clear()
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    monkeypatch.setattr(identity_module, "get_settings", lambda: settings)


def _reset_memory_saver() -> None:
    """Drop the process-wide MemorySaver so each test starts clean.

    Uses the public :func:`graph.reset_memory_saver` seam so the test never
    depends on a module-private global. Keeps each test isolated while still
    letting start -> get -> decision share state WITHIN a test.
    """
    graph_module.reset_memory_saver()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient on a fresh offline app with an isolated in-memory store."""
    _install_settings(monkeypatch, _offline_settings())
    _reset_memory_saver()
    with TestClient(create_app()) as test_client:
        yield test_client
    _reset_memory_saver()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_loan_status(values: dict[str, Any]) -> LoanStatus:
    """Derive the facility's current status from a snapshot's loan_events."""
    events = [LoanEvent.model_validate(e) for e in values["loan_events"]]
    return current_status(events)


def _start(client: TestClient, thread_id: str) -> dict[str, Any]:
    """Start an origination run and return the response body (asserts 200)."""
    resp = client.post(
        "/api/v1/origination",
        json={"tdb_code": APPLICANT_CODE, "thread_id": thread_id},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


# ---------------------------------------------------------------------------
# Happy path: start -> pause at 稟議 -> decision -> terminal lifecycle
# ---------------------------------------------------------------------------


class TestOriginationLifecycle:
    """start (pause at 稟議) -> decision(approve|decline) -> terminal status."""

    def test_start_pauses_at_the_credit_decision(self, client: TestClient) -> None:
        """A start pauses at the 稟議 interrupt awaiting the credit decision.

        The grounded advisory recommendation is on state and, up to the pause,
        the facility is UNDER_REVIEW (the credit decision is HITL-gated). With
        the applicant resolved at intake, a sub-floor applicant is recommended
        DECLINE -- a real, applicant-specific recommendation, not an artifact of
        an empty state.
        """
        body = _start(client, "orig-pause")
        assert body["thread_id"] == "orig-pause"
        assert body["awaiting_decision"] is True
        assert body["phase"] == "awaiting_decision"
        # The advisory recommendation the banker reviews is on the snapshot,
        # grounded, and reflects the resolved applicant (sub-floor -> decline).
        rec = body["values"]["origination_recommendation"]
        assert rec is not None
        assert rec["recommendation"] == "decline"
        assert rec["grounded"] is True
        assert rec["max_facility_amount"] == 0  # no ceiling on a decline
        # The applicant was resolved at intake (profile + score on state).
        assert body["values"]["company_profile"] is not None
        assert body["values"]["tdb_score"] == 41
        # The credit decision is gated: the facility sits at UNDER_REVIEW.
        assert _current_loan_status(body["values"]) is LoanStatus.UNDER_REVIEW
        # No decision has been recorded yet. The field is only written by the
        # HITL node on resume, so up to the pause it is absent from the snapshot
        # values entirely (a never-written field is not serialised) -- which is
        # exactly "no decision yet". Assert absent-or-None to cover both.
        assert body["values"].get("origination_decision") is None

    def test_creditworthy_applicant_is_recommended_approve_with_ceiling(
        self, client: TestClient
    ) -> None:
        """A creditworthy applicant resolves to an APPROVE rec + a facility ceiling.

        The complement of the sub-floor case: intake loads a score at/above the
        approve floor and the applicant's sales, so the deterministic 稟議
        recommendation is APPROVE with a provisional 融資上限 > 0 -- the data-load
        producing a real, positive recommendation over the service surface.
        """
        resp = client.post(
            "/api/v1/origination",
            json={"tdb_code": CREDITWORTHY_CODE, "thread_id": "orig-approve-rec"},
        )
        assert resp.status_code == 200, resp.text
        rec = resp.json()["values"]["origination_recommendation"]
        assert rec["recommendation"] == "approve"
        assert rec["grounded"] is True
        assert rec["max_facility_amount"] > 0

    def test_run_is_durable_across_requests(self, client: TestClient) -> None:
        """GET reads the SAME paused run a previous POST started (shared store)."""
        _start(client, "orig-durable")
        resp = client.get("/api/v1/origination/orig-durable")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"] == "orig-durable"
        assert body["awaiting_decision"] is True
        assert body["phase"] == "awaiting_decision"
        assert _current_loan_status(body["values"]) is LoanStatus.UNDER_REVIEW

    def test_approve_decision_disburses(self, client: TestClient) -> None:
        """decision(approve) drives APPROVED -> DISBURSED and completes the run."""
        _start(client, "orig-approve")
        resp = client.post(
            "/api/v1/origination/orig-approve/decision",
            json={"decision": "approve"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["awaiting_decision"] is False
        assert body["phase"] == "done"
        assert body["values"]["origination_decision"] == "approve"
        # The banker's 承認 records APPROVED, then the deterministic disbursement
        # records the terminal-for-origination DISBURSED.
        assert _current_loan_status(body["values"]) is LoanStatus.DISBURSED

    def test_decline_decision_is_terminal_and_never_disburses(self, client: TestClient) -> None:
        """decision(decline) records DECLINED (terminal); never disburses."""
        _start(client, "orig-decline")
        resp = client.post(
            "/api/v1/origination/orig-decline/decision",
            json={"decision": "decline"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["awaiting_decision"] is False
        assert body["phase"] == "done"
        assert body["values"]["origination_decision"] == "decline"
        statuses = [LoanEvent.model_validate(e).status for e in body["values"]["loan_events"]]
        assert _current_loan_status(body["values"]) is LoanStatus.DECLINED
        assert LoanStatus.DISBURSED not in statuses

    def test_idempotent_start_does_not_start_a_second_run(self, client: TestClient) -> None:
        """A second POST with the same thread_id returns the existing snapshot.

        Idempotency is keyed by the caller-supplied thread_id: once a run exists
        for it, a repeat start must NOT re-run the graph; it returns what the
        thread already has (the same paused run, same recommendation).
        """
        first = _start(client, "orig-idem")
        second = client.post(
            "/api/v1/origination",
            json={"tdb_code": APPLICANT_CODE, "thread_id": "orig-idem"},
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["thread_id"] == "orig-idem"
        assert body["awaiting_decision"] is True
        # Same run: identical recommendation and a single APPLIED..UNDER_REVIEW
        # event chain (no second run appended duplicate transitions).
        assert (
            body["values"]["origination_recommendation"]
            == first["values"]["origination_recommendation"]
        )
        assert body["values"]["loan_events"] == first["values"]["loan_events"]
        assert _current_loan_status(body["values"]) is LoanStatus.UNDER_REVIEW

    def test_server_generates_thread_id_when_omitted(self, client: TestClient) -> None:
        """Omitting thread_id has the server mint one (still a valid paused run)."""
        resp = client.post("/api/v1/origination", json={"tdb_code": APPLICANT_CODE})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"]  # a generated id is present
        assert body["awaiting_decision"] is True


# ---------------------------------------------------------------------------
# Debt-service-capacity annotation (!1) survives the real round-trip
# ---------------------------------------------------------------------------


class TestDebtCapacityAnnotation:
    """The advisory debt_capacity block reaches the API snapshot end to end.

    !1 added a deterministic debt-service-capacity check to
    ``loan_origination_node`` -- it annotates ``origination_recommendation``
    with the proposed ceiling's implied annual debt service vs a prudent
    fraction of the firm's demonstrated ordinary profit (経常利益), banded
    within_capacity / stretch / over_capacity. The frontend view-mapping is
    unit-tested directly (tests/test_origination_capacity_view.py); THIS test
    proves the block survives the surface the UI / API actually traverses: the
    real origination graph -> checkpointer -> snapshot -> HTTP response. It is
    the one place the live graph->snapshot round-trip is asserted.

    The check is ADVISORY ONLY: these assertions confirm the block is present
    and well-formed; they must NOT couple it to the credit decision (which stays
    HITL-gated and score-driven).
    """

    def test_block_is_present_and_well_formed_for_a_creditworthy_applicant(
        self, client: TestClient
    ) -> None:
        """A creditworthy APPROVE carries a complete, banded debt_capacity block.

        The block is nested on ``origination_recommendation`` and is passed
        through the API verbatim (no API-side reshaping), so its presence here
        proves the deterministic node figure travelled the full graph->snapshot
        ->HTTP path intact.
        """
        resp = client.post(
            "/api/v1/origination",
            json={"tdb_code": CREDITWORTHY_CODE, "thread_id": "orig-capacity"},
        )
        assert resp.status_code == 200, resp.text
        rec = resp.json()["values"]["origination_recommendation"]
        # APPROVE carries a positive ceiling, so the capacity check has a real
        # facility to assess.
        assert rec["recommendation"] == "approve"
        assert rec["max_facility_amount"] > 0

        cap = rec["debt_capacity"]
        assert cap["band"] in {"within_capacity", "stretch", "over_capacity"}
        # Both legs of the implied service and the prudent ceiling are present,
        # well-typed, and non-negative -- the deterministic figures, intact.
        assert isinstance(cap["annual_debt_service"], int)
        assert isinstance(cap["prudent_service_ceiling"], int)
        assert cap["annual_debt_service"] >= 0
        assert cap["prudent_service_ceiling"] >= 0
        assert isinstance(cap["reason"], str) and cap["reason"]
        # 経常利益 is the denominator the whole check rests on; it must be carried.
        assert "annual_ordinary_profit" in cap

    def test_healthy_service_co_is_within_capacity(self, client: TestClient) -> None:
        """The 2000001 fixture's figures band the ceiling as within_capacity.

        Hand-derived from app/backend/tools/fixtures/normal_service_co.json
        (flat 12 months): sales 100,000,000/mo -> annual 1,200,000,000;
        ceiling = 1,200,000,000 * 0.5 = 600,000,000; implied service =
        600,000,000/5 + 600,000,000*0.05 = 120,000,000 + 30,000,000 =
        150,000,000. Ordinary profit = 100,000,000 - 1,000,000 - 10,000,000 =
        89,000,000/mo -> annual 1,068,000,000; prudent ceiling = 534,000,000.
        150,000,000 <= 534,000,000 -> within_capacity. A healthy applicant is
        exactly the case where the band reassures rather than warns.
        """
        resp = client.post(
            "/api/v1/origination",
            json={"tdb_code": CREDITWORTHY_CODE, "thread_id": "orig-within"},
        )
        assert resp.status_code == 200, resp.text
        cap = resp.json()["values"]["origination_recommendation"]["debt_capacity"]
        assert cap["band"] == "within_capacity"
        assert cap["annual_debt_service"] == 150_000_000
        assert cap["prudent_service_ceiling"] == 534_000_000
        assert cap["annual_ordinary_profit"] == 1_068_000_000

    def test_block_survives_through_the_approve_decision(self, client: TestClient) -> None:
        """The annotation persists on the snapshot after the banker decides.

        ``loan_origination_node`` runs once (before the 稟議 pause), so the block
        is written into the durable snapshot and must still be readable after
        the HITL resume drives the run to its terminal state -- confirming it
        rides the checkpointer, not just the start response.
        """
        client.post(
            "/api/v1/origination",
            json={"tdb_code": CREDITWORTHY_CODE, "thread_id": "orig-cap-persist"},
        )
        resp = client.post(
            "/api/v1/origination/orig-cap-persist/decision",
            json={"decision": "approve"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["phase"] == "done"
        cap = body["values"]["origination_recommendation"]["debt_capacity"]
        assert cap["band"] == "within_capacity"

    def test_thin_margin_applicant_is_over_capacity_despite_approve(
        self, client: TestClient
    ) -> None:
        """A large-but-thin applicant APPROVES on score yet bands over_capacity.

        The product's core warning, end to end. Hand-derived from
        app/backend/tools/fixtures/thin_margin_trading_co.json (flat 12 months):
        sales 200,000,000/mo -> annual 2,400,000,000; ceiling =
        2,400,000,000 * 0.5 = 1,200,000,000; implied service =
        1,200,000,000/5 + 1,200,000,000*0.05 = 240,000,000 + 60,000,000 =
        300,000,000. Ordinary profit = 200,000,000 - 180,000,000 - 19,500,000 =
        500,000/mo -> annual 6,000,000; prudent ceiling = 3,000,000.
        300,000,000 > 3,000,000 * 1.5 -> over_capacity (ratio 100x). The credit
        recommendation is still APPROVE (TDB 65 clears the floor): the band is
        ADVISORY and independent of the decision, surfacing that a
        creditworthy-by-score applicant is being offered far more than its P&L
        can service.
        """
        resp = client.post(
            "/api/v1/origination",
            json={"tdb_code": THIN_MARGIN_CODE, "thread_id": "orig-over-cap"},
        )
        assert resp.status_code == 200, resp.text
        rec = resp.json()["values"]["origination_recommendation"]
        # APPROVE on score, with a positive (size-anchored) ceiling.
        assert rec["recommendation"] == "approve"
        assert rec["max_facility_amount"] == 1_200_000_000
        # ...yet the advisory capacity check flags it as over-sized.
        cap = rec["debt_capacity"]
        assert cap["band"] == "over_capacity"
        assert cap["annual_debt_service"] == 300_000_000
        assert cap["prudent_service_ceiling"] == 3_000_000
        assert cap["annual_ordinary_profit"] == 6_000_000
        # The reason names the over-capacity verdict for the banker.
        assert "余力超過" in cap["reason"]

    def test_decline_carries_a_zero_facility_within_capacity_block(
        self, client: TestClient
    ) -> None:
        """A sub-floor DECLINE has a 0 ceiling -> no debt service -> within_capacity.

        The honest degenerate case: a declined applicant is recommended no
        facility, so the implied debt service is 0 and the band is
        within_capacity (0 service can never exceed capacity). This pins the
        ADVISORY-ONLY contract: the capacity band is independent of the credit
        decision and never itself drives the decline.
        """
        resp = client.post(
            "/api/v1/origination",
            json={"tdb_code": SUBFLOOR_CODE, "thread_id": "orig-cap-decline"},
        )
        assert resp.status_code == 200, resp.text
        rec = resp.json()["values"]["origination_recommendation"]
        assert rec["recommendation"] == "decline"
        assert rec["max_facility_amount"] == 0
        cap = rec["debt_capacity"]
        assert cap["band"] == "within_capacity"
        assert cap["annual_debt_service"] == 0


# ---------------------------------------------------------------------------
# Collateral / guarantee coverage annotation (breadth #6) end to end
# ---------------------------------------------------------------------------


class TestCoverageAnnotation:
    """The advisory coverage block reaches the API snapshot end to end.

    The breadth twin of TestDebtCapacityAnnotation. The coverage check (the
    balance-sheet lens: secured + guaranteed value vs the proposed facility) is
    computed by ``loan_origination_node`` and nested on
    ``origination_recommendation``; the optional 担保 / 保証 figures ride the
    HTTP start body straight into the graph invoke. These tests prove the band
    moves correctly across the REAL graph -> checkpointer -> snapshot -> HTTP
    path for each band, and that it stays ADVISORY (independent of the credit
    decision and of the recommended ceiling).

    The bands are hand-derived from the creditworthy applicant's ceiling:
    normal_service_co (2000001) APPROVES with a 600,000,000 facility (sales
    100,000,000/mo -> annual 1,200,000,000; ceiling = annual * 0.5). Coverage
    floors: well_covered at ratio >= 1.0, partial at >= 0.5, uncovered below.
    """

    _FACILITY = 600_000_000  # the 2000001 APPROVE ceiling (hand-derived)

    def test_no_coverage_data_bands_uncovered(self, client: TestClient) -> None:
        """With no 担保/保証 supplied, a positive facility bands 'uncovered'.

        The prudent-banker default end to end: unknown coverage -> 0 -> the
        whole facility is the clean-risk tail. This is the band every real run
        sees until a banker supplies coverage.
        """
        resp = client.post(
            "/api/v1/origination",
            json={"tdb_code": CREDITWORTHY_CODE, "thread_id": "orig-cov-none"},
        )
        assert resp.status_code == 200, resp.text
        rec = resp.json()["values"]["origination_recommendation"]
        assert rec["recommendation"] == "approve"
        cov = rec["coverage"]
        assert cov["band"] == "uncovered"
        assert cov["covered_amount"] == 0
        assert cov["uncovered_amount"] == self._FACILITY

    def test_full_collateral_bands_well_covered(self, client: TestClient) -> None:
        """Collateral >= the facility bands 'well_covered' with a 0 uncovered tail."""
        resp = client.post(
            "/api/v1/origination",
            json={
                "tdb_code": CREDITWORTHY_CODE,
                "thread_id": "orig-cov-full",
                "collateral_value": self._FACILITY,
            },
        )
        assert resp.status_code == 200, resp.text
        cov = resp.json()["values"]["origination_recommendation"]["coverage"]
        assert cov["band"] == "well_covered"
        assert cov["covered_amount"] == self._FACILITY
        assert cov["uncovered_amount"] == 0

    def test_collateral_plus_guarantee_sum_into_coverage(self, client: TestClient) -> None:
        """Both legs (担保 + 保証) sum into the covered amount; a half-cover -> partial.

        Supplies collateral + guarantee that together cover exactly half the
        facility (300,000,000 of 600,000,000) -> ratio 0.5 -> 'partial', with the
        other half as the uncovered tail. Proves both HTTP fields travel into the
        graph and the node sums them.
        """
        half = self._FACILITY // 2  # 300,000,000
        resp = client.post(
            "/api/v1/origination",
            json={
                "tdb_code": CREDITWORTHY_CODE,
                "thread_id": "orig-cov-partial",
                "collateral_value": half - 50_000_000,
                "guarantee_coverage": 50_000_000,
            },
        )
        assert resp.status_code == 200, resp.text
        cov = resp.json()["values"]["origination_recommendation"]["coverage"]
        assert cov["covered_amount"] == half
        assert cov["band"] == "partial"
        assert cov["uncovered_amount"] == self._FACILITY - half

    def test_coverage_survives_through_the_approve_decision(self, client: TestClient) -> None:
        """The coverage block persists on the snapshot after the banker decides.

        Like the debt-capacity block, coverage is written once (before the 稟議
        pause), so it must still be readable after the HITL resume drives the run
        to its terminal state -- confirming it rides the checkpointer.
        """
        client.post(
            "/api/v1/origination",
            json={
                "tdb_code": CREDITWORTHY_CODE,
                "thread_id": "orig-cov-persist",
                "collateral_value": self._FACILITY,
            },
        )
        resp = client.post(
            "/api/v1/origination/orig-cov-persist/decision",
            json={"decision": "approve"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["phase"] == "done"
        cov = body["values"]["origination_recommendation"]["coverage"]
        assert cov["band"] == "well_covered"

    def test_coverage_does_not_alter_the_recommended_ceiling(self, client: TestClient) -> None:
        """Supplying coverage is ADVISORY: the recommended facility is unchanged.

        The same applicant with and without coverage must produce the SAME
        max_facility_amount; coverage annotates, it never resizes the facility.
        """
        bare = client.post(
            "/api/v1/origination",
            json={"tdb_code": CREDITWORTHY_CODE, "thread_id": "orig-cov-bare"},
        ).json()["values"]["origination_recommendation"]["max_facility_amount"]
        with_cov = client.post(
            "/api/v1/origination",
            json={
                "tdb_code": CREDITWORTHY_CODE,
                "thread_id": "orig-cov-amt",
                "collateral_value": 999_000_000,
                "guarantee_coverage": 999_000_000,
            },
        ).json()["values"]["origination_recommendation"]["max_facility_amount"]
        assert bare == with_cov == self._FACILITY


# ---------------------------------------------------------------------------
# Validation / error surface
# ---------------------------------------------------------------------------


class TestValidationAndErrors:
    """Route guards: 422 bad code/decision, 404 unknown thread, 409 completed."""

    @pytest.mark.parametrize(
        "bad_code",
        ["123", "12345678", "abcdefg", "", "123456a"],
    )
    def test_start_rejects_malformed_tdb_code(self, client: TestClient, bad_code: str) -> None:
        resp = client.post("/api/v1/origination", json={"tdb_code": bad_code})
        assert resp.status_code == 422, resp.text

    def test_get_unknown_thread_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/origination/does-not-exist")
        assert resp.status_code == 404, resp.text

    def test_decision_unknown_thread_is_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/origination/does-not-exist/decision",
            json={"decision": "approve"},
        )
        assert resp.status_code == 404, resp.text

    def test_decision_invalid_value_is_422(self, client: TestClient) -> None:
        """An unknown decision is rejected before the run is even looked up.

        Only approve / decline are accepted by the origination surface; the
        turnaround vocabulary (revise / reject) is explicitly NOT valid here.
        """
        _start(client, "orig-baddecision")
        for invalid in ("maybe", "revise", "reject", ""):
            resp = client.post(
                "/api/v1/origination/orig-baddecision/decision",
                json={"decision": invalid},
            )
            assert resp.status_code == 422, resp.text

    def test_decision_after_completion_is_409(self, client: TestClient) -> None:
        """A completed (no longer paused) run has nothing to decide -> 409."""
        _start(client, "orig-409")
        first = client.post(
            "/api/v1/origination/orig-409/decision",
            json={"decision": "approve"},
        )
        assert first.status_code == 200, first.text
        # The run is now DISBURSED / done; a second decision is a conflict.
        second = client.post(
            "/api/v1/origination/orig-409/decision",
            json={"decision": "approve"},
        )
        assert second.status_code == 409, second.text

    def test_decline_then_decision_is_409(self, client: TestClient) -> None:
        """A declined (terminal) run is likewise no longer awaiting -> 409."""
        _start(client, "orig-decline-409")
        client.post(
            "/api/v1/origination/orig-decline-409/decision",
            json={"decision": "decline"},
        )
        resp = client.post(
            "/api/v1/origination/orig-decline-409/decision",
            json={"decision": "approve"},
        )
        assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# Auth guard seam: auth_required + placeholder identity -> 401
# ---------------------------------------------------------------------------


class TestAuthGuard:
    """With auth_required set, the placeholder identity is refused at the seam."""

    @pytest.fixture
    def guarded_client(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        _install_settings(monkeypatch, _offline_settings(auth_required=True))
        _reset_memory_saver()
        with TestClient(create_app(), raise_server_exceptions=False) as test_client:
            yield test_client
        _reset_memory_saver()

    def test_start_is_unauthorized_under_auth_required(self, guarded_client: TestClient) -> None:
        resp = guarded_client.post("/api/v1/origination", json={"tdb_code": APPLICANT_CODE})
        assert resp.status_code == 401, resp.text

    def test_get_is_unauthorized_under_auth_required(self, guarded_client: TestClient) -> None:
        resp = guarded_client.get("/api/v1/origination/anything")
        assert resp.status_code == 401, resp.text

    def test_decision_is_unauthorized_under_auth_required(self, guarded_client: TestClient) -> None:
        resp = guarded_client.post(
            "/api/v1/origination/anything/decision",
            json={"decision": "approve"},
        )
        assert resp.status_code == 401, resp.text

"""Tests for the loan-loss provision (貸倒引当金) line in the workout handoff.

The workout handoff surfaces a deterministic provision computed from outstanding
principal (sum of lender_stakes) and the FSA class via ``provision_amount``. For
a bankrupt borrower the reserve ratio is 1.0, so the provision equals the full
outstanding balance. The line is omitted gracefully when no outstanding balance
is known.
"""

from __future__ import annotations

from app.backend.nodes.workout import _outstanding_principal, workout_node
from app.backend.state import SaiseiState
from app.shared.models.classification import FsaClass
from app.shared.models.money import format_jpy


def _state(**kwargs: object) -> SaiseiState:
    base: dict[str, object] = {"tdb_code": "1234567"}
    base.update(kwargs)
    return SaiseiState(**base)


def test_outstanding_principal_sums_lender_stakes() -> None:
    state = _state(lender_stakes={"main_bank": 550_000_000, "sub_bank": 450_000_000})
    assert _outstanding_principal(state) == 1_000_000_000


def test_outstanding_principal_zero_without_stakes() -> None:
    assert _outstanding_principal(_state()) == 0


def test_bankrupt_provision_is_full_outstanding() -> None:
    # PROVISION_RATE_BANKRUPT = 1.0 -> provision equals full outstanding balance.
    state = _state(
        fsa_classification=FsaClass.HATANSAKI,
        lender_stakes={"main_bank": 550_000_000, "sub_bank": 450_000_000},
        net_worth=-5_000_000,
    )
    handoff = workout_node(state)["workout_handoff"]
    assert "貸倒引当金 (Loan-loss Provision)" in handoff
    # Full outstanding (1,000,000,000) is both the provision and the basis.
    assert format_jpy(1_000_000_000) in handoff


def test_de_facto_bankrupt_provision_is_full_outstanding() -> None:
    state = _state(
        fsa_classification=FsaClass.JISSHITSU_HATANSAKI,
        lender_stakes={"main_bank": 300_000_000},
        net_worth=-1,
    )
    handoff = workout_node(state)["workout_handoff"]
    assert format_jpy(300_000_000) in handoff


def test_provision_omitted_without_outstanding_balance() -> None:
    # No lender_stakes -> outstanding is 0 -> provision line is the omitted form.
    state = _state(fsa_classification=FsaClass.HATANSAKI, net_worth=-1)
    handoff = workout_node(state)["workout_handoff"]
    assert "貸倒引当金: 未評価 (残高未確認)" in handoff
    assert "Loan-loss Provision" not in handoff


def test_handoff_text_and_provision_coexist() -> None:
    state = _state(
        fsa_classification=FsaClass.HATANSAKI,
        lender_stakes={"main_bank": 100_000_000},
        net_worth=-1,
    )
    handoff = workout_node(state)["workout_handoff"]
    # The existing handoff content is unchanged alongside the new line.
    assert "WORKOUT HANDOFF" in handoff
    assert "債務者区分 (FSA Class)" in handoff
    assert "貸倒引当金 (Loan-loss Provision)" in handoff

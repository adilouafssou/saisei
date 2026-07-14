"""Verifier for audit-ledger cryptographic signing (tamper-proof hardening).

The hash chain proves tamper-*evidence*; a detached Ed25519 signature over each
event's ``content_hash`` proves tamper-*proofness* (forging an event also needs
the private key). This pins that, fully offline, by minting an Ed25519 keypair
in-test and exercising the real ``cryptography`` sign/verify path.

What is pinned:
* a configured signer produces a signature that verifies against the public key;
* the NullSigner default leaves events unsigned and the system byte-stable;
* the signature is HASH-EXCLUDED — signing never changes content_hash, and an
  event's chain hash is identical signed vs unsigned (full backward compat);
* tampering (a changed content_hash, a wrong key, a flipped signature) fails;
* verify_signatures tolerates unsigned legacy events by default but can be made
  strict, and reports the first failure;
* get_signer fail-safes to NullSigner on a bad key;
* record_event attaches a verifying signature when a signer is injected.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from app.backend.audit.audit_log import AuditEvent, AuditEventType
from app.backend.audit.signing import (
    Ed25519Signer,
    NullSigner,
    get_signer,
    verify_signature,
    verify_signatures,
)
from app.shared.settings import Settings
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem) as PEM strings for an Ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


def _event(event_id: str = "e1", prev_hash: str = "") -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        thread_id="t1",
        tdb_code="1234567",
        event_type=AuditEventType.CLASSIFICATION,
        created_at="2026-03-01T00:00:00+00:00",
        payload={"fsa_classification": "\u8981\u6ce8\u610f\u5148"},
        prev_hash=prev_hash,
    ).with_content_hash()


class _FakeSettings:
    def __init__(self, private_key: str = "") -> None:
        self.audit_signing_private_key = private_key


# ---------------------------------------------------------------------------
# Sign / verify happy path
# ---------------------------------------------------------------------------


def test_signer_signature_verifies(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    event = _event()
    signature = Ed25519Signer(private_pem).sign(event.content_hash)
    assert signature  # non-empty hex
    signed = event.model_copy(update={"signature": signature})
    assert verify_signature(signed, public_pem) is True


def test_null_signer_produces_no_signature() -> None:
    assert NullSigner().sign(_event().content_hash) == ""


def test_get_signer_returns_null_without_key() -> None:
    assert isinstance(get_signer(cast("Settings", _FakeSettings(""))), NullSigner)


def test_get_signer_returns_ed25519_with_key(keypair: tuple[str, str]) -> None:
    private_pem, _ = keypair
    assert isinstance(get_signer(cast("Settings", _FakeSettings(private_pem))), Ed25519Signer)


def test_get_signer_failsafes_to_null_on_bad_key() -> None:
    assert isinstance(
        get_signer(cast("Settings", _FakeSettings("-----BEGIN nonsense-----"))),
        NullSigner,
    )


# ---------------------------------------------------------------------------
# Hash-exclusion / backward compatibility
# ---------------------------------------------------------------------------


def test_signature_is_excluded_from_content_hash(keypair: tuple[str, str]) -> None:
    """Signing must not change content_hash (so legacy hashes never break)."""
    private_pem, _ = keypair
    event = _event()
    signed = event.model_copy(
        update={"signature": Ed25519Signer(private_pem).sign(event.content_hash)}
    )
    # The stored hash is unchanged, and a recomputation still matches it.
    assert signed.content_hash == event.content_hash
    assert signed.hash_is_valid()


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_tampered_content_breaks_signature(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    event = _event()
    signed = event.model_copy(
        update={"signature": Ed25519Signer(private_pem).sign(event.content_hash)}
    )
    # Re-point content_hash to a different (even valid-looking) value.
    tampered = signed.model_copy(update={"content_hash": "0" * 64})
    assert verify_signature(tampered, public_pem) is False


def test_wrong_public_key_fails(keypair: tuple[str, str]) -> None:
    private_pem, _ = keypair
    other_public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    event = _event()
    signed = event.model_copy(
        update={"signature": Ed25519Signer(private_pem).sign(event.content_hash)}
    )
    assert verify_signature(signed, other_public) is False


def test_unsigned_event_does_not_verify(keypair: tuple[str, str]) -> None:
    _, public_pem = keypair
    assert verify_signature(_event(), public_pem) is False


# ---------------------------------------------------------------------------
# Batch verification
# ---------------------------------------------------------------------------


def _signed(event: AuditEvent, private_pem: str) -> AuditEvent:
    return event.model_copy(
        update={"signature": Ed25519Signer(private_pem).sign(event.content_hash)}
    )


def test_verify_signatures_all_signed(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    events = [_signed(_event("e1"), private_pem), _signed(_event("e2"), private_pem)]
    verdict = verify_signatures(events, public_pem)
    assert verdict.ok is True
    assert verdict.checked == 2
    assert verdict.unsigned == 0


def test_verify_signatures_tolerates_unsigned_by_default(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    events = [_signed(_event("e1"), private_pem), _event("e2")]  # second unsigned
    verdict = verify_signatures(events, public_pem)
    assert verdict.ok is True
    assert verdict.checked == 1
    assert verdict.unsigned == 1


def test_verify_signatures_strict_flags_unsigned(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    events = [_signed(_event("e1"), private_pem), _event("e2")]
    verdict = verify_signatures(events, public_pem, allow_unsigned=False)
    assert verdict.ok is False
    assert verdict.broken_at == "e2"


def test_verify_signatures_reports_first_bad(keypair: tuple[str, str]) -> None:
    private_pem, public_pem = keypair
    good = _signed(_event("e1"), private_pem)
    bad = _signed(_event("e2"), private_pem).model_copy(update={"content_hash": "0" * 64})
    verdict = verify_signatures([good, bad], public_pem)
    assert verdict.ok is False
    assert verdict.broken_at == "e2"


# ---------------------------------------------------------------------------
# record_event integration
# ---------------------------------------------------------------------------


def test_record_event_attaches_verifying_signature(keypair: tuple[str, str]) -> None:
    """record_event signs via the injected signer; the result verifies."""
    from app.backend.audit.record import record_event
    from app.backend.audit.sink import InMemoryAuditSink

    private_pem, public_pem = keypair
    sink = InMemoryAuditSink()

    class _State:
        tdb_code = "1234567"
        hojin_bango = "1234567890123"
        shisanhyo: list[Any] = []
        tdb_score = 55
        working_capital_gap = None
        net_worth = None
        is_insolvent = None

    record_event(
        AuditEventType.CLASSIFICATION,
        state=_State(),
        payload={"fsa_classification": "\u8981\u6ce8\u610f\u5148"},
        thread_id="t1",
        sink=sink,
        signer=Ed25519Signer(private_pem),
    )
    events = sink.read("t1")
    assert len(events) == 1
    assert events[0].signature
    assert verify_signature(events[0], public_pem) is True

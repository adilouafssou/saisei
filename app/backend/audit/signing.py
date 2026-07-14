"""Cryptographic signing for the audit ledger (audit-ledger hardening).

The hash chain (``content_hash`` + ``prev_hash``) makes the ledger tamper-
*evident*: any retro-edit is detectable by re-deriving the chain. It is not
tamper-*proof* — someone who can rewrite the whole table can also recompute a
consistent chain. A DETACHED cryptographic signature over each event's
``content_hash`` closes that gap: forging or rewriting an event now also requires
the private signing key, which need never live in the database. An examiner with
only the PUBLIC key can then prove every event is authentic.

Design / safety posture
-----------------------
* **Ed25519** (via ``cryptography``, already present through ``pyjwt[crypto]``):
  small, fast, modern signatures. We sign the event's ``content_hash`` bytes —
  the hash already commits to every other field (incl. ``prev_hash``), so signing
  it transitively authenticates the whole event and the chain link.
* **Detached + hash-excluded.** The signature is stored in the ``signature``
  field, which is excluded from the content hash. So signing NEVER changes an
  event's identity, and an unsigned (legacy / offline) event verifies its chain
  exactly as before — adding signing is fully backward-compatible.
* **Offline / opt-in.** With no ``audit_signing_private_key`` configured the
  signer is :class:`NullSigner` (empty signature), so ``make verify`` / CI and
  the demo are unchanged and no key material is required.
* **Best-effort on write.** A signing failure logs and yields an empty signature
  rather than raising, mirroring the ledger's never-fatal write contract — a
  misconfigured key must not break the regulated workflow.

Key material is provided as PEM. Either the literal PEM text, or a secret
REFERENCE resolved through the secret-provider seam (``@/path``, ``@file:/path``,
or ``@env:NAME``) so a deployment can mount the private key as a secret and pass
only its location -- and so a Vault / cloud secret manager drops in behind the
seam. The private key signs at write time; only the public key is needed to
verify, and it can be distributed to examiners freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.backend.audit.audit_log import AuditEvent
from app.backend.secrets import resolve_secret
from app.shared.logging import get_logger
from app.shared.settings import Settings, get_settings

__all__ = [
    "Signer",
    "NullSigner",
    "Ed25519Signer",
    "SignatureVerdict",
    "get_signer",
    "verify_signature",
    "verify_signatures",
    "signing_enabled",
]

_log = get_logger(__name__)


@runtime_checkable
class Signer(Protocol):
    """Seam producing a detached signature over an event's content hash."""

    def sign(self, content_hash: str) -> str:
        """Return a detached signature (hex) over ``content_hash``, or "" if none."""
        ...


class NullSigner:
    """Offline default: produces no signature.

    Keeps the ledger fully offline and byte-stable when no signing key is
    configured; signed verification simply treats these events as unsigned.
    """

    def sign(self, content_hash: str) -> str:  # noqa: D102 - see class doc
        return ""


@dataclass(frozen=True)
class SignatureVerdict:
    """Result of verifying the signatures across a list of events.

    Attributes:
        ok: True iff no event FAILED verification (see ``allow_unsigned``).
        checked: How many events carried a signature and were verified.
        unsigned: How many events had no signature.
        broken_at: ``event_id`` of the first event whose signature did not
            verify, or None.
        reason: Human-readable explanation (empty when ``ok``).
    """

    ok: bool
    checked: int = 0
    unsigned: int = 0
    broken_at: str | None = None
    reason: str = ""


def _load_pem(value: str) -> bytes:
    """Resolve a PEM value through the secret seam, returning the PEM bytes.

    ``value`` is either literal PEM text or a secret reference (``@/path``,
    ``@file:/path``, ``@env:NAME``); :func:`~app.backend.secrets.resolve_secret`
    dereferences a reference and passes literal PEM through unchanged, so a
    deployment can mount the key as a file / env secret or back it with Vault
    without changing this code.
    """
    return resolve_secret(value.strip()).encode("utf-8")


class Ed25519Signer:
    """Signs an event's content hash with an Ed25519 private key.

    Args:
        private_key_pem: The Ed25519 private key as PEM text, or ``@/path`` to a
            PEM file. Loaded once at construction.
    """

    def __init__(self, private_key_pem: str) -> None:
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
        )

        key = load_pem_private_key(_load_pem(private_key_pem), password=None)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("audit signing key must be an Ed25519 private key")
        self._key = key

    def sign(self, content_hash: str) -> str:
        """Return the hex Ed25519 signature over the content-hash bytes.

        Best-effort: a signing error logs and returns "" so the never-fatal
        audit-write contract holds (an unsigned event is still chain-valid).
        """
        if not content_hash:
            return ""
        try:
            return self._key.sign(content_hash.encode("utf-8")).hex()
        except Exception as exc:  # noqa: BLE001 - signing must never break the write
            _log.warning("audit.sign_failed", error=str(exc))
            return ""


def signing_enabled(settings: Settings | None = None) -> bool:
    """Return whether an audit signing private key is configured."""
    settings = settings or get_settings()
    return bool((getattr(settings, "audit_signing_private_key", "") or "").strip())


def get_signer(settings: Settings | None = None) -> Signer:
    """Return the configured signer (Ed25519 when a key is set, else Null).

    Fail-safe: if the key is set but cannot be loaded (bad PEM / wrong type),
    this logs and returns :class:`NullSigner` so a misconfiguration degrades to
    unsigned-but-working rather than breaking every audit write.
    """
    settings = settings or get_settings()
    pem = (getattr(settings, "audit_signing_private_key", "") or "").strip()
    if not pem:
        return NullSigner()
    # Resolve through the secret seam so a missing file/env reference degrades to
    # unsigned (NullSigner) rather than raising on every audit write.
    if not resolve_secret(pem):
        _log.warning("audit.signer_secret_unresolved")
        return NullSigner()
    try:
        return Ed25519Signer(pem)
    except Exception as exc:  # noqa: BLE001 - degrade to unsigned, never break
        _log.warning("audit.signer_init_failed", error=str(exc))
        return NullSigner()


def verify_signature(event: AuditEvent, public_key_pem: str) -> bool:
    """Return whether ``event.signature`` is a valid Ed25519 signature.

    Verifies the detached signature over ``event.content_hash`` against the
    provided public key (PEM text or ``@/path``). An event with an empty
    signature returns ``False`` (it is unsigned, not verified) — callers that
    want to permit unsigned legacy events should check ``event.signature`` first.

    Args:
        event: The signed event to verify.
        public_key_pem: The Ed25519 public key (PEM text, or ``@/path``).

    Returns:
        True iff the signature verifies against the content hash.
    """
    if not event.signature or not event.content_hash:
        return False
    try:
        from cryptography.hazmat.primitives.serialization import (
            load_pem_public_key,
        )

        public_key = load_pem_public_key(_load_pem(public_key_pem))
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        if not isinstance(public_key, Ed25519PublicKey):
            return False
        public_key.verify(bytes.fromhex(event.signature), event.content_hash.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 - any failure means "does not verify"
        return False


def verify_signatures(
    events: list[AuditEvent],
    public_key_pem: str,
    *,
    allow_unsigned: bool = True,
) -> SignatureVerdict:
    """Verify the detached signatures across an ordered list of events.

    For each event: an event WITH a signature must verify against the public
    key; an event WITHOUT one is counted as ``unsigned`` and tolerated when
    ``allow_unsigned`` is True (the default — so a ledger that pre-dates signing,
    or mixes signed and legacy events, is not reported as broken). The first
    event whose present signature fails is reported in ``broken_at``.

    Args:
        events: Events in write order (e.g. from ``AuditSink.read``).
        public_key_pem: The Ed25519 public key (PEM text or ``@/path``).
        allow_unsigned: When True, events with no signature are tolerated; when
            False, a missing signature is itself a failure.

    Returns:
        A :class:`SignatureVerdict`.
    """
    checked = 0
    unsigned = 0
    for event in events:
        if not event.signature:
            unsigned += 1
            if not allow_unsigned:
                return SignatureVerdict(
                    ok=False,
                    checked=checked,
                    unsigned=unsigned,
                    broken_at=event.event_id,
                    reason=f"event {event.event_id} is unsigned",
                )
            continue
        if not verify_signature(event, public_key_pem):
            return SignatureVerdict(
                ok=False,
                checked=checked,
                unsigned=unsigned,
                broken_at=event.event_id,
                reason=f"signature does not verify for {event.event_id}",
            )
        checked += 1
    return SignatureVerdict(ok=True, checked=checked, unsigned=unsigned)

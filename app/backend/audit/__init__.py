"""Immutable audit-log package (Feature 7).

An append-only, hash-chained, data-version-pinned ledger of every classification,
guarantee-release assessment, and human decision. See the rebuild spec at
``docs/en/specs/FEATURE7_AUDIT_LOG_SPEC.md``.

This package is a side-record: capturing an audit event NEVER changes a gate,
route, score, figure, or the deterministic verdict (mirroring observability /
LangSmith tracing). It is offline-safe and best-effort.
"""

from __future__ import annotations

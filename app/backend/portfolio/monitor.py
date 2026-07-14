"""Continuous book-monitoring planner (Feature 8.1 / V2 — scheduled ingest).

The Portfolio persistence seam (``store.py`` + ``recorder.py``) lets a bank keep
the book at rest. This module is the next, still-deterministic slice on top of
it: given the persisted snapshots, decide — purely — **which borrowers are due
for a refresh** and **which just crossed a deterioration threshold**, so a
scheduler can surface them to a banker *before* the borrower calls.

What this is, and what it deliberately is NOT
--------------------------------------------
- It IS pure, deterministic, offline planning over already-computed, already-
  persisted figures: an ordering + selection over snapshots. Same inputs ->
  same plan. No network, no LLM, no store writes here.
- It is NOT a daemon / cron / job runner (that is deployment infrastructure,
  not application logic) and it does NOT auto-run an assessment. Saisei's
  invariant holds: a re-assessment is always an explicit, auditable human
  action. This module only answers "who should the banker look at, and why?".
- It computes no verdict and no figure: a "crossing" is detected from the EWS
  the deterministic spine ALREADY produced and the authoritative
  ``EWS_SUBSTANDARD`` floor from ``shared.constants`` (no magic numbers).

This keeps the governance footprint honest: the heavy, deliberate decision
(holding the whole book at rest) lives in the opt-in persistence seam; this is
just the pure read-side planning that makes the held book useful.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.backend.portfolio.store import PortfolioSnapshot
from app.shared.constants import EWS_SUBSTANDARD

__all__ = [
    "RefreshItem",
    "CrossingAlert",
    "plan_refresh",
    "detect_crossings",
]

#: Default staleness horizon: a monthly-reporting cadence means a snapshot older
#: than ~31 days is due for a refresh. Callers may override per deployment.
_DEFAULT_MAX_AGE = dt.timedelta(days=31)


def _parse_iso(ts: str) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp string to an aware datetime, or None.

    Snapshots store ``updated_at`` as the ISO string the recorder wrote
    (``datetime.now(UTC).isoformat()``). A blank/garbled value yields ``None``
    so callers treat it as "age unknown" rather than crashing.
    """
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Normalise to aware UTC so age arithmetic is well-defined.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed


@dataclass(frozen=True)
class RefreshItem:
    """One borrower the scheduler should surface for re-assessment.

    Attributes:
        tdb_code: The borrower's TDB code (the per-tenant key).
        company_name: Display name (may be empty).
        age_days: Whole days since the last snapshot, or ``None`` when the
            snapshot has no parseable timestamp (still surfaced, age unknown).
        reason: ``"stale"`` (older than the horizon) or ``"unknown_age"``.
    """

    tdb_code: str
    company_name: str
    age_days: int | None
    reason: str


def plan_refresh(
    snapshots: list[PortfolioSnapshot],
    *,
    now: dt.datetime | None = None,
    max_age: dt.timedelta | None = None,
) -> list[RefreshItem]:
    """Return the borrowers due for a refresh, most-overdue first (pure).

    A borrower is "due" when its latest snapshot is older than ``max_age`` (or
    has no parseable timestamp — surfaced as ``unknown_age`` so a corrupt row is
    never silently skipped). The result is ordered most-overdue first, with
    unknown-age rows last, ties broken by ``tdb_code`` for byte-stable output.

    This decides WHO to surface; it triggers nothing. A banker still presses
    診断実行, so the re-assessment stays an explicit human action.

    Args:
        snapshots: The tenant's persisted snapshots (from ``store.read``).
        now: The reference time (defaults to ``datetime.now(UTC)``); injectable
            for deterministic tests.
        max_age: Staleness horizon (defaults to ~31 days, a monthly cadence).

    Returns:
        Ordered :class:`RefreshItem` list (empty when nothing is due).
    """
    now = now or dt.datetime.now(dt.UTC)
    horizon = max_age or _DEFAULT_MAX_AGE

    items: list[RefreshItem] = []
    for snap in snapshots:
        updated = _parse_iso(snap.updated_at)
        if updated is None:
            items.append(
                RefreshItem(
                    tdb_code=snap.tdb_code,
                    company_name=snap.company_name,
                    age_days=None,
                    reason="unknown_age",
                )
            )
            continue
        age = now - updated
        if age >= horizon:
            items.append(
                RefreshItem(
                    tdb_code=snap.tdb_code,
                    company_name=snap.company_name,
                    age_days=age.days,
                    reason="stale",
                )
            )

    # Most-overdue first; unknown-age (age_days None) sorted last; tdb_code tie-break.
    return sorted(
        items,
        key=lambda i: (
            0 if i.age_days is not None else 1,
            -(i.age_days or 0),
            i.tdb_code,
        ),
    )


@dataclass(frozen=True)
class CrossingAlert:
    """One borrower that crossed the 要注意 deterioration floor since last seen.

    Attributes:
        tdb_code: The borrower's TDB code.
        company_name: Display name (may be empty).
        prev_ews: The previous snapshot's EWS (below the floor).
        new_ews: The new snapshot's EWS (at/above the floor).
    """

    tdb_code: str
    company_name: str
    prev_ews: float
    new_ews: float


def detect_crossings(
    previous: list[PortfolioSnapshot],
    current: list[PortfolioSnapshot],
    *,
    floor: float | None = None,
) -> list[CrossingAlert]:
    """Return borrowers that crossed the 要注意 floor UPWARD since the last book.

    A crossing is a borrower whose EWS was BELOW the floor in ``previous`` and is
    AT/ABOVE it in ``current`` — the "just deteriorated past the line" event a
    banker must catch. Borrowers absent from ``previous`` are NOT treated as
    crossings (no prior baseline to cross from); they are simply new rows the
    watchlist already surfaces.

    Pure and deterministic: it compares the EWS the spine already computed
    against the authoritative ``EWS_SUBSTANDARD`` floor (overridable for tests).
    It detects nothing about WHY and decides no verdict — only the threshold
    event. Output is ordered by new EWS descending (worst first), ``tdb_code``
    tie-break.

    Args:
        previous: The prior persisted snapshots (the baseline book).
        current: The latest snapshots (after a refresh).
        floor: The deterioration floor (defaults to ``EWS_SUBSTANDARD`` = 40).

    Returns:
        Ordered :class:`CrossingAlert` list (empty when nothing crossed).
    """
    line = float(EWS_SUBSTANDARD if floor is None else floor)
    prev_by_code = {s.tdb_code: s for s in previous}

    alerts: list[CrossingAlert] = []
    for snap in current:
        prior = prev_by_code.get(snap.tdb_code)
        if prior is None:
            continue  # no baseline -> not a crossing (already a new watchlist row)
        if float(prior.ews) < line <= float(snap.ews):
            alerts.append(
                CrossingAlert(
                    tdb_code=snap.tdb_code,
                    company_name=snap.company_name,
                    prev_ews=float(prior.ews),
                    new_ews=float(snap.ews),
                )
            )

    return sorted(alerts, key=lambda a: (-a.new_ews, a.tdb_code))

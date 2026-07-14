"""Offline, advisory analysis package for the Saisei decision spine.

NOTHING in this package is imported by the LangGraph spine. These modules are
pure, offline analyses over PERSISTED data (the who-was-right corpus captured at
each HITL resolution). They produce advisory reports a human reads; they never
edit a constant, gate, route, or call an LLM. Consistent with the
"more power, never more authority" principle of the rest of the stack.
"""

from __future__ import annotations

from app.backend.analysis.claim_grounding import (
    ClaimGroundingResult,
    ClaimVerdict,
    EvidencePacket,
    SentenceProvenance,
    check_claims_grounded,
    guard_grounded_text,
)
from app.backend.analysis.evidence import (
    SIGNAL_KEYS,
    available_signal_keys,
    build_evidence_packet,
)
from app.backend.analysis.faithfulness import (
    ClaimFaithfulness,
    FaithfulnessResult,
    score_claim_faithfulness,
    score_claims,
)
from app.backend.analysis.grounding_pipeline import (
    GroundedText,
    ProvenanceEntry,
    ground_qualitative_text,
)
from app.backend.analysis.threshold_calibration import (
    BandDistanceStats,
    CalibrationReport,
    calibrate_reconciliation_threshold,
    report_to_display_rows,
)

__all__ = [
    "SIGNAL_KEYS",
    "BandDistanceStats",
    "CalibrationReport",
    "ClaimFaithfulness",
    "ClaimGroundingResult",
    "ClaimVerdict",
    "EvidencePacket",
    "FaithfulnessResult",
    "GroundedText",
    "ProvenanceEntry",
    "SentenceProvenance",
    "available_signal_keys",
    "build_evidence_packet",
    "calibrate_reconciliation_threshold",
    "check_claims_grounded",
    "ground_qualitative_text",
    "guard_grounded_text",
    "report_to_display_rows",
    "score_claim_faithfulness",
    "score_claims",
]

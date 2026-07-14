"""Feature 0 — evidence packet, faithfulness, and grounding-pipeline tests.

Offline, deterministic. Covers the phases the bare claim-grounding tests don't:
- the evidence packet derives citable ids only from POPULATED state signals;
- the faithfulness proxy scores entailment and demotes unsupported claims;
- the end-to-end pipeline strips (default) or flags (UI) and yields provenance;
- with no LLM, qualitative output is empty so the whole pipeline is a no-op.
"""

from __future__ import annotations

from app.backend.analysis.claim_grounding import UNVERIFIED_MARKER, EvidencePacket
from app.backend.analysis.evidence import (
    SIGNAL_KEYS,
    available_signal_keys,
    build_evidence_packet,
)
from app.backend.analysis.faithfulness import (
    lexical_overlap_score,
    score_claim_faithfulness,
    score_claims,
)
from app.backend.analysis.grounding_pipeline import ground_qualitative_text
from app.backend.state import SaiseiState
from app.shared.models.classification import FsaClass
from app.shared.settings import Settings

#: No-LLM settings -> faithfulness uses the deterministic lexical proxy.
_OFFLINE = Settings(llm_api_key="", llm_model="")


# ---------------------------------------------------------------------------
# Evidence packet
# ---------------------------------------------------------------------------


def test_available_signal_keys_empty_state() -> None:
    state = SaiseiState(tdb_code="1234567")
    assert available_signal_keys(state) == frozenset()


def test_available_signal_keys_reflects_populated_fields() -> None:
    state = SaiseiState(
        tdb_code="1234567",
        ews_score=62.0,
        fsa_classification=FsaClass.YOCHUISAKI,
        working_capital_gap=-5_000_000,
        tdb_score=44,
    )
    keys = available_signal_keys(state)
    assert "ews" in keys
    assert "fsa_classification" in keys
    assert "working_capital_gap" in keys
    assert "tdb_score" in keys
    # not populated -> absent
    assert "net_worth" not in keys
    assert keys <= SIGNAL_KEYS


def test_build_evidence_packet_merges_signals_and_sources() -> None:
    state = SaiseiState(tdb_code="1234567", ews_score=62.0)
    packet = build_evidence_packet(state, source_labels=["past_keikakusho"])
    assert isinstance(packet, EvidencePacket)
    assert packet.resolve("ews") == "signal"
    assert packet.resolve("past_keikakusho") == "source"
    assert packet.resolve("net_worth") is None


# ---------------------------------------------------------------------------
# Faithfulness proxy
# ---------------------------------------------------------------------------


def test_lexical_overlap_full_and_zero() -> None:
    assert (
        lexical_overlap_score(
            "price pass-through restores margin", "price pass-through restores margin fully"
        )
        == 1.0
    )
    assert lexical_overlap_score("totally unrelated wording", "different evidence") == 0.0
    assert lexical_overlap_score("", "evidence") == 0.0
    assert lexical_overlap_score("claim", "") == 0.0


def test_score_claim_faithfulness_offline_uses_lexical() -> None:
    rec = score_claim_faithfulness(
        "margin compression is severe",
        "the firm shows severe margin compression this quarter",
        settings=_OFFLINE,
    )
    assert rec.method == "lexical"
    assert rec.faithful is True
    assert rec.score > 0.5


def test_score_claims_demotes_unsupported() -> None:
    result = score_claims(
        {
            "margin compression is severe": "severe margin compression confirmed",
            "the moon is made of cheese": "the firm's working capital is tight",
        },
        settings=_OFFLINE,
    )
    assert not result.all_faithful
    demoted_claims = {c.claim for c in result.demoted}
    assert "the moon is made of cheese" in demoted_claims


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------


def _packet() -> EvidencePacket:
    return EvidencePacket.build(
        signal_keys=["ews", "working_capital_gap"],
        source_labels=["benchmark"],
    )


def test_pipeline_empty_text_is_noop() -> None:
    out = ground_qualitative_text("", _packet(), settings=_OFFLINE)
    assert out.text == ""
    assert out.fully_grounded is True
    assert out.provenance == []


def test_pipeline_strips_ungrounded_by_default() -> None:
    text = "EWSは高い [ews]。根拠なく回復します。"
    out = ground_qualitative_text(text, _packet(), settings=_OFFLINE)
    assert "[ews]" in out.text
    assert "回復します" not in out.text
    assert out.fully_grounded is False


def test_pipeline_flag_mode_marks_unverified() -> None:
    text = "根拠なく回復します。"
    out = ground_qualitative_text(text, _packet(), flag=True, settings=_OFFLINE)
    assert UNVERIFIED_MARKER in out.text
    statuses = {p.status for p in out.provenance}
    assert "unverified" in statuses


def test_pipeline_faithfulness_demotes_grounded_but_unsupported() -> None:
    # Cited claim resolves (phase 2), but evidence text does not entail it
    # (phase 3) -> demoted, so stripped in default mode.
    text = "資金繰りは完全に健全です [benchmark]。"
    out = ground_qualitative_text(
        text,
        _packet(),
        evidence_texts={"benchmark": "unrelated precedent about retail pricing"},
        settings=_OFFLINE,
    )
    assert out.fully_grounded is False
    assert out.text == ""


def test_pipeline_keeps_grounded_and_faithful() -> None:
    text = "価格転嫁は有効です [benchmark]。"
    out = ground_qualitative_text(
        text,
        _packet(),
        evidence_texts={"benchmark": "価格転嫁は有効な手段である"},
        settings=_OFFLINE,
    )
    assert out.fully_grounded is True
    assert "[benchmark]" in out.text

"""Feature 0 — claim-grounding verifier offline tests.

The claim-grounding verifier is the qualitative analogue of
``numeric_preservation``: a deterministic, offline gate that ensures no
unattributable assertion reaches the banker as fact. These tests are the
verifier-for-the-verifier and pin the contract:

- a sentence with a citation that resolves to a deterministic signal or a
  retrieved source is KEPT (grounded);
- a sentence with no citation, or a citation that does not resolve, is STRIPPED
  (default abstain posture) or FLAGGED unverified (UI posture);
- a sentence mixing a real and a fabricated citation FAILS (all-or-nothing);
- headings / pure-punctuation fragments are NON_CLAIM and pass through;
- the check is pure and deterministic (same inputs -> identical result);
- the guard returns text that is always safe to surface.

No network, no LLM — ``make verify`` stays green offline.
"""

from __future__ import annotations

from app.backend.analysis.claim_grounding import (
    UNVERIFIED_MARKER,
    ClaimGroundingResult,
    ClaimVerdict,
    EvidencePacket,
    check_claims_grounded,
    extract_citations,
    guard_grounded_text,
    split_sentences,
)


def _packet() -> EvidencePacket:
    """A representative evidence packet: two signals + two retrieved sources."""
    return EvidencePacket.build(
        signal_keys=["ews", "working_capital_gap"],
        source_labels=["past_keikakusho", "benchmark"],
    )


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def test_split_sentences_handles_jp_and_latin() -> None:
    text = "売上は減少した。Margin compressed sharply! 本当か？"
    assert split_sentences(text) == [
        "売上は減少した。",
        "Margin compressed sharply!",
        "本当か？",
    ]


def test_split_sentences_empty() -> None:
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


def test_extract_citations_single_and_grouped() -> None:
    assert extract_citations("foo [ews] bar") == ["ews"]
    # comma / JP-comma grouped ids inside one bracket are split out.
    assert extract_citations("x [ews, benchmark] y") == ["ews", "benchmark"]
    assert extract_citations("x [ews、past_keikakusho] y") == [
        "ews",
        "past_keikakusho",
    ]


def test_extract_citations_none() -> None:
    assert extract_citations("no citation here.") == []


# ---------------------------------------------------------------------------
# EvidencePacket resolution
# ---------------------------------------------------------------------------


def test_packet_resolves_signal_and_source_case_insensitively() -> None:
    packet = _packet()
    assert packet.resolve("ews") == "signal"
    assert packet.resolve("EWS") == "signal"
    assert packet.resolve(" Working_Capital_Gap ") == "signal"
    assert packet.resolve("past_keikakusho") == "source"
    assert packet.resolve("unknown_label") is None


# ---------------------------------------------------------------------------
# Core grounding behaviour
# ---------------------------------------------------------------------------


def test_grounded_sentence_is_kept() -> None:
    text = "EWSスコアは高水準です [ews]。"
    result = check_claims_grounded(text, _packet())
    assert result.grounded is True
    assert result.cleaned_text == text.strip()
    assert [p.verdict for p in result.provenance] == [ClaimVerdict.GROUNDED]
    assert result.provenance[0].resolved == [("ews", "signal")]


def test_uncited_sentence_is_stripped_by_default() -> None:
    text = "This firm will certainly recover within a year."
    result = check_claims_grounded(text, _packet())
    assert result.grounded is False
    assert result.cleaned_text == ""
    assert result.ungrounded[0].verdict is ClaimVerdict.UNGROUNDED
    assert result.ungrounded[0].kept is False


def test_unresolving_citation_is_stripped() -> None:
    text = "Recovery is assured [made_up_source]."
    result = check_claims_grounded(text, _packet())
    assert result.grounded is False
    assert result.cleaned_text == ""


def test_mixed_real_and_fake_citation_fails_all_or_nothing() -> None:
    text = "Margin repair is feasible [benchmark, ghost_source]."
    result = check_claims_grounded(text, _packet())
    assert result.grounded is False
    assert result.cleaned_text == ""
    # the real citation resolved, but the sentence still fails.
    assert ("benchmark", "source") in result.ungrounded[0].resolved


def test_flag_mode_keeps_and_marks_unverified() -> None:
    text = "This firm will certainly recover."
    result = check_claims_grounded(text, _packet(), flag=True)
    assert result.grounded is False
    assert result.flagged is True
    assert UNVERIFIED_MARKER in result.cleaned_text
    assert result.provenance[0].kept is True


def test_heading_and_punctuation_are_non_claims() -> None:
    text = "# 事業再生計画\n----\nEWSは低下しました [ews]。"
    result = check_claims_grounded(text, _packet())
    verdicts = [p.verdict for p in result.provenance]
    assert ClaimVerdict.NON_CLAIM in verdicts
    assert ClaimVerdict.GROUNDED in verdicts
    # both the heading and the grounded claim survive; nothing ungrounded.
    assert result.grounded is True
    assert "# 事業再生計画" in result.cleaned_text


def test_mixed_document_strips_only_ungrounded() -> None:
    text = (
        "資金繰りギャップは赤字です [working_capital_gap]。"
        "類似事例では価格転嫁が有効でした [past_keikakusho]。"
        "経営者は個人的に信頼できると思います。"
    )
    result = check_claims_grounded(text, _packet())
    assert result.grounded is False
    # the two cited claims survive; the uncited opinion is dropped.
    assert "[working_capital_gap]" in result.cleaned_text
    assert "[past_keikakusho]" in result.cleaned_text
    assert "個人的に信頼" not in result.cleaned_text
    assert len(result.ungrounded) == 1


# ---------------------------------------------------------------------------
# Determinism + guard + mapping coercion
# ---------------------------------------------------------------------------


def test_check_is_deterministic() -> None:
    text = "EWSは高い [ews]。根拠なく回復します。"
    first = check_claims_grounded(text, _packet())
    second = check_claims_grounded(text, _packet())
    assert first.cleaned_text == second.cleaned_text
    assert first.grounded == second.grounded
    assert [p.verdict for p in first.provenance] == [p.verdict for p in second.provenance]


def test_guard_returns_cleaned_text_and_result() -> None:
    text = "根拠なく確実に回復します。EWSは低下 [ews]。"
    cleaned, result = guard_grounded_text(text, _packet())
    assert isinstance(result, ClaimGroundingResult)
    assert cleaned == result.cleaned_text
    assert "[ews]" in cleaned
    assert "確実に回復" not in cleaned


def test_fully_grounded_guard_is_identity() -> None:
    text = "EWSは低下しました [ews]。"
    cleaned, result = guard_grounded_text(text, _packet())
    assert result.grounded is True
    assert cleaned == text.strip()


def test_evidence_accepts_plain_mapping() -> None:
    mapping = {"signal_keys": ["ews"], "source_labels": ["benchmark"]}
    result = check_claims_grounded("EWSは高い [ews]。", mapping)
    assert result.grounded is True


def test_reason_is_empty_when_grounded_else_describes() -> None:
    grounded = check_claims_grounded("EWSは低下 [ews]。", _packet())
    assert grounded.reason() == ""
    bad = check_claims_grounded("根拠なく回復。", _packet())
    assert "stripped" in bad.reason()


def test_empty_text_is_trivially_grounded() -> None:
    result = check_claims_grounded("", _packet())
    assert result.grounded is True
    assert result.cleaned_text == ""
    assert result.provenance == []

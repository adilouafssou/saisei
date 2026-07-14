"""Verifier for the shared Markdown -> PDF renderer + the PDF exporters.

The PDF path embeds a CJK font that is a BUILD/DEPLOY input (a binary, not
source), so these tests SKIP cleanly when no font is available — mirroring the
repo's config-gated / resource-gated test discipline. When a font IS present
(vendored at ``assets/fonts/NotoSansJP-Regular.ttf`` or via
``SAISEI_PDF_FONT_PATH``), they pin the load-bearing invariants:

1. **It is a real PDF.** Bytes begin with the ``%PDF`` signature and end with
   the ``%%EOF`` trailer.
2. **Numeric preservation.** Converting a report to PDF adds, drops, or alters
   NO yen figure: the multiset of yen values extracted from the PDF text equals
   the source Markdown's. (Extraction uses ``pypdf`` when available; if it is
   not installed the numeric check is skipped, but the structural checks still
   run.)
3. **Determinism.** Same Markdown in -> byte-identical PDF out (creation date is
   pinned in the renderer).
4. **Fail-loud, never tofu.** With no font resolvable, the renderer raises
   ``PdfFontUnavailableError`` rather than emitting an unreadable PDF.
5. **Filename contracts** for every PDF exporter.

All tests are offline and deterministic.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

import pytest
from app.backend.analysis.numeric_preservation import extract_yen_values
from app.backend.export._markdown_pdf import (
    PdfFontUnavailableError,
    pdf_font_available,
    render_markdown_to_pdf,
)
from app.backend.export.explainability_report import (
    build_explainability_pdf,
    build_explainability_report,
    explainability_pdf_filename,
)
from app.backend.export.model_card import (
    build_model_card_pdf,
    constants_changelog_pdf_filename,
    model_card_pdf_filename,
)
from app.backend.nodes.ews_scoring import compute_ews_breakdown, compute_ews_score
from app.backend.state import SaiseiState
from app.backend.tools.tdb_api import CompanyProfile
from app.shared.models.accounting import TrialBalance
from app.shared.models.classification import FsaClass

#: Skip the rendering tests when no CJK font is vendored / configured. The
#: filename + fail-loud tests below do NOT need a font and always run.
_requires_font = pytest.mark.skipif(
    not pdf_font_available(),
    reason="No CJK font for PDF export (vendor assets/fonts/NotoSansJP-Regular.ttf "
    "or set SAISEI_PDF_FONT_PATH).",
)

_SAMPLE_MD = "\n".join(
    [
        "# \u8aac\u660e\u30ec\u30dd\u30fc\u30c8 (Explainability)",
        "",
        "- \u4f01\u696d\u540d (Company): \u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e",
        "- \u58f2\u4e0a (Uriage): \u00a5150,000,000",
        "",
        "## EWS",
        "",
        "| \u30b7\u30b0\u30ca\u30eb (Signal) | \u5bc4\u4e0e (Points) |",
        "| --- | ---: |",
        "| \u58f2\u4e0a\u6e1b\u5c11 | 12.50 |",
        "| \u7c97\u5229\u7387\u4f4e\u4e0b | 7.25 |",
        "",
        "1. \u65bd\u7b56\u3092\u5b9f\u884c\u3059\u308b\u3002",
    ]
)


def _pdf_text(data: bytes) -> str | None:
    """Extract text from PDF bytes via pypdf, or ``None`` when pypdf is absent."""
    try:
        import io

        from pypdf import PdfReader
    except ImportError:
        return None
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _profile() -> CompanyProfile:
    return CompanyProfile(
        tdb_code="1234567",
        hojin_bango="1234567890123",
        name="\u30c6\u30b9\u30c8\u88fd\u9020\u682a\u5f0f\u4f1a\u793e",
        prefecture="\u611b\u77e5\u770c",
        industry="\u88fd\u9020\u696d",
        established_year=1990,
        employees=42,
    )


def _history() -> list[TrialBalance]:
    rows: list[TrialBalance] = []
    for i in range(6):
        sales = 100_000_000 - i * 8_000_000
        cogs = int(sales * (0.72 + i * 0.01))
        rows.append(
            TrialBalance(
                period=dt.date(2025, 1, 1) + dt.timedelta(days=30 * i),
                uriage=sales,
                uriage_genka=cogs,
                hanbaihi=20_000_000,
            )
        )
    return rows


def _state() -> SaiseiState:
    history = _history()
    return SaiseiState(
        tdb_code="1234567",
        company_profile=_profile(),
        tdb_score=58,
        shisanhyo=history,
        working_capital_gap=-5_000_000,
        ews_score=compute_ews_score(history),
        ews_breakdown=[s.__dict__ for s in compute_ews_breakdown(history)],
        fsa_classification=FsaClass.YOCHUISAKI,
        special_attention=True,
        classification_reason="working-capital deficit",
    )


# --- Fail-loud + filename contracts (no font required) --------------------


def test_render_without_font_raises_not_tofu(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no resolvable font, the renderer raises rather than emitting tofu."""
    import app.backend.export._markdown_pdf as mod

    monkeypatch.setattr(mod, "pdf_font_path", lambda: None)
    with pytest.raises(PdfFontUnavailableError):
        render_markdown_to_pdf("# hi\n")


def test_pdf_filenames_are_safe() -> None:
    """Every PDF exporter's filename mirrors the cross-platform contract."""
    assert (
        explainability_pdf_filename("\u30c6\u30b9\u30c8 \u88fd\u9020")
        == "explainability_\u30c6\u30b9\u30c8_\u88fd\u9020.pdf"
    )
    assert explainability_pdf_filename("bad:name?") == "explainability_bad_name.pdf"
    assert explainability_pdf_filename("") == "explainability_borrower.pdf"
    assert model_card_pdf_filename() == "model_card_saisei_engine.pdf"
    assert model_card_pdf_filename("") == "model_card_engine.pdf"
    assert constants_changelog_pdf_filename() == "governing_constants_changelog.pdf"


# --- Rendering invariants (require a vendored / configured font) ----------


@_requires_font
def test_pdf_has_signature_and_trailer() -> None:
    """The output is a real PDF: %PDF header and %%EOF trailer."""
    data = render_markdown_to_pdf(_SAMPLE_MD)
    assert data[:4] == b"%PDF"
    assert b"%%EOF" in data[-1024:]


@_requires_font
def test_pdf_is_deterministic() -> None:
    """Same Markdown in -> byte-identical PDF out (creation date pinned)."""
    assert render_markdown_to_pdf(_SAMPLE_MD) == render_markdown_to_pdf(_SAMPLE_MD)


@_requires_font
def test_pdf_preserves_every_yen_figure() -> None:
    """Yen figures survive into the PDF text (skipped if pypdf is unavailable)."""
    data = build_explainability_pdf(_state())
    text = _pdf_text(data)
    if text is None:
        pytest.skip("pypdf not installed; structural checks cover the rest")
    md = build_explainability_report(_state())
    source = Counter(extract_yen_values(md))
    rendered = Counter(extract_yen_values(text))
    assert rendered == source


@_requires_font
def test_explainability_pdf_is_a_real_pdf() -> None:
    """The explainability PDF exporter yields a real, non-empty PDF."""
    data = build_explainability_pdf(_state())
    assert data[:4] == b"%PDF"
    assert len(data) > 0


@_requires_font
def test_model_card_pdf_is_a_real_pdf() -> None:
    """The model-card PDF exporter yields a real, non-empty PDF."""
    data = build_model_card_pdf()
    assert data[:4] == b"%PDF"
    assert len(data) > 0

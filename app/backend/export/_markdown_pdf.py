"""Shared, deterministic Markdown → PDF renderer (CJK-correct, light).

The sibling of :func:`app.backend.export._markdown_docx.render_markdown_to_docx`.
FSA examiners archive **PDF** (immutable, layout-stable, searchable); this turns
the SAME deterministic Markdown the DOCX path walks into a ``.pdf`` so every
examiner artifact (explainability report, model card, change log, Keikakusho)
gets a PDF path from ONE renderer, with no duplicated layout logic.

Why this design (the senior call, not a hand-rolled PDF engine)
--------------------------------------------------------------
The documents are **Japanese**, and CJK PDF is the hard 20% of any PDF writer:
it requires embedding + subsetting a CID-keyed TrueType font and a ``ToUnicode``
CMap, or the text renders as tofu / is not searchable. Re-implementing TTF
subsetting by hand is exactly the wrong place to take NIH risk in a regulated
credit document whose ONE inviolable rule is numeric preservation. So this uses
:mod:`fpdf2` — pure-Python, **no native libraries** (unlike WeasyPrint's
Pango/Cairo), with correct Unicode TTF embedding, automatic glyph SUBSETTING
(only the glyphs actually used are embedded, keeping the file light), and a
``ToUnicode`` map so copy/search works. The only real weight is the font, which
*every* CJK-PDF approach needs; subsetting makes the embedded portion tiny.

Numeric-preservation invariant
------------------------------
Identical contract to the DOCX renderer: it walks the Markdown line-by-line and
writes each line's text **verbatim** (only mapping ``#`` headings, ``-`` /
numbered lists, and ``| ... |`` tables to layout). It never parses, reformats,
or re-renders a figure — the bytes of every yen value are carried across
unchanged. Pure and deterministic given a fixed font: same Markdown in → same
PDF text out (we pin the document's creation metadata so the bytes are stable).

Font resolution
---------------
The CJK font is a BUILD/DEPLOY input (a binary cannot live in source as text).
It is resolved, in order, from:
  1. the ``SAISEI_PDF_FONT_PATH`` environment variable, then
  2. the vendored ``assets/fonts/NotoSansJP-Regular.ttf``.
When no font is found, :func:`render_markdown_to_pdf` raises
:class:`PdfFontUnavailableError` with an actionable message — it never silently
emits tofu. Callers that must be best-effort should catch that and fall back to
the DOCX path.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

__all__ = [
    "render_markdown_to_pdf",
    "pdf_font_path",
    "pdf_font_available",
    "PdfFontUnavailableError",
]

#: Repo-root-relative vendored font location (a build input; see module docstring).
#: ``app/backend/export/_markdown_pdf.py`` -> parents[3] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VENDORED_FONT = _REPO_ROOT / "assets" / "fonts" / "NotoSansJP-Regular.ttf"
#: Environment override for the CJK font path (takes precedence when set).
_FONT_ENV = "SAISEI_PDF_FONT_PATH"
#: The internal font family name registered with fpdf2.
_FONT_FAMILY = "SaiseiCJK"

#: Fixed creation timestamp so the emitted PDF bytes are deterministic (a PDF's
#: ``/CreationDate`` + ``/ModDate`` metadata is the only non-content source of
#: byte variability). Pinned to the Unix epoch in UTC; the report content carries
#: its own dates. fpdf2 accepts a ``datetime`` for ``creation_date``.
_FIXED_PDF_DATE = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)

# --- Markdown structure (mirrors _markdown_docx so the two never diverge). ---
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_TABLE_DELIM_RE = re.compile(r"^\|[\s:|-]+\|$")

#: Heading point sizes by Markdown level (1..6); body is the last entry's peer.
_HEADING_PT = {1: 18, 2: 15, 3: 13, 4: 12, 5: 11, 6: 11}
_BODY_PT = 10.5
_LINE_H = 6.0  # mm line height for body text


class PdfFontUnavailableError(RuntimeError):
    """Raised when no CJK font is available to embed in the PDF.

    A regulated Japanese document must never be emitted as unreadable tofu, so
    the renderer fails loud (with the resolution order) rather than producing a
    broken PDF. Vendor ``assets/fonts/NotoSansJP-Regular.ttf`` or set
    ``SAISEI_PDF_FONT_PATH`` to fix.
    """


def pdf_font_path() -> Path | None:
    """Return the resolved CJK font path, or ``None`` when none is available.

    Resolution order: ``SAISEI_PDF_FONT_PATH`` env var, then the vendored
    ``assets/fonts/NotoSansJP-Regular.ttf``. Returns ``None`` only when neither
    exists, so callers can gate on availability without catching an exception.
    """
    override = os.environ.get(_FONT_ENV)
    if override:
        p = Path(override)
        if p.is_file():
            return p
    return _VENDORED_FONT if _VENDORED_FONT.is_file() else None


def pdf_font_available() -> bool:
    """Whether a CJK font is available to embed (PDF export is possible)."""
    return pdf_font_path() is not None


def _strip_inline_emphasis(text: str) -> str:
    """Remove Markdown ``**bold**`` / ``*italic*`` markers, keeping the text.

    Only the surrounding emphasis markers are removed; digits, currency markers
    (¥ / 円), commas, and signs are never touched, so yen figures survive
    byte-for-byte (identical rule to the DOCX renderer).
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    return text


def _split_table_cells(row: str) -> list[str]:
    """Split a ``| a | b |`` Markdown row into verbatim, trimmed cell strings."""
    inner = row.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [_strip_inline_emphasis(cell.strip()) for cell in inner.split("|")]


def _flush_table(pdf: object, table_rows: list[str]) -> None:
    """Render buffered Markdown table rows as a real PDF table (verbatim cells).

    Uses fpdf2's ``table`` context manager so every cell string is written
    verbatim (numeric preservation holds inside tables exactly as for
    paragraphs). Ragged rows are padded to the widest row.
    """
    if not table_rows:
        return
    parsed = [_split_table_cells(r) for r in table_rows]
    n_cols = max(len(cells) for cells in parsed)
    with pdf.table(  # type: ignore[attr-defined]
        borders_layout="SINGLE_TOP_LINE",
        line_height=_LINE_H,
        first_row_as_headings=True,
    ) as table:
        for cells in parsed:
            row = table.row()
            for i in range(n_cols):
                row.cell(cells[i] if i < len(cells) else "")


def render_markdown_to_pdf(markdown: str) -> bytes:
    """Render a deterministic Markdown document to ``.pdf`` bytes.

    Walks ``markdown`` line-by-line and writes each line's text verbatim into the
    PDF, mapping only the Markdown *structure* (heading levels, bullet / ordered
    list items, and ``| ... |`` tables) to layout. No figure is ever reformatted;
    the bytes of every yen value are carried across unchanged.

    The embedded CJK font is auto-subsetted by fpdf2 (only used glyphs ship), so
    a Japanese report stays small. The document's creation metadata is pinned so
    the output is byte-deterministic for a fixed font + input.

    Args:
        markdown: The deterministic Markdown source of truth.

    Returns:
        The generated ``.pdf`` file as bytes.

    Raises:
        PdfFontUnavailableError: When no CJK font can be resolved (see module
            docstring) — the renderer never emits unreadable tofu.
    """
    font = pdf_font_path()
    if font is None:
        raise PdfFontUnavailableError(
            "No CJK font available for PDF export. Set the "
            f"{_FONT_ENV} environment variable to a .ttf, or vendor the font at "
            f"{_VENDORED_FONT}. (A Japanese PDF must embed a CJK font or it "
            "renders as unreadable boxes.)"
        )

    # Imported lazily so merely importing this module (e.g. for pdf_font_available)
    # does not require fpdf2 to be installed in environments that never export PDF.
    from fpdf import FPDF

    pdf = FPDF(unit="mm", format="A4")
    pdf.set_margins(left=18, top=18, right=18)
    pdf.set_auto_page_break(auto=True, margin=18)
    # Deterministic metadata: pin the creation date (the only non-content source
    # of byte variability) so the same input -> the same bytes. fpdf2 accepts a
    # datetime and formats the PDF date string itself.
    pdf.creation_date = _FIXED_PDF_DATE
    # Register + subset the CJK font (subsetting is on by default in fpdf2).
    pdf.add_font(_FONT_FAMILY, style="", fname=str(font))
    pdf.add_font(_FONT_FAMILY, style="B", fname=str(font))
    pdf.set_font(_FONT_FAMILY, size=_BODY_PT)
    pdf.add_page()

    effective_w = pdf.epw  # effective page width (mm) inside the margins

    table_buffer: list[str] = []

    def _flush() -> None:
        _flush_table(pdf, table_buffer)
        table_buffer.clear()

    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()

        # --- Table handling (buffer contiguous rows; drop the delimiter). ---
        if _TABLE_ROW_RE.match(stripped):
            if not _TABLE_DELIM_RE.match(stripped):
                table_buffer.append(stripped)
            continue
        if table_buffer:
            _flush()

        if not stripped:
            pdf.ln(_LINE_H * 0.6)  # blank line -> vertical gap
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            level = min(len(heading.group(1)), 6)
            pdf.set_font(_FONT_FAMILY, size=_HEADING_PT[level])
            pdf.multi_cell(effective_w, _LINE_H + 1.5, _strip_inline_emphasis(heading.group(2)))
            pdf.set_font(_FONT_FAMILY, size=_BODY_PT)
            pdf.ln(1.0)
            continue

        bullet = _BULLET_RE.match(stripped)
        if bullet:
            # Use a real bullet glyph; the text is written verbatim after it.
            pdf.multi_cell(
                effective_w, _LINE_H, f"\u2022 {_strip_inline_emphasis(bullet.group(1))}"
            )
            continue

        ordered = _ORDERED_RE.match(stripped)
        if ordered:
            # Keep the author's own numbering verbatim (do not renumber).
            pdf.multi_cell(
                effective_w,
                _LINE_H,
                f"{ordered.group(1)}. {_strip_inline_emphasis(ordered.group(2))}",
            )
            continue

        pdf.multi_cell(effective_w, _LINE_H, _strip_inline_emphasis(stripped))

    if table_buffer:
        _flush()

    out = pdf.output()
    # fpdf2 returns a bytearray; normalise to immutable bytes.
    return bytes(out)

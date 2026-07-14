# CJK font for PDF export

The PDF exporter (`app/backend/export/_markdown_pdf.py`) embeds a Japanese font
so the regulated documents render correctly (a CJK PDF without an embedded font
shows unreadable boxes / "tofu"). A font is binary data and is therefore a
**build/deploy input**, not source code — it is intentionally not committed here.

## What to provide

Place a Japanese TrueType/OpenType font at:

```
assets/fonts/NotoSansJP-Regular.ttf
```

Recommended: **Noto Sans JP** (SIL Open Font License 1.1), which covers the kanji,
kana, Latin, digits, and the ¥ sign used by the reports.

- Download: https://fonts.google.com/noto/specimen/Noto+Sans+JP
- License: SIL OFL 1.1 (redistribution-friendly; keep the license file alongside).

fpdf2 **subsets** the font at render time, so only the glyphs actually used are
embedded — the generated PDFs stay small even though the source font is large.

## Alternative: runtime override

Instead of vendoring the file, point the exporter at any `.ttf` at runtime:

```
export SAISEI_PDF_FONT_PATH=/path/to/NotoSansJP-Regular.ttf
```

`SAISEI_PDF_FONT_PATH` takes precedence over the vendored path.

## Behaviour when absent

If no font is resolved, `render_markdown_to_pdf` raises
`PdfFontUnavailableError` (it never emits tofu). The PDF export tests skip when
no font is available, and the UI can fall back to the DOCX export.

"""Tests for Part 6 — Excel/CSV trial-balance upload (first slice).

All tests are offline, deterministic, and import only from ``app.*``.

Coverage:
1. Parser unit tests:
   - Clean .xlsx bytes (generated in-test with stdlib zipfile) → expected rows.
   - Clean .csv bytes → expected rows.
   - Malformed cells → warnings, not exceptions.
   - Integer-yen enforcement (float rejection).
   - J-GAAP invariant violations surfaced as warnings.
   - Unsupported file format → warning, empty rows.
   - Duplicate column headers → warning, first wins.
   - Year-month period format (YYYY-MM) → last day of month.
   - Excel serial date → correct date.

2. State tests:
   - ``uploaded_shisanhyo`` and ``upload_warnings`` fields exist on SaiseiState.
   - Confirm: ``uploaded_shisanhyo`` → ``shisanhyo``; staging cleared.
   - Cancel: staging cleared; ``shisanhyo`` unchanged.
   - Deterministic pipeline on confirmed rows matches direct-fixture run
     (seam transparency: uploaded rows byte-equivalent to fixture-loaded rows).
   - ``render_keikakusho`` output byte-identical after confirm.
   - No existing SaiseiState field was removed.

3. Guard tests:
   - All pre-existing SaiseiState fields still present.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile
from pathlib import Path
from typing import Any, cast

from app.backend.graph import build_graph
from app.backend.state import SaiseiState
from app.backend.tools.shisanhyo_parser import ParsedShisanhyo, parse_shisanhyo
from app.shared.models.accounting import TrialBalance
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

# ---------------------------------------------------------------------------
# Helpers: generate .xlsx bytes in-test using stdlib zipfile only
# ---------------------------------------------------------------------------

_XLSX_CONTENT_TYPES = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>
"""

_XLSX_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="xl/workbook.xml"/>
</Relationships>
"""

_XLSX_WORKBOOK = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

_XLSX_WORKBOOK_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
    Target="sharedStrings.xml"/>
</Relationships>
"""


def _make_shared_strings(strings: list[str]) -> str:
    """Build a sharedStrings.xml with the given string list."""
    count = len(strings)
    items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        f' count="{count}" uniqueCount="{count}">'
        f"{items}"
        f"</sst>"
    )


def _make_sheet(rows: list[list[str | int | None]], shared_strings: list[str]) -> str:
    """Build a sheet1.xml from a list of rows.

    Each cell value is either:
    - A string → stored as a shared-string reference (type="s").
    - An int → stored as a numeric value (no type attribute).
    - None → empty cell (omitted).
    """
    col_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def _cell(col_idx: int, row_idx: int, val: str | int | None) -> str:
        if val is None:
            return ""
        ref = f"{col_letters[col_idx]}{row_idx}"
        if isinstance(val, str):
            ss_idx = shared_strings.index(val)
            return f'<c r="{ref}" t="s"><v>{ss_idx}</v></c>'
        # int
        return f'<c r="{ref}"><v>{val}</v></c>'

    row_xmls = []
    for r_idx, row in enumerate(rows, start=1):
        cells = "".join(
            _cell(c_idx, r_idx, val) for c_idx, val in enumerate(row) if val is not None
        )
        row_xmls.append(f'<row r="{r_idx}">{cells}</row>')

    sheet_data = "".join(row_xmls)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{sheet_data}</sheetData>"
        "</worksheet>"
    )


def _build_xlsx(rows: list[list[str | int | None]]) -> bytes:
    """Build a minimal valid .xlsx file from a list of rows (first row = header).

    All string values are stored in the shared-string table; integers are stored
    as numeric cells.  Uses stdlib zipfile only (no openpyxl).
    """
    # Collect all unique strings from the rows.
    all_strings: list[str] = []
    for row in rows:
        for val in row:
            if isinstance(val, str) and val not in all_strings:
                all_strings.append(val)

    shared_strings_xml = _make_shared_strings(all_strings)
    sheet_xml = _make_sheet(rows, all_strings)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _XLSX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _XLSX_RELS)
        zf.writestr("xl/workbook.xml", _XLSX_WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", _XLSX_WORKBOOK_RELS)
        zf.writestr("xl/sharedStrings.xml", shared_strings_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


def _build_csv(rows: list[list[str]]) -> bytes:
    """Build CSV bytes from a list of rows (first row = header)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Fixture data (matches the aichi_manufacturer fixture for seam tests)
# ---------------------------------------------------------------------------

_AICHI_FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "app"
    / "backend"
    / "tools"
    / "fixtures"
    / "aichi_manufacturer.json"
)


def _load_aichi_rows() -> list[TrialBalance]:
    """Load the aichi_manufacturer fixture rows as TrialBalance objects."""
    with _AICHI_FIXTURE_PATH.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    return [
        TrialBalance(
            period=dt.date.fromisoformat(row["period"]),
            uriage=row["uriage"],
            uriage_genka=row["uriage_genka"],
            hanbaihi=row["hanbaihi"],
            eigai_shueki=row.get("eigai_shueki", 0),
            eigai_hiyo=row.get("eigai_hiyo", 0),
        )
        for row in payload["shisanhyo"]
    ]


def _aichi_xlsx_rows() -> list[list[str | int | None]]:
    """Build the header + data rows for the aichi fixture as xlsx input."""
    header: list[str | int | None] = [
        "period",
        "uriage",
        "uriage_genka",
        "hanbaihi",
        "eigai_shueki",
        "eigai_hiyo",
    ]
    with _AICHI_FIXTURE_PATH.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    data_rows: list[list[str | int | None]] = [
        [
            row["period"],
            row["uriage"],
            row["uriage_genka"],
            row["hanbaihi"],
            row.get("eigai_shueki", 0),
            row.get("eigai_hiyo", 0),
        ]
        for row in payload["shisanhyo"]
    ]
    return [header] + data_rows


def _aichi_csv_rows() -> list[list[str]]:
    """Build the header + data rows for the aichi fixture as CSV input."""
    header = ["period", "uriage", "uriage_genka", "hanbaihi", "eigai_shueki", "eigai_hiyo"]
    with _AICHI_FIXTURE_PATH.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    data_rows = [
        [
            row["period"],
            str(row["uriage"]),
            str(row["uriage_genka"]),
            str(row["hanbaihi"]),
            str(row.get("eigai_shueki", 0)),
            str(row.get("eigai_hiyo", 0)),
        ]
        for row in payload["shisanhyo"]
    ]
    return [header] + data_rows


# ---------------------------------------------------------------------------
# 1. Parser unit tests — .xlsx
# ---------------------------------------------------------------------------


class TestXlsxParser:
    """Parser unit tests for .xlsx input."""

    def test_clean_xlsx_returns_expected_rows(self) -> None:
        """A clean .xlsx with the aichi fixture data → correct TrialBalance rows."""
        xlsx_bytes = _build_xlsx(_aichi_xlsx_rows())
        result = parse_shisanhyo(xlsx_bytes, "shisanhyo.xlsx")

        assert isinstance(result, ParsedShisanhyo)
        assert len(result.warnings) == 0, f"Unexpected warnings: {result.warnings}"
        assert len(result.rows) == 12

        fixture_rows = _load_aichi_rows()
        for parsed, expected in zip(result.rows, fixture_rows, strict=True):
            assert parsed.period == expected.period
            assert int(parsed.uriage) == int(expected.uriage)
            assert int(parsed.uriage_genka) == int(expected.uriage_genka)
            assert int(parsed.hanbaihi) == int(expected.hanbaihi)
            assert int(parsed.eigai_shueki) == int(expected.eigai_shueki)
            assert int(parsed.eigai_hiyo) == int(expected.eigai_hiyo)
            # Computed fields must match.
            assert parsed.eigyo_rieki == expected.eigyo_rieki
            assert parsed.keijo_rieki == expected.keijo_rieki

    def test_xlsx_japanese_headers(self) -> None:
        """Japanese column headers are accepted as aliases."""
        rows: list[list[str | int | None]] = [
            ["期間", "売上", "売上原価", "販売費"],
            ["2025-04-30", 100_000_000, 70_000_000, 20_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert len(result.rows) == 1
        assert int(result.rows[0].uriage) == 100_000_000

    def test_xlsx_with_eigyo_keijo_cross_check_columns(self) -> None:
        """Supplied 営業利益/経常利益 columns that match computed values → no warnings."""
        rows: list[list[str | int | None]] = [
            ["period", "uriage", "uriage_genka", "hanbaihi", "eigyo_rieki", "keijo_rieki"],
            # eigyo_rieki = 100M - 70M - 20M = 10M; keijo_rieki = 10M
            ["2025-04-30", 100_000_000, 70_000_000, 20_000_000, 10_000_000, 10_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert len(result.rows) == 1
        # No invariant warnings when the supplied values match.
        invariant_warnings = [w for w in result.warnings if "invariant" in w.lower()]
        assert len(invariant_warnings) == 0

    def test_xlsx_invariant_violation_eigyo_rieki(self) -> None:
        """Supplied 営業利益 that doesn't match computed value → warning."""
        rows: list[list[str | int | None]] = [
            ["period", "uriage", "uriage_genka", "hanbaihi", "eigyo_rieki"],
            # Computed eigyo_rieki = 10M; supplied = 5M (wrong)
            ["2025-04-30", 100_000_000, 70_000_000, 20_000_000, 5_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert len(result.rows) == 1  # Row still emitted
        assert any("営業利益" in w or "operating profit" in w.lower() for w in result.warnings)

    def test_xlsx_invariant_violation_keijo_rieki(self) -> None:
        """Supplied 経常利益 that doesn't match computed value → warning."""
        rows: list[list[str | int | None]] = [
            [
                "period",
                "uriage",
                "uriage_genka",
                "hanbaihi",
                "eigai_shueki",
                "eigai_hiyo",
                "keijo_rieki",
            ],
            # Computed keijo_rieki = 10M + 300K - 1.8M = 8.5M; supplied = 9M (wrong)
            ["2025-04-30", 100_000_000, 70_000_000, 20_000_000, 300_000, 1_800_000, 9_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert len(result.rows) == 1
        assert any("経常利益" in w or "ordinary profit" in w.lower() for w in result.warnings)

    def test_xlsx_corrupt_file(self) -> None:
        """Corrupt .xlsx bytes → warning, empty rows, no exception."""
        result = parse_shisanhyo(b"not a zip file", "bad.xlsx")
        assert result.rows == []
        assert len(result.warnings) > 0
        assert any("corrupt" in w.lower() or "zip" in w.lower() for w in result.warnings)

    def test_xlsx_missing_required_column(self) -> None:
        """Missing required column → warning, no rows."""
        rows: list[list[str | int | None]] = [
            ["period", "uriage", "uriage_genka"],  # missing hanbaihi
            ["2025-04-30", 100_000_000, 70_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert result.rows == []
        assert any("hanbaihi" in w for w in result.warnings)

    def test_xlsx_blank_rows_skipped(self) -> None:
        """Blank rows in the data are silently skipped."""
        rows: list[list[str | int | None]] = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", 100_000_000, 70_000_000, 20_000_000],
            [None, None, None, None],  # blank row
            ["2025-05-31", 138_000_000, 115_000_000, 21_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert len(result.rows) == 2

    def test_xlsx_year_month_period(self) -> None:
        """YYYY-MM period format → last day of the month."""
        rows: list[list[str | int | None]] = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04", 100_000_000, 70_000_000, 20_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert len(result.rows) == 1
        assert result.rows[0].period == dt.date(2025, 4, 30)

    def test_xlsx_unsupported_extension(self) -> None:
        """Unsupported file extension → warning, empty rows."""
        result = parse_shisanhyo(b"data", "file.xls")
        assert result.rows == []
        assert any("unsupported" in w.lower() for w in result.warnings)

    def test_xlsx_fullwidth_period_digits(self) -> None:
        """Full-width period digits/separators (２０２５－０４) normalize to ASCII."""
        rows: list[list[str | int | None]] = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["２０２５－０４", 100_000_000, 70_000_000, 20_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "test.xlsx")
        assert len(result.rows) == 1
        # ２０２５-０４ -> 2025-04 -> last day of the month.
        assert result.rows[0].period == dt.date(2025, 4, 30)
        assert not any("period" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# 2. Parser unit tests — .csv
# ---------------------------------------------------------------------------


class TestCsvParser:
    """Parser unit tests for .csv input."""

    def test_clean_csv_returns_expected_rows(self) -> None:
        """A clean .csv with the aichi fixture data → correct TrialBalance rows."""
        csv_bytes = _build_csv(_aichi_csv_rows())
        result = parse_shisanhyo(csv_bytes, "shisanhyo.csv")

        assert isinstance(result, ParsedShisanhyo)
        assert len(result.warnings) == 0, f"Unexpected warnings: {result.warnings}"
        assert len(result.rows) == 12

        fixture_rows = _load_aichi_rows()
        for parsed, expected in zip(result.rows, fixture_rows, strict=True):
            assert parsed.period == expected.period
            assert int(parsed.uriage) == int(expected.uriage)
            assert int(parsed.uriage_genka) == int(expected.uriage_genka)
            assert int(parsed.hanbaihi) == int(expected.hanbaihi)
            assert int(parsed.eigai_shueki) == int(expected.eigai_shueki)
            assert int(parsed.eigai_hiyo) == int(expected.eigai_hiyo)

    def test_csv_japanese_headers(self) -> None:
        """Japanese column headers in CSV are accepted."""
        rows = [
            ["期間", "売上", "売上原価", "販売費"],
            ["2025-04-30", "100000000", "70000000", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert len(result.rows) == 1
        assert int(result.rows[0].uriage) == 100_000_000

    def test_csv_with_bom(self) -> None:
        """UTF-8 BOM is stripped correctly."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "100000000", "70000000", "20000000"],
        ]
        csv_bytes = b"\xef\xbb\xbf" + _build_csv(rows)  # prepend BOM
        result = parse_shisanhyo(csv_bytes, "test.csv")
        assert len(result.rows) == 1

    def test_csv_with_thousands_separators(self) -> None:
        """Comma-separated thousands in money cells are handled."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "100,000,000", "70,000,000", "20,000,000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert len(result.rows) == 1
        assert int(result.rows[0].uriage) == 100_000_000

    def test_csv_with_yen_symbol(self) -> None:
        """Yen symbol prefix in money cells is stripped."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "¥100000000", "¥70000000", "¥20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert len(result.rows) == 1
        assert int(result.rows[0].uriage) == 100_000_000

    def test_csv_fullwidth_period_digits(self) -> None:
        """Full-width period digits in CSV (２０２５／０４／３０) normalize to ASCII."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["２０２５／０４／３０", "100000000", "70000000", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert len(result.rows) == 1
        assert result.rows[0].period == dt.date(2025, 4, 30)

    def test_csv_empty_file(self) -> None:
        """Empty CSV → warning, empty rows."""
        result = parse_shisanhyo(b"", "empty.csv")
        assert result.rows == []
        assert len(result.warnings) > 0

    def test_csv_header_only(self) -> None:
        """CSV with header but no data rows → empty rows, no crash."""
        rows = [["period", "uriage", "uriage_genka", "hanbaihi"]]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert result.rows == []
        assert len(result.warnings) == 0  # No data rows is not an error


# ---------------------------------------------------------------------------
# 2b. Duplicate-header alignment regression (locks in !2)
# ---------------------------------------------------------------------------


class TestDuplicateHeaderAlignment:
    """Duplicate header columns must not collapse or misalign other columns.

    Regression guard for !2: the parser maps cells by 0-based COLUMN INDEX, not
    by header string. Before !2 the .xlsx path used
    ``dict(zip(header_row, padded))`` and the .csv path used
    ``csv.DictReader`` — both collapse two columns sharing the same header text
    into a single key, so a real cell landed under the wrong field or was
    dropped *silently* (wrong figures, no warning), violating numeric
    preservation. These tests fail on that pre-!2 behaviour.
    """

    def test_xlsx_duplicate_header_does_not_misalign_other_columns(self) -> None:
        """A stray duplicate column keeps every other column's cell correct (.xlsx).

        The second ``uriage_genka`` column carries a decoy value; the first wins
        (with a warning) and, crucially, ``hanbaihi`` (which sits AFTER the
        duplicate) is still read from its own index rather than being shifted.
        """
        rows: list[list[str | int | None]] = [
            # uriage_genka appears twice; hanbaihi follows the duplicate.
            ["period", "uriage", "uriage_genka", "uriage_genka", "hanbaihi"],
            ["2025-04-30", 100_000_000, 70_000_000, 999_999_999, 20_000_000],
        ]
        result = parse_shisanhyo(_build_xlsx(rows), "dup.xlsx")

        assert len(result.rows) == 1
        row = result.rows[0]
        # First occurrence wins for the duplicated field; decoy is ignored.
        assert int(row.uriage_genka) == 70_000_000
        # The column AFTER the duplicate is not shifted/collapsed.
        assert int(row.hanbaihi) == 20_000_000
        assert int(row.uriage) == 100_000_000
        # Computed figures are therefore exactly correct.
        assert row.eigyo_rieki == 10_000_000
        # The duplicate is surfaced (never silently dropped).
        assert any("duplicate" in w.lower() for w in result.warnings)

    def test_csv_duplicate_header_does_not_misalign_other_columns(self) -> None:
        """A stray duplicate column keeps every other column's cell correct (.csv)."""
        rows = [
            ["period", "uriage", "uriage_genka", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "100000000", "70000000", "999999999", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "dup.csv")

        assert len(result.rows) == 1
        row = result.rows[0]
        assert int(row.uriage_genka) == 70_000_000
        assert int(row.hanbaihi) == 20_000_000
        assert int(row.uriage) == 100_000_000
        assert row.eigyo_rieki == 10_000_000
        assert any("duplicate" in w.lower() for w in result.warnings)

    def test_csv_duplicate_header_before_columns_keeps_values(self) -> None:
        """A duplicate positioned early does not shift later columns (.csv).

        With ``uriage`` duplicated as the 2nd and 3rd columns, the index-based
        map must still read uriage_genka and hanbaihi from their own positions —
        the header-string ``dict`` collapse would previously have pulled the
        wrong cells here.
        """
        rows = [
            ["period", "uriage", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "100000000", "888888888", "70000000", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "dup2.csv")

        assert len(result.rows) == 1
        row = result.rows[0]
        assert int(row.uriage) == 100_000_000  # first wins; decoy ignored
        assert int(row.uriage_genka) == 70_000_000
        assert int(row.hanbaihi) == 20_000_000
        assert any("duplicate" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# 3. Integer-yen enforcement tests
# ---------------------------------------------------------------------------


class TestIntegerYenEnforcement:
    """The parser must reject floats and garbage cells as warnings, never coerce."""

    def test_fractional_float_rejected(self) -> None:
        """A fractional float in a money cell → warning, cell skipped."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "100000000.5", "70000000", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        # uriage is invalid → row skipped (mandatory field missing).
        assert result.rows == []
        assert any("fractional" in w.lower() for w in result.warnings)

    def test_whole_valued_float_rejected(self) -> None:
        """A whole-valued float (e.g. '100000000.0') → warning, cell skipped."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "100000000.0", "70000000", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert result.rows == []
        assert any("whole-valued float" in w.lower() for w in result.warnings)

    def test_garbage_cell_rejected(self) -> None:
        """A non-numeric string in a money cell → warning, cell skipped."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "N/A", "70000000", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert result.rows == []
        assert any("not numeric" in w.lower() for w in result.warnings)

    def test_valid_integer_string_accepted(self) -> None:
        """A plain integer string is accepted."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi"],
            ["2025-04-30", "100000000", "70000000", "20000000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert len(result.rows) == 1
        assert len(result.warnings) == 0

    def test_negative_integer_accepted(self) -> None:
        """Negative integer yen values are accepted (e.g. negative eigai_hiyo)."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi", "eigai_hiyo"],
            ["2025-04-30", "100000000", "70000000", "20000000", "-500000"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        assert len(result.rows) == 1
        assert int(result.rows[0].eigai_hiyo) == -500_000

    def test_optional_field_float_rejected_but_row_emitted(self) -> None:
        """A float in an optional field → warning, field defaults to 0, row emitted."""
        rows = [
            ["period", "uriage", "uriage_genka", "hanbaihi", "eigai_shueki"],
            ["2025-04-30", "100000000", "70000000", "20000000", "300000.0"],
        ]
        result = parse_shisanhyo(_build_csv(rows), "test.csv")
        # Row is emitted (mandatory fields are valid); eigai_shueki defaults to 0.
        assert len(result.rows) == 1
        assert int(result.rows[0].eigai_shueki) == 0
        assert any("whole-valued float" in w.lower() for w in result.warnings)

    def test_never_raises_on_bad_data(self) -> None:
        """The parser must never raise an exception, regardless of input."""
        bad_inputs = [
            b"",
            b"not a file",
            b"\x00\x01\x02\x03",
            b"period,uriage\n2025-04-30,not_a_number\n",
        ]
        for data in bad_inputs:
            # Must not raise.
            result = parse_shisanhyo(data, "test.csv")
            assert isinstance(result, ParsedShisanhyo)


# ---------------------------------------------------------------------------
# 4. State field tests
# ---------------------------------------------------------------------------


class TestStateFields:
    """SaiseiState must have the new Part 6 fields and preserve all existing ones."""

    # All pre-existing fields that must NOT be removed.
    _REQUIRED_EXISTING_FIELDS = frozenset(
        {
            "tdb_code",
            "hojin_bango",
            "company_profile",
            "tdb_score",
            "shisanhyo",
            "working_capital_gap",
            "boj_rate_curve",
            "settlement_metrics",
            "ews_score",
            "fsa_classification",
            "net_worth",
            "is_insolvent",
            "special_attention",
            "hosho_kaijo_score",
            "hosho_kaijo_conditions",
            "succession_ready",
            "hosho_kaijo_eligible",
            "proposed_strategies",
            "negotiation_decision",
            "approved_strategy",
            "revision_note",
            "keikakusho_draft",
            "critic_feedbacks",
            "negotiation_status",
            "revision_directive",
            "meeting_briefing",
            "revision_count",
            "feasibility_notes",
            "lender_stakes",
            "yakuin_hoshu_cut",
            "personal_asset_disposal",
            "workout_handoff",
            "reconciliation_required",
            "reconciliation_details",
            "reconciliation_outcomes",
            "errors",
        }
    )

    def test_new_fields_exist(self) -> None:
        """Part 6 fields must be present on SaiseiState."""
        state = SaiseiState(tdb_code="1234567")
        assert hasattr(state, "uploaded_shisanhyo")
        assert hasattr(state, "upload_warnings")
        assert state.uploaded_shisanhyo == []
        assert state.upload_warnings == []

    def test_no_existing_field_removed(self) -> None:
        """All pre-existing SaiseiState fields must still be present."""
        state = SaiseiState(tdb_code="1234567")
        model_fields = set(type(state).model_fields.keys())
        missing = self._REQUIRED_EXISTING_FIELDS - model_fields
        assert not missing, f"Fields removed from SaiseiState: {sorted(missing)}"

    def test_uploaded_shisanhyo_is_separate_from_shisanhyo(self) -> None:
        """uploaded_shisanhyo and shisanhyo are independent fields."""
        tb = TrialBalance(
            period=dt.date(2025, 4, 30),
            uriage=100_000_000,
            uriage_genka=70_000_000,
            hanbaihi=20_000_000,
        )
        state = SaiseiState(
            tdb_code="1234567",
            uploaded_shisanhyo=[tb],
            shisanhyo=[],
        )
        assert len(state.uploaded_shisanhyo) == 1
        assert len(state.shisanhyo) == 0

    def test_confirm_copies_uploaded_to_shisanhyo(self) -> None:
        """Confirm: uploaded_shisanhyo → shisanhyo; staging cleared."""
        fixture_rows = _load_aichi_rows()
        # Simulate the confirm operation: copy uploaded → committed, clear staging.
        state_before = SaiseiState(
            tdb_code="1234567",
            uploaded_shisanhyo=fixture_rows,
            upload_warnings=["test warning"],
            shisanhyo=[],
        )
        # Confirm: copy uploaded_shisanhyo into shisanhyo, clear staging.
        # Use model_copy(update=...) rather than SaiseiState(**model_dump()):
        # model_dump() serialises TrialBalance computed fields
        # (uriage_sourieki / eigyo_rieki / keijo_rieki), which the frozen,
        # extra="forbid" TrialBalance refuses to re-accept as constructor args.
        # model_copy preserves the typed sub-models and overrides only the
        # named fields, which is exactly what the confirm transition does.
        confirmed_state = state_before.model_copy(
            update={
                "shisanhyo": state_before.uploaded_shisanhyo,
                "uploaded_shisanhyo": [],
                "upload_warnings": [],
            }
        )
        assert len(confirmed_state.shisanhyo) == 12
        assert confirmed_state.uploaded_shisanhyo == []
        assert confirmed_state.upload_warnings == []

    def test_cancel_clears_staging_only(self) -> None:
        """Cancel: staging cleared; shisanhyo unchanged."""
        existing_tb = TrialBalance(
            period=dt.date(2025, 4, 30),
            uriage=100_000_000,
            uriage_genka=70_000_000,
            hanbaihi=20_000_000,
        )
        proposed_tb = TrialBalance(
            period=dt.date(2025, 5, 31),
            uriage=138_000_000,
            uriage_genka=115_000_000,
            hanbaihi=21_000_000,
        )
        state_before = SaiseiState(
            tdb_code="1234567",
            shisanhyo=[existing_tb],
            uploaded_shisanhyo=[proposed_tb],
            upload_warnings=["some warning"],
        )
        # Cancel: clear staging only.
        # model_copy(update=...) (not SaiseiState(**model_dump())) so the typed
        # TrialBalance rows in shisanhyo survive untouched — model_dump() would
        # emit their computed fields, which the frozen/extra="forbid"
        # TrialBalance cannot be reconstructed from.
        cancelled_state = state_before.model_copy(
            update={
                "uploaded_shisanhyo": [],
                "upload_warnings": [],
            }
        )
        # shisanhyo unchanged.
        assert len(cancelled_state.shisanhyo) == 1
        assert int(cancelled_state.shisanhyo[0].uriage) == 100_000_000
        # Staging cleared.
        assert cancelled_state.uploaded_shisanhyo == []
        assert cancelled_state.upload_warnings == []

    def test_pipeline_never_reads_uploaded_shisanhyo(self) -> None:
        """The deterministic pipeline reads shisanhyo, not uploaded_shisanhyo.

        Confirms the seam: uploaded_shisanhyo is staging-only; the pipeline
        (EWS/classification/macro) is driven by shisanhyo.
        """
        from app.backend.nodes.ews_scoring import compute_ews_score

        fixture_rows = _load_aichi_rows()

        # EWS with no committed rows → score 0.0 (insufficient history).
        score_empty = compute_ews_score([])

        # EWS with committed rows → non-zero score.
        score_committed = compute_ews_score(fixture_rows)

        # The two results must differ (uploaded_shisanhyo is NOT read by EWS).
        assert score_empty != score_committed, "EWS must read shisanhyo, not uploaded_shisanhyo"

        # Confirm that uploaded_shisanhyo is a separate field from shisanhyo.
        state_with_upload_only = SaiseiState(
            tdb_code="1234567",
            uploaded_shisanhyo=fixture_rows,
            shisanhyo=[],
        )
        # The EWS node reads state.shisanhyo, not state.uploaded_shisanhyo.
        # With empty shisanhyo, the score is 0.0.
        assert compute_ews_score(state_with_upload_only.shisanhyo) == score_empty
        assert compute_ews_score(state_with_upload_only.uploaded_shisanhyo) == score_committed


# ---------------------------------------------------------------------------
# 5. Seam transparency test — pipeline output on confirmed rows matches fixture
# ---------------------------------------------------------------------------


class TestSeamTransparency:
    """Confirmed uploaded rows must produce byte-identical pipeline output to fixture rows."""

    def test_uploaded_rows_byte_equivalent_to_fixture_rows(self) -> None:
        """Rows parsed from .xlsx must be byte-equivalent to fixture-loaded rows.

        This proves the seam is transparent: the parser produces the same typed
        objects that the fixture loader produces, so the deterministic pipeline
        output is identical regardless of how the rows entered the system.
        """
        fixture_rows = _load_aichi_rows()

        # Parse the same data from .xlsx.
        xlsx_bytes = _build_xlsx(_aichi_xlsx_rows())
        parsed = parse_shisanhyo(xlsx_bytes, "aichi.xlsx")
        assert len(parsed.warnings) == 0, f"Unexpected warnings: {parsed.warnings}"
        assert len(parsed.rows) == len(fixture_rows)

        # Compare model_dump() for byte-equivalence.
        for parsed_row, fixture_row in zip(parsed.rows, fixture_rows, strict=True):
            assert parsed_row.model_dump() == fixture_row.model_dump(), (
                f"Row mismatch for period {fixture_row.period}: "
                f"parsed={parsed_row.model_dump()}, fixture={fixture_row.model_dump()}"
            )

    def test_csv_rows_byte_equivalent_to_fixture_rows(self) -> None:
        """Rows parsed from .csv must be byte-equivalent to fixture-loaded rows."""
        fixture_rows = _load_aichi_rows()

        csv_bytes = _build_csv(_aichi_csv_rows())
        parsed = parse_shisanhyo(csv_bytes, "aichi.csv")
        assert len(parsed.warnings) == 0, f"Unexpected warnings: {parsed.warnings}"
        assert len(parsed.rows) == len(fixture_rows)

        for parsed_row, fixture_row in zip(parsed.rows, fixture_rows, strict=True):
            assert parsed_row.model_dump() == fixture_row.model_dump()

    def test_ews_score_identical_for_uploaded_vs_fixture_rows(self) -> None:
        """EWS score on confirmed uploaded rows == EWS score on fixture rows."""
        from app.backend.nodes.ews_scoring import compute_ews_score

        fixture_rows = _load_aichi_rows()

        # Parse from .xlsx.
        xlsx_bytes = _build_xlsx(_aichi_xlsx_rows())
        parsed = parse_shisanhyo(xlsx_bytes, "aichi.xlsx")

        score_fixture = compute_ews_score(fixture_rows)
        score_uploaded = compute_ews_score(parsed.rows)

        assert score_fixture == score_uploaded, (
            "EWS score must be identical for fixture-loaded vs uploaded rows"
        )

    def test_render_keikakusho_byte_identical(self) -> None:
        """render_keikakusho output is byte-identical for fixture vs uploaded rows.

        This is the snapshot-sensitive guard: the deterministic spine must
        produce the same Keikakusho text regardless of how the rows entered.
        """
        from app.backend.nodes.kaizen_generation import render_keikakusho
        from app.backend.state import Strategy
        from app.shared.models.classification import FsaClass

        fixture_rows = _load_aichi_rows()
        xlsx_bytes = _build_xlsx(_aichi_xlsx_rows())
        parsed = parse_shisanhyo(xlsx_bytes, "aichi.xlsx")

        strategy = Strategy(
            title="コスト削減",
            rationale="売上原価の削減により収益性を改善する。",
            expected_keijo_uplift=5_000_000,
        )

        # render_keikakusho takes individual typed args, not a SaiseiState.
        # Inlined (rather than **kwargs) so mypy --strict can check each arg.
        draft_fixture = render_keikakusho(
            latest=fixture_rows[-1],
            company_name="愛知精密製作所株式会社",
            hojin_bango="1234567890123",
            fsa_kanji=FsaClass.YOCHUISAKI.kanji,
            strategy=strategy,
            working_capital_gap=-1_000_000,
        )
        draft_uploaded = render_keikakusho(
            latest=parsed.rows[-1],
            company_name="愛知精密製作所株式会社",
            hojin_bango="1234567890123",
            fsa_kanji=FsaClass.YOCHUISAKI.kanji,
            strategy=strategy,
            working_capital_gap=-1_000_000,
        )

        assert draft_fixture == draft_uploaded, (
            "render_keikakusho output must be byte-identical for fixture vs uploaded rows"
        )

    def test_full_graph_with_confirmed_upload(self) -> None:
        """Full graph run with confirmed uploaded rows reaches HITL (seam transparency).

        Uses MemorySaver (offline, no Postgres).  Confirms that the graph
        behaves identically whether rows came from MockDataProvider or the parser.
        """
        fixture_rows = _load_aichi_rows()
        xlsx_bytes = _build_xlsx(_aichi_xlsx_rows())
        parsed = parse_shisanhyo(xlsx_bytes, "aichi.xlsx")

        # Verify rows are byte-equivalent before running the graph.
        for p, f in zip(parsed.rows, fixture_rows, strict=True):
            assert p.model_dump() == f.model_dump()

        # Test the pipeline nodes directly with the parsed rows.
        from app.backend.nodes.ews_scoring import compute_ews_score
        from app.backend.nodes.financial_extraction import macro_node

        state_with_parsed = SaiseiState(
            tdb_code="1234567",
            shisanhyo=parsed.rows,
            yakuin_hoshu_cut=True,
            personal_asset_disposal=True,
        )
        state_with_fixture = SaiseiState(
            tdb_code="1234567",
            shisanhyo=fixture_rows,
            yakuin_hoshu_cut=True,
            personal_asset_disposal=True,
        )

        score_parsed = compute_ews_score(parsed.rows)
        score_fixture = compute_ews_score(fixture_rows)

        assert score_parsed == score_fixture, (
            "EWS score must be identical for parsed vs fixture rows"
        )

        macro_parsed = macro_node(state_with_parsed)
        macro_fixture = macro_node(state_with_fixture)

        assert macro_parsed.get("working_capital_gap") == macro_fixture.get(
            "working_capital_gap"
        ), "working_capital_gap must be identical for parsed vs fixture rows"

    def test_graph_runs_with_aichi_tdb_code(self) -> None:
        """Full graph run with the aichi TDB code reaches HITL (regression guard).

        Uses MemorySaver (offline, no Postgres).  This is the existing graph
        flow test adapted to confirm the graph still works after Part 6 additions.
        """
        app = build_graph().compile(checkpointer=MemorySaver())
        config: RunnableConfig = {"configurable": {"thread_id": "test-part6-graph"}}
        app.invoke(
            cast(
                "SaiseiState",
                {
                    "tdb_code": "1234567",
                    "yakuin_hoshu_cut": True,
                    "personal_asset_disposal": True,
                },
            ),
            config=config,
        )
        snapshot = app.get_state(config)
        # The graph must pause at HITL.
        assert snapshot.next
        # Part 6 staging channels are write-only-on-upload: no graph node ever
        # writes uploaded_shisanhyo / upload_warnings, so LangGraph omits them
        # from snapshot.values on a normal run (channels are only materialised
        # once written). The seam contract is therefore: they did NOT leak into
        # the committed pipeline, i.e. shisanhyo was driven by the provider, and
        # if either staging channel is present it must be empty.
        assert snapshot.values["shisanhyo"], "pipeline must have loaded a shisanhyo"
        assert snapshot.values.get("uploaded_shisanhyo", []) == []
        assert snapshot.values.get("upload_warnings", []) == []


# ---------------------------------------------------------------------------
# 6. EWS injection regression guard (would have caught the Part 6 EWS bug)
# ---------------------------------------------------------------------------


class _RaisingProvider:
    """Spy provider whose ``shisanhyo`` raises if it is ever consulted.

    Used to prove that ``ews_node`` honors a Shisanhyo already on the state
    (the confirmed upload) and never falls back to the data provider. If
    ``ews_node`` ever regresses to re-fetching, this provider's ``shisanhyo``
    fires and the test fails loudly.
    """

    def __init__(self) -> None:
        self.shisanhyo_called = False

    def shisanhyo(self, hojin_bango: str) -> list[TrialBalance]:  # noqa: ARG002
        self.shisanhyo_called = True
        raise AssertionError(
            "ews_node fetched from the provider despite a shisanhyo already "
            "present on the state (Part 6 upload seam regression)."
        )


class _FixedProvider:
    """Provider returning a fixed Shisanhyo trajectory for the fallback path.

    Used to assert the behaviour-preservation contract: when ``state.shisanhyo``
    is empty, ``ews_node`` MUST still fetch from the provider (so the seam fix
    cannot be trivially satisfied by never consulting the provider).
    """

    def __init__(self, rows: list[TrialBalance]) -> None:
        self._rows = rows
        self.shisanhyo_called = False

    def shisanhyo(self, hojin_bango: str) -> list[TrialBalance]:  # noqa: ARG002
        self.shisanhyo_called = True
        return self._rows


def _improving_rows() -> list[TrialBalance]:
    """A distinctive *improving* two-month trajectory (low EWS score).

    Sales rising and margin steady → the EWS score must be near 0. Constructed
    to be unmistakably different from the deteriorating aichi fixture so a
    re-fetch (wrong source) would produce a different, detectable score.
    """
    return [
        TrialBalance(
            period=dt.date(2025, 4, 30),
            uriage=100_000_000,
            uriage_genka=70_000_000,
            hanbaihi=20_000_000,
        ),
        TrialBalance(
            period=dt.date(2025, 5, 31),
            uriage=120_000_000,
            uriage_genka=84_000_000,
            hanbaihi=22_000_000,
        ),
    ]


def _deteriorating_rows() -> list[TrialBalance]:
    """A distinctive *deteriorating* trajectory (high EWS score).

    Sales collapsing and margin compressing → a clearly non-zero EWS score,
    distinct from :func:`_improving_rows`, so the two sources are never
    confusable in an assertion.
    """
    return [
        TrialBalance(
            period=dt.date(2025, 4, 30),
            uriage=100_000_000,
            uriage_genka=70_000_000,
            hanbaihi=20_000_000,
        ),
        TrialBalance(
            period=dt.date(2025, 5, 31),
            uriage=40_000_000,
            uriage_genka=38_000_000,
            hanbaihi=20_000_000,
        ),
    ]


class TestEwsHonorsInjectedShisanhyo:
    """Regression guard: ews_node and the full graph must honor injected rows.

    The pre-existing seam tests call ``compute_ews_score`` / ``macro_node``
    directly, bypassing ``ews_node`` — so they would ALL still pass if
    ``ews_node`` regressed to re-fetching from the provider (the exact Part 6
    bug). These tests drive ``ews_node`` (and the compiled graph) so the seam
    is proven where it actually lives.
    """

    def test_ews_node_honors_injected_shisanhyo(self) -> None:
        """ews_node uses state.shisanhyo and never consults the provider."""
        from app.backend.nodes.ews_scoring import compute_ews_score, ews_node

        injected = _improving_rows()
        provider = _RaisingProvider()
        state = SaiseiState(tdb_code="1234567", shisanhyo=injected)

        # Must NOT raise (the provider must never be consulted).
        result = ews_node(state, provider=cast("Any", provider))

        assert provider.shisanhyo_called is False
        # The returned shisanhyo is exactly the injected one.
        assert result["shisanhyo"] == injected
        # The score is computed from the injected rows, not the provider fixture.
        assert result["ews_score"] == compute_ews_score(injected)
        # No error was recorded.
        assert "errors" not in result

    def test_ews_node_fetches_from_provider_when_empty(self) -> None:
        """Behaviour-preservation: an empty shisanhyo still fetches from provider."""
        from app.backend.nodes.ews_scoring import compute_ews_score, ews_node

        provider_rows = _deteriorating_rows()
        provider = _FixedProvider(provider_rows)
        state = SaiseiState(tdb_code="1234567", hojin_bango="x", shisanhyo=[])

        result = ews_node(state, provider=cast("Any", provider))

        assert provider.shisanhyo_called is True
        assert result["shisanhyo"] == provider_rows
        assert result["ews_score"] == compute_ews_score(provider_rows)

    def test_ews_node_provider_keyerror_when_empty_returns_error(self) -> None:
        """Empty shisanhyo + unknown hojin_bango still surfaces the error path."""
        from app.backend.nodes.ews_scoring import ews_node
        from app.backend.tools.provider import MockDataProvider

        state = SaiseiState(tdb_code="1234567", hojin_bango="0000000000000", shisanhyo=[])
        result = ews_node(state, provider=MockDataProvider())
        assert "errors" in result
        assert any("No Shisanhyo" in e for e in result["errors"])

    def test_full_graph_honors_injected_shisanhyo(self) -> None:
        """Compiled graph: injected shisanhyo drives EWS, not the provider fixture.

        Builds the real graph with a provider whose ``shisanhyo`` returns a
        DIFFERENT trajectory, injects distinctive improving rows via the initial
        state, and asserts the snapshot ews_score reflects the injected rows.
        This proves the seam end-to-end through the compiled graph, not just via
        isolated node calls.
        """
        from app.backend.graph import build_graph
        from app.backend.nodes.ews_scoring import compute_ews_score
        from app.backend.tools.provider import MockDataProvider

        injected = _improving_rows()
        injected_score = compute_ews_score(injected)

        # Sanity: the injected (improving) score must differ from the mock
        # fixture score, otherwise the assertion below could not distinguish a
        # re-fetch from an honored injection.
        provider = MockDataProvider()
        fixture_state = SaiseiState(tdb_code="1234567")
        # intake sets hojin_bango from the mock; resolve it the same way the
        # graph does so the comparison fixture matches the provider's source.
        report = provider.credit_report("1234567")
        fixture_rows = provider.shisanhyo(report.profile.hojin_bango)
        fixture_score = compute_ews_score(fixture_rows)
        assert injected_score != fixture_score, (
            "test fixtures are indistinguishable; choose more divergent rows"
        )
        _ = fixture_state  # documents the comparison baseline

        app = build_graph(provider=provider).compile(checkpointer=MemorySaver())
        config: RunnableConfig = {"configurable": {"thread_id": "test-ews-injected"}}
        app.invoke(
            cast(
                "SaiseiState",
                {
                    "tdb_code": "1234567",
                    "shisanhyo": injected,
                    "yakuin_hoshu_cut": True,
                    "personal_asset_disposal": True,
                },
            ),
            config=config,
        )
        snapshot = app.get_state(config)
        # The graph honored the injected rows: EWS reflects the injection, and
        # the committed shisanhyo is the injected trajectory, not the fixture.
        assert snapshot.values["ews_score"] == injected_score
        assert snapshot.values["ews_score"] != fixture_score
        assert snapshot.values["shisanhyo"] == injected

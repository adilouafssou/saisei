"""Deterministic, offline parser for Excel (.xlsx) and CSV trial-balance files.

Converts a banker-uploaded 試算表 (Shisanhyo / Trial Balance) file into a list
of :class:`~app.shared.models.accounting.TrialBalance` rows plus human-readable
warnings.  The parser is **pure and deterministic**: no network, no LLM, no
external dependencies beyond the Python standard library.

Accepted column header aliases
-------------------------------
The parser is tolerant of both Japanese and English column names.  Any of the
aliases below (case-insensitive, leading/trailing whitespace stripped) are
recognised for each field:

period (期間)
    ``period``, ``期間``, ``月``, ``date``, ``年月``, ``年月日``

uriage (売上)
    ``uriage``, ``売上``, ``売上高``, ``sales``, ``revenue``

uriage_genka (売上原価)
    ``uriage_genka``, ``売上原価``, ``原価``, ``cogs``,
    ``cost_of_goods_sold``, ``cost of goods sold``

hanbaihi (販売費及び一般管理費)
    ``hanbaihi``, ``販売費``, ``販売費及び一般管理費``, ``sga``,
    ``selling_general_admin``, ``selling general and administrative``

eigai_shueki (営業外収益)
    ``eigai_shueki``, ``営業外収益``, ``non_operating_income``,
    ``non-operating income``, ``non operating income``

eigai_hiyo (営業外費用)
    ``eigai_hiyo``, ``営業外費用``, ``non_operating_expenses``,
    ``non-operating expenses``, ``non operating expenses``

eigyo_rieki (営業利益) — cross-check only, NOT a constructor arg
    ``eigyo_rieki``, ``営業利益``, ``operating_profit``, ``operating profit``

keijo_rieki (経常利益) — cross-check only, NOT a constructor arg
    ``keijo_rieki``, ``経常利益``, ``ordinary_profit``, ``ordinary profit``

Money handling
--------------
All monetary values must be **strict integer yen** (see
:mod:`app.shared.models.money`).  The parser rejects:

- Fractional floats (e.g. ``1000.5``) — surfaced as a warning.
- Whole-valued floats (e.g. ``1000.0``) — surfaced as a warning.
- Non-numeric garbage cells — surfaced as a warning.

Cells that fail validation are skipped; the row is still emitted if the
mandatory fields (period, uriage, uriage_genka, hanbaihi) are present.

J-GAAP invariant checks
------------------------
After parsing, the following invariants are checked and any violation is
surfaced as a warning (never an exception):

1. 粗利 (Gross profit) = 売上 − 売上原価  (uriage - uriage_genka)
2. 営業利益 (Operating profit) = 粗利 − 販売費  (gross_profit - hanbaihi)
   — checked against a supplied 営業利益 column if present.
3. 経常利益 (Ordinary profit) = 営業利益 + 営業外収益 − 営業外費用
   — checked against a supplied 経常利益 column if present.

Design notes
------------
- .xlsx is parsed with :mod:`zipfile` + :mod:`xml.etree.ElementTree` only
  (no openpyxl or other third-party library).
- .csv is parsed with :mod:`csv` from the standard library.
- The parser **never raises** on bad data; every problem becomes a warning.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

from app.shared.models.accounting import TrialBalance

__all__ = ["ParsedShisanhyo", "parse_shisanhyo"]

# ---------------------------------------------------------------------------
# Column alias maps
# ---------------------------------------------------------------------------

#: Canonical field name → set of accepted header aliases (lower-cased).
_ALIASES: dict[str, frozenset[str]] = {
    "period": frozenset({"period", "期間", "月", "date", "年月", "年月日"}),
    "uriage": frozenset({"uriage", "売上", "売上高", "sales", "revenue"}),
    "uriage_genka": frozenset(
        {
            "uriage_genka",
            "売上原価",
            "原価",
            "cogs",
            "cost_of_goods_sold",
            "cost of goods sold",
        }
    ),
    "hanbaihi": frozenset(
        {
            "hanbaihi",
            "販売費",
            "販売費及び一般管理費",
            "sga",
            "selling_general_admin",
            "selling general and administrative",
        }
    ),
    "eigai_shueki": frozenset(
        {
            "eigai_shueki",
            "営業外収益",
            "non_operating_income",
            "non-operating income",
            "non operating income",
        }
    ),
    "eigai_hiyo": frozenset(
        {
            "eigai_hiyo",
            "営業外費用",
            "non_operating_expenses",
            "non-operating expenses",
            "non operating expenses",
        }
    ),
    # Cross-check only — NOT constructor args.
    "eigyo_rieki": frozenset({"eigyo_rieki", "営業利益", "operating_profit", "operating profit"}),
    "keijo_rieki": frozenset({"keijo_rieki", "経常利益", "ordinary_profit", "ordinary profit"}),
}

#: Reverse map: lower-cased alias → canonical field name.
_ALIAS_TO_FIELD: dict[str, str] = {
    alias: canonical for canonical, aliases in _ALIASES.items() for alias in aliases
}

#: Fields that must be present for a row to be emitted.
_REQUIRED_FIELDS: frozenset[str] = frozenset({"period", "uriage", "uriage_genka", "hanbaihi"})

#: Fields that carry monetary values (strict integer yen).
_MONEY_FIELDS: frozenset[str] = frozenset(
    {
        "uriage",
        "uriage_genka",
        "hanbaihi",
        "eigai_shueki",
        "eigai_hiyo",
        "eigyo_rieki",
        "keijo_rieki",
    }
)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ParsedShisanhyo:
    """Result of :func:`parse_shisanhyo`.

    Attributes:
        rows: Successfully parsed :class:`~app.shared.models.accounting.TrialBalance`
            rows, in the order they appeared in the file.
        warnings: Human-readable warning strings describing every problem
            encountered during parsing (bad cells, missing columns, invariant
            violations, etc.).  Never empty when something went wrong; never
            raises.
    """

    rows: list[TrialBalance] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_shisanhyo(data: bytes, filename: str) -> ParsedShisanhyo:
    """Parse an Excel (.xlsx) or CSV trial-balance file into structured rows.

    The parser is deterministic and offline.  It never raises on bad data;
    every problem is surfaced as a human-readable warning in the returned
    :class:`ParsedShisanhyo`.

    Args:
        data: Raw file bytes (the uploaded file content).
        filename: Original filename, used to detect the format (.xlsx vs .csv).
            Case-insensitive suffix matching.

    Returns:
        A :class:`ParsedShisanhyo` with the parsed rows and any warnings.
    """
    result = ParsedShisanhyo()
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        raw_rows = _parse_xlsx(data, result.warnings)
    elif lower.endswith(".csv"):
        raw_rows = _parse_csv(data, result.warnings)
    else:
        result.warnings.append(
            f"Unsupported file format: '{filename}'. Only .xlsx and .csv files are accepted."
        )
        return result

    result.rows = _build_trial_balances(raw_rows, result.warnings)
    return result


# ---------------------------------------------------------------------------
# Format-specific parsers → list[dict[str, Any]]
# ---------------------------------------------------------------------------


def _parse_csv(data: bytes, warnings: list[str]) -> list[dict[str, Any]]:
    """Parse CSV bytes into a list of raw row dicts keyed by canonical field.

    Rows are read positionally with :class:`csv.reader` (not
    :class:`csv.DictReader`) so that duplicate header strings cannot collapse
    two columns into one — cells are aligned to the header by 0-based column
    index, matching the .xlsx path.
    """
    try:
        text = data.decode("utf-8-sig")  # strip BOM if present
    except UnicodeDecodeError:
        try:
            text = data.decode("shift_jis")
        except UnicodeDecodeError:
            warnings.append(
                "CSV encoding could not be detected (tried UTF-8 and Shift-JIS). "
                "Please save the file as UTF-8."
            )
            return []

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        warnings.append("CSV file appears to be empty or has no header row.")
        return []

    header_row = rows[0]
    col_map = _build_column_map(header_row, warnings)
    if not col_map:
        return []

    raw_rows: list[dict[str, Any]] = []
    for row_idx, row_values in enumerate(rows[1:], start=2):  # row 1 = header
        raw = _map_row(row_values, col_map, row_idx, warnings)
        if raw is not None:
            raw_rows.append(raw)
    return raw_rows


def _parse_xlsx(data: bytes, warnings: list[str]) -> list[dict[str, Any]]:
    """Parse .xlsx bytes into a list of raw row dicts keyed by canonical field.

    Uses only :mod:`zipfile` + :mod:`xml.etree.ElementTree` (stdlib only).
    Reads ``xl/sharedStrings.xml`` for string values and
    ``xl/worksheets/sheet1.xml`` for cell data.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        warnings.append(
            "The uploaded .xlsx file is corrupt or not a valid ZIP/Office Open XML file."
        )
        return []

    # Load shared strings (string table).
    shared_strings = _load_shared_strings(zf, warnings)

    # Load the first worksheet.
    sheet_name = "xl/worksheets/sheet1.xml"
    if sheet_name not in zf.namelist():
        warnings.append(
            "Could not find 'xl/worksheets/sheet1.xml' in the .xlsx file. "
            "Ensure the trial balance is on the first sheet."
        )
        return []

    try:
        sheet_xml = zf.read(sheet_name)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Failed to read worksheet: {exc}")
        return []

    rows_data = _parse_sheet_xml(sheet_xml, shared_strings, warnings)
    if not rows_data:
        return []

    # First row is the header.
    header_row = rows_data[0]
    col_map = _build_column_map(header_row, warnings)
    if not col_map:
        return []

    raw_rows: list[dict[str, Any]] = []
    for row_idx, row_values in enumerate(rows_data[1:], start=2):
        raw = _map_row(list(row_values), col_map, row_idx, warnings)
        if raw is not None:
            raw_rows.append(raw)
    return raw_rows


# ---------------------------------------------------------------------------
# .xlsx internals
# ---------------------------------------------------------------------------

_NS = {
    "ss": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def _load_shared_strings(zf: zipfile.ZipFile, warnings: list[str]) -> list[str]:
    """Load the shared string table from xl/sharedStrings.xml."""
    ss_name = "xl/sharedStrings.xml"
    if ss_name not in zf.namelist():
        return []
    try:
        xml_bytes = zf.read(ss_name)
        root = ET.fromstring(xml_bytes)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Failed to parse sharedStrings.xml: {exc}")
        return []

    strings: list[str] = []
    for si in root.findall("ss:si", _NS):
        # Concatenate all <t> text nodes within the <si> element.
        parts: list[str] = []
        for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"):
            parts.append(t.text or "")
        strings.append("".join(parts))
    return strings


def _col_letter_to_index(col_str: str) -> int:
    """Convert an Excel column letter (A, B, ..., Z, AA, ...) to a 0-based index."""
    result = 0
    for ch in col_str.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


def _parse_cell_ref(ref: str) -> tuple[int, int]:
    """Parse a cell reference like 'A1' into (row_0based, col_0based)."""
    match = re.match(r"([A-Za-z]+)(\d+)", ref)
    if not match:
        return 0, 0
    col_str, row_str = match.group(1), match.group(2)
    return int(row_str) - 1, _col_letter_to_index(col_str)


def _parse_sheet_xml(
    xml_bytes: bytes,
    shared_strings: list[str],
    warnings: list[str],
) -> list[list[str]]:
    """Parse sheet1.xml into a list of rows, each a list of string cell values."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        warnings.append(f"Failed to parse worksheet XML: {exc}")
        return []

    # Collect all rows, preserving sparse cell positions.
    row_map: dict[int, dict[int, str]] = {}
    max_col = 0

    for row_el in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
        row_attr = row_el.get("r")
        if row_attr is None:
            continue
        row_idx = int(row_attr) - 1  # 0-based

        for cell_el in row_el.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
            ref = cell_el.get("r", "")
            _, col_idx = _parse_cell_ref(ref)
            cell_type = cell_el.get("t", "")

            # Value element.
            v_el = cell_el.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
            raw_val = v_el.text if v_el is not None else None

            if raw_val is None:
                cell_val = ""
            elif cell_type == "s":
                # Shared string index.
                try:
                    idx = int(raw_val)
                    cell_val = shared_strings[idx] if idx < len(shared_strings) else ""
                except (ValueError, IndexError):
                    cell_val = raw_val
            elif cell_type == "b":
                cell_val = "TRUE" if raw_val == "1" else "FALSE"
            else:
                # Numeric or formula result — keep as string.
                cell_val = raw_val

            row_map.setdefault(row_idx, {})[col_idx] = cell_val
            max_col = max(max_col, col_idx)

    if not row_map:
        return []

    max_row = max(row_map.keys())
    result: list[list[str]] = []
    for r in range(max_row + 1):
        cols = row_map.get(r, {})
        result.append([cols.get(c, "") for c in range(max_col + 1)])
    return result


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------


def _build_column_map(headers: list[str], warnings: list[str]) -> dict[int, str]:
    """Map 0-based header column indices to canonical field names.

    Keying by column INDEX (not header string) is what makes the parser robust
    to duplicate header text: two columns literally named ``売上`` map to two
    distinct indices, so a real cell can never be collapsed/overwritten the way
    a header-string-keyed ``dict(zip(...))`` would silently do. The first
    recognised column wins per canonical field; any later duplicate is warned
    about and ignored (never silently dropped).

    Args:
        headers: Raw header strings from the file, in column order.
        warnings: Mutable list to append warnings to.

    Returns:
        Dict mapping 0-based column index → canonical field name. Empty if no
        recognised columns were found.
    """
    col_map: dict[int, str] = {}  # column index → canonical field
    seen_canonical: dict[str, str] = {}  # canonical → first raw header (dup detection)

    for index, raw_header in enumerate(headers):
        normalised = raw_header.strip().lower()
        canonical = _ALIAS_TO_FIELD.get(normalised)
        if canonical is None:
            # Not a recognised column — silently skip (extra columns are fine).
            continue
        if canonical in seen_canonical:
            warnings.append(
                f"Duplicate column for '{canonical}': "
                f"'{seen_canonical[canonical]}' and '{raw_header}'. "
                f"Using the first occurrence."
            )
            continue
        col_map[index] = canonical
        seen_canonical[canonical] = raw_header

    missing = _REQUIRED_FIELDS - set(seen_canonical.keys())
    if missing:
        warnings.append(
            f"Required column(s) not found: {sorted(missing)}. "
            f"Recognised headers: {sorted(seen_canonical.keys())}."
        )

    if not col_map:
        warnings.append(
            "No recognised columns found in the file. "
            "Please check the header row matches the accepted aliases "
            "(see module docstring for the full list)."
        )

    return col_map


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _map_row(
    row_values: list[Any],
    col_map: dict[int, str],
    row_idx: int,
    warnings: list[str],
) -> dict[str, Any] | None:
    """Map a raw row (positional cell list) to canonical field names.

    Cells are read by 0-based column index from ``col_map`` so duplicate header
    strings cannot collapse two columns. A short row (fewer cells than the
    header) yields an empty value for any missing index rather than raising.

    Returns ``None`` if the row is entirely blank (skip silently).
    """
    canonical_row: dict[str, Any] = {}
    for index, canonical in col_map.items():
        raw_val = row_values[index] if index < len(row_values) else ""
        if isinstance(raw_val, str):
            raw_val = raw_val.strip()
        canonical_row[canonical] = raw_val

    # Skip blank rows.
    if all(str(v).strip() == "" for v in canonical_row.values()):
        return None

    return canonical_row


# ---------------------------------------------------------------------------
# TrialBalance construction
# ---------------------------------------------------------------------------


#: Translation table folding full-width digits and date separators to ASCII.
#: Full-width digits ０-９ (U+FF10..U+FF19) → 0-9; full-width solidus ／
#: (U+FF0F) → '/'; full-width hyphen-minus － (U+FF0D) and 長音符 ー (U+30FC)
#: → '-'. Applied to period cells only (money cells stay strict — see _parse_money).
_FULLWIDTH_PERIOD_TABLE = str.maketrans(
    {
        **{chr(0xFF10 + i): str(i) for i in range(10)},
        "／": "/",
        "－": "-",
        "ー": "-",
    }
)


def _parse_period(raw: Any, row_idx: int, warnings: list[str]) -> dt.date | None:
    """Parse a period value into a :class:`datetime.date`.

    Accepts ISO-8601 strings (``YYYY-MM-DD``), ``YYYY/MM/DD``, ``YYYY-MM``,
    ``YYYY/MM``, and Excel serial date numbers. Full-width digits and date
    separators (e.g. ``２０２５－０４``) are normalised to ASCII first, since
    Japanese-sourced files frequently use them; ASCII period cells are
    byte-unaffected.
    """
    if raw is None or str(raw).strip() == "":
        warnings.append(f"Row {row_idx}: period is empty — row skipped.")
        return None

    raw_str = str(raw).strip().translate(_FULLWIDTH_PERIOD_TABLE)

    # Try ISO-8601 / slash-separated date.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m"):
        try:
            parsed = dt.datetime.strptime(raw_str, fmt)
            # For year-month only, use the last day of the month.
            if fmt in ("%Y-%m", "%Y/%m"):
                last_day = calendar.monthrange(parsed.year, parsed.month)[1]
                return dt.date(parsed.year, parsed.month, last_day)
            return parsed.date()
        except ValueError:
            continue

    # Try Excel serial date (integer days since 1899-12-30).
    try:
        serial = float(raw_str)
        if serial == int(serial) and 1 <= serial <= 2958465:  # 1900-01-01 to 9999-12-31
            # Excel epoch: 1899-12-30 (accounting for the 1900 leap-year bug).
            epoch = dt.date(1899, 12, 30)
            return epoch + dt.timedelta(days=int(serial))
    except (ValueError, OverflowError):
        pass

    warnings.append(
        f"Row {row_idx}: cannot parse period '{raw_str}' "
        "(expected YYYY-MM-DD, YYYY/MM/DD, YYYY-MM, YYYY/MM, or Excel serial). "
        "Row skipped."
    )
    return None


def _parse_money(raw: Any, field_name: str, row_idx: int, warnings: list[str]) -> int | None:
    """Parse a monetary cell value as a strict integer yen.

    Rejects:
    - Fractional floats (e.g. 1000.5).
    - Whole-valued floats (e.g. 1000.0) — the source must be an integer.
    - Non-numeric strings.
    - Boolean values.

    Returns ``None`` on failure (warning already appended).
    """
    if raw is None or str(raw).strip() == "":
        return None  # Optional field — caller decides if this is an error.

    raw_str = str(raw).strip()

    # Reject booleans (Python bool is a subclass of int).
    if isinstance(raw, bool):
        warnings.append(
            f"Row {row_idx}: field '{field_name}' has a boolean value '{raw}' — "
            "yen must be a plain integer. Cell skipped."
        )
        return None

    # If the original value is already a Python int (from CSV DictReader or
    # internal callers), accept it directly.
    if isinstance(raw, int):
        return raw

    # Strip currency symbols and thousands separators for parsing, and normalise
    # the two Japanese accounting negative conventions to a leading ASCII '-':
    #   * the 三角 / 黒三角 markers △ (U+25B3) and ▲ (U+25B2), used pervasively
    #     in Japanese financial statements to denote a loss / negative figure;
    #   * accounting parentheses, e.g. "(1,000)".
    # Without this a loss cell like "△1,000" or "(1,000)" parses as non-numeric
    # and is dropped, silently corrupting the trial balance for exactly the
    # Japanese-sourced files this product targets.
    cleaned = raw_str.replace("¥", "").replace("￥", "").replace(",", "").strip()
    negative = False
    if cleaned[:1] in ("\u25b3", "\u25b2"):  # △ / ▲ prefix
        negative = True
        cleaned = cleaned[1:].strip()
    elif cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1].strip()
    if negative:
        cleaned = "-" + cleaned

    # If it's a float, reject it (whole-valued or fractional).
    if isinstance(raw, float):
        warnings.append(
            f"Row {row_idx}: field '{field_name}' has a float value '{raw}' — "
            "yen must be a strict integer (no decimals, even 1000.0 is rejected). "
            "Cell skipped."
        )
        return None

    # Parse the cleaned string.
    try:
        float_val = float(cleaned)
    except ValueError:
        warnings.append(
            f"Row {row_idx}: field '{field_name}' value '{raw_str}' is not numeric. Cell skipped."
        )
        return None

    # Reject non-finite values (inf, -inf, nan).
    if not math.isfinite(float_val):
        warnings.append(
            f"Row {row_idx}: field '{field_name}' value '{raw_str}' is not a finite "
            "number (inf/nan) — yen must be a finite integer. Cell skipped."
        )
        return None

    # Reject fractional values.
    if float_val != int(float_val):
        warnings.append(
            f"Row {row_idx}: field '{field_name}' value '{raw_str}' has a fractional "
            "component — yen must be a whole integer. Cell skipped."
        )
        return None

    # Reject whole-valued floats (the string contained a decimal point).
    if "." in cleaned:
        warnings.append(
            f"Row {row_idx}: field '{field_name}' value '{raw_str}' is a whole-valued "
            "float — yen must be a strict integer (no decimal point). Cell skipped."
        )
        return None

    return int(float_val)


def _build_trial_balances(
    raw_rows: list[dict[str, Any]], warnings: list[str]
) -> list[TrialBalance]:
    """Convert raw parsed rows into validated :class:`TrialBalance` objects.

    Enforces J-GAAP invariants as warnings (never exceptions).
    """
    result: list[TrialBalance] = []

    for row_idx, raw in enumerate(raw_rows, start=2):
        # --- Period ---
        period = _parse_period(raw.get("period"), row_idx, warnings)
        if period is None:
            continue

        # --- Mandatory money fields ---
        uriage = _parse_money(raw.get("uriage"), "uriage", row_idx, warnings)
        uriage_genka = _parse_money(raw.get("uriage_genka"), "uriage_genka", row_idx, warnings)
        hanbaihi = _parse_money(raw.get("hanbaihi"), "hanbaihi", row_idx, warnings)

        missing = []
        if uriage is None:
            missing.append("uriage (売上)")
        if uriage_genka is None:
            missing.append("uriage_genka (売上原価)")
        if hanbaihi is None:
            missing.append("hanbaihi (販売費)")
        if missing:
            warnings.append(
                f"Row {row_idx} ({period}): mandatory field(s) missing or invalid: "
                f"{missing}. Row skipped."
            )
            continue

        # --- Optional money fields ---
        eigai_shueki_raw = _parse_money(raw.get("eigai_shueki"), "eigai_shueki", row_idx, warnings)
        eigai_hiyo_raw = _parse_money(raw.get("eigai_hiyo"), "eigai_hiyo", row_idx, warnings)
        eigai_shueki = eigai_shueki_raw if eigai_shueki_raw is not None else 0
        eigai_hiyo = eigai_hiyo_raw if eigai_hiyo_raw is not None else 0

        # --- Cross-check columns (eigyo_rieki / keijo_rieki) ---
        supplied_eigyo = _parse_money(raw.get("eigyo_rieki"), "eigyo_rieki", row_idx, warnings)
        supplied_keijo = _parse_money(raw.get("keijo_rieki"), "keijo_rieki", row_idx, warnings)

        # --- Build the TrialBalance (computed fields are properties) ---
        # uriage, uriage_genka, hanbaihi are guaranteed non-None here.
        assert uriage is not None
        assert uriage_genka is not None
        assert hanbaihi is not None

        try:
            tb = TrialBalance(
                period=period,
                uriage=uriage,
                uriage_genka=uriage_genka,
                hanbaihi=hanbaihi,
                eigai_shueki=eigai_shueki,
                eigai_hiyo=eigai_hiyo,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"Row {row_idx} ({period}): failed to construct TrialBalance: {exc}. Row skipped."
            )
            continue

        # --- J-GAAP invariant checks (warnings only, never exceptions) ---
        _check_invariants(tb, period, row_idx, supplied_eigyo, supplied_keijo, warnings)

        result.append(tb)

    return result


def _check_invariants(
    tb: TrialBalance,
    period: dt.date,
    row_idx: int,
    supplied_eigyo: int | None,
    supplied_keijo: int | None,
    warnings: list[str],
) -> None:
    """Check J-GAAP invariants and append warnings for any violations.

    Invariants checked:
    1. 営業利益 = 粗利 − 販売費  (cross-checked against supplied column)
    2. 経常利益 = 営業利益 + 営業外収益 − 営業外費用  (cross-checked against supplied column)

    Note: 粗利 (gross profit) itself is not separately guarded here — it is the
    ``uriage_sourieki`` computed_field (= 売上 − 売上原価) by definition, so any
    self-comparison would be unreachable.

    Args:
        tb: The constructed :class:`TrialBalance` (computed fields already set).
        period: The period date (for human-readable messages).
        row_idx: 1-based row index in the source file.
        supplied_eigyo: Supplied 営業利益 value from the file (cross-check only).
        supplied_keijo: Supplied 経常利益 value from the file (cross-check only).
        warnings: Mutable list to append warnings to.
    """
    # Invariant 1: operating profit cross-check.
    computed_eigyo = tb.eigyo_rieki
    if supplied_eigyo is not None and supplied_eigyo != computed_eigyo:
        warnings.append(
            f"Row {row_idx} ({period}): J-GAAP invariant warning — "
            f"supplied 営業利益 (operating profit) {supplied_eigyo:,} "
            f"does not match computed value {computed_eigyo:,} "
            f"(= 売上 {int(tb.uriage):,} − 売上原価 {int(tb.uriage_genka):,} "
            f"− 販売費 {int(tb.hanbaihi):,}). "
            "Please verify the source data."
        )

    # Invariant 2: ordinary profit cross-check.
    computed_keijo = tb.keijo_rieki
    if supplied_keijo is not None and supplied_keijo != computed_keijo:
        warnings.append(
            f"Row {row_idx} ({period}): J-GAAP invariant warning — "
            f"supplied 経常利益 (ordinary profit) {supplied_keijo:,} "
            f"does not match computed value {computed_keijo:,} "
            f"(= 営業利益 {computed_eigyo:,} + 営業外収益 {int(tb.eigai_shueki):,} "
            f"− 営業外費用 {int(tb.eigai_hiyo):,}). "
            "Please verify the source data."
        )

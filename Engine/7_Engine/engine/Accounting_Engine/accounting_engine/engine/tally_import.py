"""Import party and item masters from files exported out of Tally.

Supported inputs (format is auto-detected):

* **Ledgers in Default Template** (.xlsx) – Tally's import template (handled by ``importer.py``).
* **List of Ledgers** report – Tally → Display → List of Accounts → Ledgers, exported as
  PDF, or as text, Excel or CSV. Lines look like ``12 Das Store Sundry Debtors 25,007.00 Dr``.
* **Stock Summary** (.xlsx) – Tally → Stock Summary → Export. Bold rows are stock groups,
  indented rows are items, the unit hides in the quantity cell's number format, godown lines
  repeat under items.
* **Plain item / ledger tables** (.xlsx/.csv) with a header row (Name, Under/Group, Unit, Rate…).

Everything goes through a *preview* first (counts, detected groups, skipped lines) so the
user can untick groups that are not parties (expenses, bank accounts…) before importing.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import openpyxl

from . import db
from .importer import HEADERS as TEMPLATE_HEADERS
from .importer import ImportResult, import_ledger_template
from .models import Party

# --------------------------------------------------------------------------- #
# reference data
# --------------------------------------------------------------------------- #
# Tally's default (primary + reserved) groups. Anything else in a Tally company is user-defined.
TALLY_DEFAULT_GROUPS = [
    "Sundry Debtors", "Sundry Creditors", "Bank Accounts", "Bank OD A/c", "Bank OCC A/c", "Cash-in-Hand",
    "Capital Account", "Reserves & Surplus", "Retained Earnings", "Current Assets", "Current Liabilities",
    "Deposits (Asset)", "Loans & Advances (Asset)", "Stock-in-Hand", "Fixed Assets", "Investments",
    "Loans (Liability)", "Secured Loans", "Unsecured Loans", "Duties & Taxes", "Provisions",
    "Suspense A/c", "Branch / Divisions", "Misc. Expenses (ASSET)", "Sales Accounts", "Purchase Accounts",
    "Direct Expenses", "Direct Incomes", "Indirect Expenses", "Indirect Incomes",
    "Expenses (Direct)", "Expenses (Indirect)", "Income (Direct)", "Income (Indirect)",
]
# Default groups that are never trading parties – unticked by default in the preview.
NON_PARTY_GROUPS = {g.lower() for g in TALLY_DEFAULT_GROUPS if "debtor" not in g.lower() and "creditor" not in g.lower()}
UNKNOWN_GROUP = "(group not detected)"
# words that mark a user-defined group as NOT a customer/supplier group (unticked by default)
_NON_PARTY_HINT_RE = re.compile(
    r"\b(exp|exps|expense|expenses|fees?|salary|salaries|staff|wages|gst|vat|tds|tcs|cgst|sgst|igst|input|output|"
    r"paid|payable|receivable|reverse|reversed|tax|taxes|duty|duties|bank|loan|loans|asset|assets|capital|"
    r"depreciation|interest|commission|charges|rent|electricity|block|cash|investment|investments|"
    r"provision|provisions|suspense|stock|purchase|sales|income|incomes|drawings|reserve|reserves)\b", re.IGNORECASE)

UNIT_CANON = {
    "pc": "Pcs", "pcs": "Pcs", "nos": "Nos", "no": "Nos", "bag": "Bag", "bags": "Bag", "bg": "Bag",
    "pkt": "Pkt", "pkts": "Pkt", "tin": "Tin", "tins": "Tin", "kg": "Kg", "kgs": "Kg", "ltr": "Ltr",
    "ltrs": "Ltr", "lt": "Ltr", "l": "Ltr", "qtl": "Qtl", "qtls": "Qtl", "mtr": "Mtr", "mtrs": "Mtr",
    "m": "Mtr", "box": "Box", "set": "Set", "sets": "Set", "job": "Job", "pair": "Pair", "dz": "Dz",
    "ton": "Ton", "gm": "Gm", "g": "Gm", "unit": "Unit", "bottle": "Bottle", "btl": "Bottle",
}


def canon_unit(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    s = str(u).strip().strip('."').strip()
    if not s:
        return None
    return UNIT_CANON.get(s.lower(), s[:1].upper() + s[1:])


# --------------------------------------------------------------------------- #
# result types
# --------------------------------------------------------------------------- #
@dataclass
class LedgerRow:
    name: str
    group: str
    opening_balance: Optional[float] = None
    opening_drcr: Optional[str] = None


@dataclass
class ItemRow:
    name: str
    group: Optional[str] = None
    unit: Optional[str] = None
    rate: Optional[float] = None


@dataclass
class ImportPreview:
    kind: str  # ledger_template | ledger_list | stock_summary | item_list | unknown
    source_name: str = ""
    ledgers: list[LedgerRow] = field(default_factory=list)
    items: list[ItemRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # human-readable, e.g. godown names, unparsed lines
    warnings: list[str] = field(default_factory=list)
    template_result: Optional[ImportResult] = None  # only for kind == ledger_template (already applied)

    @property
    def group_counts(self) -> dict[str, int]:
        return dict(Counter(l.group for l in self.ledgers).most_common())

    @property
    def item_group_counts(self) -> dict[str, int]:
        return dict(Counter(i.group or "(no group)" for i in self.items).most_common())

    def default_party_groups(self) -> list[str]:
        """Groups ticked by default: debtors/creditors and user-defined groups that don't look like
        expense / tax / asset / staff groups."""
        out = []
        for g in self.group_counts:
            gl = g.lower()
            if gl in NON_PARTY_GROUPS or g == UNKNOWN_GROUP:
                continue
            if "debtor" in gl or "creditor" in gl:
                out.append(g)
            elif not _NON_PARTY_HINT_RE.search(gl):
                out.append(g)
        return out

    def examples(self, group: str, n: int = 3) -> list[str]:
        return [l.name for l in self.ledgers if l.group == group][:n]

    def describe(self) -> str:
        if self.kind == "ledger_template":
            return f"Tally ledger template · {self.template_result.summary() if self.template_result else ''}"
        if self.kind == "ledger_list":
            return f"Tally 'List of Ledgers' · {len(self.ledgers):,} ledgers in {len(self.group_counts)} groups"
        if self.kind == "stock_summary":
            return f"Tally 'Stock Summary' · {len(self.items):,} items in {len(self.item_group_counts)} stock groups"
        if self.kind == "item_list":
            return f"Item list · {len(self.items):,} items"
        return "Unrecognised file"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def detect_and_parse(data: bytes, filename: str, db_path=None, apply_template: bool = True) -> ImportPreview:
    """Parse ``data`` and return a preview. The Tally *template* is applied immediately (it already
    carries everything, no group selection needed) when ``apply_template`` is True."""
    ext = Path(filename).suffix.lower()
    name = Path(filename).name
    if not data:
        return ImportPreview(kind="unknown", source_name=name, warnings=["The file is empty."])

    if ext == ".pdf" or data[:4] == b"%PDF":
        try:
            text = pdf_to_text(data)
        except Exception as e:  # noqa: BLE001 - truncated / encrypted / not really a PDF
            return ImportPreview(kind="unknown", source_name=name, warnings=[f"Cannot read the PDF: {e}"])
        return _parse_text_report(text, name)
    if ext in (".txt", ".text") or (ext == "" and _looks_like_text(data)):
        text = _decode_text(data)
        head = [l for l in text.splitlines()[:40] if l.strip()]
        if head and sum(1 for l in head if l.lstrip().startswith('"') and '","' in l) >= 0.8 * len(head):
            ext = ".csv"  # Tally's 'ASCII (Comma delimited)' export: "1","105 Bn BSF","GEM",""
        else:
            return _parse_text_report(text, name)
    if ext == ".csv":
        text = _decode_text(data)
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delim = dialect.delimiter
        except csv.Error:
            delim = ","
        rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
        return _parse_table(rows, name)
    if ext in (".xlsx", ".xlsm", ".xls"):
        if ext == ".xls":
            return ImportPreview(kind="unknown", source_name=name,
                                 warnings=["Old .xls format is not supported – export from Tally as .xlsx (or open and Save As .xlsx)."])
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        except Exception as e:  # noqa: BLE001
            return ImportPreview(kind="unknown", source_name=name, warnings=[f"Cannot open workbook: {e}"])
        ws = wb.worksheets[0]
        if _is_stock_summary(ws):
            wb_styled = openpyxl.load_workbook(io.BytesIO(data))  # need fonts/indent/number formats
            return parse_stock_summary(wb_styled.worksheets[0], name)
        header_idx, header = _find_header_row(ws)
        if header is not None and [h.lower() for h in header[: len(TEMPLATE_HEADERS)]] == [h.lower() for h in TEMPLATE_HEADERS]:
            pv = ImportPreview(kind="ledger_template", source_name=name)
            if apply_template:
                pv.template_result = import_ledger_template(io.BytesIO(data), db_path)
                pv.warnings.extend(pv.template_result.warnings)
            return pv
        rows = [[_cell_text(c) for c in r] for r in ws.iter_rows()]
        return _parse_table(rows, name)
    return ImportPreview(kind="unknown", source_name=name,
                         warnings=[f"Unsupported file type '{ext}'. Use PDF, TXT, XLSX or CSV."])


_DRCR_FMT_RE = re.compile(r'"\s*(Dr|Cr)\.?\s*"', re.IGNORECASE)


def _cell_text(c):
    """Cell value; a number whose format carries Dr/Cr (Tally: 0.00" Dr") becomes '1200.0 Dr'."""
    v = c.value
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        m = _DRCR_FMT_RE.search(c.number_format or "")
        if m:
            return f"{abs(v)} {m.group(1).capitalize()}"
    return v


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    return b"\x00" not in sample or data[:2] in (b"\xff\xfe", b"\xfe\xff")


def _decode_text(data: bytes) -> str:
    """Tally writes text/CSV exports as UTF-8 (with or without BOM), UTF-16 or Windows-1252."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# --------------------------------------------------------------------------- #
# PDF / text reports ("List of Ledgers", "List of Stock Items")
# --------------------------------------------------------------------------- #
def pdf_to_text(data: bytes) -> str:
    """Text of a Tally PDF report, one report row per line.

    Tally writes every column as a separate text object, so pypdf's default mode returns each
    cell on its own line ("1" / "105 Bn BSF" / "GEM"). The layout mode (pypdf >= 4) keeps the
    horizontal positions and re-joins a row into one line, which is what the parser expects.
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pypdf is not installed (pip install -r requirements.txt)") from e
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        text = ""
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:  # pypdf < 4 has no extraction_mode
            text = ""
        except Exception:  # noqa: BLE001 - one bad page should not kill the import
            text = ""
        if not text.strip():
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                text = ""
        # collapse the wide column gaps layout mode produces; keep line structure
        parts.append("\n".join(re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in text.splitlines()))
    return "\n".join(parts)


_SERIAL_RE = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d+)[.)]?\s+(.+?)\s*$")
_BALANCE_RE = re.compile(r"\s+(\(-\)\s*|-)?(\d[\d,]*(?:\.\d+)?)\s*(Dr|Cr)\.?\s*$", re.IGNORECASE)
_HEADER_RE = re.compile(r"^\s*(Sl|Sr|S)\.?\s*No\.?", re.IGNORECASE)
_SKIP_RE = re.compile(r"^\s*(continued\s*\.*|page\s+\d+.*|list of (ledgers|stock items|accounts).*|.*\bto\b\s+\d{1,2}-[A-Za-z]{3}-\d{2,4}\s*$)\s*$", re.IGNORECASE)


def _parse_text_report(text: str, source_name: str) -> ImportPreview:
    low = text.lower()
    if "list of stock items" in low or "stock item" in low[:2000]:
        kind = "item_list"
    else:
        kind = "ledger_list"
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)  # stray control chars from PDF extraction
    records, unparsed = _numbered_records(text)
    pv = ImportPreview(kind=kind, source_name=source_name)
    if not records:
        pv.kind = "unknown"
        pv.warnings.append("No numbered 'Sl. No.  Name  Under' lines found. Export the report from Tally as PDF/Excel "
                           "(Display → List of Accounts → Ledgers → Export) and try again.")
        return pv
    pv.skipped.extend(f"line: {u}" for u in unparsed[:50])
    if len(unparsed) > 50:
        pv.skipped.append(f"… and {len(unparsed) - 50} more unparsed lines")

    # split off opening balances
    stripped: list[tuple[str, Optional[float], Optional[str]]] = []
    for rec in records:
        bal, drcr = None, None
        m = _BALANCE_RE.search(rec)
        if m:
            try:
                bal = float(m.group(2).replace(",", ""))
                if m.group(1):
                    bal = -bal  # Tally prints negative balances as '(-)1,000.00 Dr'
                drcr = m.group(3).capitalize()
                rec = rec[: m.start()].rstrip()
            except ValueError:
                pass
        stripped.append((rec, bal, drcr))

    names_groups = split_name_and_group([r[0] for r in stripped])
    n_unknown = 0
    for (rec, bal, drcr), (nm, grp) in zip(stripped, names_groups):
        if not grp:
            grp = UNKNOWN_GROUP
            n_unknown += 1
        if kind == "item_list":
            pv.items.append(ItemRow(name=nm, group=None if grp == UNKNOWN_GROUP else grp))
        else:
            pv.ledgers.append(LedgerRow(name=nm, group=grp, opening_balance=bal, opening_drcr=drcr))
    if n_unknown:
        pv.warnings.append(f"{n_unknown} line(s) where the group could not be separated from the name – "
                           f"listed under '{UNKNOWN_GROUP}' (unticked by default).")
    return pv


_CONTINUED_RE = re.compile(r"^\s*continued\b", re.IGNORECASE)


def _serial_of(m: "re.Match[str]") -> int:
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return -1


def _numbered_records(text: str) -> tuple[list[str], list[str]]:
    """Collect 'serial + text' records, joining wrapped continuation lines.

    Only lines between a 'Sl. No. …' column header and the page's 'continued …' footer are read,
    so company name / address / page headers never leak into names. Serial numbers must be
    consecutive, which keeps names that start with digits ('105 Bn BSF') intact.
    """
    records: list[str] = []
    unparsed: list[str] = []
    expected = 1
    in_table = False
    current: Optional[str] = None
    # Company name / address lines printed above the first header repeat on every page. Seeing one
    # again means a new page started – even when the export has no 'continued' footer or no
    # 'Sl. No.' header on the later pages.
    preamble: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _HEADER_RE.match(line):
            in_table = True
            continue
        if _CONTINUED_RE.match(line) or (line.lower() in preamble and (in_table or current is not None)):
            in_table = False
            continue
        m = _SERIAL_RE.match(line)
        if not in_table:
            # tolerate exports without the 'Sl. No.' header line: (re)start at the next expected record
            serial = _serial_of(m) if m else -1
            if m and serial == expected:
                in_table = True
            else:
                if not records and current is None and not _SKIP_RE.match(line):
                    preamble.add(line.lower())
                continue
        if _SKIP_RE.match(line):
            continue
        if m:
            serial = _serial_of(m)
            if serial == expected:
                if current is not None:
                    records.append(current)
                current = m.group(2)
                expected = serial + 1
                continue
        if current is not None and len(line) < 120:
            current = f"{current} {line}".strip()  # wrapped name / group
        else:
            unparsed.append(line)
    if current is not None:
        records.append(current)
    return records, unparsed


def split_name_and_group(texts: list[str]) -> list[tuple[str, str]]:
    """Given 'Name Group' strings, split off the group. Known Tally groups are matched first;
    user-defined groups (e.g. 'GEM', 'Govt. Debtors') are learned from repeated trailing words."""
    known = sorted(TALLY_DEFAULT_GROUPS, key=len, reverse=True)
    known_low = [(g, g.lower()) for g in known]
    out: list[Optional[tuple[str, str]]] = [None] * len(texts)
    unmatched: list[int] = []
    for i, t in enumerate(texts):
        t_norm = re.sub(r"\s+", " ", t).strip()
        low = t_norm.lower()
        hit = None
        for g, gl in known_low:
            if low.endswith(" " + gl) and len(low) > len(gl) + 1:
                hit = g
                break
        if hit:
            out[i] = (t_norm[: len(t_norm) - len(hit)].rstrip(" -:"), hit)
        else:
            unmatched.append(i)

    if unmatched:
        # learn user-defined groups from trailing 1-4 word suffixes of the unmatched lines
        suffix_counts: Counter = Counter()          # lower-cased suffix -> occurrences
        suffix_variants: dict[str, Counter] = {}    # lower-cased suffix -> exact spellings
        toks_by_idx: dict[int, list[str]] = {}
        for i in unmatched:
            toks = re.sub(r"\s+", " ", texts[i]).strip().split(" ")
            toks_by_idx[i] = toks
            for n in (1, 2, 3, 4):
                if len(toks) > n:  # must leave at least one word for the name
                    exact = " ".join(toks[-n:])
                    suffix_counts[exact.lower()] += 1
                    suffix_variants.setdefault(exact.lower(), Counter())[exact] += 1
        min_count = 2
        # How much of an n-word suffix is covered by (n+1)-word suffixes that themselves repeat.
        # 'Tea Group' (17) splits completely into 'Garg Tea Group' (14) + 'Berlia Tea Group' (3): it
        # is not a group of its own. 'Raju Bihani Ledger' (20) is followed by 16 different words: it is.
        covered: Counter = Counter()
        for i in unmatched:
            toks = toks_by_idx[i]
            for n in (1, 2, 3):
                if len(toks) > n + 1 and suffix_counts[" ".join(toks[-(n + 1):]).lower()] >= min_count:
                    covered[" ".join(toks[-n:]).lower()] += 1
        for i in unmatched:
            toks = toks_by_idx[i]
            chosen = None
            prev_count = None
            prev_suf = ""
            generic_start = toks[-1].lower() in _GENERIC_GROUP_WORDS
            for n in (1, 2, 3, 4):
                if len(toks) <= n:
                    break
                suf = " ".join(toks[-n:]).lower()
                cnt = suffix_counts[suf]
                # A bare 'Group' / 'Ledger' is never a group of its own: always take one more word.
                if cnt < (1 if n <= 2 and generic_start else min_count):
                    break
                if prev_count is not None and not (n == 2 and generic_start):
                    # A group is printed by Tally identically on every line. Extending the suffix
                    # is only right if (almost) every occurrence carries the extra word AND that
                    # word is spelled consistently: 'GEM' -> 'BSF GEM' fails the count test,
                    # 'IDFC Finance' -> 'IDFC IDFC Finance' fails the spelling test (…Idfc IDFC Finance).
                    top = suffix_variants[suf].most_common(1)[0][1]
                    if top < 0.97 * cnt:
                        break
                    partitioned = generic_start and n == 3 and covered[prev_suf] >= 0.8 * prev_count
                    if cnt < 0.9 * prev_count and not partitioned:
                        break
                chosen, prev_count, prev_suf = n, cnt, suf
            if chosen:
                grp = suffix_variants[" ".join(toks[-chosen:]).lower()].most_common(1)[0][0]
                name = " ".join(toks[:-chosen]).rstrip(" -:")
                name, grp = _move_company_tail(name, grp, weak=prev_count == 1)
                out[i] = (name, grp)
            else:
                out[i] = (" ".join(toks), "")
    return [o if o else ("", "") for o in out]


_COMPANY_TAIL_RE = re.compile(
    r"^(ltd|limited|pvt|llp|co\.|company|corporation|inc|huf|enterprises?|industries|traders|"
    r"stores?|agency|agencies|associates|brothers|bros|sons|&\s*co)\b\.?", re.IGNORECASE)
_GENERIC_GROUP_WORDS = {"group", "ledger", "ledgers", "account", "accounts", "a/c"}


def _move_company_tail(name: str, grp: str, weak: bool = False) -> tuple[str, str]:
    """'Aar Pee Infraprojects Pvt. Ltd. R K Garg' + 'Group' -> 'Aar Pee Infraprojects Pvt. Ltd.' + 'R K Garg Group'.
    Only when the learned group is a single generic word, and the name ends with words after a
    company designator – those words belong to the (multi-word, low-frequency) group.
    Conversely a learned group never *starts* with a company designator ('Ltd Garg Tea Group'):
    such words go back to the name."""
    gtoks = grp.split(" ")
    while len(gtoks) > 1 and _COMPANY_TAIL_RE.match(gtoks[0]):
        name = f"{name} {gtoks.pop(0)}"
    grp = " ".join(gtoks)
    if grp.lower() not in _GENERIC_GROUP_WORDS and not weak:  # weak: 'X Group' seen only once
        return name, grp
    toks = name.split(" ")
    for k in range(len(toks) - 1, 0, -1):
        if _COMPANY_TAIL_RE.search(toks[k]):
            tail = toks[k + 1:]
            if 0 < len(tail) <= 4:
                return " ".join(toks[: k + 1]), " ".join(tail + [grp])
            return name, grp
    return name, grp


# --------------------------------------------------------------------------- #
# Stock Summary (.xlsx)
# --------------------------------------------------------------------------- #
def _is_stock_summary(ws) -> bool:
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 20), values_only=True):
        vals = [v.strip().lower() for v in row if isinstance(v, str)]
        if "stock summary" in vals:
            return True
        # Tally's report header row; a hand-made list with a 'Unit' column, or a ledger table
        # ('Particulars | Under | Opening Balance'), is a plain table instead
        if "particulars" in vals and not ({"unit", "units", "uom", "under", "group", "opening balance"} & set(vals)):
            return True
    return False


_UNIT_FMT_RE = re.compile(r'"\s*([A-Za-z]+)\.?\s*"')


def parse_stock_summary(ws, source_name: str) -> ImportPreview:
    pv = ImportPreview(kind="stock_summary", source_name=source_name)
    # locate data start: the row after the one containing 'Quantity' (or after 'Particulars')
    start = None
    for r in range(1, min(ws.max_row, 40) + 1):
        vals = [str(c.value).strip().lower() if c.value is not None else "" for c in ws[r]]
        if "quantity" in vals:
            start = r + 1
            break
        if "particulars" in vals and start is None:
            start = r + 1
    if start is None:
        start = 1
    # column indices: name = first column; find qty / rate / value columns from the header line if present
    qty_col, rate_col = 2, 3
    hdr_row = start - 1
    if hdr_row >= 1:
        for c in ws[hdr_row]:
            v = str(c.value).strip().lower() if c.value is not None else ""
            if v == "quantity":
                qty_col = c.column
            elif v == "rate":
                rate_col = c.column

    rows = []
    for r in range(start, ws.max_row + 1):
        c = ws.cell(row=r, column=1)
        name = str(c.value).strip() if c.value is not None else ""
        if not name:
            continue
        if name.lower() in ("grand total", "total"):
            break
        rows.append({
            "row": r, "name": name, "bold": bool(c.font and c.font.b),
            "indent": float(c.alignment.indent or 0) if c.alignment else 0.0,
            "qty": ws.cell(row=r, column=qty_col).value, "rate": ws.cell(row=r, column=rate_col).value,
            "fmt": ws.cell(row=r, column=qty_col).number_format or "",
        })

    # Row classes (as Tally exports them):
    #   bold                     -> stock group (indent 0) / bold sub-group
    #   non-bold, indent < modal -> plain sub-group nested in a group (e.g. 'EEZY' under 'Exide Auto')
    #   non-bold, modal indent   -> stock item
    #   godown / batch lines     -> repeat a name, or repeat the previous item's figures
    nonbold = [x for x in rows if not x["bold"]]
    item_indent = Counter(x["indent"] for x in nonbold).most_common(1)[0][0] if nonbold else 0.0
    nonbold_names = Counter(x["name"].lower() for x in nonbold)
    repeated = {n for n, k in nonbold_names.items() if k > 1}
    # Godown names repeat under every item that has stock, so they are recognised by name. Only
    # when nothing repeats (a single stocked item -> its godown line appears once) do we fall back
    # to "repeats the previous item's figures"; with godowns identified by name that fallback would
    # drop a real item that happens to follow another with the same qty/rate/value ('Amaron 40L'
    # and 'Amaron 40R', 1 pc each at the same rate).
    figures_fallback = not repeated
    skipped: Counter = Counter()
    subgroups: Counter = Counter()
    group_stack: list[tuple[float, str]] = []
    prev_item: Optional[dict] = None
    value_col = rate_col + 1
    for x in rows:
        if x["bold"]:
            while group_stack and group_stack[-1][0] >= x["indent"]:
                group_stack.pop()
            group_stack.append((x["indent"], x["name"]))
            prev_item = None
            continue
        if x["name"].lower() in repeated:
            skipped[x["name"]] += 1
            continue
        if group_stack and x["indent"] < item_indent and x["indent"] > group_stack[-1][0]:
            subgroups[x["name"]] += 1  # plain sub-group heading between the group and its items
            continue
        if figures_fallback and prev_item is not None and x["indent"] >= prev_item["indent"] and x["qty"] not in (None, "") \
                and (x["qty"], x["rate"]) == (prev_item["qty"], prev_item["rate"]) \
                and ws.cell(row=x["row"], column=value_col).value == ws.cell(row=prev_item["row"], column=value_col).value:
            skipped[x["name"]] += 1  # godown line repeating the item's own figures
            continue
        while group_stack and group_stack[-1][0] >= x["indent"]:
            group_stack.pop()
        group = group_stack[-1][1] if group_stack else None
        m = _UNIT_FMT_RE.search(x["fmt"])
        if not m:  # closing quantity empty – try the other quantity cells (opening / inwards / outwards)
            for cell in ws[x["row"]][1:]:
                m = _UNIT_FMT_RE.search(cell.number_format or "")
                if m:
                    break
        unit = canon_unit(m.group(1)) if m else None
        rate = x["rate"] if isinstance(x["rate"], (int, float)) else None
        pv.items.append(ItemRow(name=x["name"], group=group, unit=unit, rate=float(rate) if rate is not None else None))
        prev_item = x
    for n, k in subgroups.most_common():
        pv.skipped.append(f"{n} – sub-group heading (not an item)")
    for n, k in skipped.most_common():
        pv.skipped.append(f"{n} (×{k}) – treated as godown/batch line")
    if not pv.items:
        pv.kind = "unknown"
        pv.warnings.append("No stock items found below the 'Particulars' header.")
    return pv


# --------------------------------------------------------------------------- #
# generic tables (.xlsx / .csv with a header row)
# --------------------------------------------------------------------------- #
def _find_header_row(ws_or_rows, max_scan: int = 30):
    rows = ws_or_rows if isinstance(ws_or_rows, list) else [list(r) for r in ws_or_rows.iter_rows(min_row=1, max_row=max_scan, values_only=True)]
    for i, row in enumerate(rows[:max_scan]):
        cells = [str(v).strip() if v is not None else "" for v in row]
        low = [c.lower() for c in cells]
        if any(k in low for k in ("name", "name of ledger", "ledger name", "ledger", "item", "item name",
                                  "stock item", "name of item", "particulars", "under", "group name")):
            return i, cells
    return None, None


_NAME_KEYS = ("name of ledger", "ledger name", "name of item", "stock item", "item name", "item", "ledger", "name", "particulars")
_GROUP_KEYS = ("under", "group name", "group", "parent", "stock group", "category")
_OPEN_KEYS = ("opening balance", "opening", "op. balance", "balance")
_DRCR_KEYS = ("dr/cr", "dr / cr", "drcr", "type")
_UNIT_KEYS = ("unit", "units", "uom", "unit of measure", "base unit")
_RATE_KEYS = ("rate", "price", "std rate", "selling price", "last rate")


def _col(header_low: list[str], keys) -> Optional[int]:
    for k in keys:
        if k in header_low:
            return header_low.index(k)
    for k in keys:  # partial match
        for i, h in enumerate(header_low):
            if h and k in h:
                return i
    return None


def _parse_table(rows: list[list], source_name: str) -> ImportPreview:
    idx, header = _find_header_row(rows)
    if header is None:
        body = [r for r in rows if r and any(v not in (None, "") for v in r)]
        serials = [str(r[0]).strip().replace(",", "") for r in body]
        if body and len(body[0]) >= 2 and all(x.isdigit() for x in serials) and serials[:3] == ["1", "2", "3"][: len(serials)]:
            # header-less 'Sl. No., Name, Under, Opening Balance' export (Tally ASCII / CSV)
            width = max(len(r) for r in body)
            header = ["Sl. No.", "Name", "Under", "Opening Balance"][:width] + [""] * max(0, width - 4)
            rows = [header] + body
            idx = 0
    if header is None:
        # no header: single column of names -> item list
        names = [str(r[0]).strip() for r in rows if r and r[0] not in (None, "")]
        if names:
            pv = ImportPreview(kind="item_list", source_name=source_name,
                               items=[ItemRow(name=n) for n in names])
            pv.warnings.append("No header row found – every value in the first column was taken as an item name.")
            return pv
        return ImportPreview(kind="unknown", source_name=source_name, warnings=["Empty table."])
    low = [h.lower() for h in header]
    joined = " ".join(low)
    is_ledger = any(k in joined for k in ("ledger", "under", "opening"))
    is_item = any(k in joined for k in ("item", "stock", "unit", "uom", "rate"))
    if is_item and not is_ledger:
        kind = "item_list"
    elif is_ledger:
        kind = "ledger_list"
    else:
        kind = "item_list"  # a plain 'Name' list – user can still choose in the UI
    name_c = _col(low, _NAME_KEYS)
    if name_c is None:
        return ImportPreview(kind="unknown", source_name=source_name, warnings=["No 'Name' column found."])
    pv = ImportPreview(kind=kind, source_name=source_name)
    group_c, open_c, drcr_c = _col(low, _GROUP_KEYS), _col(low, _OPEN_KEYS), _col(low, _DRCR_KEYS)
    unit_c, rate_c = _col(low, _UNIT_KEYS), _col(low, _RATE_KEYS)

    def cell(r, c):
        return r[c] if c is not None and c < len(r) else None

    for r in rows[idx + 1:]:
        nm = cell(r, name_c)
        nm = str(nm).strip() if nm is not None else ""
        if not nm or nm.lower() in ("grand total", "total"):
            continue
        # skip a serial number that leaked into the name column
        grp = cell(r, group_c)
        grp = str(grp).strip() if grp is not None else ""
        if kind == "ledger_list":
            bal, drcr = None, None
            ob = cell(r, open_c)
            if ob not in (None, ""):
                s = str(ob).strip()
                m = re.match(r"^(-?[\d,]*\.?\d+)\s*(Dr|Cr)?$", s, re.IGNORECASE)
                if m:
                    try:
                        bal = float(m.group(1).replace(",", ""))
                    except ValueError:
                        bal = None
                    drcr = (m.group(2) or "").capitalize() or None
            d = cell(r, drcr_c)
            if d and str(d).strip().lower()[:2] in ("dr", "cr"):
                drcr = str(d).strip()[:2].capitalize()
            pv.ledgers.append(LedgerRow(name=nm, group=grp or UNKNOWN_GROUP, opening_balance=bal, opening_drcr=drcr))
        else:
            rate = cell(r, rate_c)
            try:
                rate = float(str(rate).replace(",", "")) if rate not in (None, "") else None
            except ValueError:
                rate = None
            pv.items.append(ItemRow(name=nm, group=grp or None, unit=canon_unit(cell(r, unit_c)), rate=rate))
    if kind == "ledger_list" and all(l.group == UNKNOWN_GROUP for l in pv.ledgers):
        pv.warnings.append("No 'Under'/'Group' column – all ledgers listed under '(group not detected)'. "
                           "Tick it to import them as Sundry Debtors.")
    return pv


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def apply_ledgers(pv: ImportPreview, selected_groups: list[str], db_path=None,
                  unknown_group_as: str = "Sundry Debtors") -> tuple[int, int]:
    sel = {g for g in selected_groups}
    parties = []
    for l in pv.ledgers:
        if l.group not in sel or not l.name.strip():
            continue
        grp = unknown_group_as if l.group == UNKNOWN_GROUP else l.group
        parties.append(Party(name=l.name.strip()[:200], group_name=grp, opening_balance=l.opening_balance,
                             opening_drcr=l.opening_drcr))
    return db.bulk_upsert_parties(parties, db_path)


def apply_items(pv: ImportPreview, db_path=None, source: str = "import") -> tuple[int, int]:
    rows = [{"name": i.name.strip()[:200], "group": i.group, "unit": i.unit, "rate": i.rate, "source": source}
            for i in pv.items if i.name.strip()]
    return db.bulk_upsert_items(rows, db_path)

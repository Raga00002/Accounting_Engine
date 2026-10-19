"""Import an existing Tally 'Ledgers in Default Template' workbook into the database.

Tally's layout (see fixtures/Ledgers_in_Default_Template.xlsx):
  * A row with a value in column A (Name) starts a ledger block.
  * Following rows with an empty Name column belong to the same ledger and carry
    additional bills (E-H) and/or additional address lines (I).
  * Opening balance may be a formula string (e.g. '=+C2+C3-C6'); that is skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import IO, Optional, Union

import openpyxl

from . import db
from .models import Bill, Party

HEADERS = [
    "Name",
    "Group Name",
    "Ledger - Opening Balance",
    "Ledger Opening Balance - Dr/Cr",
    "Bill - Date",
    "Bill - Name",
    "Bill - Amount",
    "Bill Amount - Dr/Cr",
    "Address",
    "State",
    "Country",
    "Pincode",
]


@dataclass
class ImportResult:
    parties_added: int = 0
    parties_updated: int = 0
    bills_added: int = 0
    bills_skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Ledgers: {self.parties_added} added, {self.parties_updated} updated · "
            f"Bills: {self.bills_added} added, {self.bills_skipped} skipped"
            + (f" · {len(self.warnings)} warning(s)" if self.warnings else "")
        )


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s.startswith("="):
        return None  # formula – cannot evaluate without Excel
    try:
        return float(s)
    except ValueError:
        return None


def _to_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%b-%y", "%d.%m.%Y", "%d/%b/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:  # text cells like '2023-03-10 00:00:00'
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _to_str(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _drcr(v) -> Optional[str]:
    s = (_to_str(v) or "").lower()
    if s.startswith("dr"):
        return "Dr"
    if s.startswith("cr"):
        return "Cr"
    return None


def import_ledger_template(
    source: Union[str, Path, IO[bytes]],
    db_path=None,
    import_bills: bool = True,
    mark_bills_exported: bool = True,
) -> ImportResult:
    res = ImportResult()
    # data_only=True gives Excel's cached results for formula cells; the raw workbook lets us
    # detect formulas whose cached value is missing (never calculated by Excel).
    wb = openpyxl.load_workbook(source, data_only=True)
    ws = wb["Ledger"] if "Ledger" in wb.sheetnames else wb.worksheets[0]
    try:
        if hasattr(source, "seek"):
            source.seek(0)
        wb_raw = openpyxl.load_workbook(source, data_only=False)
        ws_raw = wb_raw[ws.title]
    except Exception:  # pragma: no cover - defensive
        ws_raw = None

    header = [(_to_str(c.value) or "") for c in ws[1]]
    if [h.lower() for h in header[: len(HEADERS)]] != [h.lower() for h in HEADERS]:
        res.warnings.append(
            "Header row does not exactly match the Tally default template; importing by column position."
        )

    blocks: list[dict] = []
    current: Optional[dict] = None
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        row = list(row) + [None] * (12 - len(row))
        name, group, ob, ob_drcr, bdate, bname, bamt, bdrcr, addr, state, country, pin = row[:12]
        if _to_str(name):
            raw_ob = ws_raw.cell(row=idx, column=3).value if ws_raw is not None else ob
            is_formula = isinstance(raw_ob, str) and raw_ob.strip().startswith("=")
            current = {
                "name": _to_str(name),
                "group": _to_str(group) or "Sundry Debtors",
                "ob": _to_float(ob),
                "ob_formula": is_formula and _to_float(ob) is None,
                "ob_drcr": _drcr(ob_drcr),
                "address": [],
                "state": _to_str(state),
                "country": _to_str(country),
                "pincode": _to_str(pin),
                "bills": [],
            }
            blocks.append(current)
        if current is None:
            continue  # stray continuation rows before any ledger
        if _to_str(addr):
            current["address"].append(_to_str(addr))
        for key, val in (("state", state), ("country", country), ("pincode", pin)):
            if not current[key] and _to_str(val):
                current[key] = _to_str(val)
        if any(x not in (None, "") for x in (bdate, bname, bamt)):
            current["bills"].append(
                {"date": _to_date(bdate), "name": _to_str(bname), "amount": _to_float(bamt), "drcr": _drcr(bdrcr)}
            )

    for b in blocks:
        existing = db.get_party_by_name(b["name"], db_path)
        if b["ob_formula"]:
            res.warnings.append(f"{b['name']}: opening balance is a formula – left blank.")
        party = Party(
            id=existing.id if existing else None,
            name=b["name"],
            group_name=b["group"],
            opening_balance=b["ob"] if b["ob"] is not None else (existing.opening_balance if existing else None),
            opening_drcr=b["ob_drcr"] or (existing.opening_drcr if existing else None),
            address="\n".join(b["address"]) or (existing.address if existing else None),
            state=b["state"] or (existing.state if existing else None),
            country=b["country"] or (existing.country if existing else None),
            pincode=b["pincode"] or (existing.pincode if existing else None),
            aliases=existing.aliases if existing else [],
        )
        saved = db.upsert_party(party, db_path)
        if existing:
            res.parties_updated += 1
        else:
            res.parties_added += 1

        if not import_bills:
            continue
        for bl in b["bills"]:
            if bl["date"] is None or bl["name"] is None or bl["amount"] is None:
                res.bills_skipped += 1
                res.warnings.append(f"{b['name']}: bill {bl} incomplete – skipped.")
                continue
            if db.bill_no_exists(saved.id, bl["name"], db_path):
                res.bills_skipped += 1
                continue
            drcr = bl["drcr"] or ("Cr" if "creditor" in saved.group_name.lower() else "Dr")
            db.insert_bill(
                Bill(
                    party_id=saved.id,
                    bill_date=bl["date"],
                    bill_no=bl["name"],
                    amount=bl["amount"],
                    drcr=drcr,
                    txn_type="purchase" if drcr == "Cr" else "sale",
                    source="import",
                    exported=mark_bills_exported,
                ),
                db_path,
            )
            res.bills_added += 1
    return res

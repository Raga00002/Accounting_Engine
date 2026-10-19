"""Bank deposit slips / counterfoils -> Contra or Receipt vouchers for Tally.

Flow: photo -> Claude reads the slip (bank, branch, date, account no., account name, amount in
figures and words, cash/cheque, cheque no.) -> the user confirms per slip whether it is a
Contra (own cash deposited: Bank Dr / Cash Cr) or a Receipt from a party (Bank Dr / Party Cr)
-> saved in ``bank_txns`` -> exported as voucher XML / Tally Excel / CSV.

Bank ledger names are remembered per account number (settings key ``bank_ledger:<acct>``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import config, db, extractor, tenant
from .extractor import ExtractionError, _num, _parse_date, _text
from .models import Party
from .tally_export import BankAlloc, SimpleEntry, SimpleVoucher, TallySettings

# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
class BankSlipDraft(BaseModel):
    bank_name: Optional[str] = None
    branch: Optional[str] = None
    slip_date: Optional[date] = None
    account_no: Optional[str] = None
    account_name: Optional[str] = None
    amount: Optional[float] = None
    amount_words: Optional[str] = None
    mode: str = "cash"  # cash | cheque | transfer | other
    cheque_no: Optional[str] = None
    cheque_bank: Optional[str] = None
    depositor: Optional[str] = None  # name / phone written by the depositor, if any
    raw_text: Optional[str] = None
    notes: list[str] = Field(default_factory=list)
    source: str = "image"


class BankTxn(BaseModel):
    id: Optional[int] = None
    txn_date: date
    account_no: str
    bank_ledger: str
    bank_name: Optional[str] = None
    branch: Optional[str] = None
    amount: float
    vch_type: str  # "Contra" | "Receipt"
    party_id: Optional[int] = None
    counter_ledger: str  # Cash ledger or the party name at save time
    mode: str = "cash"
    inst_no: Optional[str] = None
    inst_bank: Optional[str] = None  # drawee bank of a cheque / remitter's bank – Tally's "Bank Name" in the bank allocation
    vch_no: str
    narration: Optional[str] = None
    image_path: Optional[str] = None
    raw_text: Optional[str] = None
    exported: bool = False


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
SLIP_TOOL: dict[str, Any] = {
    "name": "record_bank_slip",
    "description": "Record the contents of ONE bank pay-in slip / deposit counterfoil / deposit receipt.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bank_name": {"type": ["string", "null"], "description": "Bank name printed on the slip, e.g. 'Punjab National Bank'"},
            "branch": {"type": ["string", "null"], "description": "Branch name if written or stamped (e.g. 'Nagaon'); null if not readable"},
            "slip_date": {"type": ["string", "null"], "description": "Date of deposit as YYYY-MM-DD, read from the DDMMYYYY boxes or the bank stamp. null if absent"},
            "account_no": {"type": ["string", "null"], "description": "Account number digits exactly as written in the boxes (no spaces). null if absent"},
            "account_name": {"type": ["string", "null"], "description": "Name of the account holder as written in the Name field"},
            "amount": {"type": ["number", "null"], "description": "Amount deposited in figures (rupees). Use the boxed/circled total; ignore denomination breakdowns"},
            "amount_words": {"type": ["string", "null"], "description": "Amount in words as written, verbatim"},
            "mode": {"type": "string", "enum": ["cash", "cheque", "transfer", "other"], "description": "cash unless a cheque/DD number and bank are filled in"},
            "cheque_no": {"type": ["string", "null"], "description": "Cheque / DD number if this is a cheque deposit"},
            "cheque_bank": {"type": ["string", "null"], "description": "Drawee bank of the cheque, if any"},
            "depositor": {"type": ["string", "null"], "description": "Depositor name / phone number if written separately from the account holder"},
            "raw_text": {"type": "string", "description": "Verbatim transcription of everything handwritten or stamped on the slip"},
            "notes": {"type": "array", "items": {"type": "string"}, "description": "Doubts: illegible digits, figures vs words mismatch, faded stamp, etc."},
        },
        "required": ["bank_name", "branch", "slip_date", "account_no", "account_name", "amount", "amount_words", "mode",
                     "cheque_no", "cheque_bank", "depositor", "raw_text", "notes"],
    },
}

SLIP_PROMPT = """You are a meticulous accounts clerk for {business}, an Indian trader. You read photographs of bank
pay-in slips / deposit counterfoils (Punjab National Bank, SBI, etc.) and record them exactly.

Conventions:
- The date is written one digit per box as DDMMYYYY (e.g. boxes 1 9 0 9 2 0 2 6 -> 2026-09-19). Never guess a date;
  a bank stamp may also carry the date.
- The account number is written one digit per box; copy every digit. A leading '0' box may be part of it.
- 'Name' is the ACCOUNT HOLDER (our own firm, e.g. 'Janata Chira Udyog', 'Wings Industries'), not the depositor.
- Amount in figures is usually circled or boxed and repeated in the Total row; the amount in words is written below
  it ('One lac forty thousand only' = 140000; 'Three lac' = 300000; 'Fifty thousand' = 50000; lac/lakh = 100000).
  If figures and words disagree, report the figures in `amount` and explain in `notes`.
- Red/other-colour numbers written by the cashier are the bank's own notes; the total row in the cashier's hand still
  states the amount. Denomination breakdowns (2000 x n, 500 x n) are not the amount.
- mode is 'cash' unless a cheque number and bank are filled in the 'Cheque No. & Name of Bank' row.
- Today's date is {today}; do not use it as the slip date.

Known bank accounts of the business (account number -> ledger name):
{accounts}

Always respond by calling the record_bank_slip tool exactly once."""


def _slip_prompt(accounts: dict[str, str]) -> str:
    acc = "\n".join(f"- {k}: {v}" for k, v in accounts.items()) or "- (none yet)"
    return SLIP_PROMPT.format(business=config.BUSINESS_NAME, today=date.today().isoformat(), accounts=acc)


def extract_slip(data: bytes, filename: str = "slip.jpg", accounts: Optional[dict[str, str]] = None,
                 mock_key: Optional[str] = None) -> BankSlipDraft:
    if config.EXTRACTOR_MODE == "mock":
        from . import runtime

        with runtime.scan("slip_image") as sc:
            d = _mock(mock_key or Path(filename).stem)
            sc.record("mock")
        return d
    media_type, payload = extractor._prepare_image(data, filename)
    block = ({"type": "document", "source": {"type": "base64", "media_type": media_type, "data": payload}}
             if media_type == "application/pdf" else
             {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": payload}})
    raw = extractor._call_claude(
        [block, {"type": "text", "text": "Read this bank deposit slip and record it with the record_bank_slip tool."}],
        [], [], tool=SLIP_TOOL, system=_slip_prompt(accounts or {}), kind="slip_image",
    )
    return to_draft(raw)


def to_draft(raw: dict) -> BankSlipDraft:
    if not isinstance(raw, dict):
        raise ExtractionError("The reader returned no usable slip data.")
    notes = raw.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    elif not isinstance(notes, list):
        notes = []
    mode = str(raw.get("mode") or "cash").lower()
    if mode not in ("cash", "cheque", "transfer", "other"):
        mode = "cash"
    acct = _text(raw.get("account_no"))
    if acct:
        acct = re.sub(r"\D", "", acct) or acct
    d = BankSlipDraft(
        bank_name=_text(raw.get("bank_name")), branch=_text(raw.get("branch")), slip_date=_parse_date(raw.get("slip_date")),
        account_no=acct, account_name=_text(raw.get("account_name")), amount=_num(raw.get("amount")),
        amount_words=_text(raw.get("amount_words")), mode=mode, cheque_no=_text(raw.get("cheque_no")),
        cheque_bank=_text(raw.get("cheque_bank")), depositor=_text(raw.get("depositor")),
        raw_text=_text(raw.get("raw_text")), notes=[str(n) for n in notes if str(n).strip()],
    )
    w = words_to_number(d.amount_words)
    if d.amount is not None and w is not None and abs(w - d.amount) > 0.5:
        d.notes.append(f"Amount in words reads as {w:,.0f} but figures say {d.amount:,.0f} – check.")
    return d


def _mock(key: str) -> BankSlipDraft:
    d = config.FIXTURES_DIR / "mock_extractions" / "bank"
    f = d / f"{key}.json"
    if not f.exists():
        f = d / "default.json"
    if not f.exists():
        raise ExtractionError(f"Mock slip extraction not found for '{key}' in {d}.")
    return to_draft(json.loads(f.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# amount in words -> number (Indian English: lac/lakh/crore, 'only')
# --------------------------------------------------------------------------- #
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,
          "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "lac": 100000, "lakh": 100000, "lacs": 100000, "lakhs": 100000,
           "crore": 10000000, "crores": 10000000}


def words_to_number(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    toks = re.findall(r"[a-z]+", text.lower().replace("-", " "))
    toks = [t for t in toks if t not in ("only", "rupees", "rupee", "rs", "and", "paise")]
    if not toks or not any(t in _UNITS or t in _SCALES for t in toks):
        return None
    total = 0
    current = 0
    seen = False
    for t in toks:
        if t in _UNITS:
            current += _UNITS[t]
            seen = True
        elif t == "hundred":
            current = (current or 1) * 100
            seen = True
        elif t in _SCALES:
            total += (current or 1) * _SCALES[t]
            current = 0
            seen = True
        else:
            return None  # an unknown word -> don't pretend to understand
    return float(total + current) if seen else None


# --------------------------------------------------------------------------- #
# bank account -> ledger mapping (settings table)
# --------------------------------------------------------------------------- #
def bank_ledger_for(account_no: str, db_path=None) -> Optional[str]:
    if not account_no:
        return None
    return db.get_setting(f"bank_ledger:{account_no}", None, db_path)


def set_bank_ledger(account_no: str, ledger: str, db_path=None) -> None:
    if account_no and ledger.strip():
        db.set_setting(f"bank_ledger:{account_no}", ledger.strip(), db_path)


def known_accounts(db_path=None) -> dict[str, str]:
    with db.connect(db_path) as c:
        rows = c.execute("SELECT key, value FROM settings WHERE key LIKE 'bank_ledger:%'").fetchall()
    return {r["key"].split(":", 1)[1]: r["value"] for r in rows if r["value"]}


def delete_account(account_no: str, db_path=None) -> None:
    with db.connect(db_path) as c:
        c.execute("DELETE FROM settings WHERE key=?", (f"bank_ledger:{account_no}",))


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_txns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_date       TEXT NOT NULL,
    account_no     TEXT NOT NULL,
    bank_ledger    TEXT NOT NULL,
    bank_name      TEXT,
    branch         TEXT,
    amount         REAL NOT NULL,
    vch_type       TEXT NOT NULL CHECK (vch_type IN ('Contra','Receipt')),
    party_id       INTEGER REFERENCES parties(id) ON DELETE SET NULL,
    counter_ledger TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT 'cash',
    inst_no        TEXT,
    inst_bank      TEXT,
    vch_no         TEXT NOT NULL UNIQUE,
    narration      TEXT,
    image_path     TEXT,
    raw_text       TEXT,
    exported       INTEGER NOT NULL DEFAULT 0,
    exported_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bank_txns_date ON bank_txns(txn_date);
"""


def init(db_path=None) -> None:
    with db.connect(db_path) as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(bank_txns)").fetchall()}
        if "inst_bank" not in cols:  # databases created before the column existed
            c.execute("ALTER TABLE bank_txns ADD COLUMN inst_bank TEXT")


def _row(r) -> BankTxn:
    return BankTxn(id=r["id"], txn_date=date.fromisoformat(r["txn_date"]), account_no=r["account_no"],
                   bank_ledger=r["bank_ledger"], bank_name=r["bank_name"], branch=r["branch"], amount=r["amount"],
                   vch_type=r["vch_type"], party_id=r["party_id"], counter_ledger=r["counter_ledger"], mode=r["mode"],
                   inst_no=r["inst_no"], inst_bank=r["inst_bank"], vch_no=r["vch_no"], narration=r["narration"], image_path=r["image_path"],
                   raw_text=r["raw_text"], exported=bool(r["exported"]))


def vch_no_exists(vch_no: str, db_path=None) -> bool:
    init(db_path)
    with db.connect(db_path) as c:
        return c.execute("SELECT 1 FROM bank_txns WHERE vch_no=? COLLATE NOCASE", (vch_no.strip(),)).fetchone() is not None


def next_vch_no(prefix: str, db_path=None) -> str:
    return db.next_bill_no(prefix, db_path)


def insert_txn(t: BankTxn, db_path=None) -> BankTxn:
    init(db_path)
    with db.connect(db_path) as c:
        cur = c.execute(
            """INSERT INTO bank_txns (txn_date, account_no, bank_ledger, bank_name, branch, amount, vch_type, party_id,
               counter_ledger, mode, inst_no, inst_bank, vch_no, narration, image_path, raw_text, exported)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.txn_date.isoformat(), t.account_no, t.bank_ledger, t.bank_name, t.branch, round(float(t.amount), 2),
             t.vch_type, t.party_id, t.counter_ledger, t.mode, t.inst_no, t.inst_bank, t.vch_no.strip(), t.narration, t.image_path,
             t.raw_text, int(t.exported)),
        )
        r = c.execute("SELECT * FROM bank_txns WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row(r)


def get_txn(txn_id: int, db_path=None) -> Optional[BankTxn]:
    init(db_path)
    with db.connect(db_path) as c:
        r = c.execute("SELECT * FROM bank_txns WHERE id=?", (txn_id,)).fetchone()
    return _row(r) if r else None


def list_txns(db_path=None, date_from: Optional[date] = None, date_to: Optional[date] = None,
              exported: Optional[bool] = None) -> list[BankTxn]:
    init(db_path)
    where, args = [], []
    if date_from:
        where.append("txn_date>=?")
        args.append(date_from.isoformat())
    if date_to:
        where.append("txn_date<=?")
        args.append(date_to.isoformat())
    if exported is not None:
        where.append("exported=?")
        args.append(int(exported))
    sql = "SELECT * FROM bank_txns" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY txn_date, id"
    with db.connect(db_path) as c:
        return [_row(r) for r in c.execute(sql, args).fetchall()]


def mark_exported(ids: list[int], exported: bool = True, db_path=None) -> None:
    if not ids:
        return
    with db.connect(db_path) as c:
        c.execute(f"UPDATE bank_txns SET exported=?, exported_at=? WHERE id IN ({','.join('?' * len(ids))})",
                  [int(exported), datetime.now().isoformat(timespec="seconds") if exported else None, *ids])


def delete_txn(txn_id: int, db_path=None) -> None:
    with db.connect(db_path) as c:
        c.execute("DELETE FROM bank_txns WHERE id=?", (txn_id,))


def save_slip_image(data: bytes, filename: str, vch_no: str) -> Optional[str]:
    try:
        ext = Path(filename).suffix.lower() or ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
            ext = ".jpg"
        img_dir = tenant.current().image_dir
        img_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", vch_no).strip("_") or "slip"
        out = img_dir / f"{datetime.now():%Y%m%d_%H%M%S_%f}_slip_{safe}{ext}"
        out.write_bytes(data)
        return str(out)
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# -> Tally vouchers
# --------------------------------------------------------------------------- #
TALLY_TXN_TYPES = {"cash": "Cash", "cheque": "Cheque/DD", "transfer": "e-Fund Transfer", "other": "Others"}


def to_voucher(t: BankTxn, s: TallySettings) -> SimpleVoucher:
    # Tally's bank allocation "Bank Name" is the *instrument's* bank (the cheque's drawee bank / the remitter's
    # bank), not our own bank – the ledger already is our bank. For a cash deposit Tally asks for none, so the
    # slip's bank is kept there only as information.
    inst_bank = (t.inst_bank or "").strip()
    alloc = BankAlloc(txn_type=TALLY_TXN_TYPES.get(t.mode, "Others"),
                      inst_no=t.inst_no or "", inst_date=t.txn_date if t.inst_no else None,
                      bank_name=inst_bank if t.mode != "cash" else (t.bank_name or ""),
                      branch=(t.branch or "") if t.mode == "cash" else "")
    bank_entry = SimpleEntry(t.bank_ledger, t.amount, "Dr", bank=alloc)
    if t.vch_type == "Receipt":
        counter = SimpleEntry(t.counter_ledger, t.amount, "Cr", is_party=True, bill_type="On Account")
        vtype = s.receipt_vch_type
    else:
        counter = SimpleEntry(t.counter_ledger, t.amount, "Cr")
        vtype = s.contra_vch_type
    narr = t.narration or (f"{'Cash' if t.mode == 'cash' else t.mode.title()} deposit "
                           f"{t.bank_name or ''} {t.branch or ''} A/c …{t.account_no[-4:]}").strip()
    return SimpleVoucher(date=t.txn_date, vch_type=vtype, number=t.vch_no, entries=[bank_entry, counter],
                         narration=narr, reference=t.inst_no or "")


def vouchers_for(txns: list[BankTxn], s: TallySettings) -> list[SimpleVoucher]:
    return [to_voucher(t, s) for t in txns]


def parties_for(txns: list[BankTxn], db_path=None) -> list[Party]:
    out: list[Party] = []
    for pid in sorted({t.party_id for t in txns if t.party_id}):
        p = db.get_party(pid, db_path)
        if p:
            out.append(p)
    return out

"""SQLite persistence layer.

A fresh connection is opened per operation (cheap for SQLite, safe under Streamlit's
multi-thread script runner). All public functions accept an optional ``db_path`` so
tests can point at a temporary database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

from . import config
from .models import Bill, LineItem, Party

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS parties (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    group_name      TEXT NOT NULL DEFAULT 'Sundry Debtors',
    opening_balance REAL,
    opening_drcr    TEXT CHECK (opening_drcr IN ('Dr','Cr') OR opening_drcr IS NULL),
    address         TEXT,
    state           TEXT,
    country         TEXT,
    pincode         TEXT,
    aliases         TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    party_id      INTEGER NOT NULL REFERENCES parties(id) ON DELETE RESTRICT,
    bill_date     TEXT NOT NULL,              -- ISO yyyy-mm-dd
    bill_no       TEXT NOT NULL,
    amount        REAL NOT NULL,
    drcr          TEXT NOT NULL CHECK (drcr IN ('Dr','Cr')),
    txn_type      TEXT NOT NULL DEFAULT 'sale' CHECK (txn_type IN ('sale','purchase')),
    source        TEXT NOT NULL DEFAULT 'manual',
    raw_text      TEXT,
    image_path    TEXT,
    total_written REAL,
    exported      INTEGER NOT NULL DEFAULT 0,
    exported_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (party_id, bill_no)
);

CREATE TABLE IF NOT EXISTS bill_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id    INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    line_no    INTEGER NOT NULL,
    item       TEXT NOT NULL,
    qty        REAL,
    unit       TEXT,
    weight_qtl REAL,
    rate       REAL,
    amount     REAL,
    struck_out INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    aliases      TEXT NOT NULL DEFAULT '[]',
    default_unit TEXT,
    last_rate    REAL,
    times_used   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_bills_party ON bills(party_id);
CREATE INDEX IF NOT EXISTS idx_bills_date ON bills(bill_date);
CREATE INDEX IF NOT EXISTS idx_bills_exported ON bills(exported);
"""


# --------------------------------------------------------------------------- #
# connection helpers
# --------------------------------------------------------------------------- #
def _path(db_path: Optional[Path | str]) -> Path:
    if db_path:
        return Path(db_path)
    from . import tenant  # local import: tenant imports config only, no cycle at module load

    return tenant.current().db_path


@contextmanager
def connect(db_path: Optional[Path | str] = None) -> Iterator[sqlite3.Connection]:
    p = _path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, detect_types=0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[Path | str] = None) -> None:
    with connect(db_path) as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """Additive schema changes for databases created by older versions."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(items)").fetchall()}
    if "group_name" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN group_name TEXT")
    if "source" not in cols:
        c.execute("ALTER TABLE items ADD COLUMN source TEXT NOT NULL DEFAULT 'bill'")


def backup_to(dest: Path | str, db_path: Optional[Path | str] = None) -> Path:
    """Consistent copy of the database using SQLite's online-backup API.

    Copying the .db file directly can miss committed data that still sits in the -wal file
    (WAL mode) and, on Windows, can race with an open connection. The backup API copies
    the logical database, WAL included.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(_path(db_path)), timeout=30)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def backup_bytes(db_path: Optional[Path | str] = None) -> bytes:
    """The whole database as bytes (for a download button)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)  # Windows: the file must not be held open by us while SQLite writes it
    tmp_path = Path(tmp)
    try:
        backup_to(tmp_path, db_path)
        return tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# row <-> model
# --------------------------------------------------------------------------- #
def _row_to_party(r: sqlite3.Row) -> Party:
    return Party(
        id=r["id"],
        name=r["name"],
        group_name=r["group_name"],
        opening_balance=r["opening_balance"],
        opening_drcr=r["opening_drcr"],
        address=r["address"],
        state=r["state"],
        country=r["country"],
        pincode=r["pincode"],
        aliases=json.loads(r["aliases"] or "[]"),
    )


def _row_to_item(r: sqlite3.Row) -> LineItem:
    return LineItem(
        item=r["item"],
        qty=r["qty"],
        unit=r["unit"],
        weight_qtl=r["weight_qtl"],
        rate=r["rate"],
        amount=r["amount"],
        struck_out=bool(r["struck_out"]),
        note=r["note"],
    )


def _row_to_bill(r: sqlite3.Row, items: list[LineItem]) -> Bill:
    return Bill(
        id=r["id"],
        party_id=r["party_id"],
        bill_date=date.fromisoformat(r["bill_date"]),
        bill_no=r["bill_no"],
        amount=r["amount"],
        drcr=r["drcr"],
        txn_type=r["txn_type"],
        source=r["source"],
        raw_text=r["raw_text"],
        image_path=r["image_path"],
        total_written=r["total_written"],
        exported=bool(r["exported"]),
        items=items,
    )


# --------------------------------------------------------------------------- #
# parties
# --------------------------------------------------------------------------- #
def list_parties(db_path=None, search: Optional[str] = None) -> list[Party]:
    with connect(db_path) as c:
        if search:
            like = f"%{search.strip()}%"
            rows = c.execute(
                "SELECT * FROM parties WHERE name LIKE ? OR aliases LIKE ? ORDER BY name COLLATE NOCASE",
                (like, like),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM parties ORDER BY name COLLATE NOCASE").fetchall()
    return [_row_to_party(r) for r in rows]


def get_party(party_id: int, db_path=None) -> Optional[Party]:
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    return _row_to_party(r) if r else None


def get_party_by_name(name: str, db_path=None) -> Optional[Party]:
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM parties WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    return _row_to_party(r) if r else None


def upsert_party(p: Party, db_path=None) -> Party:
    """Insert a new party or update the existing one (by id, else by case-insensitive name)."""
    aliases = _clean_aliases(p.aliases, p.name)
    vals = (
        p.name.strip(),
        p.group_name.strip() or "Sundry Debtors",
        p.opening_balance,
        p.opening_drcr,
        (p.address or "").strip() or None,
        (p.state or "").strip() or None,
        (p.country or "").strip() or None,
        (str(p.pincode).strip() if p.pincode not in (None, "") else None),
        json.dumps(aliases, ensure_ascii=False),
    )
    with connect(db_path) as c:
        existing = None
        if p.id:
            existing = c.execute("SELECT id FROM parties WHERE id = ?", (p.id,)).fetchone()
        if existing is None:
            existing = c.execute(
                "SELECT id FROM parties WHERE name = ? COLLATE NOCASE", (p.name.strip(),)
            ).fetchone()
        if existing:
            pid = existing["id"]
            c.execute(
                """UPDATE parties SET name=?, group_name=?, opening_balance=?, opening_drcr=?,
                   address=?, state=?, country=?, pincode=?, aliases=?, updated_at=datetime('now')
                   WHERE id=?""",
                vals + (pid,),
            )
        else:
            cur = c.execute(
                """INSERT INTO parties (name, group_name, opening_balance, opening_drcr,
                   address, state, country, pincode, aliases) VALUES (?,?,?,?,?,?,?,?,?)""",
                vals,
            )
            pid = cur.lastrowid
        r = c.execute("SELECT * FROM parties WHERE id = ?", (pid,)).fetchone()
    return _row_to_party(r)


def add_party_alias(party_id: int, alias: str, db_path=None) -> Party:
    alias = (alias or "").strip()
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
        if r is None:
            raise ValueError(f"party id {party_id} not found")
        aliases = json.loads(r["aliases"] or "[]")
        if alias and alias.lower() != r["name"].lower() and alias.lower() not in {a.lower() for a in aliases}:
            aliases.append(alias)
            c.execute(
                "UPDATE parties SET aliases=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(aliases, ensure_ascii=False), party_id),
            )
        r = c.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    return _row_to_party(r)


def delete_party(party_id: int, db_path=None) -> None:
    with connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM bills WHERE party_id=?", (party_id,)).fetchone()[0]
        if n:
            raise ValueError(f"Cannot delete: party has {n} bill(s). Delete the bills first.")
        c.execute("DELETE FROM parties WHERE id=?", (party_id,))


def _clean_aliases(aliases: list[str], name: str) -> list[str]:
    out: list[str] = []
    seen = {name.strip().lower()}
    for a in aliases or []:
        a = (a or "").strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


# --------------------------------------------------------------------------- #
# bills
# --------------------------------------------------------------------------- #
def bill_no_exists(party_id: int, bill_no: str, db_path=None, exclude_bill_id: Optional[int] = None) -> bool:
    with connect(db_path) as c:
        if exclude_bill_id:
            r = c.execute(
                "SELECT 1 FROM bills WHERE party_id=? AND bill_no=? COLLATE NOCASE AND id<>?",
                (party_id, bill_no.strip(), exclude_bill_id),
            ).fetchone()
        else:
            r = c.execute(
                "SELECT 1 FROM bills WHERE party_id=? AND bill_no=? COLLATE NOCASE",
                (party_id, bill_no.strip()),
            ).fetchone()
    return r is not None


def insert_bill(b: Bill, db_path=None) -> Bill:
    with connect(db_path) as c:
        cur = c.execute(
            """INSERT INTO bills (party_id, bill_date, bill_no, amount, drcr, txn_type, source,
               raw_text, image_path, total_written, exported)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                b.party_id,
                b.bill_date.isoformat(),
                b.bill_no.strip(),
                round(float(b.amount), 2),
                b.drcr,
                b.txn_type,
                b.source,
                b.raw_text,
                b.image_path,
                b.total_written,
                int(b.exported),
            ),
        )
        bid = cur.lastrowid
        _insert_items(c, bid, b.items)
        _touch_items_master(c, b.items)
    return get_bill(bid, db_path)  # type: ignore[return-value]


def update_bill(b: Bill, db_path=None) -> Bill:
    if not b.id:
        raise ValueError("update_bill needs bill.id")
    with connect(db_path) as c:
        c.execute(
            """UPDATE bills SET party_id=?, bill_date=?, bill_no=?, amount=?, drcr=?, txn_type=?,
               raw_text=?, total_written=? WHERE id=?""",
            (
                b.party_id,
                b.bill_date.isoformat(),
                b.bill_no.strip(),
                round(float(b.amount), 2),
                b.drcr,
                b.txn_type,
                b.raw_text,
                b.total_written,
                b.id,
            ),
        )
        c.execute("DELETE FROM bill_items WHERE bill_id=?", (b.id,))
        _insert_items(c, b.id, b.items)
    return get_bill(b.id, db_path)  # type: ignore[return-value]


def _insert_items(c: sqlite3.Connection, bill_id: int, items: list[LineItem]) -> None:
    for n, it in enumerate(items, start=1):
        c.execute(
            """INSERT INTO bill_items (bill_id, line_no, item, qty, unit, weight_qtl, rate, amount,
               struck_out, note) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                bill_id,
                n,
                it.item.strip(),
                it.qty,
                (it.unit or "").strip() or None,
                it.weight_qtl,
                it.rate,
                it.effective_amount,
                int(it.struck_out),
                it.note,
            ),
        )


def _touch_items_master(c: sqlite3.Connection, items: list[LineItem]) -> None:
    """Update usage / last rate of the items a saved bill refers to (by name or alias).

    Never creates items: a spelling that is not in the master is added only after the user has
    confirmed it in the bill review (service.apply_item_decisions), so misreads never pollute it."""
    for it in items:
        if it.struck_out:
            continue
        r = _find_item_row(c, it.item)
        if r:
            c.execute(
                """UPDATE items SET default_unit=COALESCE(?, default_unit), last_rate=COALESCE(?, last_rate),
                   times_used=times_used+1, updated_at=datetime('now') WHERE id=?""",
                ((it.unit or None), it.rate, r["id"]),
            )


def get_bill(bill_id: int, db_path=None) -> Optional[Bill]:
    with connect(db_path) as c:
        r = c.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
        if not r:
            return None
        items = [
            _row_to_item(x)
            for x in c.execute(
                "SELECT * FROM bill_items WHERE bill_id=? ORDER BY line_no", (bill_id,)
            ).fetchall()
        ]
    return _row_to_bill(r, items)


def list_bills(
    db_path=None,
    party_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    exported: Optional[bool] = None,
    with_items: bool = False,
) -> list[Bill]:
    where, args = [], []
    if party_id:
        where.append("party_id=?")
        args.append(party_id)
    if date_from:
        where.append("bill_date>=?")
        args.append(date_from.isoformat())
    if date_to:
        where.append("bill_date<=?")
        args.append(date_to.isoformat())
    if exported is not None:
        where.append("exported=?")
        args.append(int(exported))
    sql = "SELECT * FROM bills"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY bill_date, id"
    with connect(db_path) as c:
        rows = c.execute(sql, args).fetchall()
        items_by_bill: dict[int, list[LineItem]] = {}
        if with_items and rows:
            ids = [r["id"] for r in rows]
            # chunk to stay under SQLite's variable limit
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                q = f"SELECT * FROM bill_items WHERE bill_id IN ({','.join('?'*len(chunk))}) ORDER BY bill_id, line_no"
                for x in c.execute(q, chunk).fetchall():
                    items_by_bill.setdefault(x["bill_id"], []).append(_row_to_item(x))
    return [_row_to_bill(r, items_by_bill.get(r["id"], [])) for r in rows]


def mark_exported(bill_ids: list[int], exported: bool = True, db_path=None) -> None:
    if not bill_ids:
        return
    with connect(db_path) as c:
        for i in range(0, len(bill_ids), 500):
            chunk = bill_ids[i : i + 500]
            c.execute(
                f"UPDATE bills SET exported=?, exported_at=? WHERE id IN ({','.join('?'*len(chunk))})",
                [int(exported), datetime.now().isoformat(timespec='seconds') if exported else None, *chunk],
            )


def delete_bill(bill_id: int, db_path=None) -> None:
    with connect(db_path) as c:
        c.execute("DELETE FROM bills WHERE id=?", (bill_id,))


# --------------------------------------------------------------------------- #
# items master
# --------------------------------------------------------------------------- #
def list_items(db_path=None) -> list[dict]:
    with connect(db_path) as c:
        rows = c.execute("SELECT * FROM items ORDER BY times_used DESC, name COLLATE NOCASE").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["aliases"] = json.loads(d.get("aliases") or "[]")
        out.append(d)
    return out


def upsert_item(name: str, aliases: Optional[list[str]] = None, default_unit: Optional[str] = None,
                last_rate: Optional[float] = None, group_name: Optional[str] = None,
                source: Optional[str] = None, db_path=None) -> None:
    with connect(db_path) as c:
        _upsert_item(c, name, aliases, default_unit, last_rate, group_name, source)


def _upsert_item(c: sqlite3.Connection, name: str, aliases: Optional[list[str]] = None,
                 default_unit: Optional[str] = None, last_rate: Optional[float] = None,
                 group_name: Optional[str] = None, source: Optional[str] = None,
                 alias_map: Optional[dict[str, int]] = None) -> bool:
    """Insert or update one item inside an open connection. Returns True if inserted.
    ``alias_map`` (lower-cased alias -> item id) lets bulk imports skip the per-row alias scan."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("item name cannot be empty")
    if alias_map is None:
        r = _find_item_row(c, name)
    else:
        r = c.execute("SELECT * FROM items WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if r is None and name.lower() in alias_map:
            r = c.execute("SELECT * FROM items WHERE id=?", (alias_map[name.lower()],)).fetchone()
    if r:
        # ``name`` may be one of the item's aliases – the canonical name stays.
        merged = _clean_aliases(json.loads(r["aliases"] or "[]") + list(aliases or []), r["name"])
        if name.lower() != r["name"].lower() and name.lower() not in {x.lower() for x in merged}:
            merged.append(name)
        c.execute(
            """UPDATE items SET aliases=?, default_unit=COALESCE(?, default_unit),
               last_rate=COALESCE(?, last_rate), group_name=COALESCE(?, group_name),
               updated_at=datetime('now') WHERE id=?""",
            (json.dumps(merged, ensure_ascii=False), default_unit or None, last_rate, group_name or None, r["id"]),
        )
        return False
    c.execute(
        "INSERT INTO items (name, aliases, default_unit, last_rate, group_name, source) VALUES (?,?,?,?,?,?)",
        (name, json.dumps(_clean_aliases(list(aliases or []), name), ensure_ascii=False),
         default_unit or None, last_rate, group_name or None, source or "bill"),
    )
    return True


def _find_item_row(c: sqlite3.Connection, text: str) -> Optional[sqlite3.Row]:
    """Item row whose name or one of whose aliases equals ``text`` (case-insensitive)."""
    text = (text or "").strip()
    if not text:
        return None
    r = c.execute("SELECT * FROM items WHERE name=? COLLATE NOCASE", (text,)).fetchone()
    if r:
        return r
    # aliases are stored as a JSON list – narrow with a case-insensitive substring match on the
    # quoted spelling, then confirm in Python (avoids scanning 8,000 rows' JSON per lookup).
    # SQLite's lower() only folds ASCII, so a spelling with non-ASCII letters ('Çhawal') skips the
    # narrowing and is compared in Python only (a few thousand JSON rows – still milliseconds).
    low = text.lower()
    if text.isascii():
        needle = json.dumps(text, ensure_ascii=False).lower()
        rows = c.execute("SELECT * FROM items WHERE instr(lower(aliases), ?) > 0", (needle,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM items WHERE aliases <> '[]'").fetchall()
    for r in rows:
        if low in {a.lower() for a in json.loads(r["aliases"] or "[]")}:
            return r
    return None


def bulk_upsert_items(rows: list[dict], db_path=None) -> tuple[int, int]:
    """rows: dicts with name and optional unit/rate/group/aliases. One transaction. Returns (added, updated)."""
    added = updated = 0
    with connect(db_path) as c:
        alias_map: dict[str, int] = {}
        for x in c.execute("SELECT id, aliases FROM items WHERE aliases <> '[]'").fetchall():
            for a in json.loads(x["aliases"] or "[]"):
                alias_map.setdefault(a.lower(), x["id"])
        for r in rows:
            try:
                ins = _upsert_item(c, r.get("name", ""), r.get("aliases"), r.get("unit"), r.get("rate"),
                                   r.get("group"), r.get("source", "import"), alias_map=alias_map)
            except ValueError:
                continue
            if ins:
                added += 1
            else:
                updated += 1
    return added, updated


def add_item_alias(name: str, alias: str, db_path=None) -> None:
    upsert_item(name, aliases=[alias], db_path=db_path)


def get_item_by_name_or_alias(text: str, db_path=None) -> Optional[dict]:
    """Exact (case-insensitive) lookup by canonical name or by any stored alias."""
    with connect(db_path) as c:
        r = _find_item_row(c, text)
    if r is None:
        return None
    d = dict(r)
    d["aliases"] = json.loads(d.get("aliases") or "[]")
    return d


def items_fingerprint(db_path=None) -> tuple:
    """Cheap change token for the items table – used to invalidate per-draft item checks."""
    with connect(db_path) as c:
        r = c.execute("SELECT COUNT(*), MAX(updated_at), MAX(id) FROM items").fetchone()
    return (r[0], r[1], r[2])


def bulk_upsert_parties(parties: list[Party], db_path=None) -> tuple[int, int]:
    """Insert/update many parties in ONE transaction (7,000+ ledgers import in seconds, not minutes)."""
    added = updated = 0
    with connect(db_path) as c:
        for p in parties:
            name = (p.name or "").strip()
            if not name:
                continue
            r = c.execute("SELECT * FROM parties WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if r:
                old_aliases = json.loads(r["aliases"] or "[]")
                aliases = _clean_aliases(old_aliases + list(p.aliases or []), name)
                c.execute(
                    """UPDATE parties SET group_name=?, opening_balance=COALESCE(?, opening_balance),
                       opening_drcr=COALESCE(?, opening_drcr), address=COALESCE(?, address),
                       state=COALESCE(?, state), country=COALESCE(?, country), pincode=COALESCE(?, pincode),
                       aliases=?, updated_at=datetime('now') WHERE id=?""",
                    (p.group_name.strip() or r["group_name"], p.opening_balance, p.opening_drcr,
                     (p.address or "").strip() or None, (p.state or "").strip() or None,
                     (p.country or "").strip() or None,
                     (str(p.pincode).strip() if p.pincode not in (None, "") else None),
                     json.dumps(aliases, ensure_ascii=False), r["id"]),
                )
                updated += 1
            else:
                c.execute(
                    """INSERT INTO parties (name, group_name, opening_balance, opening_drcr, address, state,
                       country, pincode, aliases) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (name, p.group_name.strip() or "Sundry Debtors", p.opening_balance, p.opening_drcr,
                     (p.address or "").strip() or None, (p.state or "").strip() or None,
                     (p.country or "").strip() or None,
                     (str(p.pincode).strip() if p.pincode not in (None, "") else None),
                     json.dumps(_clean_aliases(list(p.aliases or []), name), ensure_ascii=False)),
                )
                added += 1
    return added, updated


def party_names_by_usage(limit: int = 400, db_path=None) -> list[str]:
    """Party names, most-billed first (what we show Claude as 'known parties')."""
    with connect(db_path) as c:
        rows = c.execute(
            """SELECT p.name, COUNT(b.id) AS n, MAX(b.created_at) AS last
               FROM parties p LEFT JOIN bills b ON b.party_id = p.id
               GROUP BY p.id ORDER BY n DESC, last DESC, p.name COLLATE NOCASE LIMIT ?""",
            (limit,),
        ).fetchall()
    return [r["name"] for r in rows]


def delete_item(item_id: int, db_path=None) -> None:
    with connect(db_path) as c:
        c.execute("DELETE FROM items WHERE id=?", (item_id,))


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: Optional[str] = None, db_path=None) -> Optional[str]:
    with connect(db_path) as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str, db_path=None) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def next_bill_no(prefix: str, db_path=None) -> str:
    """Sequential bill number like WI/2026-27/001 (Indian financial year)."""
    today = date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    fy = f"{fy_start}-{str(fy_start + 1)[-2:]}"
    key = f"bill_seq:{prefix}:{fy}"
    with connect(db_path) as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        n = int(r["value"]) + 1 if r else 1
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(n)),
        )
    return f"{prefix}/{fy}/{n:03d}"


def parties_fingerprint(db_path=None) -> tuple:
    """Cheap change token for the parties table (count + last update) – used to invalidate match caches."""
    with connect(db_path) as c:
        r = c.execute("SELECT COUNT(*), MAX(updated_at), MAX(id) FROM parties").fetchone()
    return (r[0], r[1], r[2])


def stats(db_path=None) -> dict:
    with connect(db_path) as c:
        return {
            "parties": c.execute("SELECT COUNT(*) FROM parties").fetchone()[0],
            "bills": c.execute("SELECT COUNT(*) FROM bills").fetchone()[0],
            "unexported": c.execute("SELECT COUNT(*) FROM bills WHERE exported=0").fetchone()[0],
            "items": c.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        }

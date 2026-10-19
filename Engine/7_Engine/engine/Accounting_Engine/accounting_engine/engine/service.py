"""Use-case layer between the UI and the engine modules (keeps app pages thin and testable)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import config, db, matcher, tenant, validator
from .models import Bill, BillDraft, LineItem, MatchCandidate, Party


@dataclass
class PartyResolution:
    kind: str  # 'auto' | 'suggest' | 'new'
    candidates: list[MatchCandidate] = field(default_factory=list)

    @property
    def best(self) -> Optional[Party]:
        return self.candidates[0].party if self.candidates else None


def resolve_party(draft: BillDraft, db_path=None) -> PartyResolution:
    parties = db.list_parties(db_path)
    cands = matcher.match_party(draft.party_name_raw, parties)
    return PartyResolution(kind=matcher.classify(cands), candidates=cands)


def known_names(db_path=None) -> tuple[list[str], list[str]]:
    """Names shown to Claude as 'known' – most-used first, capped (a Tally company can hold 8,000 ledgers)."""
    parties = db.party_names_by_usage(400, db_path)
    items = [i["name"] for i in db.list_items(db_path)][:400]  # list_items is ordered by times_used desc
    return parties, items


@dataclass
class ItemResolution:
    raw: str                       # spelling on the bill
    kind: str                      # 'known' | 'suggest' | 'new'
    canonical: Optional[str] = None            # for 'known': the master name (may equal raw)
    candidates: list[tuple[str, float]] = field(default_factory=list)  # for 'suggest'
    unit: Optional[str] = None     # unit seen on the bill (pre-fills the new-item form)


def resolve_items(draft: BillDraft, db_path=None) -> list[ItemResolution]:
    """Check every distinct item description on the bill against the items master.

    known   – exact name/alias, or a strict spelling variant (match_item)
    suggest – close candidates exist; the user must pick one or add as new
    new     – nothing similar; the user confirms before it is added
    """
    items = db.list_items(db_path)
    names = [i["name"] for i in items]
    alias_map = {}
    for i in items:
        alias_map[i["name"].lower()] = i["name"]
        for a in i["aliases"]:
            alias_map[a.lower()] = i["name"]
    out: list[ItemResolution] = []
    seen: set[str] = set()
    for it in draft.items:
        raw = it.item.strip()
        key = raw.lower()
        if not raw or key in seen or it.struck_out:  # crossed-out lines are not booked – nothing to confirm
            continue
        seen.add(key)
        if key in alias_map:
            out.append(ItemResolution(raw=raw, kind="known", canonical=alias_map[key], unit=it.unit))
            continue
        strict = matcher.match_item(raw, names)
        if strict:
            out.append(ItemResolution(raw=raw, kind="known", canonical=strict, unit=it.unit))
            continue
        cands = matcher.suggest_items(raw, names)
        out.append(ItemResolution(raw=raw, kind="suggest" if cands else "new", candidates=cands, unit=it.unit))
    return out


def apply_item_decisions(draft: BillDraft, decisions: dict[str, dict], db_path=None) -> None:
    """decisions: raw name (as on bill) -> {"action": "use"|"new", "name": canonical/new name,
    "unit": ..., "group": ...}. Renames the draft's lines, stores aliases, creates new items."""
    for raw, d in decisions.items():
        name = (d.get("name") or raw).strip()
        if not name:
            continue
        if d.get("action") == "new":
            db.upsert_item(name, aliases=[raw] if raw.lower() != name.lower() else None,
                           default_unit=d.get("unit"), group_name=d.get("group"), source="bill", db_path=db_path)
        elif raw.lower() != name.lower():
            db.add_item_alias(name, raw, db_path)
        # ``name`` may itself be an alias (or a case variant) of a master item – book the canonical name
        master = db.get_item_by_name_or_alias(name, db_path)
        if master:
            name = master["name"]
        for it in draft.items:
            if it.item.strip().lower() == raw.lower() and it.item.strip() != name:
                it.note = (it.note + "; " if it.note else "") + f"on bill: '{it.item.strip()}'"
                it.item = name


def normalise_items(items: list[LineItem], db_path=None) -> list[LineItem]:
    """Snap item spellings to the items master when confidently matched."""
    names = [i["name"] for i in db.list_items(db_path)]
    if not names:
        return items
    for it in items:
        hit = matcher.match_item(it.item, names)
        if hit and hit != it.item:
            it.note = (it.note + "; " if it.note else "") + f"was '{it.item}'"
            it.item = hit
    return items


def default_group(txn_type: str) -> str:
    return config.DEFAULT_CREDITOR_GROUP if txn_type == "purchase" else config.DEFAULT_DEBTOR_GROUP


def default_drcr(txn_type: str) -> str:
    return "Cr" if txn_type == "purchase" else "Dr"


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:60] or "bill"


def save_bill_image(data: bytes, filename: str, party_name: str, bill_no: str) -> str:
    ext = Path(filename).suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".gif"}:
        ext = ".jpg"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_dir = tenant.current().image_dir
    img_dir.mkdir(parents=True, exist_ok=True)  # user may have deleted the folder
    base = f"{stamp}_{_safe_name(party_name)}_{_safe_name(bill_no)}"
    out = img_dir / f"{base}{ext}"
    n = 1
    while out.exists():  # non-ASCII party names all collapse to 'bill' – never overwrite another bill's photo
        n += 1
        out = img_dir / f"{base}_{n}{ext}"
    out.write_bytes(data)
    return str(out)


def commit_bill(
    draft: BillDraft,
    party: Party,
    bill_date: date,
    bill_no: str,
    amount: float,
    image: Optional[tuple[bytes, str]] = None,
    db_path=None,
    item_decisions: Optional[dict[str, dict]] = None,
) -> Bill:
    """Persist party (new or updated), alias, image and bill in one go.

    ``party`` may be a new Party (id None) – it is created. If the spelling on the bill
    differs from the party's name it is stored as an alias so next time it auto-matches.
    """
    bill_no = (bill_no or "").strip()
    if not bill_no:
        raise ValueError("Bill number is required.")
    if amount is None:
        raise ValueError("Bill amount is required.")
    # Check the duplicate first when the party already exists, so a rejected save leaves no
    # side effects (no re-saved party, no new alias, no new items).
    if party.id and db.bill_no_exists(party.id, bill_no, db_path):
        raise ValueError(f"Bill number '{bill_no}' already exists for {party.name}.")
    saved_party = db.upsert_party(party, db_path)
    raw = (draft.party_name_raw or "").strip()
    if raw and raw.lower() != saved_party.name.lower() and raw.lower() not in {a.lower() for a in saved_party.aliases}:
        saved_party = db.add_party_alias(saved_party.id, raw, db_path)
    if db.bill_no_exists(saved_party.id, bill_no, db_path):
        raise ValueError(f"Bill number '{bill_no}' already exists for {saved_party.name}.")
    if item_decisions:  # after the duplicate checks: a rejected save creates no items / aliases
        apply_item_decisions(draft, item_decisions, db_path)

    image_path = None
    if image and image[0]:
        try:
            image_path = save_bill_image(image[0], image[1], saved_party.name, bill_no)
        except OSError:
            image_path = None  # never lose the bill because the image could not be written

    bill = Bill(
        party_id=saved_party.id,
        bill_date=bill_date,
        bill_no=bill_no,
        amount=round(float(amount), 2),
        drcr=default_drcr(draft.txn_type),
        txn_type=draft.txn_type,
        source=draft.source,
        raw_text=draft.raw_text,
        image_path=image_path,
        total_written=draft.total_written,
        items=draft.items,
    )
    return db.insert_bill(bill, db_path)


def draft_to_review(draft: BillDraft, party_resolved: bool, require_bill_no: bool = True):
    validator.fill_missing_amounts(draft)
    return validator.validate(draft, party_resolved=party_resolved, require_bill_no=require_bill_no)

from datetime import date
from pathlib import Path

import pytest

from engine import config, db
from engine.importer import import_ledger_template
from engine.models import Bill, LineItem, Party


def test_party_upsert_and_alias(tmp_db):
    p = db.upsert_party(Party(name="Das Store", group_name="Sundry Debtors", state="Assam"), tmp_db)
    assert p.id and p.name == "Das Store"
    # case-insensitive upsert updates rather than duplicating
    p2 = db.upsert_party(Party(name="das store", address="Main Road"), tmp_db)
    assert p2.id == p.id and p2.address == "Main Road"
    assert len(db.list_parties(tmp_db)) == 1
    p3 = db.add_party_alias(p.id, "Das Stores", tmp_db)
    assert p3.aliases == ["Das Stores"]
    db.add_party_alias(p.id, "DAS STORES", tmp_db)  # duplicate ignored
    assert db.get_party(p.id, tmp_db).aliases == ["Das Stores"]
    assert db.list_parties(tmp_db, search="stores")[0].id == p.id


def test_bill_roundtrip_and_items_master(tmp_db):
    p = db.upsert_party(Party(name="P. Roy"), tmp_db)
    items = [
        LineItem(item="Rice (R.R)", qty=20, unit="Bag", weight_qtl=5.20, rate=4090),
        LineItem(item="Rice (R.R 10)", qty=10, unit="Bag", weight_qtl=1.0, rate=4290, amount=4290),
        LineItem(item="Rice (Old)", qty=12, unit="Bag", struck_out=True),
    ]
    b = db.insert_bill(
        Bill(party_id=p.id, bill_date=date(2026, 9, 26), bill_no="02", amount=25558, drcr="Dr",
             source="image", items=items),
        tmp_db,
    )
    assert b.id and len(b.items) == 3
    assert b.items[0].amount == pytest.approx(21268)  # computed weight x rate persisted
    assert b.items[2].struck_out is True
    assert db.bill_no_exists(p.id, "02", tmp_db)
    assert not db.bill_no_exists(p.id, "03", tmp_db)
    # Saving a bill never creates items – spellings enter the master only after the user confirms
    # them (service.apply_item_decisions); known items (by name or alias) get their usage updated.
    assert db.list_items(tmp_db) == []
    db.upsert_item("Rice (R.R)", aliases=["Rice RR"], db_path=tmp_db)
    b_alias = db.insert_bill(
        Bill(party_id=p.id, bill_date=date(2026, 9, 26), bill_no="02b", amount=1, drcr="Dr",
             items=[LineItem(item="rice rr", qty=1, unit="Bag", rate=4100), LineItem(item="Rice (Old)", qty=1, struck_out=True)]),
        tmp_db,
    )
    names = {i["name"]: i for i in db.list_items(tmp_db)}
    assert set(names) == {"Rice (R.R)"}
    assert names["Rice (R.R)"]["times_used"] == 1 and names["Rice (R.R)"]["last_rate"] == 4100 and names["Rice (R.R)"]["default_unit"] == "Bag"
    db.delete_bill(b_alias.id, tmp_db)

    # duplicate bill number for the same party is rejected by the DB
    with pytest.raises(Exception):
        db.insert_bill(Bill(party_id=p.id, bill_date=date(2026, 9, 26), bill_no="02", amount=1, drcr="Dr"), tmp_db)

    # update + list filters + export marking
    b.amount = 25000
    b.items = b.items[:1]
    b2 = db.update_bill(b, tmp_db)
    assert b2.amount == 25000 and len(b2.items) == 1
    assert len(db.list_bills(tmp_db, exported=False)) == 1
    db.mark_exported([b.id], True, tmp_db)
    assert db.list_bills(tmp_db, exported=False) == []
    assert db.list_bills(tmp_db, date_from=date(2026, 9, 27)) == []
    assert len(db.list_bills(tmp_db, party_id=p.id, with_items=True)[0].items) == 1

    with pytest.raises(ValueError):
        db.delete_party(p.id, tmp_db)
    db.delete_bill(b.id, tmp_db)
    db.delete_party(p.id, tmp_db)
    assert db.stats(tmp_db)["parties"] == 0


def test_next_bill_no(tmp_db):
    a = db.next_bill_no("WI", tmp_db)
    b = db.next_bill_no("WI", tmp_db)
    assert a.endswith("/001") and b.endswith("/002") and a.startswith("WI/")


def test_import_template(tmp_db):
    res = import_ledger_template(config.TEMPLATE_PATH, tmp_db)
    assert res.parties_added == 15 and res.parties_updated == 0
    # ABC has 3 bills, Titan 1 -> 4 bills
    assert res.bills_added == 4 and res.bills_skipped == 0
    abc = db.get_party_by_name("ABC Company Limited", tmp_db)
    assert abc.group_name == "Sundry Debtors"
    assert abc.opening_balance == 10000 and abc.opening_drcr == "Dr"
    assert abc.address == "15 MG Road\nNear LIC building\nBangalore"
    assert abc.state == "Karnataka" and abc.country == "India"
    titan = db.get_party_by_name("Titan Corporation", tmp_db)
    assert titan.address == "18, Raju Street\n1st main 3rd cross\nJP nagar 8th Phase"
    tb = db.list_bills(tmp_db, party_id=titan.id)
    assert len(tb) == 1 and tb[0].bill_no == "6542" and tb[0].drcr == "Cr" and tb[0].exported
    hdfc = db.get_party_by_name("HDFC Bank", tmp_db)
    assert hdfc.opening_balance == 10200  # formula cell: Excel's cached result is used
    assert res.warnings == []
    # second import is idempotent
    res2 = import_ledger_template(config.TEMPLATE_PATH, tmp_db)
    assert res2.parties_added == 0 and res2.parties_updated == 15 and res2.bills_added == 0
    assert res2.bills_skipped == 4


def test_backup_bytes_is_a_complete_database(tmp_db, tmp_path):
    import sqlite3

    p = db.upsert_party(Party(name="Backup Co"), tmp_db)
    db.insert_bill(Bill(party_id=p.id, bill_date=date(2026, 1, 1), bill_no="B1", amount=10, drcr="Dr"), tmp_db)
    data = db.backup_bytes(tmp_db)
    assert data[:15] == b"SQLite format 3"
    out = tmp_path / "restored.db"
    out.write_bytes(data)
    c = sqlite3.connect(out)
    assert c.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 1
    c.close()


def test_commit_bill_duplicate_leaves_no_side_effects(tmp_db, monkeypatch):
    from engine import config as cfg, service
    from engine.models import BillDraft, LineItem

    monkeypatch.setattr(cfg, "BILL_IMAGE_DIR", tmp_db.parent / "imgs")
    p = db.upsert_party(Party(name="Das Store"), tmp_db)
    draft = BillDraft(party_name_raw="Das Stores", items=[LineItem(item="Rice", qty=1, rate=10)])
    service.commit_bill(draft, p, date(2026, 1, 1), "1", 10, db_path=tmp_db)
    assert db.get_party(p.id, tmp_db).aliases == ["Das Stores"]
    draft2 = BillDraft(party_name_raw="Das Storez", items=[LineItem(item="Rice", qty=1, rate=10)])
    with pytest.raises(ValueError, match="already exists"):
        service.commit_bill(draft2, db.get_party(p.id, tmp_db), date(2026, 1, 2), "1", 10, db_path=tmp_db)
    assert db.get_party(p.id, tmp_db).aliases == ["Das Stores"]  # no alias added for the rejected save
    # image files never overwrite each other even when names collapse to 'bill'
    a = service.save_bill_image(b"a", "x.jpg", "দাস স্টোর", "১")
    b = service.save_bill_image(b"b", "x.jpg", "দাস স্টোর", "১")
    assert a != b and Path(a).read_bytes() == b"a" and Path(b).read_bytes() == b"b"


def test_settings_restore_replaces_database_and_keeps_backup(tmp_path, monkeypatch):
    """ui.settings._restore_database: validates, backs up the live db, swaps the file, re-inits."""
    from engine import config as cfg
    from ui import settings

    live = tmp_path / "live" / "accounting.db"
    monkeypatch.setattr(cfg, "DB_PATH", live)
    db.init_db()
    db.upsert_party(Party(name="Old Co"))
    other = tmp_path / "other.db"
    db.init_db(other)
    p = db.upsert_party(Party(name="New Co"), other)
    db.insert_bill(Bill(party_id=p.id, bill_date=date(2026, 1, 1), bill_no="N1", amount=5, drcr="Dr"), other)
    settings._restore_database(db.backup_bytes(other))
    assert [x.name for x in db.list_parties()] == ["New Co"] and db.stats()["bills"] == 1
    baks = list(live.parent.glob("accounting.before_restore_*.db"))
    assert len(baks) == 1
    import sqlite3
    c = sqlite3.connect(baks[0])
    assert c.execute("SELECT name FROM parties").fetchone()[0] == "Old Co"
    c.close()
    # garbage is rejected and the live database is untouched
    with pytest.raises(Exception):
        settings._restore_database(b"SQLite format 3\x00" + b"\x00" * 4096)
    assert [x.name for x in db.list_parties()] == ["New Co"]
    assert not list(live.parent.glob(".restore_check_*"))

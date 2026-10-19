import io
import time
from datetime import date

import openpyxl
import pytest

from engine import config, db, matcher, service, tally_import as ti
from engine.models import BillDraft, LineItem, Party

LEDGER_TXT = config.FIXTURES_DIR / "tally_list_of_ledgers.txt"
LEDGER_PDF = config.FIXTURES_DIR / "tally_list_of_ledgers_sample.pdf"
STOCK_XLSX = config.FIXTURES_DIR / "tally_stock_summary.xlsx"


def test_list_of_ledgers_text_full_export():
    pv = ti.detect_and_parse(LEDGER_TXT.read_bytes(), "Master.pdf.txt")
    assert pv.kind == "ledger_list"
    assert len(pv.ledgers) == 7879  # every serial number accounted for
    gc = pv.group_counts
    assert gc["Sundry Debtors"] > 6400 and gc["Sundry Creditors"] > 370
    assert gc["GEM"] > 260 and gc["Govt. Debtors"] > 130          # learned user-defined groups
    assert "BENGAL BATTERY CO." not in gc                           # page header never glued onto names
    by_name = {l.name: l for l in pv.ledgers}
    assert by_name["105 Bn BSF"].group == "GEM"                     # name starting with digits survives
    assert by_name["20 Wing Air Force"].opening_balance == 25007 and by_name["20 Wing Air Force"].opening_drcr == "Dr"
    assert by_name["ADJ UTILITY APPS PRIVATE LIMITED"].group == "Sundry Creditors"   # wrapped over two lines
    assert by_name["ZUVENTUS HEALTHCARE LIMITED"].group == "Sundry Debtors"          # wrapped with blank line
    assert by_name["Zemira Techno Constructions"].opening_balance == 172824
    assert by_name["Abhishek Sharma IDFC"].group == "IDFC Finance"                    # 'IDFC IDFC Finance' rejected
    assert by_name["Aar Pee Infraprojects Pvt. Ltd."].group == "R K Garg Group"      # tail after 'Ltd.' -> group
    assert "Sundry Debtors" in pv.default_party_groups() and "GEM" in pv.default_party_groups()
    for g in ("Indirect Expenses", "Bank Accounts", "SALARY A/C", "Duties & Taxes", ti.UNKNOWN_GROUP):
        assert g not in pv.default_party_groups(), g
    assert pv.examples("GEM")[0] == "105 Bn BSF"


def test_list_of_ledgers_pdf():
    pv = ti.detect_and_parse(LEDGER_PDF.read_bytes(), "sample.pdf")
    assert pv.kind == "ledger_list" and len(pv.ledgers) == 151
    assert pv.ledgers[0].name == "105 Bn BSF" and pv.ledgers[0].group == "GEM"
    assert pv.ledgers[37].opening_balance == 25007


def test_stock_summary_xlsx():
    pv = ti.detect_and_parse(STOCK_XLSX.read_bytes(), "StkSum.xlsx")
    assert pv.kind == "stock_summary"
    names = {i.name: i for i in pv.items}
    assert len(names) == len(pv.items) == 715
    assert names["Amaron 130 Ah Tubular"].group == "Amaron" and names["Amaron 130 Ah Tubular"].unit == "Pcs"
    assert names["Amaron 130 Ah Tubular"].rate == pytest.approx(6705.57)
    assert names["Distilled Water 5 Ltrs"].group is None                # ungrouped item at the bottom
    assert "BENGAL BATTERY CO" not in names and "BBC NEW" not in names  # godown lines
    assert "EEZY" not in names and "Wires" not in names                 # plain sub-group headings
    assert "Grand Total" not in names and "Amaron" not in names         # totals / groups
    assert any("godown" in s for s in pv.skipped) and any("sub-group" in s for s in pv.skipped)


def test_generic_tables_and_template_detection(tmp_db):
    csv_items = b"Item Name,Unit,Rate,Group\nRice (BPT),Bag,1540,Rice\nMustard Oil,Tin,1480,Oil\nGrand Total,,,\n"
    pv = ti.detect_and_parse(csv_items, "items.csv")
    assert pv.kind == "item_list" and [i.name for i in pv.items] == ["Rice (BPT)", "Mustard Oil"]
    assert pv.items[1].unit == "Tin" and pv.items[1].rate == 1480 and pv.items[1].group == "Oil"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Sl. No.", "Name of Ledger", "Under", "Opening Balance"])
    ws.append([1, "Das Store", "Sundry Debtors", "1,200.00 Dr"])
    ws.append([2, "Purchase", "Purchase Accounts", None])
    buf = io.BytesIO()
    wb.save(buf)
    pv = ti.detect_and_parse(buf.getvalue(), "ledgers.xlsx")
    assert pv.kind == "ledger_list" and pv.ledgers[0].opening_balance == 1200 and pv.ledgers[0].opening_drcr == "Dr"
    assert pv.default_party_groups() == ["Sundry Debtors"]

    pv = ti.detect_and_parse(config.TEMPLATE_PATH.read_bytes(), "Ledgers_in_Default_Template.xlsx", db_path=tmp_db)
    assert pv.kind == "ledger_template" and pv.template_result.parties_added == 15

    assert ti.detect_and_parse(b"", "x.xlsx").kind == "unknown"
    assert ti.detect_and_parse(b"hello", "x.docx").kind == "unknown"
    assert ti.detect_and_parse(b"no numbered lines here\n", "x.txt").kind == "unknown"


def test_apply_ledgers_and_items_bulk(tmp_db):
    pv = ti.detect_and_parse(LEDGER_TXT.read_bytes(), "Master.txt")
    t = time.time()
    added, updated = ti.apply_ledgers(pv, pv.default_party_groups(), tmp_db)
    assert added > 7000 and updated <= 5 and time.time() - t < 15  # a few names repeat in the export
    p = db.get_party_by_name("20 Wing Air Force", tmp_db)
    assert p.group_name == "Govt. Debtors" and p.opening_balance == 25007 and p.opening_drcr == "Dr"
    assert db.get_party_by_name("ACCOUNTING CHARGES", tmp_db) is None  # Indirect Expenses not ticked
    # re-import is idempotent (updates, no duplicates)
    added2, updated2 = ti.apply_ledgers(pv, pv.default_party_groups(), tmp_db)
    assert added2 == 0 and updated2 == added + updated
    assert db.stats(tmp_db)["parties"] == added

    pv2 = ti.detect_and_parse(STOCK_XLSX.read_bytes(), "StkSum.xlsx")
    a, u = ti.apply_items(pv2, tmp_db)
    assert a == 715 and u == 0
    a, u = ti.apply_items(pv2, tmp_db)
    assert a == 0 and u == 715
    items = {i["name"]: i for i in db.list_items(tmp_db)}
    assert items["Amaron 130 Ah Tubular"]["group_name"] == "Amaron" and items["Amaron 130 Ah Tubular"]["default_unit"] == "Pcs"
    assert items["Amaron 130 Ah Tubular"]["source"] == "import"
    # known names for the prompt stay capped and prefer used parties
    db.insert_bill(__import__("engine.models", fromlist=["Bill"]).Bill(
        party_id=db.get_party_by_name("Zydus Healthcare Ltd", tmp_db).id, bill_date=date(2026, 9, 26), bill_no="1",
        amount=10, drcr="Dr"), tmp_db)
    parties, names = service.known_names(tmp_db)
    assert len(parties) == 400 and parties[0] == "Zydus Healthcare Ltd" and len(names) == 400


def test_matcher_with_8000_parties_is_fast(tmp_db):
    pv = ti.detect_and_parse(LEDGER_TXT.read_bytes(), "Master.txt")
    ti.apply_ledgers(pv, pv.default_party_groups(), tmp_db)
    parties = db.list_parties(tmp_db)
    t = time.time()
    cands = matcher.match_party("Zydus Healthcare", parties)
    assert time.time() - t < 2.0
    assert cands and cands[0].party.name == "Zydus Healthcare Ltd"


def test_item_resolution_and_decisions(tmp_db):
    db.upsert_item("Rice (BPT)", default_unit="Bag", db_path=tmp_db)
    db.upsert_item("Rice (R.R 10)", db_path=tmp_db)
    db.upsert_item("Mustard Oil", aliases=["Sarso Tel"], default_unit="Tin", db_path=tmp_db)
    draft = BillDraft(party_name_raw="X", items=[
        LineItem(item="rice bpt", qty=1, rate=1),      # strict variant -> known
        LineItem(item="Sarso Tel", qty=1, rate=1),     # alias -> known
        LineItem(item="Rice RR10", qty=1, rate=1),     # suggest ('RR10' is one token, 'R.R 10' two)
        LineItem(item="Kosal", qty=2, unit="Bag", rate=9000),  # new
        LineItem(item="Kosal", qty=1, rate=9000),      # duplicate raw -> one decision
    ])
    res = {r.raw: r for r in service.resolve_items(draft, tmp_db)}
    assert res["rice bpt"].kind == "known" and res["rice bpt"].canonical == "Rice (BPT)"
    assert res["Sarso Tel"].kind == "known" and res["Sarso Tel"].canonical == "Mustard Oil"
    assert res["Rice RR10"].kind == "suggest" and res["Rice RR10"].candidates[0][0] == "Rice (R.R 10)"
    assert res["Kosal"].kind == "new" and res["Kosal"].unit == "Bag"
    assert len(res) == 4

    decisions = {
        "rice bpt": {"action": "use", "name": "Rice (BPT)"},
        "Sarso Tel": {"action": "use", "name": "Mustard Oil"},
        "Rice RR10": {"action": "use", "name": "Rice (R.R 10)"},
        "Kosal": {"action": "new", "name": "Kosal Dal", "unit": "Bag", "group": "Pulses"},
    }
    service.apply_item_decisions(draft, decisions, tmp_db)
    assert [i.item for i in draft.items] == ["Rice (BPT)", "Mustard Oil", "Rice (R.R 10)", "Kosal Dal", "Kosal Dal"]
    assert "on bill: 'rice bpt'" in draft.items[0].note
    items = {i["name"]: i for i in db.list_items(tmp_db)}
    assert "Rice RR10" in items["Rice (R.R 10)"]["aliases"] and "rice bpt" in items["Rice (BPT)"]["aliases"]
    assert items["Kosal Dal"]["group_name"] == "Pulses" and "Kosal" in items["Kosal Dal"]["aliases"]
    # second time round everything is known ('Rice RR 10' is a strict initials variant of 'Rice (R.R 10)')
    d2 = BillDraft(items=[LineItem(item="Rice RR 10", qty=1, rate=1), LineItem(item="rice rr10", qty=1, rate=1), LineItem(item="kosal", qty=1, rate=1)])
    assert all(r.kind == "known" for r in service.resolve_items(d2, tmp_db))


def test_suggest_items_ranking():
    names = ["Rice (BPT)", "Rice (R.R)", "Rice (R.R 10)", "Mustard Oil", "Mung Dal"]
    assert matcher.suggest_items("Rice RR 10", names)[0][0] == "Rice (R.R 10)"
    assert matcher.suggest_items("Mustard Oil Tin", names)[0][0] == "Mustard Oil"
    assert matcher.suggest_items("Kosal", names) == []
    # a bare generic word is not confidently matched to any one variety
    top = matcher.suggest_items("Rice", names)
    assert top and all(s <= config.ITEM_SUGGEST_MATCH + 10 for _, s in top)


def test_learned_generic_groups_and_page_breaks():
    txt = LEDGER_TXT.read_text(encoding="utf-8")
    pv = ti.detect_and_parse(txt.encode("utf-8"), "m.txt")
    by = {l.name: l.group for l in pv.ledgers}
    # 'X Group' / 'X Ledger' sub-groups: the generic word alone is never the group
    assert "Group" not in pv.group_counts and "Ledger" not in pv.group_counts
    assert by["Dalmore Tea Estate"] == "Garg Tea Group" and by["Himalayan Tea Agro (P) Ltd."] == "Berlia Tea Group"
    assert by["Raju Bihani"] == "Raju Bihani Ledger" and by["Bundu Lachengpa Raju Bihani"] == "Raju Bihani Ledger"
    assert by["Ventota Retail AP"] == "Ventota Group" and by["Canara Bank Sikkim"] == "CANARA BANK GROUP"
    assert by["Aar Pee Infraprojects Pvt Ltd-Bihar"] == "R K Garg Group"   # 'Ltd-Bihar' is a company word
    assert by["Kamac Engineers Pvt Ltd-Bihar"] == "KAMAC ENGINEER’S GROUP"
    assert by["Dr Aditi Dey"] == "Sundry Debtors" and by["Battery Corporation (Dr)"] == "Sundry Debtors"
    # the same export without 'Sl. No.' headers / without 'continued' footers parses identically
    import re
    no_hdr = "\n".join(l for l in txt.splitlines() if not re.match(r"^\s*Sl\.?\s*No", l, re.I))
    no_cont = "\n".join(l for l in txt.splitlines() if not re.match(r"^\s*continued", l, re.I))
    for variant in (no_hdr, no_cont):
        pv2 = ti.detect_and_parse(variant.encode("utf-8"), "m.txt")
        assert len(pv2.ledgers) == 7879
        assert {l.name: l.group for l in pv2.ledgers} == by


def test_text_report_variants():
    t = ("Sl. No. Name of Ledger Under Opening Balance\n"
         "1 Dás Store Sundry Debtors (-)1,000.00 Dr\n"
         "2 Ram Traders Sundry Creditors\n1,72,824.00 Cr\n"           # balance wrapped to its own line
         "3 Foo Sundry Debtors\n")
    for enc in ("utf-8", "utf-8-sig", "utf-16", "cp1252"):
        pv = ti.detect_and_parse(t.encode(enc), "x.txt")
        assert [(l.name, l.opening_balance, l.opening_drcr) for l in pv.ledgers] == [
            ("Dás Store", -1000.0, "Dr"), ("Ram Traders", 172824.0, "Cr"), ("Foo", None, None)], enc
    pv = ti.detect_and_parse("﻿Item Name;Unit;Rate\nRice (BPT);Bag;1540\n".encode("utf-8"), "x.csv")
    assert pv.kind == "item_list" and pv.items[0].unit == "Bag" and pv.items[0].rate == 1540


def test_excel_drcr_in_number_format_and_particulars_table():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["BENGAL BATTERY CO."]); ws.append(["List of Ledgers"]); ws.append([])
    ws.append(["Sl. No.", "Name of Ledger", "Under", "Opening Balance"])
    ws.append([1, "Das Store", "Sundry Debtors", 1200]); ws.cell(row=5, column=4).number_format = '""0.00" Dr"'
    ws.append([2, "Ram Traders", "Sundry Creditors", -500]); ws.cell(row=6, column=4).number_format = '""0.00" Cr"'
    buf = io.BytesIO(); wb.save(buf)
    pv = ti.detect_and_parse(buf.getvalue(), "l.xlsx")
    assert [(l.name, l.opening_balance, l.opening_drcr) for l in pv.ledgers] == [("Das Store", 1200.0, "Dr"), ("Ram Traders", 500.0, "Cr")]
    # a hand-made list headed 'Particulars' with a Unit column is a plain table, not a Stock Summary
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Particulars", "Unit", "Rate"]); ws.append(["Rice (BPT)", "Bag", 1540])
    buf = io.BytesIO(); wb.save(buf)
    pv = ti.detect_and_parse(buf.getvalue(), "i.xlsx")
    assert pv.kind == "item_list" and pv.items[0].unit == "Bag"
    # ledger table with only a header row -> nothing to import, no crash downstream
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Name of Ledger", "Under"])
    buf = io.BytesIO(); wb.save(buf)
    assert ti.detect_and_parse(buf.getvalue(), "e.xlsx").ledgers == []


def test_migration_from_old_schema_and_item_alias_semantics(tmp_path):
    import sqlite3
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript("""
    CREATE TABLE parties (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        group_name TEXT NOT NULL DEFAULT 'Sundry Debtors', opening_balance REAL, opening_drcr TEXT, address TEXT,
        state TEXT, country TEXT, pincode TEXT, aliases TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        aliases TEXT NOT NULL DEFAULT '[]', default_unit TEXT, last_rate REAL, times_used INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')));
    INSERT INTO items (name, aliases, default_unit, last_rate, times_used) VALUES ('Rice (BPT)', '["rice bpt"]', 'Bag', 1540, 3);
    """)
    c.commit(); c.close()
    db.init_db(old); db.init_db(old)  # idempotent
    it = db.list_items(old)[0]
    assert it["group_name"] is None and it["source"] == "bill" and it["aliases"] == ["rice bpt"] and it["times_used"] == 3
    db.upsert_item("Rice (BPT)", group_name="Rice", db_path=old)
    db.upsert_item("Rice (BPT)", group_name=None, default_unit=None, db_path=old)  # None never wipes
    it = db.list_items(old)[0]
    assert it["group_name"] == "Rice" and it["default_unit"] == "Bag"
    # aliases are cleaned on insert; upserting by an alias spelling updates the canonical item
    db.upsert_item("Mustard Oil", aliases=["Sarso Tel", "sarso tel", "Mustard Oil"], db_path=old)
    assert db.get_item_by_name_or_alias("SARSO TEL", old)["aliases"] == ["Sarso Tel"]
    db.upsert_item("Sarso Tel", default_unit="Tin", db_path=old)
    assert [i["name"] for i in db.list_items(old)] == ["Rice (BPT)", "Mustard Oil"]
    assert db.get_item_by_name_or_alias("mustard oil", old)["default_unit"] == "Tin"
    assert db.get_item_by_name_or_alias("nothing", old) is None
    a, u = db.bulk_upsert_items([{"name": "sarso tel", "group": "Oil"}, {"name": "New One"}], old)
    assert (a, u) == (1, 1) and db.get_item_by_name_or_alias("Mustard Oil", old)["group_name"] == "Oil"
    # struck-out lines need no confirmation
    d = BillDraft(items=[LineItem(item="Ghost", qty=1, rate=1, struck_out=True), LineItem(item="Rice (BPT)", qty=1, rate=1)])
    assert [r.raw for r in service.resolve_items(d, old)] == ["Rice (BPT)"]


def test_list_of_ledgers_pdf_with_per_column_text_objects():
    """Tally writes each column as its own text object; only pypdf's layout mode re-joins a row."""
    pdf = (config.FIXTURES_DIR / "tally_list_of_ledgers_columns.pdf").read_bytes()
    text = ti.pdf_to_text(pdf)
    assert any(line.startswith("1 105 Bn BSF GEM") for line in text.splitlines()), text[:400]
    pv = ti.detect_and_parse(pdf, "Master.pdf")
    assert pv.kind == "ledger_list" and len(pv.ledgers) == 151 and pv.warnings == []
    by = {l.name: l for l in pv.ledgers}
    assert by["20 Wing Air Force"].opening_balance == 25007 and by["20 Wing Air Force"].opening_drcr == "Dr"
    assert by["20 Wing Air Force - Fd"].group == "Deposits (Asset)"
    assert by["2PC GENERAL BROKING & SERVICE"].group == "Sundry Creditors"


def test_qa3_parser_hardening_and_alias_lookup(tmp_db):
    # a truncated / fake PDF is reported, not raised
    pv = ti.detect_and_parse(b"%PDF-1.4 garbage", "broken.pdf")
    assert pv.kind == "unknown" and pv.warnings and "PDF" in pv.warnings[0]
    # 'Particulars | Under' is a ledger table, not a Stock Summary
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Particulars", "Under", "Opening Balance"]); ws.append(["Das Store", "Sundry Debtors", "1,200.00 Dr"])
    buf = io.BytesIO(); wb.save(buf)
    pv = ti.detect_and_parse(buf.getvalue(), "l.xlsx")
    assert pv.kind == "ledger_list" and pv.ledgers[0].group == "Sundry Debtors"
    # balance formats
    t = ("Sl. No. Name Under Opening Balance\n1 A Sundry Debtors (-) 500.00 Cr\n2 B Sundry Debtors -250.50 Dr\n"
         "3 C Sundry Debtors 1,72,824.00 Dr\n4 D Ltd GEM 10.00 Cr.\n5 E Ltd GEM\n6 12345 Sundry Debtors\n")
    pv = ti.detect_and_parse(t.encode(), "x.txt")
    assert [(l.name, l.group, l.opening_balance, l.opening_drcr) for l in pv.ledgers] == [
        ("A", "Sundry Debtors", -500.0, "Cr"), ("B", "Sundry Debtors", -250.5, "Dr"), ("C", "Sundry Debtors", 172824.0, "Dr"),
        ("D Ltd", "GEM", 10.0, "Cr"), ("E Ltd", "GEM", None, None), ("12345", "Sundry Debtors", None, None)]
    # bulk import: case / whitespace duplicates collapse, non-string names don't abort the transaction
    a, u = db.bulk_upsert_items([{"name": "ABC"}, {"name": "abc "}, {"name": " ABC"}, {"name": ""}, {"name": 12}], tmp_db)
    assert (a, u) == (2, 2) and [i["name"] for i in db.list_items(tmp_db)] == ["12", "ABC"]
    a, u = db.bulk_upsert_parties([Party(name="ABC"), Party(name="abc")], tmp_db)
    assert (a, u) == (1, 1) and len(db.list_parties(tmp_db)) == 1
    # alias lookup works for non-ASCII spellings in any case
    db.upsert_item("Rice", aliases=["Çhawal"], db_path=tmp_db)
    assert db.get_item_by_name_or_alias("çhawal", tmp_db)["name"] == "Rice"
    assert db.get_item_by_name_or_alias("ÇHAWAL", tmp_db)["name"] == "Rice"
    # a 'new' decision whose name is an alias books the canonical name; a rejected save adds nothing
    draft = BillDraft(party_name_raw="P", items=[LineItem(item="Kosal", qty=1, rate=1)])
    service.apply_item_decisions(draft, {"Kosal": {"action": "new", "name": "çhawal"}}, tmp_db)
    assert draft.items[0].item == "Rice" and "Kosal" in db.get_item_by_name_or_alias("Rice", tmp_db)["aliases"]
    p = db.upsert_party(Party(name="P"), tmp_db)
    d1 = BillDraft(party_name_raw="P", items=[LineItem(item="Mung", qty=1, rate=1)])
    service.commit_bill(d1, p, date(2026, 1, 1), "9", 1, db_path=tmp_db, item_decisions={"Mung": {"action": "new", "name": "Mung Dal"}})
    d2 = BillDraft(party_name_raw="P", items=[LineItem(item="Matar", qty=1, rate=1)])
    with pytest.raises(ValueError, match="already exists"):
        service.commit_bill(d2, p, date(2026, 1, 1), "9", 1, db_path=tmp_db, item_decisions={"Matar": {"action": "new", "name": "Matar"}})
    assert db.get_item_by_name_or_alias("Matar", tmp_db) is None
    assert db.get_item_by_name_or_alias("Mung Dal", tmp_db)["times_used"] == 1
    # two lines that differ only by case share one question and one decision
    d3 = BillDraft(items=[LineItem(item="Matar", qty=1, rate=1), LineItem(item="MATAR", qty=2, rate=1)])
    res = service.resolve_items(d3, tmp_db)
    assert [r.raw for r in res] == ["Matar"] and res[0].kind == "new"
    service.apply_item_decisions(d3, {"Matar": {"action": "new", "name": "Matar Dal"}}, tmp_db)
    assert [i.item for i in d3.items] == ["Matar Dal", "Matar Dal"]


def test_match_item_never_snaps_across_numbers_or_words():
    pv = ti.detect_and_parse(STOCK_XLSX.read_bytes(), "StkSum.xlsx")
    names = [i.name for i in pv.items]
    # a real master: no item may auto-snap to a *different* item (one digit / one word apart)
    for a in names:
        m = matcher.match_item(a, [b for b in names if b != a])
        assert m is None, (a, m)
    assert matcher.match_item("Amaron 150 Ah Tubular", names) is None          # 150 != 130
    assert matcher.match_item("Distilled Water 1 Ltrs", names) is None         # 1 != 5
    assert matcher.match_item("AMARON 40R", names) is None                     # 40R != 40L
    assert matcher.match_item("Microtek Online UPS", ["Microtek Offline UPS"]) is None
    # …but spelling / case / punctuation variants still snap
    assert matcher.match_item("amaron 130 ah tubuler", names) == "Amaron 130 Ah Tubular"
    assert matcher.match_item("Microtek Ofline UPS", names) == "Microtek Offline UPS"
    assert matcher.match_item("rice bpt", ["Rice (BPT)"]) == "Rice (BPT)"
    # runs of initials merge: this user's items are named 'Rice (R.R)', bills say 'Rice RR' / 'R.R. Rice'
    assert matcher.match_item("Rice RR", ["Rice (R.R)", "Rice (R.R 10)"]) == "Rice (R.R)"
    assert matcher.match_item("Rice RR 10", ["Rice (R.R)", "Rice (R.R 10)"]) == "Rice (R.R 10)"
    assert matcher.match_item("Rice RR10", ["Rice (R.R)", "Rice (R.R 10)"]) is None
    # the near-misses are still offered for confirmation
    assert matcher.suggest_items("Amaron 150 Ah Tubular", names)[0][0] == "Amaron 130 Ah Tubular"


def _stock_sheet(rows):
    from openpyxl.styles import Alignment, Font
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Stock Summary"]); ws.append(["Particulars", "Quantity", "Rate", "Value"])
    for name, bold, indent, qty, rate, val in rows:
        ws.append([name, qty, rate, val]); r = ws.max_row
        ws.cell(row=r, column=1).font = Font(b=bold); ws.cell(row=r, column=1).alignment = Alignment(indent=indent)
        if qty is not None:
            ws.cell(row=r, column=2).number_format = '""0" Pcs"'
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def test_stock_summary_item_with_same_figures_as_previous_is_kept():
    # godowns are recognised by their repeated name; a real item that merely repeats the previous
    # item's qty/rate/value ('40L' and '40R', 1 pc each at the same rate) must survive
    pv = ti.detect_and_parse(_stock_sheet([
        ("Amaron", True, 0, None, None, None),
        ("Amaron 40L", False, 2, 1, 3500, 3500), ("Amaron 40R", False, 2, 1, 3500, 3500),
        ("Amaron 130 Ah", False, 2, 2, 6700, 13400), ("Main Godown", False, 2, 2, 6700, 13400),
        ("Amaron 150 Ah", False, 2, 3, 7000, 21000), ("Main Godown", False, 2, 3, 7000, 21000),
        ("Grand Total", True, 0, 6, None, None)]), "s.xlsx")
    assert [i.name for i in pv.items] == ["Amaron 40L", "Amaron 40R", "Amaron 130 Ah", "Amaron 150 Ah"]
    assert pv.items[0].group == "Amaron" and pv.items[0].unit == "Pcs" and pv.items[0].rate == 3500
    # only one stocked item -> the godown line appears once; the figures fallback still catches it
    pv = ti.detect_and_parse(_stock_sheet([
        ("Amaron", True, 0, None, None, None), ("Amaron 40L", False, 2, None, None, None),
        ("Amaron 130 Ah", False, 2, 2, 6700, 13400), ("Main Godown", False, 2, 2, 6700, 13400),
        ("Grand Total", True, 0, 2, None, None)]), "s.xlsx")
    assert [i.name for i in pv.items] == ["Amaron 40L", "Amaron 130 Ah"] and any("godown" in s for s in pv.skipped)

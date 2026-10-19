from datetime import date
from xml.etree import ElementTree as ET

import pytest

from engine import bank, config, db, tally_export as tx
from engine.models import Party


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "EXTRACTOR_MODE", "mock")


@pytest.mark.parametrize("text, value", [
    ("One lac forty thousand only", 140000), ("Three lac only", 300000), ("Three lac fifty thousand only", 350000),
    ("Fifty thousand only", 50000), ("One lac only", 100000), ("Five lac only", 500000), ("Twenty thousand", 20000),
    ("Rupees Two Lakh Twenty-Five Thousand Six Hundred and Fifty only", 225650), ("one crore", 10000000),
    ("Fourty thousand", 40000), ("", None), (None, None), ("illegible scrawl", None), ("Ek lakh", None),
])
def test_words_to_number(text, value):
    assert bank.words_to_number(text) == value


def test_mock_slip_extraction_and_words_check():
    d = bank.extract_slip(b"x", "slip_140000.jpg")
    assert d.account_no == "3730002100302218" and d.amount == 140000 and d.slip_date == date(2026, 9, 19)
    assert d.account_name == "Janata Chira Udyog" and d.mode == "cash" and d.notes == []
    # figures vs words mismatch is flagged
    d2 = bank.to_draft({"amount": 14000, "amount_words": "One lac forty thousand only", "mode": "CASH", "account_no": "37-30 0021 00302218"})
    assert d2.account_no == "3730002100302218" and d2.mode == "cash"
    assert any("words reads as 140,000" in n for n in d2.notes)
    with pytest.raises(bank.ExtractionError):
        bank.to_draft(["not", "a", "dict"])  # type: ignore[arg-type]
    # cheque slip whose words disagree with the figures (used by the UI probes)
    d3 = bank.extract_slip(b"x", "slip_mismatch.jpg")
    assert d3.mode == "cheque" and d3.cheque_no == "123456" and d3.cheque_bank == "State Bank of India"
    assert d3.amount == 140000 and bank.words_to_number(d3.amount_words) == 114000 and any("114,000" in n for n in d3.notes)


def test_account_ledger_mapping(tmp_db):
    assert bank.bank_ledger_for("3730002100302218", tmp_db) is None
    bank.set_bank_ledger("3730002100302218", "PNB A/c 2218", tmp_db)
    bank.set_bank_ledger("3730002100303271", " PNB Wings ", tmp_db)
    assert bank.bank_ledger_for("3730002100302218", tmp_db) == "PNB A/c 2218"
    assert bank.known_accounts(tmp_db) == {"3730002100302218": "PNB A/c 2218", "3730002100303271": "PNB Wings"}
    bank.delete_account("3730002100303271", tmp_db)
    assert list(bank.known_accounts(tmp_db)) == ["3730002100302218"]


def test_txn_roundtrip_and_vouchers(tmp_db):
    p = db.upsert_party(Party(name="Das Store"), tmp_db)
    t1 = bank.insert_txn(bank.BankTxn(txn_date=date(2026, 9, 19), account_no="3730002100302218", bank_ledger="PNB A/c 2218",
                                      bank_name="Punjab National Bank", branch="Nagaon", amount=140000, vch_type="Contra",
                                      counter_ledger="Cash", vch_no="DEP/2026-27/001"), tmp_db)
    t2 = bank.insert_txn(bank.BankTxn(txn_date=date(2026, 9, 25), account_no="3730002100302218", bank_ledger="PNB A/c 2218",
                                      amount=350000, vch_type="Receipt", party_id=p.id, counter_ledger=p.name,
                                      vch_no="DEP/2026-27/002", mode="cheque", inst_no="123456"), tmp_db)
    assert t1.id and t2.id and bank.vch_no_exists("dep/2026-27/001", tmp_db)
    with pytest.raises(Exception):  # duplicate voucher number
        bank.insert_txn(bank.BankTxn(txn_date=date(2026, 1, 1), account_no="1", bank_ledger="B", amount=1, vch_type="Contra",
                                     counter_ledger="Cash", vch_no="DEP/2026-27/001"), tmp_db)
    assert [t.vch_no for t in bank.list_txns(tmp_db, exported=False)] == ["DEP/2026-27/001", "DEP/2026-27/002"]
    assert bank.list_txns(tmp_db, date_from=date(2026, 9, 20)) and len(bank.list_txns(tmp_db, date_to=date(2026, 9, 20))) == 1

    s = tx.TallySettings(cash_ledger="Cash", company="Wings")
    vs = bank.vouchers_for([t1, t2], s)
    assert vs[0].vch_type == "Contra" and vs[0].entries[0].bank.txn_type == "Cash" and vs[0].entries[1].ledger == "Cash"
    assert vs[1].vch_type == "Receipt" and vs[1].entries[0].bank.txn_type == "Cheque/DD" and vs[1].entries[0].bank.inst_no == "123456"
    assert vs[1].entries[1].is_party and vs[1].entries[1].ledger == "Das Store" and vs[1].reference == "123456"
    assert all(v.balanced() for v in vs)
    bundle = tx.build_simple_bundle(vs, list(tx.FORMATS), s, stem="Bank", bank_ledgers=["PNB A/c 2218"],
                                    parties=bank.parties_for([t1, t2], tmp_db))
    assert tx.validate_xml(bundle.files["Bank.xml"]) == 2 and not bundle.warnings
    root = ET.fromstring(bundle.files["Bank.xml"])
    r = root.findall(".//VOUCHER")[1]
    assert r.get("VCHTYPE") == "Receipt" and r.findtext("REFERENCE") == "123456"
    ba = r.find("ALLLEDGERENTRIES.LIST/BANKALLOCATIONS.LIST")
    assert ba.findtext("TRANSACTIONTYPE") == "Cheque/DD" and ba.findtext("INSTRUMENTNUMBER") == "123456" and ba.findtext("INSTRUMENTDATE") == "20260925"
    m = ET.fromstring(bundle.files["Bank_masters.xml"])
    assert {l.get("NAME") for l in m.findall(".//LEDGER")} == {"PNB A/c 2218", "Das Store"}

    bank.mark_exported([t1.id], True, tmp_db)
    assert [t.vch_no for t in bank.list_txns(tmp_db, exported=False)] == ["DEP/2026-27/002"]
    bank.delete_txn(t2.id, tmp_db)
    assert bank.get_txn(t2.id, tmp_db) is None
    # deleting the party nulls the reference instead of failing
    t3 = bank.insert_txn(bank.BankTxn(txn_date=date(2026, 9, 26), account_no="1", bank_ledger="B", amount=5, vch_type="Receipt",
                                      party_id=p.id, counter_ledger=p.name, vch_no="R3"), tmp_db)
    db.delete_party(p.id, tmp_db)
    assert bank.get_txn(t3.id, tmp_db).party_id is None


def test_instrument_bank_and_transaction_types(tmp_db):
    """Tally's bank-allocation 'Bank Name' is the instrument's bank (cheque drawee / remitter), never our own
    bank; mode 'other' maps to Tally's 'Others'; the column is added to databases created before it existed."""
    s = tx.TallySettings()
    chq = bank.insert_txn(bank.BankTxn(txn_date=date(2026, 9, 20), account_no="3730002100302218", bank_ledger="PNB A/c 2218",
                                       bank_name="Punjab National Bank", branch="Nagaon", amount=140000, vch_type="Contra",
                                       counter_ledger="Cash", vch_no="C/1", mode="cheque", inst_no="123456",
                                       inst_bank="State Bank of India"), tmp_db)
    assert bank.get_txn(chq.id, tmp_db).inst_bank == "State Bank of India"
    v = bank.to_voucher(chq, s)
    a = v.entries[0].bank
    assert (a.txn_type, a.bank_name, a.branch, a.inst_no, a.inst_date) == ("Cheque/DD", "State Bank of India", "", "123456", date(2026, 9, 20))
    xml = ET.fromstring(tx.simple_vouchers_xml([v], s).encode()).find(".//BANKALLOCATIONS.LIST")
    assert xml.findtext("BANKNAME") == "State Bank of India" and xml.findtext("TRANSACTIONTYPE") == "Cheque/DD"
    # a cheque whose bank is unknown carries no BANKNAME rather than our own bank's
    v2 = bank.to_voucher(chq.model_copy(update={"inst_bank": None}), s)
    assert v2.entries[0].bank.bank_name == ""
    assert ET.fromstring(tx.simple_vouchers_xml([v2], s).encode()).find(".//BANKALLOCATIONS.LIST/BANKNAME") is None
    # cash keeps the slip's bank / branch as information; 'other' -> Tally's 'Others'
    cash = chq.model_copy(update={"mode": "cash", "inst_no": None, "inst_bank": None})
    a = bank.to_voucher(cash, s).entries[0].bank
    assert (a.txn_type, a.bank_name, a.branch, a.inst_date) == ("Cash", "Punjab National Bank", "Nagaon", None)
    assert bank.to_voucher(chq.model_copy(update={"mode": "other"}), s).entries[0].bank.txn_type == "Others"
    assert bank.to_voucher(chq.model_copy(update={"mode": "transfer"}), s).entries[0].bank.txn_type == "e-Fund Transfer"
    # migration: an old table without inst_bank gets the column on init()
    with db.connect(tmp_db) as c:
        c.execute("DROP TABLE bank_txns")
        c.execute("CREATE TABLE bank_txns (id INTEGER PRIMARY KEY AUTOINCREMENT, txn_date TEXT NOT NULL, account_no TEXT NOT NULL, "
                  "bank_ledger TEXT NOT NULL, bank_name TEXT, branch TEXT, amount REAL NOT NULL, vch_type TEXT NOT NULL, party_id INTEGER, "
                  "counter_ledger TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'cash', inst_no TEXT, vch_no TEXT NOT NULL UNIQUE, narration TEXT, "
                  "image_path TEXT, raw_text TEXT, exported INTEGER NOT NULL DEFAULT 0, exported_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        c.execute("INSERT INTO bank_txns (txn_date, account_no, bank_ledger, amount, vch_type, counter_ledger, vch_no) "
                  "VALUES ('2026-09-01', '1', 'B', 10, 'Contra', 'Cash', 'OLD/1')")
    old = bank.list_txns(tmp_db)
    assert len(old) == 1 and old[0].inst_bank is None and old[0].vch_no == "OLD/1"

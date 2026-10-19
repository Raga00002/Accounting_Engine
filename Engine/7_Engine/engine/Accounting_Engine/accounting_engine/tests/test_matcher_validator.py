from datetime import date

import pytest

from engine import matcher, validator
from engine.models import BillDraft, LineItem, Party

PARTIES = [
    Party(id=1, name="Das Store", aliases=["Das Stores"]),
    Party(id=2, name="A.G. Store"),
    Party(id=3, name="P. Roy", aliases=["Pradip Roy"]),
    Party(id=4, name="Mohendra Traders", aliases=["Mohendra"]),
    Party(id=5, name="Gautam Kotna"),
    Party(id=6, name="National Traders"),
    Party(id=7, name="HDFC Bank", group_name="Bank Accounts"),
]


@pytest.mark.parametrize(
    "raw, expected_id, kind",
    [
        ("Das Store", 1, "auto"),
        ("das stores", 1, "auto"),
        ("M/S Das Store", 1, "auto"),
        ("AG Store", 2, "auto"),
        ("A G Stores", 2, "auto"),
        ("P Roy", 3, "auto"),
        ("Pradip Roy", 3, "auto"),
        ("Mohendra", 4, "auto"),
        ("Gotam Kotna", 5, "suggest"),  # spelling variant -> needs confirmation
    ],
)
def test_party_matching(raw, expected_id, kind):
    cands = matcher.match_party(raw, PARTIES)
    assert cands, raw
    assert cands[0].party.id == expected_id, [(c.party.name, c.score) for c in cands]
    assert matcher.classify(cands) == kind, [(c.party.name, c.score) for c in cands]


def test_unknown_party_is_new():
    cands = matcher.match_party("Bharat Rice Mill", PARTIES)
    assert matcher.classify(cands) == "new", [(c.party.name, c.score) for c in cands]
    assert matcher.match_party("", PARTIES) == []
    assert matcher.match_party(None, PARTIES) == []


def test_item_matching():
    names = ["Rice (BPT)", "Rice (Sri)", "Mustard Oil", "Mung Dal", "Masur Dal"]
    assert matcher.match_item("rice bpt", names) == "Rice (BPT)"
    assert matcher.match_item("Mustard oil", names) == "Mustard Oil"
    assert matcher.match_item("Kosal", names) is None
    # different varieties must NOT be merged
    assert matcher.match_item("Rice (R.R 10)", ["Rice (R.R)"]) is None
    assert matcher.match_item("Rice (R.R Usna)", ["Rice (R.R)"]) is None
    assert matcher.match_item("Sona Rice", ["Rice (Sona)"]) is None
    assert matcher.match_item("rice r.r.", ["Rice (R.R)"]) == "Rice (R.R)"


def _das_store_draft() -> BillDraft:
    return BillDraft(
        party_name_raw="Das Store",
        bill_date=date(2026, 9, 26),
        bill_no="13",
        items=[
            LineItem(item="Rice (BPT)", qty=5, unit="Bag", rate=1540, amount=7700),
            LineItem(item="Rice (Sri)", qty=2, unit="Bag", weight_qtl=0.52, rate=5550, amount=2886),
            LineItem(item="Mung Dal", qty=2, unit="Bag", weight_qtl=0.60, rate=9600, amount=5760),
            LineItem(item="Masur (Etc)", qty=3, unit="Bag", weight_qtl=0.90, rate=7500, amount=6750),
            LineItem(item="Matar", qty=4, unit="Bag", weight_qtl=1.20, rate=5700, amount=6840),
            LineItem(item="Kosal", qty=2, unit="Bag", weight_qtl=0.60, rate=9000, amount=5400),
            LineItem(item="Rice (R.R)", qty=5, unit="Bag", weight_qtl=1.30, rate=4130, amount=5369),
            LineItem(item="Rice (Jol)", qty=1, unit="Bag", weight_qtl=0.26, rate=17000, amount=4420),
        ],
        total_written=45125,
    )


def test_validator_clean_bill():
    d = _das_store_draft()
    rep = validator.validate(d, party_resolved=True)
    assert rep.ok and rep.warnings == [], rep
    assert rep.computed_total == 45125
    assert validator.final_amount(d) == 45125


def test_validator_fills_missing_amounts_and_flags_mismatch():
    # A.G. Store: no amounts, no total written
    d = BillDraft(
        party_name_raw="A.G. Store",
        items=[
            LineItem(item="Rice (R.R)", qty=50, unit="Pkt", weight_qtl=13.10, rate=4090),
            LineItem(item="Rice (10)", qty=30, unit="Bag", weight_qtl=3.09, rate=4290),
        ],
    )
    validator.fill_missing_amounts(d)
    assert d.items[0].amount == pytest.approx(53579)
    assert d.items[1].amount == pytest.approx(13256.1)
    rep = validator.validate(d, party_resolved=False)
    assert "Party" in rep.missing[0] and any("date" in m for m in rep.missing) and any("number" in m for m in rep.missing)
    assert rep.computed_total == pytest.approx(66835.1)

    # Mohendra: struck-out line excluded; written total 27124 matches active lines
    m = BillDraft(
        party_name_raw="Mohendra", bill_date=date(2026, 9, 26), bill_no="24",
        items=[
            LineItem(item="Rice (BPT)", qty=4, unit="Bag", rate=1535, amount=6140),
            LineItem(item="Sona Rice", qty=4, unit="Bag", rate=1850, amount=7400),
            LineItem(item="Rice (R.R)", qty=4, unit="Bag", weight_qtl=1.04, rate=4100, amount=4264),
            LineItem(item="Rice (HMT)", qty=2, unit="Bag", weight_qtl=0.52, rate=6200, amount=3224),
            LineItem(item="Rice (Sri)", qty=2, unit="Bag", weight_qtl=0.52, rate=5550, amount=2886),
            LineItem(item="Raha", qty=1, unit="Bag", weight_qtl=0.30, rate=10700, amount=3210),
            LineItem(item="Rice (Gobi)", qty=2, unit="Bag", weight_qtl=0.60, rate=4900, amount=2940, struck_out=True),
        ],
        total_written=27124,
    )
    rep = validator.validate(m, party_resolved=True)
    assert rep.ok and rep.computed_total == 27124 and rep.warnings == [], rep

    # Bad arithmetic gets a warning, not a block
    m.items[0].amount = 6000
    rep = validator.validate(m, party_resolved=True)
    assert rep.ok and len(rep.warnings) == 2  # line mismatch + total mismatch


def test_validator_blocks_undeterminable_line():
    d = BillDraft(party_name_raw="X", bill_date=date(2026, 1, 1), bill_no="1",
                  items=[LineItem(item="Oil", qty=40)])
    rep = validator.validate(d, party_resolved=True)
    assert not rep.ok and "Line 1" in rep.missing[0]


@pytest.mark.parametrize(
    "raw, expected_id",
    [
        ("Das Traders", 1),            # same identity word, different business type
        ("National Enterprises", 6),   # ditto
        ("Das Store Bakery", 1),       # extra identifying word (token_set_ratio would say 100)
        ("P Roy & Sons", 3),
        ("Mohendra Store", 4),         # alias 'Mohendra' + a type word
    ],
)
def test_type_word_conflicts_never_auto_match(raw, expected_id):
    cands = matcher.match_party(raw, PARTIES)
    assert cands and cands[0].party.id == expected_id, [(c.party.name, c.score) for c in cands]
    assert matcher.classify(cands) == "suggest", [(c.party.name, c.score) for c in cands]


def test_safe_variants_still_auto_match():
    for raw in ["The Das Store", "Das Store & Co", "M/S Mohendra Traders", "Mohendra Trading Co", "DAS STORE",
                "P.Roy", "M/s. Das Store", "Das Stores", "A.G Stores"]:
        cands = matcher.match_party(raw, PARTIES)
        assert matcher.classify(cands) == "auto", (raw, [(c.party.name, c.score) for c in cands])


UNITS = [
    Party(id=1, name="200 Bn BSF", group_name="GEM"),
    Party(id=2, name="21 Bn BSF", group_name="GEM"),
    Party(id=3, name="12 Wing Air Force", group_name="Govt. Debtors"),
    Party(id=4, name="14 Wing Air Force", group_name="Govt. Debtors"),
    Party(id=5, name="2 Wing Air Force", group_name="Govt. Debtors"),
    Party(id=6, name="Das Store", aliases=["Das Stores"]),
]


@pytest.mark.parametrize(
    "raw, top_id",
    [
        ("20 Bn BSF", 1),          # 95% on characters, but 20 != 200
        ("12 Wing Airforce", 3),   # spelling slip AND (for the runner-ups) different numbers
        ("2 Wing Air Force ", 5),  # exact -> auto; '12'/'20' runner-ups are capped, not ambiguous
        ("12 wing air force", 3),
        ("200 Bn B.S.F.", 1),
    ],
)
def test_numbered_units_never_auto_match_a_different_number(raw, top_id):
    cands = matcher.match_party(raw, UNITS)
    assert cands and cands[0].party.id == top_id, [(c.party.name, c.score) for c in cands]
    exact = matcher.normalize(raw) == matcher.normalize(cands[0].party.name) or matcher.core(raw) == matcher.core(cands[0].party.name)
    assert matcher.classify(cands) == ("auto" if exact else "suggest"), (raw, [(c.party.name, c.score) for c in cands])
    # different numbers are still offered as suggestions, below the auto threshold
    for c in cands:
        if matcher._numeric_tokens(matcher.normalize(raw).split()) != matcher._numeric_tokens(matcher.normalize(c.party.name).split()):
            assert 65 <= c.score < 92, (raw, c.party.name, c.score)


def test_type_word_on_one_side_only_is_not_a_conflict():
    # Books say "Mohendra" (no type word); the bill says "Mohendra Traders" -> same firm.
    books = [Party(id=1, name="Mohendra"), Party(id=2, name="Das Store"), Party(id=3, name="Das Traders")]
    cands = matcher.match_party("Mohendra Traders", books)
    assert cands[0].party.id == 1 and matcher.classify(cands) == "auto"
    # ...but two different type words still conflict, and a bare identity word shared by two firms is ambiguous.
    assert matcher.classify(matcher.match_party("Das Traders", books[:2])) == "suggest"
    assert matcher.classify(matcher.match_party("Das", books)) == "suggest"

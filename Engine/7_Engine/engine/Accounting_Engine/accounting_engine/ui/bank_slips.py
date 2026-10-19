"""Bank Slips page: deposit counterfoils -> Contra / Receipt vouchers -> Tally XML / Excel / CSV."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from engine import bank, config, db, tally_export, tenant
from engine.bank import BankSlipDraft, BankTxn
from engine.extractor import ExtractionError
from ui.common import download_buttons, money, remember, remembered, toast_ok

VCH_PREFIX = "DEP"
MODES = ["cash", "cheque", "transfer", "other"]


@dataclass
class SlipState:
    txn: Optional[BankTxn] = None
    missing: list[str] = field(default_factory=list)
    new_ledger_for: Optional[tuple[str, str]] = None  # (account_no, ledger) to remember on save

    @property
    def can_save(self) -> bool:
        return self.txn is not None and not self.missing


def _init() -> None:
    ss = st.session_state
    ss.setdefault("bk_cards", [])
    ss.setdefault("bk_version", 0)
    ss.setdefault("bk_queue", [])
    ss.setdefault("bk_seen", {})
    ss.setdefault("bk_saved_ids", [])
    ss.setdefault("bk_show_input", True)


def _new_card(draft: BankSlipDraft, image: tuple[bytes, str], error: str | None = None) -> dict:
    ss = st.session_state
    ss.bk_version += 1
    return {"draft": draft, "image": image, "k": f"s{ss.bk_version}", "saved": None, "error": error, "state": None}


def _read_all(files: list[tuple[str, bytes]]) -> list[dict]:
    accounts = bank.known_accounts()
    cards = []
    prog = st.progress(0.0, text="Reading slips…")
    for i, (name, data) in enumerate(files, start=1):
        prog.progress((i - 1) / max(1, len(files)), text=f"Reading {i}/{len(files)}: {name}")
        try:
            cards.append(_new_card(bank.extract_slip(data, name, accounts, mock_key=Path(name).stem), (data, name)))
        except ExtractionError as e:
            cards.append(_new_card(BankSlipDraft(), (data, name), error=str(e)))
        except Exception as e:  # noqa: BLE001
            cards.append(_new_card(BankSlipDraft(), (data, name), error=f"Unexpected error: {e}"))
    prog.progress(1.0, text=f"Read {len(files)} slip(s).")
    return cards


# --------------------------------------------------------------------------- #
def _input_stage() -> None:
    ss = st.session_state
    if config.EXTRACTOR_MODE == "mock":
        st.info("Reader is in **mock** mode – sample slips return canned results.", icon="🧪")
    st.caption("Upload photos of pay-in slips / deposit counterfoils. Each becomes a **Contra** (own cash deposited) or a "
               "**Receipt** (a customer's payment) voucher – you confirm per slip.")
    files = st.file_uploader("Slip photo(s)", type=["jpg", "jpeg", "png", "webp", "pdf"], accept_multiple_files=True,
                             key=f"bk_upl_{ss.bk_version}")
    current = {(f.name, f.size) for f in files or []}
    for f in files or []:
        fp = (f.name, f.size)
        if fp not in ss.bk_seen:
            ss.bk_seen[fp] = ss.bk_version
            ss.bk_queue.append((f.name, f.getvalue()))
    for fp, ver in list(ss.bk_seen.items()):
        if ver == ss.bk_version and fp not in current:
            ss.bk_queue = [(n, d) for n, d in ss.bk_queue if (n, len(d)) != fp]
            del ss.bk_seen[fp]
    if ss.bk_queue:
        st.markdown(f"**{len(ss.bk_queue)} slip(s) ready:** " + " · ".join(n for n, _ in ss.bk_queue))
        thumbs = st.columns(min(6, len(ss.bk_queue)))
        for i, (n, d) in enumerate(ss.bk_queue[:12]):
            with thumbs[i % len(thumbs)]:
                try:
                    st.image(d, caption=n, width=140) if not n.lower().endswith(".pdf") else st.caption(f"📄 {n}")
                except Exception:  # noqa: BLE001
                    st.caption(n)
        b1, b2 = st.columns([2, 1])
        if b1.button(f"🔍 Read all {len(ss.bk_queue)} slip(s)", type="primary", key=f"bk_read_{ss.bk_version}"):
            cards = _read_all(list(ss.bk_queue))
            ss.bk_queue, ss.bk_seen = [], {}
            ss.bk_cards.extend(cards)
            ss.bk_show_input = False
            st.rerun()
        if b2.button("🗑️ Clear list", key=f"bk_clear_{ss.bk_version}"):
            ss.bk_queue, ss.bk_seen = [], {}
            ss.bk_version += 1
            st.rerun()


# --------------------------------------------------------------------------- #
def _auto_no_cb(key: str) -> None:
    st.session_state[key] = bank.next_vch_no(VCH_PREFIX)


def _render_card(card: dict, parties_by_id: dict[int, str], ts: tally_export.TallySettings) -> SlipState:
    d: BankSlipDraft = card["draft"]
    k = card["k"]
    st_ = SlipState()
    left, right = st.columns([3, 2])
    with right:
        try:
            st.image(card["image"][0], caption=card["image"][1], width="stretch")
        except Exception:  # noqa: BLE001
            st.caption(card["image"][1])
        if d.amount_words:
            st.caption(f"In words: *{d.amount_words}*")
        for n in d.notes:
            st.warning(n, icon="⚠️")
    # Widget defaults come from the card's remembered edits first (see ui.common.remembered): the
    # widget state itself is dropped by Streamlit whenever the card is not rendered (other page).
    f = lambda name: f"bk_{name}_{k}"  # noqa: E731
    with left:
        c1, c2, c3 = st.columns(3)
        txn_date = remember(card, f("date"), c1.date_input("Deposit date *", value=remembered(card, f("date"), d.slip_date or date.today()),
                                                           key=f("date"), format="DD/MM/YYYY"))
        if d.slip_date is None:
            c1.caption("⚠️ No date read – defaulted to today.")
        amount = remember(card, f("amt"), c2.number_input("Amount ₹ *", min_value=0.0, step=100.0,
                                                          value=float(remembered(card, f("amt"), d.amount or 0.0)), key=f("amt")))
        w = bank.words_to_number(d.amount_words)
        amount_confirmed = True
        if w is not None and amount and abs(w - amount) > 0.5:
            # Figures and words disagree: never book either silently – the user confirms the figure
            # (or corrects the amount so that it matches the words).
            c2.caption(f"⚠️ Words say {money(w)}")
            okkey = f("amtok") + f"_{amount:.2f}"  # per figure: a confirmation never carries over to a different amount
            amount_confirmed = remember(card, okkey, c2.checkbox(f"Confirm {money(amount)} – the words are wrong", key=okkey,
                                                                  value=bool(remembered(card, okkey, False))))
        m0 = remembered(card, f("mode"), d.mode)
        mode = remember(card, f("mode"), c3.selectbox("Mode", MODES, index=MODES.index(m0) if m0 in MODES else 0, key=f("mode")))
        inst_no, inst_bank = "", ""
        if mode != "cash":
            inst_no = remember(card, f("inst"), c3.text_input("Cheque / ref no.", value=remembered(card, f("inst"), d.cheque_no or ""), key=f("inst")))
            inst_bank = remember(card, f("ibank"), c3.text_input("Cheque's bank (drawee / remitter)", key=f("ibank"),
                                                                 value=remembered(card, f("ibank"), d.cheque_bank or ""),
                                                                 help="Goes to Tally's bank allocation 'Bank Name' – the bank the cheque is drawn on, not ours."))

        c4, c5 = st.columns([2, 2])
        acct = remember(card, f("acct"), c4.text_input("Account no. *", value=remembered(card, f("acct"), d.account_no or ""), key=f("acct")))
        acct = "".join(ch for ch in acct if ch.isdigit()) or acct.strip()
        known = bank.known_accounts()
        ledger_known = known.get(acct)
        lkey = f("led")
        # Pre-filled with the ledger learned for this account, else a guess. When the account becomes
        # known while the field still holds that untouched pre-fill (e.g. another card of this batch
        # just named it, or the account number was corrected), follow the learned name instead of
        # silently "renaming" the mapping back to the guess on save.
        auto = ledger_known or (f"PNB A/c {acct[-4:]}" if acct and (d.bank_name or "").lower().startswith("punjab") else "")
        ed = card.setdefault("edits", {})
        if lkey not in st.session_state:  # first render, or state dropped while another page was shown
            st.session_state[lkey] = ed.get(lkey, auto)
            ed.setdefault(lkey + "_auto", auto)
        elif ledger_known and st.session_state[lkey] == ed.get(lkey + "_auto") != ledger_known:
            st.session_state[lkey] = ledger_known
            ed[lkey + "_auto"] = ledger_known
        if ledger_known:
            ledger = c5.text_input("Bank ledger in Tally", key=lkey, help="Remembered for this account number; edit here to rename it.")
            if ledger.strip() and ledger.strip() != ledger_known:
                st_.new_ledger_for = (acct, ledger.strip())
        else:
            ledger = c5.text_input("Bank ledger in Tally * (new account – name it once)", key=lkey,
                                   help=f"Account holder on the slip: {d.account_name or '—'}")
            if acct and ledger.strip():
                st_.new_ledger_for = (acct, ledger.strip())
        remember(card, lkey, ledger)
        c4.caption(f"{d.bank_name or 'Bank?'}{' · ' + d.branch if d.branch else ''} · holder: {d.account_name or '—'}")

        # Default = Receipt (a customer paid cash into our account) – confirmed with the user; Contra for own deposits.
        # The option labels must be static: Streamlit's radio loses its selection on the browser side
        # when the labels change (e.g. with the ledger name typed above) and silently falls back to
        # the default on the next click – a Contra would turn into a Receipt.
        vch = remember(card, f("vch"), st.radio("Book as", ["Receipt", "Contra"], horizontal=True, key=f("vch"),
                                                index=1 if remembered(card, f("vch"), "Receipt") == "Contra" else 0,
                                                format_func=lambda v: "Contra – own cash deposited (Bank Dr / Cash Cr)" if v == "Contra"
                                                else "Receipt – a party's payment (Bank Dr / Party Cr)"))
        party_id = None
        counter = ts.cash_ledger
        if vch == "Receipt":
            ids = list(parties_by_id)
            prev = remembered(card, f("party"), None)
            pid = remember(card, f("party"), st.selectbox("Party *", ids, index=ids.index(prev) if prev in parties_by_id else None,
                                                          placeholder="Type to search…", key=f("party"),
                                                          format_func=lambda i: parties_by_id.get(i, str(i))))
            if pid in parties_by_id:
                party_id = pid
                counter = parties_by_id[pid].split(" — ")[0]
        st.caption(f"Entry: **{ledger.strip() or 'Bank'}** Dr {money(amount)} / **{counter if vch != 'Receipt' or party_id else 'Party?'}** Cr")
        c6, c7 = st.columns([1, 2])
        vkey = f("no")
        if vkey not in st.session_state:
            st.session_state[vkey] = remembered(card, vkey, "")
        vch_no = remember(card, vkey, c6.text_input("Voucher no. *", key=vkey))
        c6.button("Auto-number", key=f("auto"), on_click=_auto_no_cb, args=(vkey,))
        narr0 = f"{'Cash' if mode == 'cash' else mode.title()} deposit {d.bank_name or ''} {d.branch or ''}".strip()
        narration = remember(card, f("narr"), c7.text_input("Narration", value=remembered(card, f("narr"), narr0), key=f("narr")))

    if amount <= 0:
        st_.missing.append("Amount must be greater than zero.")
    elif not amount_confirmed:
        st_.missing.append(f"Amount in words ({money(w)}) differs from the figures ({money(amount)}) – correct the amount or confirm it.")
    if not acct:
        st_.missing.append("Account number is missing.")
    if not ledger.strip():
        st_.missing.append("Bank ledger name is required.")
    if vch == "Receipt" and party_id is None:
        st_.missing.append("Pick the party for a Receipt.")
    if not vch_no.strip():
        st_.missing.append("Voucher number is missing (use Auto-number).")
    elif bank.vch_no_exists(vch_no):
        st_.missing.append(f"Voucher number “{vch_no}” already exists.")
    for m in st_.missing:
        st.error(m, icon="⛔")
    if not st_.missing:
        st_.txn = BankTxn(txn_date=txn_date, account_no=acct, bank_ledger=ledger.strip(), bank_name=d.bank_name,
                          branch=d.branch, amount=round(float(amount), 2), vch_type=vch, party_id=party_id,
                          counter_ledger=counter, mode=mode, inst_no=inst_no.strip() or None, inst_bank=inst_bank.strip() or None,
                          vch_no=vch_no.strip(),
                          narration=narration.strip() or None, raw_text=d.raw_text)
    return st_


def _save(card: dict, s: SlipState) -> bool:
    try:
        t = s.txn
        if bank.vch_no_exists(t.vch_no):  # e.g. two cards of this batch were given the same number
            card["error_save"] = f"Voucher number “{t.vch_no}” already exists – change it and save again."
            return False
        t.image_path = bank.save_slip_image(card["image"][0], card["image"][1], t.vch_no)
        saved = bank.insert_txn(t)
        if s.new_ledger_for:
            bank.set_bank_ledger(*s.new_ledger_for)
    except Exception as e:  # noqa: BLE001
        card["error_save"] = f"Could not save: {e}"
        return False
    card["saved"] = saved
    card["image"] = (b"", card["image"][1])  # photo is on disk now – free the bytes, keep the name
    card.pop("error_save", None)
    st.session_state.bk_saved_ids.append(saved.id)
    return True


def _status(card: dict) -> str:
    if card.get("saved"):
        return "✅ saved"
    if card.get("error"):
        return "❌ could not read"
    s = card.get("state")
    return "…" if s is None else ("🟢 ready" if s.can_save else "🟠 needs input")


def _summary(slot, cards: list[dict]) -> None:
    rows = []
    for i, c in enumerate(cards, start=1):
        d: BankSlipDraft = c["draft"]
        s: SlipState | None = c.get("state")
        t = c.get("saved") or (s.txn if s else None)
        rows.append({"#": i, "File": c["image"][1], "Date": t.txn_date if t else d.slip_date,
                     "Account": (t.account_no if t else d.account_no) or "", "Holder": d.account_name or "",
                     "Amount": t.amount if t else d.amount, "In words": d.amount_words or "",
                     "Voucher": (f"{t.vch_type} · {t.vch_no}" if t else ""), "Ledger": t.bank_ledger if t else "",
                     "Status": _status(c), "Issues": "; ".join(s.missing[:2]) if s and not c.get("saved") else ""})
    with slot:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                                    "Amount": st.column_config.NumberColumn("Amount ₹", format="%.2f")})


def _review_stage() -> None:
    ss = st.session_state
    cards: list[dict] = ss.bk_cards
    ts = tally_export.TallySettings.load()
    parties_by_id = {p.id: f"{p.name} — {p.group_name}" for p in db.list_parties()}
    n_saved = sum(1 for c in cards if c.get("saved"))
    a, b, c = st.columns([3, 1, 1])
    a.markdown(f"**{len(cards)} slip(s) read · {n_saved} saved**")
    if b.button("▲ Hide the input panel" if ss.bk_show_input else "➕ Read more slips", key="bk_more"):
        ss.bk_show_input = not ss.bk_show_input
        st.rerun()
    if c.button("✖ Discard unsaved & start over", key="bk_discard"):
        ss.bk_cards, ss.bk_saved_ids, ss.bk_show_input = [], [], True
        ss.bk_version += 1
        st.rerun()
    table_slot, save_slot, export_slot = st.container(), st.container(), st.container()
    labels = [f"{i}. {card['image'][1][:28]}" for i, card in enumerate(cards, start=1)]
    st.caption("Open a tab, check the slip, save it — then move to the next tab. "
               "**Save all ready** above saves every tab that has no open issue.")
    tabs = st.tabs(labels)  # static labels: a changing label would re-mount the strip and jump to tab 1
    for tab, (i, card) in zip(tabs, enumerate(cards, start=1)):
        d: BankSlipDraft = card["draft"]
        title = f"{i}. {card['image'][1]} — {money(d.amount)} · a/c …{(d.account_no or '')[-4:]}"
        with tab:
            if card.get("saved"):
                t: BankTxn = card["saved"]
                st.success(f"✅ {title} · {t.vch_type} {t.vch_no} — saved · {t.bank_ledger} Dr {money(t.amount)} / {t.counter_ledger} Cr")
                continue
            if card.get("error"):
                st.error(f"❌ {title} – could not read: {card['error']}")
                if st.button("🔁 Retry", key=f"bk_retry_{card['k']}"):
                    cards[i - 1] = _read_all([(card["image"][1], card["image"][0])])[0]
                    st.rerun()
                if st.button("📝 Enter manually", key=f"bk_manual_{card['k']}"):
                    cards[i - 1] = _new_card(BankSlipDraft(), card["image"])
                    st.rerun()
                continue
            st.markdown(f"{_status(card)} — **{title}**")
            state = _render_card(card, parties_by_id, ts)
            card["state"] = state
            if card.get("error_save"):
                st.error(card["error_save"])
            if st.button("💾 Save this slip", type="primary", disabled=not state.can_save, key=f"bk_save_{card['k']}"):
                if _save(card, state):
                    toast_ok("Saved.")
                    st.rerun()
    _summary(table_slot, cards)
    ready = [c for c in cards if not c.get("saved") and not c.get("error") and c.get("state") and c["state"].can_save]
    with save_slot:
        if st.button(f"💾 Save all ready ({len(ready)})", type="primary", disabled=not ready, key="bk_save_all"):
            n = sum(1 for c in ready if _save(c, c["state"]))
            toast_ok(f"Saved {n} slip(s).")
            st.rerun()
    if ss.bk_saved_ids:
        with export_slot:
            _export_block([t for t in (bank.get_txn(i) for i in ss.bk_saved_ids) if t], key="bk_qe",
                          title=f"Send {len(ss.bk_saved_ids)} saved slip(s) to Tally")


def _export_block(txns: list[BankTxn], key: str, title: str, mark: bool = False) -> None:
    if not txns:
        return
    ts = tally_export.TallySettings.load()
    cache = st.session_state.setdefault(f"{key}_cache", {})
    sig = (tuple(t.id for t in txns), tuple(t.vch_no for t in txns), ts)
    if cache.get("sig") != sig:  # build (and write to the exports folder) once, not on every rerun
        try:
            vs = bank.vouchers_for(txns, ts)
            bundle = tally_export.build_simple_bundle(vs, list(tally_export.FORMATS), ts, stem=f"Bank_Vouchers_{datetime.now():%Y%m%d_%H%M%S}",
                                                     bank_ledgers=[t.bank_ledger for t in txns], parties=bank.parties_for(txns))
        except Exception as e:  # noqa: BLE001
            st.warning(f"Could not prepare the Tally files: {e}")
            return
        if mark:  # the "build & mark" export is kept in the exports folder; the quick block after saving is download-only
            for name, data in bundle.files.items():
                try:
                    tenant.current().export_dir.mkdir(parents=True, exist_ok=True)  # the user may have deleted the folder
                    (tenant.current().export_dir / name).write_bytes(data)
                except OSError:
                    pass
        cache.clear()
        cache.update({"sig": sig, "files": bundle.files, "warnings": bundle.warnings})
    with st.container(border=True):
        st.markdown(f"**{title}** – Contra/Receipt voucher XML (Import → Transactions), Excel in Tally's Accounting Voucher "
                    "template + row layout, CSV, masters XML for bank / party ledgers.")
        for w in cache["warnings"]:
            st.warning(w)
        download_buttons(cache["files"], key_prefix=f"{key}_{len(txns)}_{txns[-1].id}", zip_name="bank_vouchers_tally.zip")
        if mark and st.button("Mark these as exported", key=f"{key}_mark"):
            bank.mark_exported([t.id for t in txns], True)
            st.session_state.pop("bk_build_ids", None)  # done with this build – don't keep offering to mark it
            toast_ok("Marked as exported.")
            st.rerun()


def _saved_section() -> None:
    st.subheader("Saved slips & export")
    c1, c2, c3 = st.columns([1, 1, 1])
    today = date.today()
    fy = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    dfrom = c1.date_input("From", value=fy, key="bk_from", format="DD/MM/YYYY")
    dto = c2.date_input("To", value=today, key="bk_to", format="DD/MM/YYYY")
    only_new = c3.toggle("Only not-yet-exported", value=True, key="bk_only_new")
    txns = bank.list_txns(date_from=dfrom, date_to=dto, exported=False if only_new else None)
    if not txns:
        st.caption("No saved slips match.")
        return
    df = pd.DataFrame([{"ID": t.id, "Date": t.txn_date, "Voucher": t.vch_type, "No": t.vch_no, "Bank ledger": t.bank_ledger,
                        "Counter ledger": t.counter_ledger, "Amount": t.amount, "Mode": t.mode, "Exported": "✓" if t.exported else ""}
                       for t in txns])
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                                "Amount": st.column_config.NumberColumn("Amount ₹", format="%.2f")})
    if st.button(f"📥 Build Tally files for these {len(txns)} slip(s)", key="bk_build"):
        st.session_state["bk_build_ids"] = [t.id for t in txns]
    ids = st.session_state.get("bk_build_ids")
    if ids:
        sel = [t for t in (bank.get_txn(i) for i in ids) if t]
        _export_block(sel, key="bk_bx", title=f"{len(sel)} slip(s)", mark=True)
    by_id = {t.id: f"{t.txn_date:%d/%m/%Y} · {t.vch_type} {t.vch_no} · {money(t.amount)}" for t in txns}
    tid = st.selectbox("Delete a slip", [None, *by_id], format_func=lambda i: "—" if i is None else by_id.get(i, str(i)), key="bk_del_pick")
    if tid and st.checkbox("Yes, delete it", key=f"bk_del_c_{tid}") and st.button("🗑️ Delete", key=f"bk_del_{tid}"):
        bank.delete_txn(tid)
        toast_ok("Deleted.")
        st.rerun()


def render() -> None:
    _init()
    st.title("🏦 Bank Slips")
    ss = st.session_state
    if not ss.bk_cards:
        _input_stage()
    else:
        # cards stay rendered while more slips are added – Streamlit forgets the state of widgets
        # that are not rendered, which would wipe the edits made on the cards
        if ss.bk_show_input:
            with st.container(border=True):
                _input_stage()
        _review_stage()
    st.divider()
    _saved_section()

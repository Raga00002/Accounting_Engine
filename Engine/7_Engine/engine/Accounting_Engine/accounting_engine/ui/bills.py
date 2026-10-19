"""Bills & Export page: browse saved bills, build the Tally Excel, delete mistakes."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from engine import config, db, exporter, tally_export, tenant
from ui.common import download_buttons, money, toast_ok


def _filters():
    c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
    today = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    dfrom = c1.date_input("From", value=fy_start, key="bx_from", format="DD/MM/YYYY")
    dto = c2.date_input("To", value=today, key="bx_to", format="DD/MM/YYYY")
    parties = db.list_parties()
    by_id = {p.id: p for p in parties}  # dict lookup: 8,000 ledgers x 8,000 options otherwise
    pid = c3.selectbox("Party", [None, *by_id], key="bx_party",
                       format_func=lambda i: "All parties" if i is None else by_id[i].name if i in by_id else str(i))
    if pid is not None and pid not in by_id:
        pid = None  # stale selection (party deleted / database restored) -> all parties
    only_new = c4.toggle("Only not-yet-exported", value=True, key="bx_only_new")
    if dfrom > dto:
        st.error("'From' date is after 'To' date.")
    return dfrom, dto, pid, only_new, by_id


LEDGER_XLSX = "ledger_xlsx"
FORMAT_LABELS = {
    LEDGER_XLSX: "Ledger master Excel (Tally 'Ledgers in Default Template')",
    **tally_export.FORMATS,
}


def _last_export_section() -> None:
    last = st.session_state.get("bx_last_export")
    if not last:
        return
    files, n, folder, warnings = last
    st.markdown(f"**{n} bill(s) exported** · files also saved in `{folder}`")
    for w in warnings:
        st.warning(w)
    download_buttons(files, key_prefix="bx_dl", zip_name="tally_export.zip")
    st.caption("Tally Prime: **Import → Transactions** for the voucher XML / Excel · **Import → Masters** for the "
               "Ledger Excel and the Masters XML (import masters first if the ledgers / stock items are new). "
               "CSV is for spreadsheets and other software – Tally itself wants the .xlsx.")


def render() -> None:
    st.title("📚 Bills & Export")
    dfrom, dto, pid, only_new, parties = _filters()
    bills = db.list_bills(party_id=pid, date_from=dfrom, date_to=dto, exported=False if only_new else None, with_items=True)

    if not bills:
        st.info("No bills match these filters.")
        _last_export_section()  # the file just built must stay downloadable after its bills were marked
        return

    rows = []
    for b in bills:
        p = parties.get(b.party_id)
        rows.append({
            "Export": True, "ID": b.id, "Date": b.bill_date, "Bill No": b.bill_no,
            "Party": p.name if p else f"#{b.party_id}", "Amount": b.amount, "Dr/Cr": b.drcr, "Type": b.txn_type,
            "Items": len(b.items), "Source": b.source, "Exported": "✓" if b.exported else "",
        })
    df = pd.DataFrame(rows)
    # Key includes the exact set of bills shown: the editor's tick/untick state is otherwise
    # applied by row position and would carry over to a different list with the same length.
    table_key = f"bx_table_{hash(tuple(b.id for b in bills))}"
    edited = st.data_editor(
        df, hide_index=True, width="stretch", key=table_key,
        disabled=[c for c in df.columns if c != "Export"],
        column_config={
            "Export": st.column_config.CheckboxColumn("Export"),
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
            "Amount": st.column_config.NumberColumn("Amount ₹", format="%.2f"),
        },
    )
    selected_ids = [int(i) for i in edited.loc[edited["Export"] == True, "ID"].tolist()]  # noqa: E712
    total = sum(b.amount for b in bills if b.id in selected_ids)
    st.caption(f"{len(selected_ids)} of {len(bills)} bill(s) selected · total {money(total)}")

    st.subheader("Export to Tally")
    formats = st.multiselect(
        "Files to build", list(FORMAT_LABELS), default=list(FORMAT_LABELS), key="bx_formats",
        format_func=lambda k: FORMAT_LABELS[k],
        help="Voucher XML is Tally's native import; Excel needs a one-time column mapping in Tally; "
             "CSV is for other tools. Masters XML creates missing ledgers / units / stock items.",
    )
    o1, o2, o3, o4 = st.columns(4)
    inc_ob = o1.checkbox("Ledger Excel: opening balances", value=True, key="bx_ob",
                         help="Untick if these ledgers already exist in Tally with balances.")
    inc_addr = o2.checkbox("Ledger Excel: address / state / pincode", value=True, key="bx_addr")
    inc_items = o3.checkbox("Ledger Excel: add 'Items' sheet", value=True, key="bx_items")
    mark = o4.checkbox("Mark as exported when built", value=True, key="bx_mark",
                       help="The files are also saved in the exports folder, so it is safe to mark at build time.")
    ts = tally_export.TallySettings.load()
    st.caption(f"Voucher settings: {ts.sales_vch_type} → *{ts.sales_ledger}*, {ts.purchase_vch_type} → *{ts.purchase_ledger}*, "
               f"{'with stock items' if ts.inventory else 'accounting only'}"
               + (f", company *{ts.company}*" if ts.company else "") + " — change under ⚙️ Settings.")

    if st.button("📥 Build files", type="primary", disabled=not (selected_ids and formats), key="bx_build"):
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        files: dict[str, bytes] = {}
        warnings: list[str] = []
        to_mark = list(selected_ids)
        try:
            if LEDGER_XLSX in formats:
                fname = f"Tally_Ledgers_{stamp}.xlsx"
                data, _ids = exporter.export_bills(
                    bill_ids=selected_ids, include_opening_balance=inc_ob, include_address=inc_addr,
                    include_items_sheet=inc_items, mark_exported=False, out_path=tenant.current().export_dir / fname,
                )
                files[fname] = data
            vformats = [f for f in formats if f != LEDGER_XLSX]
            if vformats:
                wanted = set(selected_ids)
                chosen = [b for b in bills if b.id in wanted]
                bundle = tally_export.build_bundle(chosen, vformats, s=ts, stem=f"Tally_Vouchers_{stamp}")
                warnings.extend(bundle.warnings)
                tenant.current().export_dir.mkdir(parents=True, exist_ok=True)  # the user may have deleted the folder
                for name, data in bundle.files.items():
                    (tenant.current().export_dir / name).write_bytes(data)
                files.update(bundle.files)
                to_mark = list(bundle.bill_ids)  # a bill skipped by the bundle (party gone) is not "exported"
            if mark:
                db.mark_exported(to_mark, True)
        except Exception as e:  # noqa: BLE001 - e.g. exports folder not writable
            st.error(f"Could not build the export: {e}")
        else:
            st.session_state["bx_last_export"] = (files, len(to_mark), str(tenant.current().export_dir), warnings)
            toast_ok(f"Built {len(files)} file(s) for {len(to_mark)} bill(s).")
            if mark:
                st.rerun()  # refresh the list (marked bills drop out of 'not-yet-exported')
    _last_export_section()

    st.subheader("Bill details / delete")
    bills_by_id = {b.id: b for b in bills}
    bill_labels = {b.id: f"{b.bill_date:%d/%m/%Y} · {b.bill_no} · {parties[b.party_id].name if b.party_id in parties else ''} · {money(b.amount)}"
                   for b in bills}
    bid = st.selectbox("Bill", list(bills_by_id), key="bx_detail", format_func=lambda i: bill_labels.get(i, str(i)))
    if bid not in bills_by_id:
        bid = bills[0].id  # stale selection after the filters changed
    b = bills_by_id[bid]
    l, r = st.columns([3, 2])
    with l:
        if b.items:
            idf = pd.DataFrame([i.model_dump() for i in b.items])
            idf["amount"] = [i.effective_amount for i in b.items]
            st.dataframe(idf[["item", "qty", "unit", "weight_qtl", "rate", "amount", "struck_out"]], hide_index=True, width="stretch")
        else:
            st.caption("No line items recorded for this bill.")
        if b.total_written is not None:
            st.caption(f"Total written on bill: {money(b.total_written)} · booked: {money(b.amount)}")
        if b.raw_text:
            with st.expander("Raw transcription"):
                st.code(b.raw_text)
        confirm = st.checkbox("I want to delete this bill", key=f"bx_del_confirm_{bid}")
        if st.button("🗑️ Delete bill", disabled=not confirm, key=f"bx_del_{bid}"):
            try:
                db.delete_bill(bid)
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not delete: {e}")
            else:
                toast_ok("Bill deleted.")
                st.rerun()
    with r:
        if b.image_path and tenant.owns_path(b.image_path) and Path(b.image_path).exists():
            if b.image_path.lower().endswith(".pdf"):
                st.caption(f"PDF stored at {b.image_path}")
            else:
                try:
                    st.image(b.image_path, width="stretch")
                except Exception:  # noqa: BLE001
                    st.caption(f"Image stored at {b.image_path} (preview not available)")
        else:
            st.caption("No image stored for this bill.")

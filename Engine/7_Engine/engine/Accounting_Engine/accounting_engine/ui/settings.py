"""Settings & Import page."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import streamlit as st

from engine import bank, config, db, templates, tenant
from engine import tally_export, tally_import as ti
from engine.importer import import_ledger_template
from ui.common import toast_ok


def render() -> None:
    st.title("⚙️ Settings & Import")

    s = db.stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Parties", s["parties"])
    c2.metric("Bills", s["bills"])
    c3.metric("Not exported", s["unexported"])
    c4.metric("Items", s["items"])

    is_admin = st.session_state.get("ae_is_admin", True)
    feats = (st.session_state.get("ae_cust") or {}).get("features") or {}
    if is_admin:
        from engine import runtime

        key_ok = runtime.has_api_key()
        st.subheader("AI reader")
        st.markdown(
            f"- Mode: **{config.EXTRACTOR_MODE}**\n"
            f"- Model: `{config.CLAUDE_MODEL}` (fallbacks: {', '.join(config.CLAUDE_FALLBACK_MODELS)})\n"
            f"- API key: {'✅ set' if key_ok else '❌ missing'}\n"
            f"- Database: `{tenant.current().db_path}`\n"
            f"- Bill images: `{tenant.current().image_dir}` · Exports: `{tenant.current().export_dir}`"
        )
        st.caption("Change these in the `.env` file next to app.py and restart the app.")

    if feats.get("tally_import", True):
        _import_section()
    _tally_settings_section()
    _templates_section()
    if feats.get("bank_slips", True):
        _bank_accounts_section()

    st.subheader("Backup")
    if tenant.current().db_path.exists():
        # SQLite's backup API – a plain file copy can miss data still in the -wal file.
        try:
            st.download_button("⬇️ Download database backup (.db)", data=db.backup_bytes(),
                               file_name=f"accounting_{datetime.now():%Y%m%d_%H%M}.db", key="st_backup")
        except Exception as e:  # noqa: BLE001
            st.error(f"Backup not available: {e}")
    restore = st.file_uploader("Restore database from a backup (.db) – replaces current data!", type=["db"], key="st_restore")
    if restore is not None:
        sure = st.checkbox("Yes, replace the current database with this backup", key="st_restore_sure")
        if st.button("♻️ Restore", disabled=not sure, key="st_do_restore"):
            data = restore.getvalue()
            if data[:16] != b"SQLite format 3\x00":
                st.error("That file is not a SQLite database.")
            else:
                try:
                    _restore_database(data)
                except Exception as e:  # noqa: BLE001
                    st.error(f"Restore failed – the current database was left unchanged: {e}")
                else:
                    st.rerun()


def _restore_database(data: bytes) -> None:
    """Replace the live database with ``data`` after validating it, keeping a backup of the old one."""
    # 1. Validate the upload on a scratch copy before touching anything.
    db_path = tenant.current().db_path
    scratch = db_path.with_name(f".restore_check_{datetime.now():%Y%m%d_%H%M%S}.db")
    scratch.write_bytes(data)
    try:
        chk = sqlite3.connect(str(scratch))
        try:
            if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("the backup file is corrupt (integrity check failed)")
            tables = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"parties", "bills"} <= tables:
                raise ValueError("the file is a SQLite database but not an Accounting Engine backup")
        finally:
            chk.close()  # Windows: the file cannot be deleted while a connection holds it
    finally:
        try:
            scratch.unlink()
        except OSError:
            pass
    # 2. Keep the current database (WAL content included).
    bak = None
    if db_path.exists():
        bak = db_path.with_name(f"{db_path.stem}.before_restore_{datetime.now():%Y%m%d_%H%M%S}.db")
        db.backup_to(bak)
    # 3. Swap. Stale -wal/-shm files belong to the old database and must go first.
    for suffix in ("-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
    db_path.write_bytes(data)
    db.init_db()
    # 4. A backup may reference files outside this data folder – drop such paths (the rows stay).
    with sqlite3.connect(str(db_path)) as c:
        for table in ("bills", "bank_txns"):
            try:
                rows = c.execute(f"SELECT id, image_path FROM {table} WHERE image_path IS NOT NULL").fetchall()
            except sqlite3.OperationalError:
                continue
            bad = [r[0] for r in rows if not tenant.owns_path(r[1])]
            if bad:
                c.executemany(f"UPDATE {table} SET image_path=NULL WHERE id=?", [(i,) for i in bad])
    toast_ok("Restored." + (f" Previous database kept at {bak.name}" if bak else ""))


# --------------------------------------------------------------------------- #
# Tally master import wizard: upload -> preview (choose groups) -> import
# --------------------------------------------------------------------------- #
def _import_section() -> None:
    st.subheader("Import parties & items from Tally")
    st.markdown(
        "Export from Tally and upload here – the format is detected automatically:\n"
        "- **Parties:** *Display → List of Accounts → Ledgers* exported as **PDF / Excel / CSV / TXT**, "
        "or the *Ledgers in Default Template* workbook.\n"
        "- **Items:** *Stock Summary* exported as **Excel**, or any Excel/CSV list with an *Item / Name* column.\n"
        "Existing parties and items are updated, never duplicated."
    )
    up = st.file_uploader("Tally export file", type=["pdf", "xlsx", "csv", "txt"], key="st_tally_file")
    ss = st.session_state
    fp = (up.name, up.size, getattr(up, "file_id", None)) if up is not None else None
    if fp != ss.get("st_tally_fp"):
        ss["st_tally_fp"] = fp
        ss.pop("st_tally_preview", None)
    c1, c2 = st.columns(2)
    if c1.button("🔍 Read file", type="primary", disabled=up is None, key="st_tally_read"):
        try:
            with st.spinner("Reading…"):
                ss["st_tally_preview"] = ti.detect_and_parse(up.getvalue(), up.name)
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read the file: {e}")
    if c2.button("🧪 Load the sample template (demo data)", key="st_sample", disabled=not config.TEMPLATE_PATH.exists()):
        try:
            res = import_ledger_template(config.TEMPLATE_PATH)
            st.success(res.summary())
        except Exception as e:  # noqa: BLE001
            st.error(f"Import failed: {e}")

    pv = ss.get("st_tally_preview")
    if pv is None:
        return
    st.markdown(f"**Detected:** {pv.describe()}")
    for w in pv.warnings:
        st.warning(w)
    if pv.kind == "ledger_template":
        st.success("Imported directly (this format already carries groups, addresses and opening balances).")
        ss.pop("st_tally_preview", None)
        return
    if pv.kind == "unknown" or not (pv.ledgers or pv.items):
        if pv.kind != "unknown":
            st.warning("The file was recognised but contains no rows to import.")
        return

    if pv.kind == "ledger_list":
        _ledger_preview(pv)
    else:
        _item_preview(pv)
    if pv.skipped:
        with st.expander(f"Skipped lines ({len(pv.skipped)})"):
            st.write("\n".join(f"- {x}" for x in pv.skipped[:300]))


def _ledger_preview(pv: ti.ImportPreview) -> None:
    import pandas as pd

    st.caption("Tick the groups that are customers / suppliers. Expense, bank, tax and asset groups are "
               "unticked by default. Ledgers in unticked groups are not imported.")
    default = set(pv.default_party_groups())
    rows = [{"Import": g in default, "Group": g, "Ledgers": n, "Examples": ", ".join(pv.examples(g))}
            for g, n in pv.group_counts.items()]
    edited = st.data_editor(
        pd.DataFrame(rows), hide_index=True, width="stretch", key=f"st_groups_{pv.source_name}_{len(pv.ledgers)}",
        disabled=["Group", "Ledgers", "Examples"],
        column_config={"Import": st.column_config.CheckboxColumn("Import"),
                       "Examples": st.column_config.TextColumn("Examples", width="large")},
    )
    selected = [str(g) for g in edited.loc[edited["Import"] == True, "Group"].tolist()]  # noqa: E712
    n_sel = sum(pv.group_counts.get(g, 0) for g in selected)
    unknown_as = "Sundry Debtors"
    if ti.UNKNOWN_GROUP in selected:
        unknown_as = st.selectbox("Import '(group not detected)' ledgers as", ["Sundry Debtors", "Sundry Creditors"],
                                  key="st_unknown_as")
    st.caption(f"{n_sel:,} ledgers in {len(selected)} group(s) will be imported as parties.")
    if st.button(f"📤 Import {n_sel:,} parties", type="primary", disabled=not n_sel, key="st_apply_ledgers"):
        try:
            with st.spinner("Importing…"):
                added, updated = ti.apply_ledgers(pv, selected, unknown_group_as=unknown_as)
            st.session_state.pop("st_tally_preview", None)
            st.success(f"Parties: {added:,} added, {updated:,} updated.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Import failed: {e}")


def _item_preview(pv: ti.ImportPreview) -> None:
    import pandas as pd

    rows = [{"Item": i.name, "Group": i.group or "", "Unit": i.unit or "", "Rate": i.rate} for i in pv.items]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=300)
    groups = pv.item_group_counts
    st.caption(f"{len(pv.items):,} items · groups: " + ", ".join(f"{g} ({n})" for g, n in list(groups.items())[:12])
               + (" …" if len(groups) > 12 else ""))
    if st.button(f"📤 Import {len(pv.items):,} items", type="primary", key="st_apply_items"):
        try:
            with st.spinner("Importing…"):
                added, updated = ti.apply_items(pv)
            st.session_state.pop("st_tally_preview", None)
            st.success(f"Items: {added:,} added, {updated:,} updated.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Import failed: {e}")


# --------------------------------------------------------------------------- #
# Tally voucher export settings
# --------------------------------------------------------------------------- #
def _tally_settings_section() -> None:
    st.subheader("Tally voucher export settings")
    st.caption("Names must match your Tally company exactly (ledger names, voucher types, units). "
               "Used for the voucher XML / Excel / CSV files.")
    ts = tally_export.TallySettings.load()
    with st.form("tally_settings", border=True):
        c1, c2 = st.columns(2)
        company = c1.text_input("Tally company name (optional)", value=ts.company,
                                help="Leave blank to import into whichever company is open in Tally.")
        godown = c2.text_input("Godown / location (optional)", value=ts.godown,
                               help="Leave blank for Tally's default (Main Location).")
        c3, c4, c5, c6 = st.columns(4)
        sales_ledger = c3.text_input("Sales ledger", value=ts.sales_ledger)
        sales_vch = c4.text_input("Sales voucher type", value=ts.sales_vch_type)
        purchase_ledger = c5.text_input("Purchase ledger", value=ts.purchase_ledger)
        purchase_vch = c6.text_input("Purchase voucher type", value=ts.purchase_vch_type)
        c7, c8, c9, c10 = st.columns(4)
        inventory = c7.radio("Voucher content", ["With stock items", "Accounting only"],
                             index=0 if ts.inventory else 1,
                             help="Accounting only = party + sales/purchase ledger; item details go in the narration. "
                                  "Use it if you don't maintain stock items in Tally.")
        default_unit = c8.text_input("Unit when a line has none", value=ts.default_unit)
        weight_unit = c9.text_input("Unit for weight-priced lines", value=ts.weight_unit,
                                    help="Lines like '.52 × 5550' are exported as 0.52 of this unit at 5550 per unit.")
        stock_group = c10.text_input("Stock group for new items (optional)", value=ts.stock_group)
        c11, c12, c13 = st.columns(3)
        cash_ledger = c11.text_input("Cash ledger (bank deposits)", value=ts.cash_ledger)
        contra_vch = c12.text_input("Contra voucher type", value=ts.contra_vch_type)
        receipt_vch = c13.text_input("Receipt voucher type", value=ts.receipt_vch_type)
        if st.form_submit_button("💾 Save Tally settings", type="primary"):
            try:
                tally_export.TallySettings(
                    company=company, sales_ledger=sales_ledger or "Sales", purchase_ledger=purchase_ledger or "Purchase",
                    sales_vch_type=sales_vch or "Sales", purchase_vch_type=purchase_vch or "Purchase",
                    inventory=inventory == "With stock items", godown=godown, default_unit=default_unit or "Nos",
                    weight_unit=weight_unit or "Qtl", stock_group=stock_group,
                    cash_ledger=cash_ledger or "Cash", contra_vch_type=contra_vch or "Contra",
                    receipt_vch_type=receipt_vch or "Receipt",
                ).save()
                toast_ok("Tally settings saved.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not save: {e}")


# --------------------------------------------------------------------------- #
# Bank accounts -> Tally bank ledger names (learned from slips, editable here)
# --------------------------------------------------------------------------- #
def _bank_accounts_section() -> None:
    st.subheader("Bank accounts")
    st.caption("Account number on the deposit slip → bank ledger name in Tally. New accounts are added the first time a slip is saved.")
    accounts = bank.known_accounts()
    if accounts:
        import pandas as pd

        edited = st.data_editor(pd.DataFrame([{"Account no.": a, "Tally bank ledger": l} for a, l in accounts.items()]),
                                hide_index=True, width="stretch", key="st_bank_accts", disabled=["Account no."])
        if st.button("💾 Save ledger names", key="st_bank_save"):
            for _, r in edited.iterrows():
                if str(r["Tally bank ledger"]).strip():
                    bank.set_bank_ledger(str(r["Account no."]), str(r["Tally bank ledger"]))
            toast_ok("Saved.")
            st.rerun()
    c1, c2, c3 = st.columns([2, 2, 1])
    acct = c1.text_input("Add account no.", key="st_bank_new_acct")
    led = c2.text_input("Tally bank ledger", key="st_bank_new_led")
    if c3.button("➕ Add", key="st_bank_add", disabled=not (acct.strip() and led.strip())):
        bank.set_bank_ledger("".join(ch for ch in acct if ch.isdigit()) or acct.strip(), led)
        toast_ok("Added.")
        st.rerun()
    if accounts:
        dl = st.selectbox("Remove account", [None, *accounts], format_func=lambda a: "—" if a is None else f"{a} → {accounts[a]}", key="st_bank_del")
        if dl and st.button("🗑️ Remove", key="st_bank_del_btn"):
            bank.delete_account(dl)
            st.rerun()


# --------------------------------------------------------------------------- #
# Excel templates (official Tally samples by default, user upload overrides)
# --------------------------------------------------------------------------- #
def _templates_section() -> None:
    st.subheader("Excel import templates")
    st.caption("By default the exports use TallyPrime's official sample workbooks (Import → Transactions → "
               "*Accounting Voucher*; Import → Masters → *Ledger*). Upload your own copy to make the export follow "
               "it exactly – only the columns present in your file are filled.")
    for kind in ("voucher", "masters"):
        k = templates.KINDS[kind]
        inf = templates.info(kind)
        with st.container(border=True):
            c1, c2 = st.columns([3, 2])
            src = {"custom": "✅ your uploaded template", "official": "Tally official sample", "built-in": "built-in headers"}[inf.source]
            c1.markdown(f"**{k['label']}** — using: {src}" + (f" · sheet *{inf.sheet}* · {len(inf.headers)} columns" if inf.headers else ""))
            if inf.headers:
                c1.caption(", ".join(inf.headers[:14]) + (" …" if len(inf.headers) > 14 else ""))
            for w in inf.warnings:
                c1.warning(w)
            up = c2.file_uploader("Upload template (.xlsx)", type=["xlsx"], key=f"st_tpl_{kind}",
                                  help=f"Must have the header row with at least: {', '.join(k['required'])}")
            if up is not None and c2.button("Use this template", key=f"st_tpl_use_{kind}"):
                try:
                    new = templates.set_custom(kind, up.getvalue())
                except ValueError as e:
                    st.error(str(e))
                except Exception as e:  # noqa: BLE001
                    st.error(f"Could not save the template: {e}")
                else:
                    toast_ok(f"Template saved ({len(new.headers)} columns).")
                    st.rerun()
            if inf.source == "custom" and c2.button("Back to Tally's official template", key=f"st_tpl_clear_{kind}"):
                try:
                    templates.clear_custom(kind)
                except OSError as e:
                    st.error(f"Could not remove: {e}")
                else:
                    st.rerun()

"""Party and Item master pages."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from engine import config, db
from engine.models import Party
from ui.common import toast_ok

GROUPS = ["Sundry Debtors", "Sundry Creditors", "Bank Accounts", "Cash-in-Hand", "Sales Accounts",
          "Purchase Accounts", "Duties & Taxes", "Direct Expenses", "Indirect Expenses", "Capital Account"]


def _party_form(p: Party | None, key: str) -> Party | None:
    with st.form(key=f"pf_{key}", border=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name *", value=p.name if p else "")
        grp = p.group_name if p else "Sundry Debtors"
        group = c2.selectbox("Group", GROUPS if grp in GROUPS else [grp, *GROUPS], index=(GROUPS.index(grp) if grp in GROUPS else 0))
        address = st.text_area("Address (one line per row)", value=p.address or "" if p else "", height=80)
        c3, c4, c5 = st.columns(3)
        state = c3.text_input("State", value=(p.state if p and p.state else config.DEFAULT_STATE))
        country = c4.text_input("Country", value=(p.country if p and p.country else config.DEFAULT_COUNTRY))
        pincode = c5.text_input("Pincode", value=(p.pincode or "") if p else "")
        c6, c7 = st.columns([2, 1])
        ob_val = float(p.opening_balance) if p and p.opening_balance is not None else 0.0
        # No lower bound: an imported ledger may carry a negative balance, and min_value=0 would then
        # crash this page (StreamlitValueBelowMinError) instead of letting the user correct it.
        ob = c6.number_input("Opening balance", step=100.0, value=ob_val)
        ob_drcr = c7.selectbox("Dr/Cr", ["Dr", "Cr"], index=(0 if not p or p.opening_drcr != "Cr" else 1))
        aliases = st.text_input("Aliases (comma separated – other spellings seen on bills)",
                                value=", ".join(p.aliases) if p else "")
        ok = st.form_submit_button("💾 Save", type="primary")
    if not ok:
        return None
    if not name.strip():
        st.error("Name is required.")
        return None
    return Party(
        id=p.id if p else None, name=name.strip(), group_name=group, address=address.strip() or None,
        state=state.strip() or None, country=country.strip() or None, pincode=pincode.strip() or None,
        opening_balance=ob if ob else None, opening_drcr=ob_drcr if ob else None,
        aliases=[a.strip() for a in aliases.split(",") if a.strip()],
    )


def render_parties() -> None:
    st.title("👥 Parties / Ledgers")
    q = st.text_input("Search", key="pm_search", placeholder="name or alias")
    parties = db.list_parties(search=q or None)
    if parties:
        df = pd.DataFrame([{
            "ID": p.id, "Name": p.name, "Group": p.group_name, "Opening": p.opening_balance, "Dr/Cr": p.opening_drcr,
            "Address": (p.address or "").replace("\n", ", "), "State": p.state, "Pincode": p.pincode,
            "Aliases": ", ".join(p.aliases),
        } for p in parties])
        st.dataframe(df, hide_index=True, width="stretch")
    else:
        st.info("No parties yet. Add one below or import your Tally ledger file from Settings.")

    tab_edit, tab_new = st.tabs(["✏️ Edit party", "➕ Add party"])
    with tab_edit:
        if parties:
            by_id = {p.id: p.name for p in parties}  # dict lookup: 8,000 ledgers x 8,000 options otherwise
            pid = st.selectbox("Party", list(by_id), key="pm_edit_pick", format_func=lambda i: by_id.get(i, str(i)))
            if pid not in by_id:
                pid = parties[0].id  # stale label from before the search changed
            p = db.get_party(pid)
            newp = _party_form(p, key=f"edit_{pid}")
            if newp:
                other = db.get_party_by_name(newp.name)
                if other and other.id != pid:
                    st.error(f"Another party is already called “{other.name}”.")
                else:
                    try:
                        db.upsert_party(newp)
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Could not save party: {e}")
                    else:
                        toast_ok("Party saved.")
                        st.rerun()
            confirm = st.checkbox("Delete this party (only possible if it has no bills)", key=f"pm_del_c_{pid}")
            if st.button("🗑️ Delete party", disabled=not confirm, key=f"pm_del_{pid}"):
                try:
                    db.delete_party(pid)
                    toast_ok("Party deleted.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
    with tab_new:
        newp = _party_form(None, key="new")
        if newp:
            if db.get_party_by_name(newp.name):
                st.error("A party with this name already exists.")
            else:
                db.upsert_party(newp)
                toast_ok("Party added.")
                st.rerun()


def render_items() -> None:
    st.title("📦 Items")
    st.caption("Import your Tally Stock Summary from Settings, or let items be learned from saved bills. "
               "Add aliases so different spellings on bills snap to one name.")
    q = st.text_input("Search", key="im_search", placeholder="name, alias or group")
    items = db.list_items()
    if q.strip():
        ql = q.strip().lower()
        items = [i for i in items if ql in i["name"].lower() or ql in (i.get("group_name") or "").lower()
                 or any(ql in a.lower() for a in i["aliases"])]
    if items:
        df = pd.DataFrame([{
            "ID": i["id"], "Name": i["name"], "Group": i.get("group_name") or "", "Default unit": i["default_unit"],
            "Last rate": i["last_rate"], "Times used": i["times_used"], "Source": i.get("source") or "",
            "Aliases": ", ".join(i["aliases"]),
        } for i in items])
        st.dataframe(df, hide_index=True, width="stretch", height=min(420, 40 + 35 * len(df)))
        st.caption(f"{len(items):,} item(s)")
    else:
        st.info("No items yet.")
    with st.form("item_form", border=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        name = c1.text_input("Item name *", placeholder="Rice (BPT)")
        unit = c2.selectbox("Default unit", [None, "Bag", "Pkt", "Tin", "Kg", "Ltr", "Qtl", "Pcs", "Box", "Nos", "Set"],
                            format_func=lambda u: "—" if u is None else u)
        rate = c3.number_input("Last rate", min_value=0.0, step=1.0, value=0.0)
        group = c4.text_input("Stock group")
        aliases = st.text_input("Aliases (comma separated)")
        if st.form_submit_button("💾 Add / update item", type="primary"):
            if not name.strip():
                st.error("Item name is required.")
            else:
                try:
                    db.upsert_item(name, [a for a in aliases.split(",") if a.strip()], unit, rate or None,
                                   group_name=group.strip() or None, source="manual")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Could not save item: {e}")
                else:
                    toast_ok("Item saved.")
                    st.rerun()
    if items:
        names_by_id = {i["id"]: i["name"] for i in items}
        iid = st.selectbox("Delete item", [None, *names_by_id], key="im_del_pick",
                           format_func=lambda i: "—" if i is None else names_by_id.get(i, str(i)))
        if isinstance(iid, int) and iid in names_by_id and st.button("🗑️ Delete", key="im_del"):
            db.delete_item(iid)
            toast_ok("Item deleted.")
            st.rerun()

"""Administrator panel: users, plans & subscriptions, payments, usage, customization, integrations, audit, backups."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

import streamlit as st

from engine import db, master, payments, secrets, tenant
from ui.common import toast_ok


def render(admin: master.User) -> None:
    st.title("🛡️ Administration")
    s = master.stats()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Users", s["users"])
    c2.metric("Pending approval", s["pending"])
    c3.metric("Active plans", s["active_subs"])
    c4.metric("Scans (30 d)", s["scans_30d"])
    c5.metric("Claude cost 30 d", f"${s['cost_30d']:.2f}")

    tabs = st.tabs(["Users", "Plans & recharge", "Payments", "Usage", "Customization", "Integrations", "Audit", "Backups"])
    with tabs[0]:
        _users(admin)
    with tabs[1]:
        _plans(admin)
    with tabs[2]:
        _payments(admin)
    with tabs[3]:
        _usage()
    with tabs[4]:
        _customization(admin)
    with tabs[5]:
        _integrations(admin)
    with tabs[6]:
        _audit()
    with tabs[7]:
        _backups(admin)


# --------------------------------------------------------------------------- #
def _users(admin: master.User) -> None:
    q = st.text_input("Search", key="ad_user_q", placeholder="e-mail, name or business")
    users = master.list_users(search=q or None)
    rows = []
    for u in users:
        acc = master.access_for(u)
        rows.append({"ID": u.id, "E-mail": u.email, "Name": u.name, "Business": u.business_name or "", "Role": u.role,
                     "Status": u.status, "Plan": acc.plan or "—", "Until": acc.end_date.isoformat() if acc.end_date else "",
                     "Scans left": "∞" if acc.allowed and acc.scans_left is None else (str(acc.scans_left) if acc.scans_left is not None else ""),
                     "Own key": "yes" if u.has_own_key else "", "Last login": (u.last_login or "")[:16]})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    pending = [u for u in users if u.status == "pending"]
    if pending:
        st.markdown(f"**{len(pending)} account(s) waiting for approval**")
        for u in pending:
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.text(f"{u.email} · {u.name} · {u.business_name or ''} · {u.phone or ''}")
            if c2.button("Approve", key=f"ad_appr_{u.id}", type="primary"):
                master.update_user(u.id, status="active")
                master.audit("approve_user", admin, target=u.email)
                st.rerun()
            if c3.button("Reject", key=f"ad_rej_{u.id}"):
                master.delete_user(u.id)
                master.audit("reject_user", admin, target=u.email)
                st.rerun()

    st.markdown("**Create a user**")
    with st.form("ad_new_user"):
        c1, c2, c3 = st.columns(3)
        email = c1.text_input("E-mail")
        name = c2.text_input("Name")
        business = c3.text_input("Business")
        c4, c5, c6 = st.columns(3)
        phone = c4.text_input("Phone")
        role = c5.selectbox("Role", ["user", "admin"])
        plan = c6.selectbox("Start with plan", ["(none)"] + [p["name"] for p in master.list_plans(only_active=True)])
        if st.form_submit_button("Create", type="primary"):
            pw = secrets.temp_password()
            try:
                u = master.create_user(email, pw, name=name, role=role, status="active", must_change_pw=True,
                                       business_name=business or None, phone=phone or None)
            except ValueError as e:
                st.error(str(e))
            else:
                if plan != "(none)":
                    master.add_subscription(u.id, plan, source="manual", note="on creation", created_by=admin.id)
                master.audit("create_user", admin, target=u.email, detail=f"role={role} plan={plan}")
                st.success(f"Created **{u.email}**. Temporary password: `{pw}` — share it with the user; they must change it at first login.")

    if users:
        st.markdown("**Manage a user**")
        by_id = {u.id: u for u in users}
        uid = st.selectbox("User", list(by_id), format_func=lambda i: f"{by_id[i].email} ({by_id[i].role}, {by_id[i].status})", key="ad_pick")
        u = by_id[uid]
        c1, c2, c3, c4, c5 = st.columns(5)
        if c1.button("Reset password", key="ad_reset"):
            pw = secrets.temp_password()
            master.set_password(u.id, pw, must_change=True)
            master.audit("reset_password", admin, target=u.email)
            st.success(f"Temporary password for {u.email}: `{pw}`")
        if u.status == "active" and c2.button("Disable", key="ad_disable", disabled=u.id == admin.id):
            try:
                master.update_user(u.id, status="disabled")
                master.audit("disable_user", admin, target=u.email)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        if u.status != "active" and c2.button("Activate", key="ad_activate"):
            master.update_user(u.id, status="active")
            master.audit("activate_user", admin, target=u.email)
            st.rerun()
        new_role = "admin" if u.role == "user" else "user"
        if c3.button(f"Make {new_role}", key="ad_role", disabled=u.id == admin.id):
            try:
                master.update_user(u.id, role=new_role)
                master.audit("change_role", admin, target=u.email, detail=new_role)
                st.rerun()
            except ValueError as e:
                st.error(str(e))
        allow = c4.toggle("May use own Claude key", value=u.allow_own_key, key=f"ad_ownkey_{u.id}")
        if allow != u.allow_own_key:
            master.update_user(u.id, allow_own_key=allow)
            master.audit("allow_own_key", admin, target=u.email, detail=str(allow))
            st.rerun()
        sure = c5.checkbox("Delete user + all their data", key=f"ad_del_ok_{u.id}")
        if c5.button("Delete", key="ad_delete", disabled=not sure or u.id == admin.id):
            try:
                master.delete_user(u.id)
                import shutil

                shutil.rmtree(tenant.for_user(u.id).root, ignore_errors=True)
                master.audit("delete_user", admin, target=u.email, detail="tenant data removed")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

        st.markdown("**Recharge / add a plan for this user**")
        with st.form(f"ad_sub_{u.id}"):
            c1, c2, c3, c4 = st.columns(4)
            plan = c1.selectbox("Plan", [p["name"] for p in master.list_plans()])
            start = c2.date_input("Start", value=date.today())
            ref = c3.text_input("Payment reference (UPI/UTR)")
            note = c4.text_input("Note")
            stack = st.checkbox("Start after the current plan ends (instead of the date above)", value=True)
            if st.form_submit_button("Add plan", type="primary"):
                sid = master.add_subscription(u.id, plan, source="manual", reference=ref, note=note, created_by=admin.id,
                                              start=None if stack else start)
                master.audit("add_subscription", admin, target=u.email, detail=f"{plan} ref={ref} id={sid}")
                toast_ok("Plan added.")
                st.rerun()
        subs = master.list_subscriptions(u.id)
        if subs:
            st.dataframe([{"ID": h["id"], "Plan": h["plan_name"], "From": h["start_date"], "To": h["end_date"],
                           "Used": h["scans_used"], "Quota": h["scan_quota"] if h["scan_quota"] is not None else "∞",
                           "Source": h["source"], "Ref": h["reference"] or "", "Cancelled": bool(h["cancelled"])} for h in subs],
                         hide_index=True, width="stretch")
            live = [h for h in subs if not h["cancelled"]]
            if live:
                cid = st.selectbox("Cancel a plan", [h["id"] for h in live], key=f"ad_cancel_pick_{u.id}",
                                   format_func=lambda i: next(f"#{h['id']} {h['plan_name']} {h['start_date']}→{h['end_date']}" for h in live if h["id"] == i))
                if st.button("Cancel selected plan", key=f"ad_cancel_{u.id}"):
                    master.cancel_subscription(cid)
                    master.audit("cancel_subscription", admin, target=u.email, detail=str(cid))
                    st.rerun()

        st.markdown("**Customization for this user only** (overrides the global values; blank = use global)")
        _customization_form(admin, user_id=u.id)


# --------------------------------------------------------------------------- #
def _plans(admin: master.User) -> None:
    plans = master.list_plans()
    st.dataframe([{"ID": p["id"], "Name": p["name"], "Days": p["days"], "Scans": p["scan_quota"] if p["scan_quota"] is not None else "∞",
                   "Price ₹": p["price_inr"], "Active": bool(p["active"]), "Description": p["description"] or ""} for p in plans],
                 hide_index=True, width="stretch")
    with st.form("ad_plan"):
        c1, c2, c3, c4, c5 = st.columns(5)
        pid = c1.selectbox("Edit", ["(new)"] + [f"{p['id']} {p['name']}" for p in plans])
        cur = next((p for p in plans if pid.startswith(f"{p['id']} ")), None) if pid != "(new)" else None
        name = c2.text_input("Name", value=cur["name"] if cur else "")
        days = c3.number_input("Days", min_value=1, value=int(cur["days"]) if cur else 30)
        quota = c4.number_input("Scan quota (0 = unlimited)", min_value=0, value=int(cur["scan_quota"] or 0) if cur else 500)
        price = c5.number_input("Price ₹", min_value=0.0, value=float(cur["price_inr"]) if cur else 999.0)
        desc = st.text_input("Description", value=(cur["description"] or "") if cur else "")
        active = st.checkbox("Active (visible to users)", value=bool(cur["active"]) if cur else True)
        if st.form_submit_button("Save plan", type="primary"):
            try:
                master.upsert_plan(name, int(days), None if quota == 0 else int(quota), price, active, desc,
                                   plan_id=cur["id"] if cur else None)
                master.audit("save_plan", admin, target=name)
                st.rerun()
            except ValueError as e:
                st.error(str(e))


def _payments(admin: master.User) -> None:
    pays = master.list_payments()
    if not pays:
        st.info("No online payments yet." + ("" if payments.configured() else " Razorpay keys are not set (Integrations)."))
        return
    users = {u.id: u.email for u in master.list_users()}
    st.dataframe([{"ID": p["id"], "User": users.get(p["user_id"], p["user_id"]), "Plan": p["plan_name"], "₹": p["amount_inr"],
                   "Status": p["status"], "Link": p["short_url"], "Payment id": p["payment_id"] or "", "Created": p["created_at"][:16],
                   "Paid": (p["paid_at"] or "")[:16]} for p in pays], hide_index=True, width="stretch")
    pend = [p for p in pays if p["status"] in ("created", "partially_paid", "confirming")]
    if pend and payments.configured() and st.button(f"Check {len(pend)} pending payment(s) with Razorpay", key="ad_sync"):
        done = 0
        for p in pend:
            try:
                if payments.sync_payment(p) == "paid":
                    done += 1
            except Exception as e:  # noqa: BLE001
                st.error(f"{p['plan_name']} for user {p['user_id']}: {e}")
                continue
        toast_ok(f"{done} payment(s) confirmed.")
        st.rerun()


def _usage() -> None:
    days = st.select_slider("Period (days)", [7, 30, 90, 365], value=30, key="ad_usage_days")
    rows = master.usage_summary(days=days)
    if not rows:
        st.info("No scans in this period.")
        return
    st.dataframe([{"User": r["email"], "Scans": r["n"], "Input tokens": r["inp"], "Output tokens": r["outp"],
                   "Est. cost $": round(r["cost"] or 0, 4), "Own-key scans": r["own"]} for r in rows],
                 hide_index=True, width="stretch")


def _customization_form(admin: master.User, user_id: int | None) -> None:
    scope_vals = master.all_settings(user_id)
    with st.form(f"ad_cust_{user_id or 'global'}"):
        vals = {}
        for key, help_ in master.CUSTOMIZABLE_KEYS.items():
            if key in ("custom_css", "labels_json", "features_json", "prompt_notes", "default_tally_json"):
                vals[key] = st.text_area(key, value=scope_vals.get(key, ""), help=help_, height=90)
            else:
                vals[key] = st.text_input(key, value=scope_vals.get(key, ""), help=help_)
        if st.form_submit_button("Save customization", type="primary"):
            bad = []
            for k in ("labels_json", "features_json", "default_tally_json"):
                if vals[k].strip():
                    try:
                        if not isinstance(json.loads(vals[k]), dict):
                            bad.append(k)
                    except ValueError:
                        bad.append(k)
            if bad:
                st.error(f"Not valid JSON objects: {', '.join(bad)}")
            else:
                for k, v in vals.items():
                    master.set_setting(k, v.strip() or None, user_id=user_id)
                master.audit("customization", admin, target=f"user:{user_id}" if user_id else "global",
                             detail=", ".join(k for k, v in vals.items() if v.strip()))
                toast_ok("Saved – visible to " + ("this user" if user_id else "all users") + " on their next page load.")
                st.rerun()


def _customization(admin: master.User) -> None:
    st.caption("Global values apply to every user. Per-user overrides live under *Users → Manage a user*. "
               "Users cannot change any of this. Feature flags: " + ", ".join(master.DEFAULT_FEATURES))
    _customization_form(admin, user_id=None)


def _integrations(admin: master.User) -> None:
    st.markdown("**Claude (Anthropic) master key** — used for every user who has no key of their own.")
    cur = secrets.decrypt(master.get_setting("anthropic_api_key_enc"))
    import os

    st.caption(f"Stored key: {secrets.mask(cur) if cur else '— (falls back to ANTHROPIC_API_KEY from .env: ' + (secrets.mask(os.getenv('ANTHROPIC_API_KEY', '')) if os.getenv('ANTHROPIC_API_KEY') else 'not set') + ')'}")
    with st.form("ad_claude_key"):
        k = st.text_input("Anthropic API key", type="password")
        c1, c2 = st.columns(2)
        if c1.form_submit_button("Save master key", type="primary"):
            if not k.strip().startswith("sk-ant-"):
                st.error("That does not look like an Anthropic key.")
            else:
                master.set_setting("anthropic_api_key_enc", secrets.encrypt(k.strip()))
                master.audit("master_key_set", admin)
                st.rerun()
        if c2.form_submit_button("Remove stored key"):
            master.set_setting("anthropic_api_key_enc", None)
            master.audit("master_key_removed", admin)
            st.rerun()
    st.markdown("**Cost estimate prices** (USD per million tokens, JSON `{\"model-substring\": [input, output]}`)")
    pj = st.text_input("price_json", value=master.get_setting("price_json") or "", key="ad_price_json")
    if st.button("Save prices", key="ad_price_save"):
        try:
            if pj.strip():
                json.loads(pj)
            master.set_setting("price_json", pj.strip() or None)
            toast_ok("Saved.")
        except ValueError:
            st.error("Not valid JSON.")

    st.markdown("**Razorpay** (online recharge via payment links)")
    kid, ksec = payments.keys()
    st.caption(f"Key id: {kid or '—'} · secret: {secrets.mask(ksec) if ksec else '—'}")
    with st.form("ad_rzp"):
        a = st.text_input("Key ID (rzp_live_… / rzp_test_…)", value=kid or "")
        b = st.text_input("Key secret", type="password")
        if st.form_submit_button("Save Razorpay keys", type="primary"):
            payments.set_keys(a, b if b else (ksec or ""))
            master.audit("razorpay_keys_set", admin)
            st.rerun()

    st.markdown("**Google sign-in**")
    from ui import auth_ui

    st.caption("Configured through `.streamlit/secrets.toml` ([auth] section – see README). Status: "
               + ("✅ configured" if auth_ui.google_configured() else "not configured"))


def _audit() -> None:
    rows = master.list_audit(400)
    if rows:
        st.dataframe([{"When": r["ts"], "Who": r["actor"], "Action": r["action"], "Target": r["target"] or "", "Detail": r["detail"] or ""}
                      for r in rows], hide_index=True, width="stretch")


def _backups(admin: master.User) -> None:
    st.caption("Master database (accounts, plans, settings) and each user's own database.")
    try:
        with master.connect() as c:
            import io
            import sqlite3

            buf = io.BytesIO()
            dst = sqlite3.connect(":memory:")
            c.backup(dst)
            for line in dst.iterdump():
                if line.startswith("INSERT INTO \"sessions\"") or line.startswith("INSERT INTO sessions"):
                    continue   # login sessions are not part of a backup
                buf.write((line + "\n").encode("utf-8"))
            dst.close()
        st.download_button("⬇️ master.db as SQL dump", data=buf.getvalue(), file_name="master_backup.sql", key="ad_bk_master")
    except Exception as e:  # noqa: BLE001
        st.error(f"Backup failed: {e}")
    users = master.list_users()
    if users:
        uid = st.selectbox("User database", [u.id for u in users], key="ad_bk_user",
                           format_func=lambda i: next(u.email for u in users if u.id == i))
        t = tenant.for_user(uid)
        if t.db_path.exists():
            try:
                data = db.backup_bytes(t.db_path)
                st.download_button(f"⬇️ {next(u.email for u in users if u.id == uid)} – accounting.db", data=data,
                                   file_name=f"user{uid}_accounting.db", key="ad_bk_tenant")
            except Exception as e:  # noqa: BLE001
                st.error(f"Backup failed: {e}")
        else:
            st.caption("This user has no data yet.")

"""My Account: profile, password, own Claude key, subscription status, recharge (manual + Razorpay)."""
from __future__ import annotations

import streamlit as st

from engine import master, payments, secrets
from ui.common import money, toast_ok


def subscription_box(user: master.User, cust: dict) -> None:
    acc = master.access_for(user)
    if user.is_admin:
        st.success("Administrator account — no subscription needed.")
        return
    if acc.allowed:
        left = "unlimited scans" if acc.scans_left is None else f"{acc.scans_left} scan(s) left"
        st.success(f"Plan **{acc.plan}** active until **{acc.end_date:%d %b %Y}** ({acc.days_left} day(s)) · {left}")
    else:
        st.error(acc.reason)


def recharge_section(user: master.User, cust: dict) -> None:
    st.subheader("Recharge / subscription")
    plans = [p for p in master.list_plans(only_active=True) if p["price_inr"] > 0]
    if not plans:
        st.info("No paid plans are published yet – contact the administrator.")
    else:
        rows = [{"Plan": p["name"], "Days": p["days"], "Scans": p["scan_quota"] if p["scan_quota"] is not None else "unlimited",
                 "Price ₹": p["price_inr"], "Details": p.get("description") or ""} for p in plans]
        st.dataframe(rows, hide_index=True, width="stretch")
    st.markdown("**Manual recharge** — pay by UPI / bank transfer and send the receipt to the administrator; the plan is "
                "activated on your account." + (f"  \nUPI: `{cust['upi_id']}`" if cust.get("upi_id") else "")
                + (f"  \nContact: {cust['support_contact']}" if cust.get("support_contact") else ""))
    if payments.configured():
        st.markdown("**Pay online (Razorpay)**")
        names = [p["name"] for p in plans]
        if names:
            c1, c2 = st.columns([2, 1])
            pick = c1.selectbox("Plan", names, key="acc_plan")
            if c2.button("Create payment link", type="primary", key="acc_pay"):
                plan = next(p for p in plans if p["name"] == pick)
                try:
                    link = payments.create_link(user, plan)
                except payments.PaymentError as e:
                    st.error(str(e))
                else:
                    master.audit("payment_link", user, target=pick, detail=link.get("short_url") or "")
                    st.success(f"Pay ₹{link['amount_inr']:,.0f} here: {link['short_url']} — then click *Check payment* below.")
        pend = [p for p in master.list_payments(user.id) if p["status"] in ("created", "partially_paid", "confirming")]
        if pend:
            st.caption(f"{len(pend)} payment link(s) awaiting confirmation.")
            for p in pend[:5]:
                c1, c2 = st.columns([3, 1])
                c1.markdown(f"- {p['plan_name']} · ₹{p['amount_inr']:,.0f} · [open link]({p['short_url']}) · {p['created_at'][:16]}")
                if c2.button("Check payment", key=f"acc_chk_{p['id']}"):
                    try:
                        status = payments.sync_payment(p)
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))
                    else:
                        if status == "paid":
                            toast_ok("Payment received – plan activated.")
                            st.rerun()
                        st.info(f"Status: {status}")
    hist = master.list_subscriptions(user.id)
    if hist:
        with st.expander("Subscription history"):
            st.dataframe([{"Plan": h["plan_name"], "From": h["start_date"], "To": h["end_date"],
                           "Scans": f"{h['scans_used']}/{h['scan_quota'] if h['scan_quota'] is not None else '∞'}",
                           "Source": h["source"], "Ref": h["reference"] or "", "Cancelled": "yes" if h["cancelled"] else ""}
                          for h in hist], hide_index=True, width="stretch")


def render(user: master.User, cust: dict) -> None:
    st.title("👤 My Account")
    st.text(f"{user.name or user.email} · {user.email} · role: {user.role}")
    subscription_box(user, cust)

    usage = master.usage_summary(user.id, days=30)
    if usage:
        st.caption("Last 30 days: " + " · ".join(f"{u['kind']}: {u['n']}" for u in usage))

    tab_sub, tab_pw, tab_key = st.tabs(["Subscription", "Password", "Claude key"])
    with tab_sub:
        recharge_section(user, cust)
    with tab_pw:
        with st.form("chpw"):
            cur = st.text_input("Current password", type="password")
            new = st.text_input("New password (min 8 characters)", type="password")
            new2 = st.text_input("Repeat new password", type="password")
            if st.form_submit_button("Change password", type="primary"):
                u, msg = master.authenticate(user.email, cur)
                if not u and "approval" not in msg and "disabled" not in msg:
                    st.error("Current password is wrong.")
                elif new != new2:
                    st.error("New passwords do not match.")
                else:
                    try:
                        master.set_password(user.id, new, must_change=False, revoke_sessions=False)
                        master.revoke_all_sessions(user.id, except_token=st.session_state.get("ae_token"))
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        master.audit("password_changed", user, target=user.email)
                        toast_ok("Password changed.")
    with tab_key:
        if not cust.get("features", {}).get("own_api_key", True) or not user.allow_own_key:
            st.info("Using the service's Claude key. Scans count against your plan.")
        else:
            st.caption("Optional: use your own Anthropic API key. Scans made with your key are not counted against "
                       "your plan's quota (the subscription itself is still required). The key is stored encrypted.")
            st.markdown(f"Current: **{'own key set (' + secrets.mask(master.get_own_api_key(user.id)) + ')' if user.has_own_key else 'service key'}**")
            with st.form("ownkey"):
                k = st.text_input("Anthropic API key (sk-ant-…)", type="password")
                c1, c2 = st.columns(2)
                if c1.form_submit_button("Save key", type="primary"):
                    if not k.strip().startswith("sk-ant-"):
                        st.error("That does not look like an Anthropic key (sk-ant-…).")
                    else:
                        master.set_own_api_key(user.id, k.strip())
                        master.audit("own_key_set", user, target=user.email)
                        toast_ok("Key saved.")
                        st.rerun()
                if c2.form_submit_button("Remove my key"):
                    master.set_own_api_key(user.id, None)
                    master.audit("own_key_removed", user, target=user.email)
                    st.rerun()


def blocked_page(user: master.User, cust: dict) -> None:
    """Shown instead of the app when the subscription is missing / used up."""
    st.title("⏸️ Subscription required")
    subscription_box(user, cust)
    st.markdown("Your data is safe and will be available again as soon as the plan is active.")
    recharge_section(user, cust)

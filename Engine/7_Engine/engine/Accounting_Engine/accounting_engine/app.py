"""Accounting Engine – bill / bank-slip photos and voice -> Tally-importable files.

Single-user mode (laptop): no login, data under ./data (AE_AUTH=off, or nothing configured).
Multi-user service: accounts, subscriptions, per-user private data folders, administrator panel
(AE_AUTH=on, or as soon as an account exists).

Run:  streamlit run app.py
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import bank, config, db, master, runtime, secrets, tenant  # noqa: E402
from engine.tally_export import TallySettings  # noqa: E402
from ui import account, admin, auth_ui, bank_slips, bills, masters, new_bill, settings, theme  # noqa: E402

st.set_page_config(page_title=f"{config.BUSINESS_NAME} · Bills → Tally", page_icon="🧾", layout="wide")


def _tenant_bootstrap(t: tenant.Tenant, cust: dict) -> None:
    """First activation of a user's folder: create it and apply the admin's default Tally settings."""
    marker = t.root / ".initialized"
    t.ensure_dirs()
    db.init_db()
    bank.init()
    if marker.exists():
        return
    defaults = cust.get("default_tally") or {}
    if defaults:
        try:
            ts = TallySettings.load()
            for k, v in defaults.items():
                if hasattr(ts, k) and v not in (None, ""):
                    setattr(ts, k, bool(v) if isinstance(getattr(ts, k), bool) else str(v))
            ts.save()
        except Exception:  # noqa: BLE001
            pass
    marker.write_text("ok")


def _md_escape(text: str) -> str:
    """User-supplied text inside Markdown: neutralise both HTML and Markdown syntax."""
    out = html.escape(str(text or ""))
    return "".join("\\" + ch if ch in "\\`*_{}[]()#+-.!|<>~" else ch for ch in out)


def _resolve_tenant() -> tenant.Tenant | None:
    """Tenant of the signed-in user of this browser session (used when nothing is active in the thread)."""
    try:
        uid, tok = st.session_state.get("ae_user_id"), st.session_state.get("ae_token")
    except Exception:  # noqa: BLE001 - no Streamlit context in this thread
        return None
    if not (uid and tok):
        return None
    u = master.user_for_session(tok)   # still a valid, active session for this user? (deleted/disabled -> no tenant)
    return tenant.for_user(u.id) if u and u.id == uid else None


def _runtime_for(user: master.User, cust: dict) -> runtime.Runtime:
    own = master.get_own_api_key(user.id) if cust.get("features", {}).get("own_api_key", True) else None
    master_key = secrets.decrypt(master.get_setting("anthropic_api_key_enc")) or config.ANTHROPIC_API_KEY
    notes = " ".join(x for x in (master.get_setting("prompt_notes"),
                                 master.all_settings(user.id).get("prompt_notes")) if x)
    prices = {}
    try:
        prices = json.loads(master.get_setting("price_json") or "{}")
    except ValueError:
        prices = {}

    def before_scan(kind: str, own_key: bool):
        return master.reserve_scan(user, own_key=own_key)   # atomic: takes the slot or refuses

    def on_usage(kind, model, inp, outp, cost, own_key, ok, reservation):
        master.consume_scan(user, kind, model, inp, outp, cost, own_key=own_key, ok=ok, reservation=reservation)

    return runtime.Runtime(user_id=user.id, user_email=user.email, is_admin=user.is_admin,
                           api_key=own or master_key, own_key=bool(own), prompt_notes=notes, prices=prices,
                           before_scan=before_scan, on_usage=on_usage)


def _must_change_password(user: master.User) -> None:
    st.title("🔑 Set a new password")
    st.info("You signed in with a temporary password. Choose your own to continue.")
    with st.form("force_pw"):
        pw = st.text_input("New password (min 8 characters)", type="password")
        pw2 = st.text_input("Repeat", type="password")
        if st.form_submit_button("Save password", type="primary"):
            if pw != pw2:
                st.error("Passwords do not match.")
            else:
                try:
                    master.set_password(user.id, pw, must_change=False, revoke_sessions=False)
                except ValueError as e:
                    st.error(str(e))
                else:
                    master.audit("password_changed", user, target=user.email, detail="first login")
                    st.rerun()


def _sidebar(cust: dict, user: master.User | None, pages: dict) -> str:
    with st.sidebar:
        theme.sidebar_brand(cust, config.BUSINESS_NAME)
        if user:
            acc = master.access_for(user)
            st.markdown(f"**{_md_escape(user.name or user.email)}**")
            if not user.is_admin and acc.plan:
                left = "∞" if acc.scans_left is None else str(acc.scans_left)
                st.caption(f"{_md_escape(acc.plan)} · {acc.days_left} d · {left} scans")
        page = st.radio("Go to", list(pages), label_visibility="collapsed", key="nav")
        try:
            s = db.stats()
            st.caption(f"{s['parties']} parties · {s['bills']} bills · {s['unexported']} awaiting export")
        except Exception:  # noqa: BLE001 - tenant not initialised yet (e.g. forced password change)
            pass
        if user is None or user.is_admin:
            st.caption(f"Reader: {config.EXTRACTOR_MODE} · {config.CLAUDE_MODEL}")
        if user and st.button("Sign out", key="nav_logout", width="stretch"):
            auth_ui.logout()
            st.rerun()
    return page


def main() -> None:
    if not auth_ui.auth_enabled():
        # ---- single-user mode (unchanged behaviour) -------------------------------------------
        db.init_db()
        bank.init()
        cust = {"features": dict(master.DEFAULT_FEATURES), "labels": {}}
        st.session_state["ae_cust"] = cust
        pages = {
            "📄 New Bill": new_bill.render,
            "🏦 Bank Slips": bank_slips.render,
            "📚 Bills & Export": bills.render,
            "👥 Parties": masters.render_parties,
            "📦 Items": masters.render_items,
            "⚙️ Settings & Import": settings.render,
        }
        page = _sidebar(cust, None, pages)
        pages[page]()
        return

    # ---- multi-user service -------------------------------------------------------------------
    # Every DB access must go to the signed-in user's own folder – also inside widget callbacks,
    # which Streamlit runs in a fresh thread before this script body activates the tenant.
    tenant.configure(_resolve_tenant, strict=True)
    user = auth_ui.gate()
    if user is None:
        return
    cust = master.effective_customization(user.id)
    st.session_state["ae_cust"] = cust
    st.session_state["ae_is_admin"] = user.is_admin
    theme.apply(cust)
    t = tenant.for_user(user.id)
    tenant.activate(t)
    _tenant_bootstrap(t, cust)
    runtime.activate(_runtime_for(user, cust))
    if user.must_change_pw:
        _sidebar(cust, user, {"🔑 New password": None})
        _must_change_password(user)
        return

    feats = cust.get("features", {})
    acc = master.access_for(user)
    # A used-up scan quota only blocks new scans (the extractor refuses with a clear message); the
    # app stays usable to finish, export and manage what was already read. No valid period = blocked.
    from datetime import date as _date

    period_active = user.is_admin or (acc.end_date is not None and acc.end_date >= _date.today())
    pages: dict = {}
    if not period_active:
        pages["⏸️ Subscription"] = lambda: account.blocked_page(user, cust)
    if period_active:
        pages[theme.label(cust, "📄 New Bill")] = new_bill.render
        if feats.get("bank_slips", True):
            pages[theme.label(cust, "🏦 Bank Slips")] = bank_slips.render
        pages[theme.label(cust, "📚 Bills & Export")] = bills.render
        pages[theme.label(cust, "👥 Parties")] = masters.render_parties
        pages[theme.label(cust, "📦 Items")] = masters.render_items
        pages[theme.label(cust, "⚙️ Settings & Import")] = settings.render
    pages[theme.label(cust, "👤 My Account")] = lambda: account.render(user, cust)
    if user.is_admin:
        pages["🛡️ Administration"] = lambda: admin.render(user)
    page = _sidebar(cust, user, pages)
    if page not in pages:  # stale nav state (e.g. plan expired between runs) -> first page
        page = next(iter(pages))
    if period_active and not acc.allowed and not user.is_admin:
        st.warning(acc.reason + " (You can still review, save and export what was already read.)", icon="⏸️")
    pages[page]()


main()

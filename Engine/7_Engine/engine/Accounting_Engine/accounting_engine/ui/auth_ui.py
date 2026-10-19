"""Login / sign-up / first-run setup and cookie-backed sessions."""
from __future__ import annotations

import os
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components

from engine import config, master, secrets

COOKIE = "ae_session"


# --------------------------------------------------------------------------- #
# cookie helpers (write through the page, read through st.context.cookies)
# --------------------------------------------------------------------------- #
def _is_https() -> bool:
    """True when the browser reached us over https (directly, or through Caddy/nginx which set X-Forwarded-Proto)."""
    try:
        if st.context.headers.get("X-Forwarded-Proto", "").lower().startswith("https"):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(getattr(st.context, "url", "") or "").lower().startswith("https://")
    except Exception:  # noqa: BLE001
        return False


def _set_cookie(value: str, days: int = master.SESSION_DAYS) -> None:
    # AE_COOKIE_SECURE=auto (default): Secure flag whenever the page is served over https; on/off force it.
    mode = os.getenv("AE_COOKIE_SECURE", "auto").strip().lower()
    secure_flag = mode in ("1", "true", "yes", "on") or (mode == "auto" and _is_https())
    secure = "; Secure" if secure_flag else ""
    components.html(
        f"""<script>
        const d = new Date(); d.setTime(d.getTime() + {days}*24*3600*1000);
        parent.document.cookie = "{COOKIE}={value}; expires=" + d.toUTCString() + "; path=/; SameSite=Lax{secure}";
        </script>""",
        height=0,
    )


def _clear_cookie() -> None:
    components.html(
        f"""<script>parent.document.cookie = "{COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax";</script>""",
        height=0,
    )


def flush_cookie() -> None:
    """Render pending cookie writes. Call once at the top of every run."""
    ss = st.session_state
    if ss.get("ae_cookie_clear"):
        _clear_cookie()
        if cookie_token() is None:
            ss.pop("ae_cookie_clear", None)
    tok = ss.get("ae_cookie_pending")
    if tok:
        if cookie_token() == tok:
            ss.pop("ae_cookie_pending", None)
        else:
            _set_cookie(tok)


def cookie_token() -> Optional[str]:
    try:
        return st.context.cookies.get(COOKIE)
    except Exception:  # noqa: BLE001
        return None


def client_ip() -> str:
    """Best-effort client address (Caddy/nginx set X-Forwarded-For; else Streamlit's view)."""
    if os.getenv("AE_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes", "on"):
        try:   # only behind Caddy/nginx we configured – otherwise any client could pick its own address
            xff = st.context.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()[:64]
        except Exception:  # noqa: BLE001
            pass
    try:
        return str(getattr(st.context, "ip_address", "") or "?")[:64]
    except Exception:  # noqa: BLE001
        return "?"


# --------------------------------------------------------------------------- #
# mode & bootstrap
# --------------------------------------------------------------------------- #
def auth_enabled() -> bool:
    """AE_AUTH=on|off|auto. auto = on as soon as an account exists or an admin bootstrap is configured."""
    mode = os.getenv("AE_AUTH", "auto").strip().lower()
    if mode in ("on", "1", "true", "yes"):
        return True
    if mode in ("off", "0", "false", "no"):
        return False
    try:
        return master.user_count() > 0 or bool(os.getenv("AE_ADMIN_EMAIL"))
    except Exception:  # noqa: BLE001
        return False


def bootstrap_admin_from_env() -> Optional[master.User]:
    email, pw = os.getenv("AE_ADMIN_EMAIL", "").strip(), os.getenv("AE_ADMIN_PASSWORD", "").strip()
    if email and pw and not master.admin_exists():
        try:
            u = master.create_user(email, pw, name="Administrator", role="admin", status="active")
            master.audit("bootstrap_admin", None, target=u.email, detail="from environment")
            return u
        except ValueError:
            return None
    return None


def _setup_token() -> str:
    p = config.DATA_DIR / "setup_token.txt"
    if p.exists():
        return p.read_text().strip()
    tok = secrets.new_token(12)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok)
    print(f"\n==> First-run setup token (create the administrator account): {tok}\n", flush=True)
    return tok


def _first_run_setup() -> None:
    st.title("🔐 First-run setup")
    st.info("No administrator account exists yet. Create it with the setup token printed in the server log "
            "(or set AE_ADMIN_EMAIL / AE_ADMIN_PASSWORD in .env and restart).")
    tok = _setup_token()
    with st.form("setup_admin"):
        email = st.text_input("Administrator e-mail")
        name = st.text_input("Name", value="Administrator")
        pw = st.text_input("Password (min 8 characters)", type="password")
        pw2 = st.text_input("Repeat password", type="password")
        given = st.text_input("Setup token")
        if st.form_submit_button("Create administrator", type="primary"):
            if given.strip() != tok:
                st.error("Wrong setup token.")
            elif pw != pw2:
                st.error("Passwords do not match.")
            else:
                try:
                    u = master.create_user(email, pw, name=name, role="admin", status="active")
                except ValueError as e:
                    st.error(str(e))
                else:
                    master.audit("bootstrap_admin", u, target=u.email, detail="setup form")
                    try:
                        (config.DATA_DIR / "setup_token.txt").unlink()   # one-time use
                    except OSError:
                        pass
                    _login(u)
                    st.rerun()


# --------------------------------------------------------------------------- #
# session resolution
# --------------------------------------------------------------------------- #
_KEEP_ON_RESET = ("ae_cookie_clear",)


def reset_session_state(keep: tuple[str, ...] = _KEEP_ON_RESET) -> None:
    """Drop everything this browser tab remembered (read bills, photos, export bytes, filters …).
    Called on login and logout so that nothing from one user is ever shown to the next person who
    signs in on the same browser."""
    for k in list(st.session_state.keys()):
        if k not in keep:
            try:
                del st.session_state[k]
            except Exception:  # noqa: BLE001
                pass


def _login(u: master.User) -> None:
    reset_session_state()
    ua = ""
    try:
        ua = st.context.headers.get("User-Agent", "")
    except Exception:  # noqa: BLE001
        pass
    tok = master.create_session(u.id, ua)
    st.session_state["ae_user_id"] = u.id
    st.session_state["ae_token"] = tok
    # The cookie is written by a tiny script in the page. A st.rerun() right after would discard it,
    # so it is rendered at the top of the following runs (flush_cookie) until the browser has it.
    st.session_state["ae_cookie_pending"] = tok
    master.audit("login", u, target=u.email)


def logout() -> None:
    tok = st.session_state.get("ae_token") or cookie_token()
    uid = st.session_state.get("ae_user_id")
    if tok:
        master.revoke_session(tok)
    if uid:
        u = master.get_user(uid)
        if u:
            master.audit("logout", u, target=u.email)
    reset_session_state(keep=())
    st.session_state["ae_cookie_clear"] = True
    try:
        if getattr(st, "user", None) is not None and getattr(st.user, "is_logged_in", False):
            st.logout()
    except Exception:  # noqa: BLE001
        pass


def current_user() -> Optional[master.User]:
    """The logged-in user for this run, from session state or the session cookie."""
    uid = st.session_state.get("ae_user_id")
    tok = st.session_state.get("ae_token")
    if uid and tok:
        u = master.user_for_session(tok)
        if u and u.id == uid:
            return u
        st.session_state.pop("ae_user_id", None)
        st.session_state.pop("ae_token", None)
    tok = cookie_token()
    if tok:
        u = master.user_for_session(tok)
        if u:
            if st.session_state.get("ae_state_owner") not in (None, u.id):
                reset_session_state()
            st.session_state["ae_user_id"] = u.id
            st.session_state["ae_token"] = tok
            return u
    return None


def google_configured() -> bool:
    try:
        auth = st.secrets.get("auth", {})  # type: ignore[attr-defined]
        return bool(auth and (auth.get("client_id") or (auth.get("google") or {}).get("client_id")))
    except Exception:  # noqa: BLE001
        return False


def _google_return(features: dict) -> None:
    """After st.login('google') Streamlit exposes st.user; map it to one of our accounts."""
    try:
        info = st.user
        if not getattr(info, "is_logged_in", False):
            return
        email = getattr(info, "email", None)
        sub = getattr(info, "sub", None) or getattr(info, "id", None) or email
        name = getattr(info, "name", "") or ""
        verified = bool(getattr(info, "email_verified", False))
    except Exception:  # noqa: BLE001
        return
    u, msg = master.login_google(email or "", str(sub or ""), name, auto_signup=features.get("signup", True),
                                 email_verified=verified)
    if u:
        _login(u)
        st.rerun()
    else:
        st.warning(msg)
        try:
            st.logout()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
def login_page(cust: dict) -> None:
    feats = cust.get("features", {})
    st.title(f"🔐 {cust.get('app_name') or config.BUSINESS_NAME} — sign in")
    if cust.get("announcement"):
        st.info(cust["announcement"])
    tabs = ["Sign in"] + (["Create account"] if feats.get("signup", True) else [])
    tab_objs = st.tabs(tabs)
    with tab_objs[0]:
        with st.form("login_form"):
            email = st.text_input("E-mail", autocomplete="username")
            pw = st.text_input("Password", type="password", autocomplete="current-password")
            ok = st.form_submit_button("Sign in", type="primary", width="stretch")
        if ok:
            ip = client_ip()
            if not master.throttle_check("login", ip):
                st.error("Too many failed attempts from your connection – wait 15 minutes and try again.")
            else:
                u, msg = master.authenticate(email, pw, client=ip)
                if u:
                    _login(u)
                    st.rerun()
                else:
                    master.throttle_hit("login", ip)
                    master.audit("login_failed", None, target=email.strip().lower()[:120], detail=f"{msg} [{ip}]")
                    st.error(msg)
        if feats.get("google_login", True) and google_configured():
            st.caption("or")
            if st.button("Continue with Google", width="stretch", key="login_google"):
                st.login("google")
            _google_return(feats)
        st.caption("Forgot your password? Ask the administrator to reset it" + (f" — {cust['support_contact']}" if cust.get("support_contact") else "."))
    if len(tab_objs) > 1:
        with tab_objs[1]:
            st.caption("Your account becomes usable after the administrator approves it and activates a plan.")
            with st.form("signup_form"):
                name = st.text_input("Your name")
                business = st.text_input("Business name")
                phone = st.text_input("Phone / WhatsApp")
                email = st.text_input("E-mail")
                pw = st.text_input("Password (min 8 characters)", type="password")
                pw2 = st.text_input("Repeat password", type="password")
                if st.form_submit_button("Create account", type="primary"):
                    ip = client_ip()
                    if pw != pw2:
                        st.error("Passwords do not match.")
                    elif not master.throttle_check("signup", ip):
                        st.error("Too many sign-ups from your connection – try again later or contact the administrator.")
                    else:
                        try:
                            u = master.create_user(email, pw, name=name, status="pending", business_name=business, phone=phone,
                                                   signup_source="self")
                        except ValueError as e:
                            st.error(str(e))
                        else:
                            master.throttle_hit("signup", ip)
                            master.audit("signup", u, target=u.email, detail=f"{business} [{ip}]")
                            st.success("Account created. You will be able to sign in once the administrator approves it.")


def gate() -> Optional[master.User]:
    """Run the login flow. Returns the active user, or None (login UI was rendered instead)."""
    master.init()
    flush_cookie()
    if not master.admin_exists():
        bootstrap_admin_from_env()
    if not master.admin_exists():
        _first_run_setup()
        return None
    u = current_user()
    if u:
        if st.session_state.get("ae_state_owner") != u.id:
            if "ae_state_owner" in st.session_state:   # tab served someone else before
                reset_session_state()
                st.session_state["ae_user_id"], st.session_state["ae_token"] = u.id, st.session_state.get("ae_token") or cookie_token()
            st.session_state["ae_state_owner"] = u.id
        return u
    cust = master.effective_customization(None)
    login_page(cust)
    return None

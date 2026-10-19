"""Apply the administrator's customization (branding, CSS, labels, banner) to the current page."""
from __future__ import annotations

import html
import re

import streamlit as st

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def apply(cust: dict) -> None:
    css = []
    color = (cust.get("primary_color") or "").strip()
    if _HEX.match(color):
        css.append(f":root {{ --primary-color: {color}; }} .stButton > button[kind='primary'] {{ background-color: {color}; border-color: {color}; }}")
    extra = (cust.get("custom_css") or "").strip()
    if extra:
        # keep it to CSS: no tags, no javascript: urls
        extra = extra.replace("</", "<\\/")
        if "javascript:" not in extra.lower() and "<script" not in extra.lower():
            css.append(extra[:20000])
    if css:
        st.markdown("<style>" + "\n".join(css) + "</style>", unsafe_allow_html=True)
    if cust.get("announcement"):
        st.info(cust["announcement"], icon="📣")


def sidebar_brand(cust: dict, fallback: str) -> None:
    logo = (cust.get("logo_url") or "").strip()
    if logo.startswith("https://") or logo.startswith("http://"):
        try:
            st.image(logo, width=140)
        except Exception:  # noqa: BLE001
            pass
    st.markdown(f"## 🧾 {html.escape(cust.get('app_name') or fallback)}")


def label(cust: dict, name: str) -> str:
    """Custom label for a page/tab name if the admin set one."""
    lab = (cust.get("labels") or {}).get(name)
    return str(lab)[:60] if lab else name

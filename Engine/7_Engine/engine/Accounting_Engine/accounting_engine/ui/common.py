"""Shared UI helpers."""
from __future__ import annotations

import math
from typing import Any, Optional

import pandas as pd
import streamlit as st

from engine.models import LineItem

ITEM_COLS = ["item", "qty", "unit", "weight_qtl", "rate", "amount", "struck_out", "note"]

ITEM_COLUMN_CONFIG = {
    "item": st.column_config.TextColumn("Item", required=True, width="medium"),
    "qty": st.column_config.NumberColumn("Qty", min_value=0, step=1, format="%g"),
    "unit": st.column_config.SelectboxColumn("Unit", options=["Bag", "Pkt", "Tin", "Kg", "Ltr", "Qtl", "Pcs", "Box"], width="small"),
    "weight_qtl": st.column_config.NumberColumn("Weight (Qtl)", min_value=0, step=0.01, format="%.2f"),
    "rate": st.column_config.NumberColumn("Rate ₹", min_value=0, step=1, format="%.2f"),
    "amount": st.column_config.NumberColumn("Amount ₹", min_value=0, step=1, format="%.2f",
                                            help="Leave blank to auto-calculate (weight × rate, else qty × rate)"),
    "struck_out": st.column_config.CheckboxColumn("Struck", help="Line crossed out on the bill – excluded from total"),
    "note": st.column_config.TextColumn("Note", width="medium"),
}


_NUM_COLS = ("qty", "weight_qtl", "rate", "amount")
_TEXT_COLS = ("item", "unit", "note")


def items_to_df(items: list[LineItem]) -> pd.DataFrame:
    rows = [it.model_dump() for it in items]
    df = pd.DataFrame(rows, columns=ITEM_COLS)
    # Explicit dtypes: an empty frame would otherwise get int64 number columns (a decimal weight
    # typed into the editor could then not be stored) and object text columns.
    for c in _TEXT_COLS:
        df[c] = df[c].astype(object).where(df[c].notna(), None)
    df["struck_out"] = df["struck_out"].fillna(False).astype(bool)
    for c in _NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df


def _missing(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _clean(v: Any) -> Optional[float]:
    if _missing(v):
        return None
    try:
        if isinstance(v, str):
            v = v.strip().replace(",", "")
            if not v:
                return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _clean_text(v: Any) -> Optional[str]:
    if _missing(v):
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none", "<na>") else None


def _clean_bool(v: Any) -> bool:
    if _missing(v):
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    try:
        return bool(v)
    except (TypeError, ValueError):
        return False


def df_to_items(df: pd.DataFrame) -> list[LineItem]:
    items: list[LineItem] = []
    for _, r in df.iterrows():
        name = _clean_text(r.get("item"))
        if not name:
            continue
        items.append(
            LineItem(
                item=name,
                qty=_clean(r.get("qty")),
                unit=_clean_text(r.get("unit")),
                weight_qtl=_clean(r.get("weight_qtl")),
                rate=_clean(r.get("rate")),
                amount=_clean(r.get("amount")),
                struck_out=_clean_bool(r.get("struck_out")),
                note=_clean_text(r.get("note")),
            )
        )
    return items


def remembered(card: dict, key: str, default: Any) -> Any:
    """Initial value for a review-card widget: what the user last set, else ``default``.

    Streamlit drops the state of every widget that is not rendered in a run (e.g. while the user
    is on another page). The cards persist in session state, so each card keeps its own copy of
    the widget values (``card["edits"]``) and re-seeds the widgets from it when they come back."""
    return card.setdefault("edits", {}).get(key, default)


def remember(card: dict, key: str, value: Any) -> Any:
    card.setdefault("edits", {})[key] = value
    return value


def money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"₹ {v:,.2f}"


def toast_ok(msg: str) -> None:
    try:
        st.toast(msg, icon="✅")
    except Exception:  # pragma: no cover
        st.success(msg)


_MIME = {
    ".xml": "application/xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".zip": "application/zip",
}


def download_buttons(files: dict[str, bytes], key_prefix: str, zip_name: str | None = None) -> None:
    """One download button per file (+ a zip of everything when there are several)."""
    if not files:
        return
    names = list(files)
    cols = st.columns(min(4, len(names) + (1 if len(names) > 1 else 0)))
    n_xlsx = sum(1 for n in names if n.lower().endswith(".xlsx"))
    for i, name in enumerate(names):
        ext = name[name.rfind("."):].lower()
        label = {".xml": "XML", ".xlsx": "Excel", ".csv": "CSV"}.get(ext, ext.strip("."))
        if name.endswith("_masters.xml"):
            label = "Masters XML"
        elif name.endswith("_TallyTemplate.xlsx"):
            label = "Excel (Tally template)"
        elif ext == ".xlsx" and n_xlsx > 1:  # two plain "Excel" buttons would be indistinguishable
            label = "Excel (ledger masters)" if name.startswith("Tally_Ledgers") else "Excel (row layout)"
        cols[i % len(cols)].download_button(f"⬇️ {label}", data=files[name], file_name=name,
                                            mime=_MIME.get(ext, "application/octet-stream"),
                                            key=f"{key_prefix}_{i}", width="stretch")
    if len(names) > 1:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, d in files.items():
                z.writestr(n, d)
        cols[len(names) % len(cols)].download_button("⬇️ All (zip)", data=buf.getvalue(),
                                                     file_name=zip_name or "tally_export.zip", mime="application/zip",
                                                     key=f"{key_prefix}_zip", width="stretch")

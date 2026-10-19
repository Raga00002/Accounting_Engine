"""Arithmetic / completeness checks on a BillDraft before it is saved."""
from __future__ import annotations

from typing import Optional

from . import config
from .models import BillDraft, LineItem, ValidationReport


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(config.AMOUNT_TOLERANCE, 0.005 * max(abs(a), abs(b)))


def fill_missing_amounts(draft: BillDraft) -> BillDraft:
    """Compute a missing line amount from weight x rate (or qty x rate). Mutates and returns draft."""
    for it in draft.items:
        if it.amount is None and it.computed_amount is not None:
            it.amount = it.computed_amount
    return draft


def validate(draft: BillDraft, party_resolved: bool, require_bill_no: bool = True) -> ValidationReport:
    rep = ValidationReport()

    if not party_resolved:
        rep.missing.append("Party (customer/supplier) is not identified.")
    if draft.bill_date is None:
        rep.missing.append("Bill date is missing.")
    if require_bill_no and not (draft.bill_no or "").strip():
        rep.missing.append("Bill number is missing.")

    active = draft.active_items
    if not active:
        rep.missing.append("No line items (or all lines are struck out).")

    for n, it in enumerate(draft.items, start=1):
        if it.struck_out:
            continue
        label = f"Line {n} ({it.item})"
        if it.effective_amount is None:
            rep.missing.append(f"{label}: amount cannot be determined – need rate and qty/weight, or the amount.")
            continue
        comp = it.computed_amount
        if it.amount is not None and comp is not None and not _close(it.amount, comp):
            rep.warnings.append(
                f"{label}: written amount {it.amount:,.2f} ≠ {'weight' if it.weight_qtl is not None else 'qty'} × rate = {comp:,.2f}."
            )
        for fld in ("qty", "weight_qtl", "rate", "amount"):
            v = getattr(it, fld)
            if v is not None and v < 0:
                rep.warnings.append(f"{label}: negative {fld} ({v}).")

    rep.computed_total = draft.computed_total
    rep.total_written = draft.total_written
    if rep.computed_total is not None and draft.total_written is not None and not _close(
        rep.computed_total, draft.total_written
    ):
        rep.warnings.append(
            f"Written total {draft.total_written:,.2f} ≠ sum of lines {rep.computed_total:,.2f} "
            f"(difference {draft.total_written - rep.computed_total:,.2f}). Check struck-out lines / OCR."
        )
    return rep


def final_amount(draft: BillDraft, prefer_written: bool = False) -> Optional[float]:
    """Amount to book. Computed sum of lines is the default; written total only if asked or if lines are incomplete."""
    comp = draft.computed_total
    if prefer_written and draft.total_written is not None:
        return draft.total_written
    if comp is not None:
        return comp
    return draft.total_written

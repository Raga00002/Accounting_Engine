"""Online recharge through Razorpay Payment Links.

Why payment links: they need no webhook endpoint (Streamlit cannot host one) – the user pays on
Razorpay's hosted page, comes back and clicks "Check payment"; we fetch the link status from the
API and, when it is *paid*, activate the plan. Keys are stored encrypted in the master database
(Settings → Payments, admin only). Nothing here runs unless both keys are set.
"""
from __future__ import annotations

import json
from typing import Optional

from . import master, secrets

API = "https://api.razorpay.com/v1"


class PaymentError(RuntimeError):
    pass


def keys(path=None) -> tuple[Optional[str], Optional[str]]:
    kid = secrets.decrypt(master.get_setting("razorpay_key_id_enc", None, None, path))
    ksec = secrets.decrypt(master.get_setting("razorpay_key_secret_enc", None, None, path))
    return (kid or None), (ksec or None)


def configured(path=None) -> bool:
    kid, ksec = keys(path)
    return bool(kid and ksec)


def set_keys(key_id: str, key_secret: str, path=None) -> None:
    master.set_setting("razorpay_key_id_enc", secrets.encrypt(key_id.strip()) if key_id.strip() else None, path=path)
    master.set_setting("razorpay_key_secret_enc", secrets.encrypt(key_secret.strip()) if key_secret.strip() else None, path=path)


def _session():
    import requests

    kid, ksec = keys()
    if not (kid and ksec):
        raise PaymentError("Online payment is not configured. Use the manual recharge instructions.")
    s = requests.Session()
    s.auth = (kid, ksec)
    s.headers["Content-Type"] = "application/json"
    return s


def create_link(user: master.User, plan: dict, callback_url: Optional[str] = None, path=None) -> dict:
    """Create a payment link for ``plan`` and record it. Returns the payments row (id, short_url…)."""
    amount = float(plan["price_inr"])
    if amount <= 0:
        raise PaymentError("This plan is free – ask the administrator to activate it.")
    payload = {
        "amount": int(round(amount * 100)),
        "currency": "INR",
        "accept_partial": False,
        "description": f"{plan['name']} – {plan.get('description') or ''}".strip(" –"),
        "customer": {"name": user.name or user.email, "email": user.email, **({"contact": user.phone} if user.phone else {})},
        "notify": {"sms": bool(user.phone), "email": True},
        "reminder_enable": True,
        "notes": {"user_id": str(user.id), "plan": plan["name"]},
        "reference_id": f"u{user.id}-{plan['name'][:20]}-{secrets.new_token(4)}",
    }
    if callback_url:
        payload["callback_url"] = callback_url
        payload["callback_method"] = "get"
    try:
        r = _session().post(f"{API}/payment_links", data=json.dumps(payload), timeout=30)
    except Exception as e:  # noqa: BLE001
        raise PaymentError(f"Could not reach Razorpay: {e}") from e
    if r.status_code >= 300:
        raise PaymentError(f"Razorpay refused the request ({r.status_code}): {r.text[:300]}")
    data = r.json()
    row_id = master.record_payment(user.id, plan["name"], amount, data.get("id", ""), data.get("short_url", ""),
                                   raw=json.dumps(data)[:4000], plan_days=int(plan["days"]),
                                   plan_quota=plan.get("scan_quota"), path=path)
    return {"id": row_id, "link_id": data.get("id"), "short_url": data.get("short_url"), "amount_inr": amount}


def fetch_link(link_id: str) -> dict:
    try:
        r = _session().get(f"{API}/payment_links/{link_id}", timeout=30)
    except Exception as e:  # noqa: BLE001
        raise PaymentError(f"Could not reach Razorpay: {e}") from e
    if r.status_code >= 300:
        raise PaymentError(f"Razorpay error ({r.status_code}): {r.text[:300]}")
    return r.json()


def sync_payment(payment_row: dict, path=None) -> str:
    """Refresh one pending payment from Razorpay; activate the plan when paid. Returns the new status.

    Safe to call repeatedly / concurrently: the row is claimed atomically before the plan is added,
    and the paid amount + currency are checked against what the link was created for."""
    if payment_row["status"] == "paid":
        return "paid"
    data = fetch_link(payment_row["link_id"])
    status = data.get("status", "created")
    if status == "paid":
        expected = int(round(float(payment_row["amount_inr"]) * 100))
        paid = int(data.get("amount_paid") or data.get("amount") or 0)
        if data.get("currency", "INR") != "INR" or paid < expected:
            master.mark_payment(payment_row["id"], "failed", raw=json.dumps(data)[:4000], path=path)
            master.audit("payment_mismatch", None, target=f"user:{payment_row['user_id']}",
                         detail=f"expected {expected} paise INR, got {paid} {data.get('currency')}", path=path)
            raise PaymentError("The amount Razorpay reports does not match this plan – the administrator has been notified.")
        if not master.claim_payment(payment_row["id"], path=path):
            return "paid"   # another request is activating / has activated it
        pays = data.get("payments") or []
        pid = pays[0].get("payment_id") if pays else None
        try:
            days = payment_row.get("plan_days")
            quota = payment_row.get("plan_quota")
            already = master.find_subscription(payment_row["user_id"], "razorpay", pid or payment_row["link_id"], path=path)
            if already:   # a previous confirmation added the period but could not mark the row – never add twice
                sub_id = already
            elif days:   # snapshot from link creation – survives a renamed / deleted plan
                sub_id = master.add_subscription(payment_row["user_id"], payment_row["plan_name"], source="razorpay",
                                                 reference=pid or payment_row["link_id"], note="Online payment",
                                                 days=int(days), scan_quota=quota, path=path)
            else:
                sub_id = master.add_subscription(payment_row["user_id"], payment_row["plan_name"], source="razorpay",
                                                 reference=pid or payment_row["link_id"], note="Online payment", path=path)
        except Exception as e:  # noqa: BLE001 - keep the payment pending so it can be retried / fixed by the admin
            master.mark_payment(payment_row["id"], "created", raw=json.dumps(data)[:4000], path=path)
            master.audit("payment_activation_failed", None, target=f"user:{payment_row['user_id']}", detail=str(e)[:300], path=path)
            raise PaymentError(f"Payment received but the plan could not be activated ({e}). "
                               "Ask the administrator to add the plan manually.") from e
        master.mark_payment(payment_row["id"], "paid", payment_id=pid, raw=json.dumps(data)[:4000],
                            subscription_id=sub_id, path=path)
        master.audit("payment_paid", None, target=f"user:{payment_row['user_id']}",
                     detail=f"{payment_row['plan_name']} ₹{payment_row['amount_inr']} {pid}", path=path)
        return "paid"
    if status in ("expired", "cancelled"):
        master.mark_payment(payment_row["id"], status, raw=json.dumps(data)[:4000], path=path)
    return status

from datetime import date, timedelta

import pytest

from engine import config, master, secrets


@pytest.fixture()
def mdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    p = tmp_path / "master.db"
    master.init(p)
    return p


def test_password_hashing_and_verification():
    h = secrets.hash_password("correct horse")
    assert h.startswith("pbkdf2_sha256$600000$") and secrets.verify_password("correct horse", h)
    assert not secrets.verify_password("wrong", h) and not secrets.verify_password("x", None)
    with pytest.raises(ValueError):
        secrets.hash_password("short")


def test_encrypt_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.delenv("AE_SECRET_KEY", raising=False)
    tok = secrets.encrypt("sk-ant-secret")
    assert tok != "sk-ant-secret" and secrets.decrypt(tok) == "sk-ant-secret"
    assert (tmp_path / "master.key").exists()
    assert secrets.decrypt("garbage") is None and secrets.encrypt("") is None
    monkeypatch.setenv("AE_SECRET_KEY", "another-key")
    assert secrets.decrypt(tok) is None  # different server key -> unreadable, not a crash


def test_users_auth_sessions_lockout(mdb):
    assert master.user_count(mdb) == 0 and not master.admin_exists(mdb)
    admin = master.create_user("Admin@Example.com", "adminpass123", "Boss", role="admin", path=mdb)
    assert admin.email == "admin@example.com" and admin.is_admin and master.admin_exists(mdb)
    with pytest.raises(ValueError):
        master.create_user("admin@example.com", "x" * 10, path=mdb)  # duplicate
    with pytest.raises(ValueError):
        master.create_user("not-an-email", "x" * 10, path=mdb)
    u = master.create_user("k@example.com", "userpass123", "Keshav", status="pending", path=mdb)
    assert master.authenticate("k@example.com", "userpass123", path=mdb)[0] is None  # pending
    master.update_user(u.id, status="active", path=mdb)
    user, msg = master.authenticate("k@example.com", "userpass123", path=mdb)
    assert user and user.id == u.id and msg == "ok"
    # lockout after repeated failures
    for _ in range(master.LOCK_AFTER_FAILURES):
        assert master.authenticate("k@example.com", "bad", path=mdb)[0] is None
    _, msg = master.authenticate("k@example.com", "userpass123", path=mdb)
    assert "Too many" in msg
    # sessions
    tok = master.create_session(u.id, "UA", path=mdb)
    assert master.user_for_session(tok, mdb).id == u.id
    assert master.user_for_session("short", mdb) is None and master.user_for_session(tok + "x", mdb) is None
    master.revoke_session(tok, mdb)
    assert master.user_for_session(tok, mdb) is None
    tok2 = master.create_session(u.id, path=mdb)
    master.update_user(u.id, status="disabled", path=mdb)  # disabling revokes sessions
    assert master.user_for_session(tok2, mdb) is None
    # the only admin cannot be demoted / disabled / deleted
    with pytest.raises(ValueError):
        master.update_user(admin.id, role="user", path=mdb)
    with pytest.raises(ValueError):
        master.update_user(admin.id, status="disabled", path=mdb)
    with pytest.raises(ValueError):
        master.delete_user(admin.id, path=mdb)
    # own api key stored encrypted and honoured only when allowed
    master.update_user(u.id, status="active", path=mdb)
    master.set_own_api_key(u.id, "sk-ant-user", mdb)
    assert master.get_own_api_key(u.id, mdb) == "sk-ant-user"
    master.update_user(u.id, allow_own_key=False, path=mdb)
    assert master.get_own_api_key(u.id, mdb) is None
    with master.connect(mdb) as c:
        assert "sk-ant" not in (c.execute("SELECT own_api_key FROM users WHERE id=?", (u.id,)).fetchone()[0] or "")


def test_google_login_flow(mdb):
    # unverified e-mail: never creates or links anything
    user, msg = master.login_google("new@example.com", "sub1", "New", auto_signup=True, path=mdb)
    assert user is None and "not verified" in msg and master.get_user_by_email("new@example.com", mdb) is None
    user, msg = master.login_google("new@example.com", "sub1", "New", auto_signup=True, email_verified=True, path=mdb)
    assert user is None and "approval" in msg
    pending = master.get_user_by_email("new@example.com", mdb)
    assert pending.status == "pending" and pending.google_sub == "sub1"
    master.update_user(pending.id, status="active", path=mdb)
    user, msg = master.login_google("new@example.com", "sub1", path=mdb)   # known sub: no e-mail check needed
    assert user and msg == "ok"
    # an admin-created password account is linked by (verified) e-mail
    pw = master.create_user("pw@example.com", "password123", path=mdb)
    user, _ = master.login_google("pw@example.com", "sub2", email_verified=True, path=mdb)
    assert user.id == pw.id and master.get_user(pw.id, mdb).google_sub == "sub2"
    # a different Google account with the same e-mail is refused
    assert "different Google" in master.login_google("pw@example.com", "sub9", email_verified=True, path=mdb)[1]
    # a self-registered password account is never taken over by a Google login
    selfu = master.create_user("self@example.com", "password123", status="active", signup_source="self", path=mdb)
    user, msg = master.login_google("self@example.com", "sub4", email_verified=True, path=mdb)
    assert user is None and "registered with a password" in msg and master.get_user(selfu.id, mdb).google_sub is None
    assert master.login_google("nobody@example.com", "sub3", auto_signup=False, email_verified=True, path=mdb)[0] is None


def test_sessions_are_hashed_and_expire(mdb):
    u = master.create_user("s2@example.com", "password123", path=mdb)
    tok = master.create_session(u.id, "UA", path=mdb)
    assert master.user_for_session(tok, mdb).id == u.id
    with master.connect(mdb) as c:
        row = c.execute("SELECT token, expires_at FROM sessions").fetchone()
    assert row["token"] != tok and len(row["token"]) == 64            # sha256 hex, never the token itself
    assert "T" not in row["expires_at"]                                  # SQLite UTC format, comparable to datetime('now')
    tok2 = master.create_session(u.id, "UA2", path=mdb)
    master.revoke_all_sessions(u.id, except_token=tok2, path=mdb)
    assert master.user_for_session(tok, mdb) is None and master.user_for_session(tok2, mdb).id == u.id
    expired = master.create_session(u.id, "UA3", days=-1, path=mdb)
    assert master.user_for_session(expired, mdb) is None


def test_account_lock_is_per_client(mdb):
    master._ip_events.clear()
    master.create_user("lock@example.com", "password123", path=mdb)
    for _ in range(master.LOCK_AFTER_FAILURES):
        assert master.authenticate("lock@example.com", "wrong", client="attacker", path=mdb)[0] is None
    u, msg = master.authenticate("lock@example.com", "password123", client="attacker", path=mdb)
    assert u is None and "Too many failed attempts" in msg
    u, msg = master.authenticate("lock@example.com", "password123", client="owner-home", path=mdb)   # slowed down, not locked
    assert u is not None and msg == "ok"
    master._ip_events.clear()


def test_login_throttle_per_client():
    master._ip_events.clear()
    for _ in range(master.IP_MAX_FAILURES):
        assert master.throttle_check("login", "1.2.3.4")
        master.throttle_hit("login", "1.2.3.4")
    assert not master.throttle_check("login", "1.2.3.4") and master.throttle_check("login", "5.6.7.8")
    master._ip_events.clear()


def test_subscriptions_quota_and_metering(mdb):
    u = master.create_user("s@example.com", "password123", path=mdb)
    acc = master.access_for(u, mdb)
    assert not acc.allowed and "recharge" in acc.reason
    sid = master.add_subscription(u.id, "Trial 7 days", created_by=None, path=mdb)
    acc = master.access_for(u, mdb)
    assert acc.allowed and acc.plan == "Trial 7 days" and acc.scans_left == 25 and acc.days_left == 7
    for _ in range(25):
        master.consume_scan(u, "bill_image", "m", 100, 50, 0.01, path=mdb)
    acc = master.access_for(u, mdb)
    assert not acc.allowed and acc.scans_left == 0 and "quota" in acc.reason
    # own-key scans are logged but do not consume quota
    master.consume_scan(u, "bill_image", "m", own_key=True, path=mdb)
    assert master.access_for(u, mdb).scans_left == 0
    # a recharge after an exhausted period starts today and takes over immediately
    master.add_subscription(u.id, "Monthly", source="manual", reference="UPI 123", path=mdb)
    acc = master.access_for(u, mdb)
    assert acc.allowed and acc.plan == "Monthly" and acc.scans_left == 500
    assert date.fromisoformat(master.list_subscriptions(u.id, mdb)[0]["start_date"]) == date.today()
    # a recharge while a usable period is running stacks after it
    master.add_subscription(u.id, "Quarterly", path=mdb)
    subs = master.list_subscriptions(u.id, mdb)
    q = next(x for x in subs if x["plan_name"] == "Quarterly")
    assert date.fromisoformat(q["start_date"]) == date.today() + timedelta(days=30)
    assert master.access_for(u, mdb).plan == "Monthly"
    # admins are never gated
    admin = master.create_user("a@example.com", "password123", role="admin", path=mdb)
    assert master.access_for(admin, mdb).allowed
    summ = master.usage_summary(u.id, path=mdb)
    assert sum(r["n"] for r in summ) == 26
    assert master.stats(mdb)["active_subs"] == 1


def test_settings_and_customization(mdb):
    u = master.create_user("c@example.com", "password123", path=mdb)
    master.set_setting("app_name", "Bill Reader", path=mdb)
    master.set_setting("features_json", '{"voice": false}', path=mdb)
    master.set_setting("app_name", "Keshav's Bills", user_id=u.id, path=mdb)
    master.set_setting("features_json", '{"voice": true, "bank_slips": false, "bogus": true}', user_id=u.id, path=mdb)
    g = master.effective_customization(None, mdb)
    assert g["app_name"] == "Bill Reader" and g["features"]["voice"] is False and g["features"]["bank_slips"] is True
    p = master.effective_customization(u.id, mdb)
    assert p["app_name"] == "Keshav's Bills" and p["features"] == {**master.DEFAULT_FEATURES, "voice": True, "bank_slips": False}
    other = master.create_user("d@example.com", "password123", path=mdb)
    assert master.effective_customization(other.id, mdb)["app_name"] == "Bill Reader"  # per-user override is private
    master.set_setting("labels_json", "not json", path=mdb)
    assert master.effective_customization(None, mdb)["labels"] == {}
    master.set_setting("app_name", "", user_id=u.id, path=mdb)  # blank removes the override
    assert master.effective_customization(u.id, mdb)["app_name"] == "Bill Reader"
    master.audit("test", u, "x", "y", path=mdb)
    assert master.list_audit(path=mdb)[0]["action"] == "test"


def test_reserve_scan_is_atomic_under_concurrency(mdb):
    """quota 2, 8 threads racing: exactly 2 get a slot, a failed scan gives its slot back."""
    import threading

    u = master.create_user("race@example.com", "password123", path=mdb)
    master.upsert_plan("Two", 30, 2, 10, path=mdb)
    master.add_subscription(u.id, "Two", path=mdb)
    results: list = []
    gate = threading.Barrier(8)

    def worker():
        gate.wait()
        results.append(master.reserve_scan(u, path=mdb))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = [r for e, r in results if e is None]
    refused = [e for e, r in results if e]
    assert len(got) == 2 and len(refused) == 6 and all("quota" in e for e in refused)
    assert master.list_subscriptions(u.id, mdb)[0]["scans_used"] == 2
    # a scan that fails after reserving releases its slot; a successful one keeps it
    master.consume_scan(u, "bill_image", "", ok=False, reservation=got[0], path=mdb)
    assert master.list_subscriptions(u.id, mdb)[0]["scans_used"] == 1
    master.consume_scan(u, "bill_image", "m", 10, 5, 0.0, ok=True, reservation=got[1], path=mdb)
    assert master.list_subscriptions(u.id, mdb)[0]["scans_used"] == 1
    # own key: needs a period, never counted
    err, res = master.reserve_scan(u, own_key=True, path=mdb)
    assert err is None and not res.counted
    # admins are never gated
    adm = master.create_user("a@example.com", "password123", role="admin", path=mdb)
    assert master.reserve_scan(adm, path=mdb)[0] is None


def test_runtime_scan_context_releases_on_failure(mdb):
    from engine import runtime
    from engine.extractor import ExtractionError

    u = master.create_user("rt@example.com", "password123", path=mdb)
    master.upsert_plan("One", 30, 1, 10, path=mdb)
    master.add_subscription(u.id, "One", path=mdb)
    rt = runtime.Runtime(user_id=u.id,
                         before_scan=lambda kind, own: master.reserve_scan(u, own_key=own, path=mdb),
                         on_usage=lambda k, m, i, o, c, own, ok, res: master.consume_scan(u, k, m, i, o, c, own_key=own, ok=ok, reservation=res, path=mdb))
    tok = runtime.activate(rt)
    try:
        with pytest.raises(RuntimeError):
            with runtime.scan("bill_image"):
                raise RuntimeError("network down")
        assert master.list_subscriptions(u.id, mdb)[0]["scans_used"] == 0   # slot given back
        with runtime.scan("bill_image") as sc:
            sc.record("m", 10, 5)
        assert master.list_subscriptions(u.id, mdb)[0]["scans_used"] == 1
        with pytest.raises(ExtractionError):
            with runtime.scan("bill_image"):
                pass
        rows = master.usage_summary(u.id, path=mdb)
        assert rows and sum(r["n"] for r in rows) == 2   # one failed + one ok attempt logged (the refused one is not)
    finally:
        runtime.deactivate(tok)


def test_payment_confirmation_idempotent_and_verified(mdb, monkeypatch):
    import threading

    from engine import payments

    u = master.create_user("pay@example.com", "password123", path=mdb)
    plan = master.get_plan("Monthly", mdb)
    rid = master.record_payment(u.id, "Monthly", plan["price_inr"], "plink_1", "https://rzp.io/x",
                                plan_days=plan["days"], plan_quota=plan["scan_quota"], path=mdb)
    row = master.list_payments(u.id, mdb)[0]
    paid = {"status": "paid", "amount": 99900, "amount_paid": 99900, "currency": "INR",
            "payments": [{"payment_id": "pay_1"}]}
    monkeypatch.setattr(payments, "fetch_link", lambda link_id: {**paid, "payments": [{"payment_id": "pay_" + link_id}]})
    # 4 concurrent "Check payment" clicks -> exactly one subscription
    gate = threading.Barrier(4)
    out: list = []

    def w():
        gate.wait()
        try:
            out.append(payments.sync_payment(row, path=mdb))
        except Exception as e:  # noqa: BLE001
            out.append(repr(e))

    ts = [threading.Thread(target=w) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == ["paid"] * 4, out
    assert len(master.list_subscriptions(u.id, mdb)) == 1
    assert master.list_payments(u.id, mdb)[0]["status"] == "paid"
    # a renamed plan: the snapshot on the payment row still activates the right period
    master.upsert_plan("Monthly-old", 30, 500, 999, plan_id=plan["id"], path=mdb)
    rid2 = master.record_payment(u.id, "Monthly", 999, "plink_2", "", plan_days=30, plan_quota=500, path=mdb)
    row2 = [p for p in master.list_payments(u.id, mdb) if p["id"] == rid2][0]
    assert payments.sync_payment(row2, path=mdb) == "paid"
    assert len(master.list_subscriptions(u.id, mdb)) == 2
    # crash between "period added" and "row marked paid": the re-check finds the period by reference, no duplicate
    with master.connect(mdb) as c:
        c.execute("UPDATE payments SET status='confirming', subscription_id=NULL, claimed_at=datetime('now','-10 minutes') WHERE id=?", (rid2,))
    row2b = [p for p in master.list_payments(u.id, mdb) if p["id"] == rid2][0]
    assert payments.sync_payment(row2b, path=mdb) == "paid"
    assert len(master.list_subscriptions(u.id, mdb)) == 2
    assert [p for p in master.list_payments(u.id, mdb) if p["id"] == rid2][0]["status"] == "paid"
    # a short-paid / wrong-currency link never activates anything
    rid3 = master.record_payment(u.id, "Monthly", 999, "plink_3", "", plan_days=30, plan_quota=500, path=mdb)
    row3 = [p for p in master.list_payments(u.id, mdb) if p["id"] == rid3][0]
    monkeypatch.setattr(payments, "fetch_link", lambda link_id: {**paid, "amount_paid": 100})
    with pytest.raises(payments.PaymentError):
        payments.sync_payment(row3, path=mdb)
    assert len(master.list_subscriptions(u.id, mdb)) == 2
    assert [p for p in master.list_payments(u.id, mdb) if p["id"] == rid3][0]["status"] == "failed"

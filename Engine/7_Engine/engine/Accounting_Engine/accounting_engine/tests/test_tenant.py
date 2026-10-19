"""Tenant isolation: two users never see each other's data."""
from datetime import date

from engine import bank, config, db, tenant
from engine.models import Bill, LineItem, Party


def _use(t):
    tok = tenant.activate(t)
    db.init_db()
    bank.init()
    return tok


def test_two_tenants_have_separate_databases_and_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    a, b = tenant.for_user(1).ensure_dirs(), tenant.for_user(2).ensure_dirs()
    assert a.root != b.root and a.db_path != b.db_path and a.image_dir != b.image_dir
    assert a.root.parent == tmp_path / "tenants"

    tok = _use(a)
    pa = db.upsert_party(Party(name="Das Store"))
    db.insert_bill(Bill(party_id=pa.id, bill_date=date(2026, 9, 26), bill_no="13", amount=100, drcr="Dr",
                        items=[LineItem(item="Rice", qty=1, rate=100)]))
    assert db.stats()["parties"] == 1 and db.stats()["bills"] == 1
    tenant.deactivate(tok)

    tok = _use(b)
    assert db.stats()["parties"] == 0 and db.stats()["bills"] == 0 and db.list_items() == []
    assert db.get_party_by_name("Das Store") is None
    db.upsert_party(Party(name="Mohendra"))
    tenant.deactivate(tok)

    tok = _use(a)
    names = [p.name for p in db.list_parties()]
    assert names == ["Das Store"]  # b's party never leaks into a
    tenant.deactivate(tok)

    # without an active tenant the legacy single-user layout is used
    assert tenant.current().legacy and tenant.current().db_path == config.DB_PATH


def test_tenant_id_is_sanitised():
    t = tenant.for_user("../../etc")
    assert t.root.name == "etc" and ".." not in str(t.root)
    try:
        tenant.for_user("../")
    except ValueError:
        pass
    else:
        raise AssertionError("empty sanitised id must be rejected")


def test_templates_and_images_are_per_tenant(tmp_path, monkeypatch):
    import io

    import openpyxl

    from engine import service, templates
    from engine.models import BillDraft

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    a, b = tenant.for_user(10).ensure_dirs(), tenant.for_user(11).ensure_dirs()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Accounting Voucher"
    for i, h in enumerate(["Voucher Date", "Voucher Type Name", "Ledger Name", "Ledger Amount"], 1):
        ws.cell(row=1, column=i, value=h)
    buf = io.BytesIO()
    wb.save(buf)

    tok = _use(a)
    templates.set_custom("voucher", buf.getvalue())
    assert templates.is_custom("voucher") and templates.custom_path("voucher").is_relative_to(a.root)
    p = service.save_bill_image(b"img", "x.jpg", "Das", "1")
    assert p.startswith(str(a.image_dir))
    tenant.deactivate(tok)

    tok = _use(b)
    assert not templates.is_custom("voucher")  # a's upload is invisible to b
    assert templates.info("voucher").source == "official"
    tenant.deactivate(tok)


def test_owns_path_and_strict_mode(tmp_path):
    from engine import tenant

    t = tenant.Tenant(id="7", root=tmp_path / "tenants" / "7")
    tok = tenant.activate(t)
    try:
        t.ensure_dirs()
        assert tenant.owns_path(t.image_dir / "x.jpg")
        assert not tenant.owns_path(tmp_path / "tenants" / "8" / "bill_images" / "x.jpg")
        assert not tenant.owns_path("/etc/passwd") and not tenant.owns_path(None)
        assert not tenant.owns_path(str(t.root) + "/../8/accounting.db")
    finally:
        tenant.deactivate(tok)
    # strict mode: no tenant + no resolver -> error instead of the shared folder; resolver supplies one
    tenant.configure(None, strict=True)
    try:
        import pytest

        with pytest.raises(tenant.NoTenantError):
            tenant.current()
        tenant.configure(lambda: t, strict=True)
        assert tenant.current().id == "7"
    finally:
        tenant.configure(None, strict=False)
        tenant._current.set(None)
    assert tenant.current().legacy

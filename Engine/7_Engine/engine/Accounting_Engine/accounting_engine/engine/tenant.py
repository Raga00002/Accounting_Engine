"""Per-user (tenant) data isolation.

Every user gets a private folder ``data/tenants/<user_id>/`` holding their own SQLite database,
bill images, exports and templates. Nothing in the engine addresses the shared data folder
directly any more – it asks ``tenant.current()`` – so a request can only ever touch the folder
of the user it was activated for. A read/write for another user is impossible by construction
(different database file), not merely filtered by a WHERE clause.

The active tenant is a ``contextvars.ContextVar``: Streamlit runs each session's script in its own
thread, and ``activate()`` is called at the top of every script run after the user is resolved.
Single-user mode (no accounts configured) uses the legacy layout under ``data/`` unchanged.
"""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import config

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Tenant:
    id: str
    root: Path
    legacy: bool = False  # True = the original single-user layout (data/accounting.db, data/bill_images …)

    @property
    def db_path(self) -> Path:
        return config.DB_PATH if self.legacy else self.root / "accounting.db"

    @property
    def image_dir(self) -> Path:
        return config.BILL_IMAGE_DIR if self.legacy else self.root / "bill_images"

    @property
    def export_dir(self) -> Path:
        return config.EXPORT_DIR if self.legacy else self.root / "exports"

    @property
    def template_dir(self) -> Path:
        return (config.DATA_DIR if self.legacy else self.root) / "templates"

    def ensure_dirs(self) -> "Tenant":
        for p in (self.root, self.image_dir, self.export_dir, self.template_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


DEFAULT = Tenant(id="default", root=config.DATA_DIR, legacy=True)
_current: contextvars.ContextVar[Optional[Tenant]] = contextvars.ContextVar("ae_tenant", default=None)

# Multi-user service: a resolver that finds the caller's tenant when nothing was activated in this
# thread (Streamlit runs widget callbacks in a fresh thread before the script body activates it),
# and strict mode so that "no tenant" is an error instead of a silent fall-back to the shared folder.
_resolver: Optional[Callable[[], Optional[Tenant]]] = None
_strict = False


class NoTenantError(RuntimeError):
    pass


def configure(resolver: Optional[Callable[[], Optional[Tenant]]], strict: bool) -> None:
    global _resolver, _strict
    _resolver, _strict = resolver, strict


def current() -> Tenant:
    t = _current.get()
    if t is None and _resolver is not None:
        try:
            t = _resolver()          # not cached: the next call resolves afresh (a thread may serve another run later)
        except Exception:  # noqa: BLE001
            t = None
    if t is None:
        if _strict:
            raise NoTenantError("No user data folder is active for this request – please sign in again.")
        return DEFAULT
    return t


def activate(t: Optional[Tenant]) -> contextvars.Token:
    """Make ``t`` the tenant for this thread/run (None = default single-user layout)."""
    return _current.set(t)


def deactivate(token: contextvars.Token) -> None:
    _current.reset(token)


def for_user(user_id: int | str) -> Tenant:
    uid = _SAFE.sub("", str(user_id))
    if not uid:
        raise ValueError("invalid user id for tenant folder")
    return Tenant(id=uid, root=config.DATA_DIR / "tenants" / uid)


def tenants_root() -> Path:
    return config.DATA_DIR / "tenants"


def folder_size(t: Tenant) -> int:
    total = 0
    if t.root.exists():
        for p in t.root.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total


def owns_path(p) -> bool:
    """True when ``p`` lies inside the active tenant's folder (image previews, downloads …).
    A restored backup or a hand-edited database can carry any path – never serve one from outside."""
    if not p:
        return False
    try:
        root = current().root.resolve()
        return Path(str(p)).resolve().is_relative_to(root)
    except Exception:  # noqa: BLE001
        return False

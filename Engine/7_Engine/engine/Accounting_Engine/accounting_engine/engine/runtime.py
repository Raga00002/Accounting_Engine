"""Per-run runtime context for the multi-user service: who is calling, which Claude key to use,
what extra prompt notes apply, and hooks that gate + meter every scan.

Set by the app at the top of each script run (after login). In single-user mode nothing is set
and the engine behaves exactly as before (key from .env, no metering).
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config

# Rough list prices (USD per million tokens) for cost estimates; the admin can override via the
# master setting 'price_json' = {"model-substring": [input, output]}.
DEFAULT_PRICES = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}


@dataclass
class Runtime:
    user_id: Optional[int] = None
    user_email: str = ""
    is_admin: bool = False
    api_key: Optional[str] = None       # key to use for Claude calls (None -> config.ANTHROPIC_API_KEY)
    own_key: bool = False               # True when api_key is the user's own key
    prompt_notes: str = ""              # admin/global + per-user notes appended to the reading prompt
    prices: dict = field(default_factory=dict)
    # before_scan(kind, own_key) -> (error message to block or None, opaque reservation)
    before_scan: Optional[Callable[[str, bool], tuple[Optional[str], Any]]] = None
    # on_usage(kind, model, input_tokens, output_tokens, cost_usd, own_key, ok, reservation)
    on_usage: Optional[Callable[[str, str, int, int, float, bool, bool, Any], None]] = None


_rt: contextvars.ContextVar[Optional[Runtime]] = contextvars.ContextVar("ae_runtime", default=None)


def current() -> Runtime:
    return _rt.get() or Runtime()


def activate(rt: Optional[Runtime]) -> contextvars.Token:
    return _rt.set(rt)


def deactivate(token: contextvars.Token) -> None:
    _rt.reset(token)


def api_key() -> str:
    rt = current()
    return (rt.api_key or config.ANTHROPIC_API_KEY or "").strip()


def has_api_key() -> bool:
    return bool(api_key())


def prompt_notes() -> str:
    return (current().prompt_notes or "").strip()


class Scan:
    """One metered reader call.

        with runtime.scan("bill_image") as sc:
            ... call Claude ...
            sc.record(model, input_tokens, output_tokens, ok=True)

    Entering takes the scan slot atomically (raises ExtractionError when the caller may not scan);
    leaving without a successful record() gives the slot back and logs the failed attempt, so a
    network error or an exception never costs the user a scan.
    """

    def __init__(self, kind: str):
        self.kind = kind
        self.reservation: Any = None
        self.recorded = False

    def __enter__(self) -> "Scan":
        rt = current()
        if rt.before_scan is not None:
            msg, self.reservation = rt.before_scan(self.kind, rt.own_key)
            if msg:
                from .extractor import ExtractionError

                raise ExtractionError(msg)
        return self

    def record(self, model: str, input_tokens: int = 0, output_tokens: int = 0, ok: bool = True) -> None:
        self.recorded = True
        rt = current()
        if rt.on_usage is None:
            return
        try:
            rt.on_usage(self.kind, model, int(input_tokens or 0), int(output_tokens or 0),
                        estimate_cost(model, input_tokens or 0, output_tokens or 0), rt.own_key, ok, self.reservation)
        except Exception as e:  # noqa: BLE001 - metering must never break a scan the user already paid for
            import sys

            print(f"[accounting-engine] usage metering failed: {e!r}", file=sys.stderr, flush=True)

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.recorded:
            self.record("", 0, 0, ok=False)


def scan(kind: str) -> Scan:
    return Scan(kind)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = current().prices or {}
    m = (model or "").lower()
    for key, pair in list(prices.items()) + list(DEFAULT_PRICES.items()):
        if key.lower() in m:
            try:
                pi, po = float(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            return round((input_tokens * pi + output_tokens * po) / 1_000_000, 6)
    return 0.0


def record_usage(kind: str, model: str, input_tokens: int, output_tokens: int, ok: bool = True) -> None:
    """Legacy one-shot metering without a reservation (single call, no slot taken beforehand)."""
    rt = current()
    if rt.on_usage is None:
        return
    try:
        rt.on_usage(kind, model, int(input_tokens or 0), int(output_tokens or 0),
                    estimate_cost(model, input_tokens or 0, output_tokens or 0), rt.own_key, ok, None)
    except Exception:  # noqa: BLE001
        pass

"""Turn a bill image or a spoken/typed description into a BillDraft using Claude.

* ``extract_from_image(bytes, filename)``  – vision
* ``extract_from_text(transcript)``        – voice transcript / typed text
* mock mode (AE_EXTRACTOR_MODE=mock) reads fixtures/mock_extractions/<key>.json so the
  whole pipeline can be exercised offline.
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from . import config
from .models import BillDraft, LineItem


class ExtractionError(RuntimeError):
    """Raised when the AI backend cannot produce a usable bill."""


# --------------------------------------------------------------------------- #
# Tool schema (forces Claude to answer with structured JSON)
# --------------------------------------------------------------------------- #
LINE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "item": {"type": "string", "description": "Item exactly as written/spoken, e.g. 'Rice (BPT)', 'Mustard Oil', 'Mung Dal'"},
        "qty": {"type": ["number", "null"], "description": "Number of packages (bags/packets/tins). null if not given"},
        "unit": {"type": ["string", "null"], "description": "Package unit: Bag, Pkt, Tin, Kg, Ltr, Qtl, Pcs. 'Bg'/'B' = Bag, 'Pkt' = packet"},
        "weight_qtl": {"type": ["number", "null"], "description": "Net weight in QUINTALS when priced by weight (e.g. '.52 x 5550' -> 0.52). null when priced per package"},
        "rate": {"type": ["number", "null"], "description": "Rate as written (per quintal when weight_qtl is given, else per package)"},
        "amount": {"type": ["number", "null"], "description": "Line amount as WRITTEN on the bill. null if not written – do NOT calculate it"},
        "struck_out": {"type": "boolean", "description": "true if the line is crossed/struck out"},
        "note": {"type": ["string", "null"], "description": "Any doubt about this line (illegible digit, ambiguous unit...)"},
    },
    "required": ["item", "qty", "unit", "weight_qtl", "rate", "amount", "struck_out"],
}

BILL_TOOL: dict[str, Any] = {
    "name": "record_bill",
    "description": "Record the structured contents of ONE trade bill / chalan / kachha bill.",
    "input_schema": {
        "type": "object",
        "properties": {
            "party_name_raw": {"type": ["string", "null"], "description": "Customer/supplier name exactly as written (usually the heading). null if absent"},
            "bill_date": {"type": ["string", "null"], "description": "Bill date as YYYY-MM-DD ONLY if a date is actually written/spoken. Never guess"},
            "bill_no": {"type": ["string", "null"], "description": "Bill/chalan/invoice number ONLY if explicitly labelled as such. A bare printed page/serial number is NOT the bill number"},
            "txn_type": {"type": "string", "enum": ["sale", "purchase"], "description": "'sale' when we sold to the party (default for a bill made out to a customer); 'purchase' when we bought from them"},
            "items": {"type": "array", "items": LINE_ITEM_SCHEMA},
            "total_written": {"type": ["number", "null"], "description": "Grand total as WRITTEN (usually underlined at the bottom). null if not written"},
            "raw_text": {"type": "string", "description": "Verbatim line-by-line transcription of everything on the bill (or the transcript for voice)"},
            "notes": {"type": "array", "items": {"type": "string"}, "description": "Uncertainties, illegible parts, page/serial numbers seen, assumptions made"},
        },
        "required": ["party_name_raw", "bill_date", "bill_no", "txn_type", "items", "total_written", "raw_text", "notes"],
    },
}

SYSTEM_PROMPT = """You are a meticulous accounts clerk for {business}, an Indian wholesale trader (mustard oil, rice, pulses, grains).
You read handwritten kachha bills / chalans and dictated bills and record them EXACTLY, without inventing anything.

Conventions on these bills:
- Heading = party (customer) name. Lines below = items. Underlined number at the bottom = grand total.
- Line format is usually:  <qty> <unit> <item>  <weight or qty> x <rate>  <amount>
  * "Bg", "B", "Bag" = bag; "Pkt" = packet; "Tin" = tin. Unit abbreviations may be scribbled – infer from context.
  * A decimal weight like ".52", "1.30", "13.10" before "x" is the net weight in QUINTALS and the rate is per quintal
    (e.g. "2 Bg Rice (Sri) .52 x 5550  2886" -> qty 2, unit Bag, weight_qtl 0.52, rate 5550, amount 2886).
  * A whole number equal to the qty before "x" means priced per package (e.g. "5 Bg Rice (BPT) 5 x 1540  7700" -> qty 5, rate 1540, weight_qtl null).
  * Rice varieties appear in brackets: (BPT), (Sri), (R.R), (HMT), (Jol), (Sona), (Gobi), (Usna)... keep them in the item name, e.g. "Rice (BPT)".
- Struck-through / crossed-out lines: still list them with struck_out=true so the reviewer can see them; they are NOT part of the total.
- Copy amounts as written. If an amount is not written, leave it null (the app computes it). Never "fix" arithmetic silently – mention doubts in notes.
- Handwritten digits: 1/7, 4/9, 0/6 and 5/8 are often confusable – use the arithmetic (weight x rate ≈ amount, lines sum ≈ total) to disambiguate and say so in notes.
- A small printed number in a different colour at the top corner is the book's page/serial number – put it in notes, NOT in bill_no, unless it is explicitly labelled Bill No./Chalan No./Invoice No.
- Dates: record only if written or spoken. Today's date is {today} – use it only when the speaker says "today".
- Dictated bills may be in English, Hindi, Assamese, Bengali or a mix ("Das Store ko 5 bag BPT rice 1540 ke rate se"). "quintal" = weight_qtl; "kilo/kg X at rate per quintal" -> weight_qtl = X/100; if the rate is per kg, keep unit Kg, qty = kg, rate per kg and weight_qtl null.
- txn_type: a bill made out to a customer is a sale. Say "purchase" only if it is clearly a purchase from a supplier (e.g. speaker says "purchase from", "kharida").

Known parties in our books (prefer these exact spellings when the bill clearly means one of them; otherwise write what is on the bill):
{parties}

Known items (prefer these spellings when they clearly match):
{items}

Always respond by calling the record_bill tool exactly once."""


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def extract_from_image(
    data: bytes,
    filename: str = "bill.jpg",
    known_parties: Optional[list[str]] = None,
    known_items: Optional[list[str]] = None,
    mock_key: Optional[str] = None,
) -> BillDraft:
    if config.EXTRACTOR_MODE == "mock":
        from . import runtime

        with runtime.scan("bill_image") as sc:  # the subscription gate applies in mock mode too
            d = _mock(mock_key or Path(filename).stem, source="image")
            sc.record("mock")
        return d
    media_type, payload = _prepare_image(data, filename)
    if media_type == "application/pdf":
        content_block = {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": payload}}
    else:
        content_block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": payload}}
    user_content = [
        content_block,
        {"type": "text", "text": "Read this bill and record it with the record_bill tool."},
    ]
    raw = _call_claude(user_content, known_parties or [], known_items or [], kind="bill_image")
    return _to_draft(raw, source="image")


def extract_from_text(
    transcript: str,
    known_parties: Optional[list[str]] = None,
    known_items: Optional[list[str]] = None,
    source: str = "voice",
    mock_key: Optional[str] = None,
) -> BillDraft:
    transcript = (transcript or "").strip()
    if not transcript:
        raise ExtractionError("Nothing to parse – the transcript is empty.")
    if config.EXTRACTOR_MODE == "mock":
        from . import runtime

        with runtime.scan("bill_text") as sc:
            d = _mock(mock_key or "voice", source=source)  # type: ignore[arg-type]
            d.raw_text = transcript
            sc.record("mock")
        return d
    user_content = [
        {
            "type": "text",
            "text": "The following is a dictated / typed bill. Record it with the record_bill tool.\n\n"
            f"TRANSCRIPT:\n{transcript}",
        }
    ]
    raw = _call_claude(user_content, known_parties or [], known_items or [], kind="bill_text")
    d = _to_draft(raw, source=source)  # type: ignore[arg-type]
    d.raw_text = d.raw_text or transcript
    return d


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
_MAX_SIDE = 2000
# Raw bytes above this are re-encoded: the API caps an image at 5 MB and base64 adds a third.
_MAX_BYTES = 3_500_000
_MAX_PDF_BYTES = 30_000_000
# Media types the API accepts for image blocks, keyed by PIL format name.
_PIL_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


def _prepare_image(data: bytes, filename: str) -> tuple[str, str]:
    """Return (media_type, base64) – downsizing big photos so they fit the API limits."""
    if not data:
        raise ExtractionError("The uploaded file is empty.")
    if data[:4] == b"%PDF":
        if len(data) > _MAX_PDF_BYTES:
            raise ExtractionError(
                f"The PDF is {len(data) / 1e6:.0f} MB – the API accepts at most 32 MB. Export it at lower resolution."
            )
        return "application/pdf", base64.b64encode(data).decode()
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(data))
        # .format is only known on the freshly opened image; exif_transpose returns a copy without it.
        fmt = (img.format or "").upper()
        img.load()  # decode now: a truncated/corrupt photo fails here instead of as an API 400
        # Orientation is only present on photos (JPEG/WEBP); GIF/PNG also carry no orientation and
        # ``exif_transpose`` would needlessly load all frames of an animated GIF.
        rotated = False
        oriented = img
        if fmt in ("JPEG", "WEBP"):
            try:
                rotated = int(img.getexif().get(0x0112, 1) or 1) != 1
            except Exception:  # noqa: BLE001 - malformed EXIF
                rotated = False
            if rotated:
                oriented = ImageOps.exif_transpose(img)
        needs_convert = fmt not in _PIL_MEDIA_TYPES
        if (
            rotated
            or max(oriented.size) > _MAX_SIDE
            or len(data) > _MAX_BYTES
            or needs_convert
            or img.mode not in ("RGB", "L")
        ):
            if "A" in oriented.getbands() or oriented.mode == "P":
                # Transparent areas would turn black in JPEG – flatten onto white (paper) first.
                rgba = oriented.convert("RGBA")
                out = Image.new("RGB", rgba.size, (255, 255, 255))
                out.paste(rgba, mask=rgba.getchannel("A"))
            else:
                out = oriented.convert("RGB")
            out.thumbnail((_MAX_SIDE, _MAX_SIDE))
            buf = io.BytesIO()
            out.save(buf, format="JPEG", quality=88, optimize=True)
            data, mt = buf.getvalue(), "image/jpeg"
        else:
            # Do not trust mimetypes.guess_type(): on Windows it reads the registry and may return
            # e.g. 'image/pjpeg' for .jpg, which the API rejects. PIL's format is authoritative.
            mt = _PIL_MEDIA_TYPES[fmt]
    except ExtractionError:
        raise
    except Exception as e:  # not an image PIL understands (corrupt file, HEIC, docx ...)
        guessed = mimetypes.guess_type(filename)[0] or "unknown type"
        raise ExtractionError(
            f"Could not read '{filename}' as an image ({guessed}). Please use a JPG, PNG, WEBP or PDF: {e}"
        ) from e
    return mt, base64.b64encode(data).decode()


def _system_prompt(parties: list[str], items: list[str]) -> str:
    return SYSTEM_PROMPT.format(
        business=config.BUSINESS_NAME,
        today=date.today().isoformat(),
        parties="\n".join(f"- {p}" for p in parties[:400]) or "- (none yet)",
        items="\n".join(f"- {i}" for i in items[:300]) or "- (none yet)",
    )


def _call_claude(user_content: list[dict], parties: list[str], items: list[str],
                 tool: Optional[dict] = None, system: Optional[str] = None, kind: str = "bill_image") -> dict:
    """Forced tool-use call. ``tool``/``system`` default to the bill schema; the bank-slip reader passes its own.

    Multi-user service: the key, the subscription gate and the metering come from ``runtime`` (per user);
    single-user mode falls back to the .env key with no gate."""
    from . import runtime

    tool = tool or BILL_TOOL
    system = system if system is not None else _system_prompt(parties, items)
    notes = runtime.prompt_notes()
    if notes:
        system = system + "\n\nAdditional instructions from the administrator / this user:\n" + notes[:4000]
    key = runtime.api_key()
    if not key:
        raise ExtractionError(
            "No Claude API key is configured. Ask the administrator to set the master key (or add your own key under My Account)."
            if runtime.current().user_id else
            "ANTHROPIC_API_KEY is not set. Put it in the .env file next to app.py (see .env.example)."
        )
    with runtime.scan(kind) as sc:
        return _call_claude_metered(sc, key, system, tool, user_content)


def _call_claude_metered(sc, key: str, system: str, tool: dict, user_content: list) -> dict:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise ExtractionError("The 'anthropic' package is not installed (pip install -r requirements.txt).") from e

    client = anthropic.Anthropic(api_key=key, timeout=config.CLAUDE_TIMEOUT_S, max_retries=2)
    models = [config.CLAUDE_MODEL, *[m for m in config.CLAUDE_FALLBACK_MODELS if m != config.CLAUDE_MODEL]]
    last_err: Optional[Exception] = None
    for model in models:
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.NotFoundError as e:  # model not available on this account -> try next
            last_err = e
            continue
        except anthropic.AuthenticationError as e:
            sc.record(model, 0, 0, ok=False)
            raise ExtractionError("The Claude API key was rejected – " + (
                "check your own key under My Account." if runtime.current().own_key else
                "the administrator must check the master key." if runtime.current().user_id else
                "check ANTHROPIC_API_KEY in .env.")) from e
        except anthropic.RateLimitError as e:
            raise ExtractionError("Anthropic rate limit hit – wait a minute and try again.") from e
        except anthropic.APITimeoutError as e:  # subclass of APIConnectionError – must come first
            raise ExtractionError(
                f"The Anthropic API did not answer within {config.CLAUDE_TIMEOUT_S:.0f}s – slow connection? Try again."
            ) from e
        except anthropic.APIConnectionError as e:
            raise ExtractionError("Could not reach the Anthropic API – check your internet connection.") from e
        except anthropic.APIStatusError as e:
            raise ExtractionError(f"Anthropic API error ({e.status_code}): {getattr(e, 'message', e)}") from e
        usage = getattr(resp, "usage", None)
        sc.record(model, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
                  ok=getattr(resp, "stop_reason", None) != "max_tokens")
        if getattr(resp, "stop_reason", None) == "max_tokens":
            # The tool input JSON is cut off – whatever came through is unreliable.
            raise ExtractionError(
                "Claude's answer was cut off (too long for AE_CLAUDE_MAX_TOKENS). "
                "Increase AE_CLAUDE_MAX_TOKENS in .env or split the bill."
            )
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use" and block.name == tool["name"]:
                inp = block.input
                if not isinstance(inp, dict):
                    break
                return dict(inp)
        raise ExtractionError("Claude did not return a structured answer. Try a clearer photo.")
    raise ExtractionError(
        f"None of the configured models are available on this API key: {', '.join(models)}. "
        f"Set AE_CLAUDE_MODEL in .env. ({last_err})"
    )


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    neg = s.startswith("-")
    s = re.sub(r"[^\d.]", "", s)  # drops '₹', ',', '/-', spaces, trailing '-' ...
    if s.count(".") > 1:  # "1.2.3" -> keep the first dot
        head, *rest = s.split(".")
        s = head + "." + "".join(rest)
    try:
        return -float(s) if neg else float(s)
    except ValueError:
        return None


def _bool(v: Any) -> bool:
    """Model output sometimes carries booleans as strings ('false', 'no') – never treat those as True."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in {"true", "yes", "y", "1", "struck", "struck_out"}


def _text(v: Any) -> Optional[str]:
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("null", "none", "nan") else None


def _parse_date(v: Any) -> Optional[date]:
    d = _parse_date_raw(v)
    # A wildly wrong year (a misread serial number, '0001-01-01' for "unknown") would crash the
    # date picker (its default range is value +/- 10 years) – treat it as "no date found".
    if d is not None and not (1990 <= d.year <= 2100):
        return None
    return d


def _parse_date_raw(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    # "%d%m%Y" / "%Y%m%d": the one-digit-per-box date on bank slips (19092026) copied without separators
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d %b %Y", "%d %B %Y",
                "%d %m %Y", "%d%m%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:  # ISO date-time such as '2026-09-26T00:00:00' / '2026-09-26 00:00:00'
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _to_draft(raw: dict, source: str) -> BillDraft:
    if not isinstance(raw, dict):
        raise ExtractionError("The reader returned an unexpected answer (not a bill record).")
    items: list[LineItem] = []
    raw_items = raw.get("items")
    for it in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(it, dict):
            continue
        name = _text(it.get("item"))
        if not name:
            continue
        items.append(
            LineItem(
                item=name,
                qty=_num(it.get("qty")),
                unit=_text(it.get("unit")),
                weight_qtl=_num(it.get("weight_qtl")),
                rate=_num(it.get("rate")),
                amount=_num(it.get("amount")),
                struck_out=_bool(it.get("struck_out", False)),
                note=_text(it.get("note")),
            )
        )
    txn = str(raw.get("txn_type") or "sale").lower()
    notes = raw.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    elif not isinstance(notes, list):
        notes = [str(notes)]
    return BillDraft(
        party_name_raw=_text(raw.get("party_name_raw")),
        bill_date=_parse_date(raw.get("bill_date")),
        bill_no=_text(raw.get("bill_no")),
        txn_type="purchase" if txn == "purchase" else "sale",
        items=items,
        total_written=_num(raw.get("total_written")),
        source=source,  # type: ignore[arg-type]
        raw_text=_text(raw.get("raw_text")),
        notes=[str(n).strip() for n in notes if str(n).strip()],
    )


def _mock(key: str, source: str) -> BillDraft:
    d = config.FIXTURES_DIR / "mock_extractions"
    f = d / f"{key}.json"
    if not f.exists():
        f = d / "default.json"
    if not f.exists():
        raise ExtractionError(f"Mock extraction not found for '{key}' and no default.json in {d}.")
    raw = json.loads(f.read_text(encoding="utf-8"))
    return _to_draft(raw, source=source)

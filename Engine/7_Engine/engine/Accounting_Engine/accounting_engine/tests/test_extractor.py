import io
import json

import pytest
from PIL import Image

from engine import config, extractor, validator
from engine.extractor import ExtractionError


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "EXTRACTOR_MODE", "mock")


def test_mock_image_extraction_all_samples():
    for stem, n_items, total in [
        ("das_store", 8, 45125), ("ag_store", 2, None), ("p_roy", 5, 79297), ("mohendra", 7, 27124), ("gotam_kotna", 2, None)
    ]:
        img = (config.FIXTURES_DIR / "sample_bills" / f"{stem}.jpg").read_bytes()
        d = extractor.extract_from_image(img, f"{stem}.jpg")
        assert d.source == "image" and len(d.items) == n_items and d.total_written == total, stem
        assert d.bill_date is None and d.bill_no is None  # never invented
        validator.fill_missing_amounts(d)
        rep = validator.validate(d, party_resolved=True, require_bill_no=False)
        assert not rep.warnings, (stem, rep.warnings)  # my transcriptions are arithmetically consistent
        if total is not None:
            assert rep.computed_total == total


def test_mock_text_extraction_keeps_transcript():
    d = extractor.extract_from_text("Das Store ko 5 bag BPT rice 1540 rate", source="voice")
    assert d.source == "voice" and d.raw_text.startswith("Das Store ko")
    with pytest.raises(ExtractionError):
        extractor.extract_from_text("   ")


def test_to_draft_is_tolerant_of_messy_model_output():
    raw = {
        "party_name_raw": "  Das Store ",
        "bill_date": "26/09/2026",
        "bill_no": 13,
        "txn_type": "PURCHASE",
        "items": [
            {"item": "Rice", "qty": "5", "unit": "Bg", "weight_qtl": None, "rate": "1,540", "amount": "7700/-", "struck_out": False},
            {"item": "", "qty": 1},  # dropped
            "garbage",  # dropped
        ],
        "total_written": "45125/-",
        "raw_text": None,
        "notes": "single note",
    }
    d = extractor._to_draft(raw, source="image")
    assert d.party_name_raw == "Das Store" and d.bill_no == "13" and d.txn_type == "purchase"
    assert d.bill_date.isoformat() == "2026-09-26"
    assert len(d.items) == 1 and d.items[0].rate == 1540 and d.items[0].amount == 7700
    assert d.total_written == 45125 and d.notes == ["single note"] and d.raw_text is None


def test_prepare_image_downsizes_and_handles_pdf():
    big = Image.new("RGB", (4000, 3000), "white")
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    mt, b64 = extractor._prepare_image(buf.getvalue(), "big.png")
    assert mt == "image/jpeg"
    out = Image.open(io.BytesIO(__import__("base64").b64decode(b64)))
    assert max(out.size) <= 2000
    mt, _ = extractor._prepare_image(b"%PDF-1.4 fake", "bill.pdf")
    assert mt == "application/pdf"
    with pytest.raises(ExtractionError):
        extractor._prepare_image(b"", "x.jpg")
    with pytest.raises(ExtractionError):
        extractor._prepare_image(b"not an image at all", "x.docx")


def test_live_mode_without_key_gives_clear_error(monkeypatch):
    monkeypatch.setattr(config, "EXTRACTOR_MODE", "live")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(ExtractionError, match="ANTHROPIC_API_KEY"):
        extractor.extract_from_text("5 bag rice")


def test_tool_schema_is_valid_json_schema():
    # every required key exists in properties
    s = extractor.BILL_TOOL["input_schema"]
    assert set(s["required"]) <= set(s["properties"])
    li = extractor.LINE_ITEM_SCHEMA
    assert set(li["required"]) <= set(li["properties"])
    json.dumps(extractor.BILL_TOOL)  # serialisable


def test_to_draft_handles_string_booleans_and_odd_shapes():
    raw = {
        "party_name_raw": "null",
        "bill_date": "2026-09-26T00:00:00",
        "bill_no": None,
        "txn_type": None,
        "items": [
            {"item": "Rice", "qty": 5, "rate": 10, "struck_out": "false", "unit": "None"},
            {"item": "Oil", "qty": 1, "rate": 10, "struck_out": "true"},
        ],
        "total_written": None,
        "raw_text": "",
        "notes": {"unexpected": "shape"},
    }
    d = extractor._to_draft(raw, source="image")
    assert d.party_name_raw is None and d.bill_date.isoformat() == "2026-09-26"
    assert d.items[0].struck_out is False and d.items[0].unit is None
    assert d.items[1].struck_out is True
    assert d.raw_text is None and len(d.notes) == 1
    # items not a list -> no items, no crash
    assert extractor._to_draft({"items": {"item": "x"}}, source="image").items == []
    with pytest.raises(ExtractionError):
        extractor._to_draft(["not", "a", "dict"], source="image")  # type: ignore[arg-type]


def test_prepare_image_keeps_original_bytes_when_small_and_maps_media_type():
    import base64

    small = Image.new("RGB", (300, 200), "white")
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    png = buf.getvalue()
    mt, b64 = extractor._prepare_image(png, "scan.jpg")  # wrong extension on purpose
    assert mt == "image/png" and base64.b64decode(b64) == png  # PIL's format wins, no re-encode
    buf = io.BytesIO()
    small.save(buf, format="JPEG")
    mt, b64 = extractor._prepare_image(buf.getvalue(), "photo.JPG")
    assert mt == "image/jpeg" and base64.b64decode(b64) == buf.getvalue()
    # EXIF-rotated photo is re-encoded upright
    from PIL import Image as _I

    tall = _I.new("RGB", (200, 400), "white")
    exif = tall.getexif()
    exif[0x0112] = 6  # rotate 90
    buf = io.BytesIO()
    tall.save(buf, format="JPEG", exif=exif.tobytes())
    mt, b64 = extractor._prepare_image(buf.getvalue(), "rot.jpg")
    out = _I.open(io.BytesIO(base64.b64decode(b64)))
    assert mt == "image/jpeg" and out.size == (400, 200)
    with pytest.raises(ExtractionError):
        extractor._prepare_image(b"\xff\xd8corrupt", "x.jpg")  # corrupt image with image extension


def test_call_claude_fallback_and_truncation(monkeypatch):
    """Exercise the live path with a fake SDK client: model fallback, truncated answer, tool block parsing."""
    import types

    import anthropic

    monkeypatch.setattr(config, "EXTRACTOR_MODE", "live")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(config, "CLAUDE_MODEL", "missing-model")
    monkeypatch.setattr(config, "CLAUDE_FALLBACK_MODELS", ["ok-model"])
    calls: list[str] = []
    script = {"stop_reason": "tool_use"}

    class _Messages:
        def create(self, **kw):
            calls.append(kw["model"])
            assert kw["tool_choice"] == {"type": "tool", "name": "record_bill"}
            if kw["model"] == "missing-model":
                import httpx

                resp = httpx.Response(404, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
                raise anthropic.NotFoundError("no such model", response=resp, body=None)
            block = types.SimpleNamespace(type="tool_use", name="record_bill",
                                          input={"party_name_raw": "Das Store", "items": [{"item": "Rice", "qty": 1, "rate": 5}]})
            return types.SimpleNamespace(content=[block], stop_reason=script["stop_reason"])

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    d = extractor.extract_from_text("Das Store 1 bag rice 5")
    assert calls == ["missing-model", "ok-model"] and d.party_name_raw == "Das Store" and d.items[0].rate == 5
    script["stop_reason"] = "max_tokens"
    with pytest.raises(ExtractionError, match="cut off"):
        extractor.extract_from_text("Das Store 1 bag rice 5")


def test_speech_transcribe_parses_wav_header(monkeypatch):
    """mic_recorder delivers a full (possibly stereo) WAV file – it must not be read as raw mono PCM."""
    import wave

    import speech_recognition as sr

    from engine import speech

    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(48000)
    w.writeframes(b"\x00\x00" * 48000 * 2)  # 1 s stereo
    w.close()
    seen = {}

    def fake_recognize(self, audio, language="en-IN"):
        seen["audio"] = audio
        return " five bag rice "

    monkeypatch.setattr(sr.Recognizer, "recognize_google", fake_recognize)
    assert speech.transcribe(buf.getvalue(), 48000, 2, "en-IN") == "five bag rice"
    a = seen["audio"]
    assert a.sample_rate == 48000 and len(a.frame_data) == 48000 * 2  # 1 s mono, header stripped
    with pytest.raises(speech.SpeechError):
        speech.transcribe(b"", 16000, 2)


def test_parse_date_rejects_absurd_years():
    assert extractor._parse_date("0001-01-01") is None  # would crash st.date_input (value - 10 years)
    assert extractor._parse_date("26/09/2026").isoformat() == "2026-09-26"
    assert extractor._parse_date("26-09-26").isoformat() == "2026-09-26"
    assert extractor._parse_date(None) is None and extractor._parse_date("not a date") is None


def test_prepare_image_keeps_base64_payload_under_api_limit():
    import base64

    # A 1900px noisy PNG is > 3.5 MB raw but within the pixel limit: it must still be re-encoded
    # so that the base64 payload stays under the API's 5 MB image cap.
    import os

    noisy = Image.frombytes("RGB", (1900, 1900), os.urandom(1900 * 1900 * 3))
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    assert len(buf.getvalue()) > extractor._MAX_BYTES
    mt, b64 = extractor._prepare_image(buf.getvalue(), "noisy.png")
    assert mt == "image/jpeg" and len(b64) < 5_000_000
    assert Image.open(io.BytesIO(base64.b64decode(b64))).size == (1900, 1900)


def test_prepare_image_flattens_transparency_on_white():
    import base64

    rgba = Image.new("RGBA", (100, 100), (0, 0, 0, 0))  # fully transparent (black underneath)
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    mt, b64 = extractor._prepare_image(buf.getvalue(), "t.png")
    out = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    assert mt == "image/jpeg" and out.getpixel((50, 50)) == (255, 255, 255)


def test_parse_date_digit_boxes():
    from datetime import date

    from engine.extractor import _parse_date

    assert _parse_date("19092026") == date(2026, 9, 19)  # DDMMYYYY as written in the slip boxes
    assert _parse_date("20260919") == date(2026, 9, 19)
    assert _parse_date("19 09 2026") == date(2026, 9, 19)
    assert _parse_date("2026-09-19") == date(2026, 9, 19)
    assert _parse_date("1909202") is None

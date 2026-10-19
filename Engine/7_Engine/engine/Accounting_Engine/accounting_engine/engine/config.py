"""Central configuration. Everything is overridable through environment variables / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.getenv("AE_DATA_DIR", PROJECT_ROOT / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
BILL_IMAGE_DIR = DATA_DIR / "bill_images"
BILL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR = DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("AE_DB_PATH", DATA_DIR / "accounting.db")).resolve()

FIXTURES_DIR = PROJECT_ROOT / "fixtures"
TEMPLATE_PATH = FIXTURES_DIR / "Ledgers_in_Default_Template.xlsx"

# --- AI backend -----------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# "live" = call the Anthropic API; "mock" = read fixtures/mock_extractions/<stem>.json (offline tests)
EXTRACTOR_MODE = os.getenv("AE_EXTRACTOR_MODE", "live").strip().lower()
CLAUDE_MODEL = os.getenv("AE_CLAUDE_MODEL", "claude-sonnet-5").strip()
# Tried in order if the configured model is not found on the account.
CLAUDE_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "AE_CLAUDE_FALLBACK_MODELS", "claude-sonnet-4-5,claude-opus-5-5,claude-haiku-4-5-20251001"
    ).split(",")
    if m.strip()
]
# A long bill (verbatim raw_text + 20 lines + notes) can exceed 4k output tokens; a cut-off answer is rejected.
CLAUDE_MAX_TOKENS = int(os.getenv("AE_CLAUDE_MAX_TOKENS", "8192"))
CLAUDE_TIMEOUT_S = float(os.getenv("AE_CLAUDE_TIMEOUT_S", "120"))

# --- Business defaults ----------------------------------------------------
BUSINESS_NAME = os.getenv("AE_BUSINESS_NAME", "Wings Industries")
DEFAULT_COUNTRY = os.getenv("AE_DEFAULT_COUNTRY", "India")
DEFAULT_STATE = os.getenv("AE_DEFAULT_STATE", "Assam")
DEFAULT_DEBTOR_GROUP = "Sundry Debtors"
DEFAULT_CREDITOR_GROUP = "Sundry Creditors"
# Bill-number prefix used when the user asks the app to auto-number (e.g. WI/2026-27/001)
BILL_NO_PREFIX = os.getenv("AE_BILL_NO_PREFIX", "WI")

# Fuzzy-match thresholds (0-100)
PARTY_AUTO_MATCH = int(os.getenv("AE_PARTY_AUTO_MATCH", "92"))
PARTY_SUGGEST_MATCH = int(os.getenv("AE_PARTY_SUGGEST_MATCH", "65"))
ITEM_AUTO_MATCH = int(os.getenv("AE_ITEM_AUTO_MATCH", "90"))
ITEM_SUGGEST_MATCH = int(os.getenv("AE_ITEM_SUGGEST_MATCH", "75"))

# Arithmetic tolerance when checking written vs computed amounts (absolute rupees)
AMOUNT_TOLERANCE = float(os.getenv("AE_AMOUNT_TOLERANCE", "1.0"))

SPEECH_LANGUAGES = {
    "English (India)": "en-IN",
    "Hindi": "hi-IN",
    "Assamese": "as-IN",
    "Bengali": "bn-IN",
    "English (US)": "en-US",
}

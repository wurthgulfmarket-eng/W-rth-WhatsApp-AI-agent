"""
Loads all configuration from environment variables (.env).
No secrets live in code - this file only reads them.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str):
    return [v.strip() for v in value.split(",") if v.strip()]


class Config:
    # WhatsApp
    WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v20.0")

    # OpenRouter
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
    # Used only for image messages - must be a vision/multimodal-capable model.
    # Defaults to the same model; override if OPENROUTER_MODEL doesn't support images.
    OPENROUTER_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", OPENROUTER_MODEL)
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

    # Google Sheets
    GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
    GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "./credentials/service_account.json")
    # On hosts without file upload (e.g. Render free tier), paste the entire
    # service_account.json content into this env var instead - it gets written
    # to GOOGLE_SERVICE_ACCOUNT_FILE automatically on startup (see main.py).
    GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    GOOGLE_SHEET_WORKSHEET_NAME = os.getenv("GOOGLE_SHEET_WORKSHEET_NAME", "Sheet1")

    # App behavior
    FUZZY_MATCH_THRESHOLD = int(os.getenv("FUZZY_MATCH_THRESHOLD", "85"))
    ESCALATION_NOTIFY_NUMBERS = _split_csv(os.getenv("ESCALATION_NOTIFY_NUMBERS", ""))
    PORT = int(os.getenv("PORT", "8000"))

    # Database - Postgres is required for persistence, since Render's free
    # tier web service filesystem is ephemeral and wipes SQLite on every
    # deploy/restart. Use Render's own managed Postgres (New > PostgreSQL in
    # Render's dashboard) and paste its Internal Database URL here - it stays
    # within Render's network so it avoids the cross-provider IPv6/pooler
    # issues an external provider can hit. Special characters in the password
    # do NOT need to be pre-encoded, storage/store.py handles that.
    DATABASE_URL = os.getenv("DATABASE_URL", "")

    # Dashboard admin login (separate from the WHATSAPP_VERIFY_TOKEN used for
    # webhook/rebuild-kb auth). Set these to enable the /dashboard login form.
    DASHBOARD_ADMIN_USERNAME = os.getenv("DASHBOARD_ADMIN_USERNAME", "")
    DASHBOARD_ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
    # Random secret used to sign the admin session cookie - set this to any
    # long random string in production so sessions survive restarts and can't
    # be forged. Falls back to WHATSAPP_VERIFY_TOKEN if unset (not ideal, but
    # keeps things working without yet another required env var).
    DASHBOARD_SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "") or WHATSAPP_VERIFY_TOKEN

    # Paths
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    KB_JSON_PATH = os.path.join(DATA_DIR, "knowledge_base.json")
    KB_INDEX_PATH = os.path.join(DATA_DIR, "kb_index.pkl")


config = Config()

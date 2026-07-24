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
    # Free-tier models occasionally hit the underlying provider's own
    # capacity limits (e.g. "Worker local total request limit reached") -
    # this isn't a transient blip a retry alone fixes, since the whole
    # provider is saturated. chat_completion() tries OPENROUTER_MODEL first,
    # then each of these in order, only moving to the next one after
    # exhausting retries on the current one - so one provider being at
    # capacity doesn't take down replies to every customer.
    # NOTE: same model-rot risk as the vision fallback below - verify
    # against https://openrouter.ai/api/v1/models if replies start failing.
    OPENROUTER_FALLBACK_MODELS = _split_csv(os.getenv(
        "OPENROUTER_FALLBACK_MODELS",
        "google/gemma-4-26b-a4b-it:free,openai/gpt-oss-20b:free",
    ))
    # Used only for image messages - must be a vision/multimodal-capable
    # model. Previously defaulted to OPENROUTER_MODEL, which is text-only
    # (nemotron) in production - every image a customer sent silently failed
    # since the model can't see images at all, always hitting the generic
    # "having trouble looking at that image" fallback. Defaults to a real
    # free vision-capable model instead, with its own fallback chain
    # (text-only fallback models can't handle vision input, so this must
    # stay separate from OPENROUTER_FALLBACK_MODELS).
    # NOTE: OpenRouter regularly retires/renames free-tier model slugs
    # (all three of the original defaults here 404'd within weeks) - if
    # image replies start failing again, check
    # https://openrouter.ai/api/v1/models for currently-live models with
    # "image" in architecture.input_modalities and update these.
    OPENROUTER_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free")
    OPENROUTER_VISION_FALLBACK_MODELS = _split_csv(os.getenv(
        "OPENROUTER_VISION_FALLBACK_MODELS",
        "nvidia/nemotron-nano-12b-v2-vl:free,google/gemma-4-31b-it:free",
    ))
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

    # Groq (voice note transcription) - OpenRouter's chat models don't do
    # audio transcription, so voice notes need a dedicated speech-to-text
    # step before the transcribed text can go through the normal reply/
    # lead-detection pipeline. Until GROQ_API_KEY is set, voice notes fall
    # back to the old "please type your question instead" reply.
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_TRANSCRIPTION_MODEL = os.getenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo")

    # Google Sheets
    GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
    GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "./credentials/service_account.json")
    # On hosts without file upload (e.g. Render free tier), paste the entire
    # service_account.json content into this env var instead - it gets written
    # to GOOGLE_SERVICE_ACCOUNT_FILE automatically on startup (see main.py).
    GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    GOOGLE_SHEET_WORKSHEET_NAME = os.getenv("GOOGLE_SHEET_WORKSHEET_NAME", "Sheet1")

    # Once a customer is matched to a company/rep, that mapping is cached in
    # Postgres so we don't re-ask for their company name on every message -
    # but without a refresh, an update to the rep's phone/name in the sheet
    # (e.g. a rep leaving, a number change) would never reach customers
    # already on file. Re-check the sheet and refresh rep_name/rep_phone/
    # rep_email if the cached record is older than this many hours.
    REP_INFO_REFRESH_HOURS = int(os.getenv("REP_INFO_REFRESH_HOURS", "12"))

    # App behavior
    FUZZY_MATCH_THRESHOLD = int(os.getenv("FUZZY_MATCH_THRESHOLD", "85"))
    ESCALATION_NOTIFY_NUMBERS = _split_csv(os.getenv("ESCALATION_NOTIFY_NUMBERS", ""))
    PORT = int(os.getenv("PORT", "8000"))

    # Lead-escalation WhatsApp message templates (optional). Free-form
    # messages only deliver within Meta's 24-hour customer-service window;
    # a Meta-approved template bypasses that restriction. Until these are
    # set (after Meta approves the template), escalation falls back to
    # free-form messages exactly as before - this is purely additive.
    WHATSAPP_ESCALATION_TEMPLATE_NAME = os.getenv("WHATSAPP_ESCALATION_TEMPLATE_NAME", "")
    WHATSAPP_ESCALATION_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_ESCALATION_TEMPLATE_LANGUAGE", "en")
    WHATSAPP_ESCALATION_OPS_TEMPLATE_NAME = os.getenv("WHATSAPP_ESCALATION_OPS_TEMPLATE_NAME", "")

    # Lead deduplication: escalated messages from the same customer within
    # this many hours of each other collapse into one "lead" on the
    # dashboard, instead of one row per message. Also used as the day-1
    # reminder's "no rep reply since" cutoff, see LEAD_FOLLOWUP_HOURS below.
    LEAD_DEDUP_WINDOW_HOURS = int(os.getenv("LEAD_DEDUP_WINDOW_HOURS", "36"))

    # Day-1 rep reminder: a nudge sent to the SALES REP (not the customer -
    # customers shouldn't be pinged twice about the same enquiry) if their
    # assigned lead is still open and they haven't replied to the original
    # escalation alert after this many hours. Must use a Meta-approved
    # template - by definition this fires outside WhatsApp's 24-hour
    # free-form messaging window, since a rep who hasn't engaged yet likely
    # hasn't messaged the business number recently either. Until the
    # template name is set (after Meta approves it), this safely no-ops.
    LEAD_FOLLOWUP_HOURS = int(os.getenv("LEAD_FOLLOWUP_HOURS", "24"))
    WHATSAPP_REP_REMINDER_TEMPLATE_NAME = os.getenv("WHATSAPP_REP_REMINDER_TEMPLATE_NAME", "")
    WHATSAPP_REP_REMINDER_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_REP_REMINDER_TEMPLATE_LANGUAGE", "en")

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

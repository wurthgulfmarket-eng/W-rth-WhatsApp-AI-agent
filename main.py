"""
Entry point. Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000

Expose this publicly (e.g. via a reverse proxy or a tunnel like ngrok during
development) and set the resulting URL as your webhook in Meta's App Dashboard
under WhatsApp > Configuration, together with WHATSAPP_VERIFY_TOKEN from .env.
"""
import json
import logging
import os

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from config import config
from ai.agent import generate_reply, generate_image_reply, try_extract_company_name
from dashboard import router as dashboard_router
from privacy_policy import PRIVACY_POLICY_HTML
from sheets.sheets_client import find_rep_for_company, find_rep_for_phone
from storage import store
from utils.phone import is_plausible_phone, to_whatsapp_number
from whatsapp.client import send_text_message, send_template_message, mark_as_read, WhatsAppError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wurth-agent")

# On hosts without file upload (e.g. Render free tier), the service account
# key is supplied as a raw JSON string in GOOGLE_SERVICE_ACCOUNT_JSON - write
# it to disk at every startup so sheets_client can read it as a normal file.
# Always overwrite (rather than skip-if-exists) so a stale/empty file from a
# previous run never shadows a valid env var.
if config.GOOGLE_SERVICE_ACCOUNT_JSON:
    try:
        json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)  # validate before writing
    except ValueError:
        logger.error(
            "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON - "
            "Google Sheets lookups will fail until this is fixed. "
            "Paste the full contents of your service_account.json file."
        )
    else:
        os.makedirs(os.path.dirname(config.GOOGLE_SERVICE_ACCOUNT_FILE), exist_ok=True)
        with open(config.GOOGLE_SERVICE_ACCOUNT_FILE, "w", encoding="utf-8") as f:
            f.write(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        logger.info("Wrote service account credentials from GOOGLE_SERVICE_ACCOUNT_JSON to %s", config.GOOGLE_SERVICE_ACCOUNT_FILE)
elif not os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE):
    logger.warning(
        "No GOOGLE_SERVICE_ACCOUNT_JSON env var and no %s file found - "
        "Google Sheets rep lookups will fail until credentials are configured.",
        config.GOOGLE_SERVICE_ACCOUNT_FILE,
    )

app = FastAPI(title="Wurth UAE WhatsApp AI Agent")
app.include_router(dashboard_router)


@app.get("/")
def health():
    return {"status": "ok", "service": "wurth-whatsapp-agent"}


@app.get("/privacy-policy", response_class=HTMLResponse)
def privacy_policy():
    return PRIVACY_POLICY_HTML


def _rebuild_kb():
    logger.info("Starting knowledge base rebuild...")
    try:
        from scraper.scrape_kb import main as scrape_main
        from kb.build_index import main as build_index_main
        scrape_main()
        build_index_main()
        logger.info("Knowledge base rebuild complete.")
    except Exception:
        logger.exception("Knowledge base rebuild failed")


# Trigger a crawl + reindex of wurth.ae/eshop.wurth.ae. Runs in the background
# so the request returns immediately; check the service logs for progress.
# Protected by the same verify token you set in Meta's webhook config, passed
# as ?token=...
@app.post("/admin/rebuild-kb")
def rebuild_kb(token: str, background_tasks: BackgroundTasks):
    if token != config.WHATSAPP_VERIFY_TOKEN:
        return Response(content="Forbidden", status_code=403)
    background_tasks.add_task(_rebuild_kb)
    return {"status": "rebuild_started"}


@app.on_event("startup")
def _auto_rebuild_kb_if_missing():
    # Render's free tier disk is ephemeral - every deploy/restart wipes data/,
    # so the knowledge base index needs rebuilding after every deploy. Do it
    # automatically instead of relying on a manual /admin/rebuild-kb call.
    if not os.path.exists(config.KB_INDEX_PATH):
        logger.info("kb_index.pkl not found on startup - auto-rebuilding knowledge base")
        import threading
        threading.Thread(target=_rebuild_kb, daemon=True).start()


# ---- Meta webhook verification (GET) ----
@app.get("/webhook")
def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == config.WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


# ---- Inbound messages (POST) ----
# Responds to Meta immediately and does the actual work (OpenRouter call, KB
# search, Sheets lookup, sending the reply) in a background task. Meta expects
# a fast response and will retry delivery of the same message if it doesn't
# get one - retries showed up as duplicate message_ids in the logs before
# this change, each one re-triggering a slow synchronous reply generation.
@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    logger.info("Inbound payload: %s", payload)

    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        if "messages" not in value:
            # Could be a status update (sent/delivered/read) - nothing to do
            return {"status": "ignored"}

        message = value["messages"][0]
        from_number = message["from"]
        message_id = message["id"]
        msg_type = message.get("type", "text")

    except (KeyError, IndexError) as e:
        logger.warning("Unrecognized payload shape: %s", e)
        return {"status": "ignored"}

    if store.already_processed(message_id):
        logger.info("Skipping already-processed message_id=%s (Meta retry)", message_id)
        return {"status": "duplicate_ignored"}
    store.mark_processed(message_id)

    if msg_type == "text":
        text = message.get("text", {}).get("body", "").strip()
        if not text:
            return {"status": "ignored_non_text"}
        background_tasks.add_task(_process_text_message, from_number, text, message_id)

    elif msg_type == "image":
        image = message.get("image", {})
        background_tasks.add_task(
            _process_image_message, from_number, image.get("id"), image.get("mime_type", "image/jpeg"),
            image.get("caption", ""), message_id,
        )

    elif msg_type == "audio":
        background_tasks.add_task(_process_audio_message, from_number, message_id)

    else:
        logger.info("Ignoring unsupported message type '%s' from %s", msg_type, from_number)
        return {"status": "ignored_unsupported_type"}

    return {"status": "accepted"}


def _process_text_message(phone: str, text: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    try:
        handle_customer_message(phone, text)
    except Exception:
        logger.exception("Failed to handle message from %s", phone)
        _send(
            phone,
            "Sorry, I'm having trouble responding right now. Please try again in a moment, "
            "or contact Würth UAE customer service at +971 800 98784.",
        )


def _process_audio_message(phone: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    store.log_message(phone, "in", "[voice note]")
    _send(
        phone,
        "Thanks for the voice note! I can't listen to audio yet - could you please type your question, "
        "or send a photo of the product instead? You can also call us at +971 800 98784.",
    )


def _process_image_message(phone: str, media_id: str, mime_type: str, caption: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    store.log_message(phone, "in", f"[image]{(' ' + caption) if caption else ''}")

    if not media_id:
        _send(phone, "Sorry, I couldn't receive that image. Could you try sending it again?")
        return

    try:
        from whatsapp.client import download_media
        image_bytes = download_media(media_id)

        customer = store.get_customer(phone)
        rep = None
        if customer and customer.get("rep_name"):
            rep = {
                "company_name": customer["company_name"],
                "rep_name": customer["rep_name"],
                "rep_phone": customer["rep_phone"],
                "rep_email": customer["rep_email"],
            }

        reply = generate_image_reply(image_bytes, mime_type, rep, caption=caption)
        _send(phone, reply)
    except Exception:
        logger.exception("Failed to handle image from %s", phone)
        _send(
            phone,
            "Sorry, I'm having trouble looking at that image right now. Could you describe the product in words, "
            "or contact Würth UAE customer service at +971 800 98784?",
        )


def handle_customer_message(phone: str, text: str):
    store.log_message(phone, "in", text)

    customer = store.get_customer(phone)

    # First contact / no company on file yet -> try to recognize them
    if not customer or not customer.get("company_name"):
        rep = None

        # Try the customer's WhatsApp number against the sheet's Company Phone
        # column first - if it matches, we can identify them and their rep
        # without asking anything, even on their very first message.
        try:
            rep = find_rep_for_phone(phone)
        except Exception:
            logger.exception("Phone-based rep lookup failed for %s", phone)

        candidate = None
        if not rep:
            candidate = try_extract_company_name(text)
            if candidate:
                try:
                    rep = find_rep_for_company(candidate)
                except Exception:
                    # Sheets lookup can fail (bad credentials, API outage, etc.) - don't let
                    # that stop the customer from getting an answer, just skip the rep lookup.
                    logger.exception("Sales rep lookup failed for candidate company '%s'", candidate)

        if rep:
            store.upsert_customer(phone, rep["company_name"], rep["rep_name"], rep["rep_phone"], rep["rep_email"])
            customer = store.get_customer(phone)
        elif candidate:
            # They told us a company but it didn't match the sheet - proceed without a rep,
            # the AI will still answer general questions.
            store.upsert_customer(phone, candidate)
            customer = store.get_customer(phone)
        else:
            reply = (
                "Hi! Thanks for reaching out to W\u00fcrth UAE. Could you tell me your company name "
                "so I can connect you with the right details and your sales representative?"
            )
            _send(phone, reply)
            return

    rep = None
    if customer and customer.get("rep_name"):
        rep = {
            "company_name": customer["company_name"],
            "rep_name": customer["rep_name"],
            "rep_phone": customer["rep_phone"],
            "rep_email": customer["rep_email"],
        }

    history = store.get_recent_history(phone, limit=6)
    reply, escalate = generate_reply(text, rep, history=history)

    if escalate:
        reply += "\n\nI've flagged this for your sales representative to follow up personally."

    conversation_id = _send(phone, reply, escalated=escalate)

    if escalate:
        _notify_escalation(conversation_id, phone, text, customer)


def _send(phone: str, message: str, escalated: bool = False) -> int | None:
    """Returns the new conversations.id, or None if the send itself failed
    (in which case there's nothing to attach escalation records to)."""
    try:
        send_text_message(phone, message)
        return store.log_message(phone, "out", message, escalated=escalated)
    except WhatsAppError as e:
        logger.error("Failed to send WhatsApp message to %s: %s", phone, e)
        return None


def _send_and_record_escalation(
    conversation_id: int | None, customer_phone: str, target_type: str, target_phone: str,
    target_name: str | None, message_text: str,
) -> bool:
    """Sends one escalation notification (to a rep or an ops-fallback
    number) and unconditionally records the attempt - success or failure -
    so delivery outcomes are visible on the dashboard instead of only
    appearing in logs. Returns whether the send succeeded, so the caller
    can decide whether to fall back to another target."""
    if not is_plausible_phone(target_phone):
        logger.warning("Skipping escalation notify to %s (%s) - implausible phone format", target_name, target_phone)
        store.record_escalation_attempt(
            conversation_id, customer_phone, target_type, target_phone, target_name,
            "freeform", None, False, None, "invalid phone format",
        )
        return False

    template_name = (
        config.WHATSAPP_ESCALATION_TEMPLATE_NAME if target_type == "rep"
        else config.WHATSAPP_ESCALATION_OPS_TEMPLATE_NAME
    )
    normalized_phone = to_whatsapp_number(target_phone)

    whatsapp_message_id = None
    error_detail = None
    success = False
    message_type = "freeform"

    try:
        if template_name:
            message_type = "template"
            components = [{"type": "body", "parameters": [
                {"type": "text", "text": target_name or "Würth UAE"},
                {"type": "text", "text": customer_phone},
                {"type": "text", "text": message_text[:1000]},
            ]}]
            resp = send_template_message(normalized_phone, template_name, config.WHATSAPP_ESCALATION_TEMPLATE_LANGUAGE, components)
        else:
            resp = send_text_message(normalized_phone, message_text)
        whatsapp_message_id = resp.get("messages", [{}])[0].get("id")
        success = True
        logger.info("Notified %s %s (%s) about enquiry from %s", target_type, target_name, target_phone, customer_phone)
    except WhatsAppError as e:
        error_detail = str(e)
        logger.error("Failed to notify %s %s at %s: %s", target_type, target_name, target_phone, e)

    store.record_escalation_attempt(
        conversation_id, customer_phone, target_type, normalized_phone, target_name,
        message_type, template_name, success, whatsapp_message_id, error_detail,
    )
    return success


def _notify_escalation(conversation_id: int | None, customer_phone: str, message: str, customer: dict | None):
    company = customer["company_name"] if customer else "Unknown company"
    rep_phone = (customer or {}).get("rep_phone", "").strip()
    rep_name = (customer or {}).get("rep_name", "").strip()

    # Notify the customer's actual assigned sales rep directly, if we have
    # their number - framed as a live opportunity to follow up on, not just
    # a generic alert, so the rep is motivated to act on it quickly.
    rep_notified = False
    if rep_phone:
        rep_alert = (
            f"New enquiry from {company} (+{customer_phone}) on WhatsApp:\n"
            f"\"{message}\"\n\n"
            f"They may be ready to place an order or need a quote - reach out soon "
            f"to help them and close this one for your target!"
        )
        rep_notified = _send_and_record_escalation(conversation_id, customer_phone, "rep", rep_phone, rep_name, rep_alert)
    else:
        logger.warning("No rep_phone on file for %s (%s) - skipping rep notification", customer_phone, company)

    # Ops-fallback only fires if the rep notification failed or there was no
    # rep to notify in the first place - not unconditionally alongside it.
    if rep_notified:
        return

    if not config.ESCALATION_NOTIFY_NUMBERS:
        logger.warning(
            "Rep notification failed/absent for %s and no ESCALATION_NOTIFY_NUMBERS configured - "
            "this lead has no notification path", customer_phone,
        )
        return

    rep_note = f" (rep: {rep_name})" if rep_name else " (no rep assigned yet)"
    alert = (
        f"Escalation needed{rep_note}\n"
        f"Customer: +{customer_phone} - {company}\n"
        f"Message: {message}"
    )
    for staff_number in config.ESCALATION_NOTIFY_NUMBERS:
        _send_and_record_escalation(conversation_id, customer_phone, "ops_fallback", staff_number, None, alert)

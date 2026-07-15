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
from ai.agent import generate_reply, needs_escalation, try_extract_company_name
from privacy_policy import PRIVACY_POLICY_HTML
from sheets.sheets_client import find_rep_for_company
from storage import store
from whatsapp.client import send_text_message, mark_as_read, WhatsAppError

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
        text = message.get("text", {}).get("body", "").strip()

    except (KeyError, IndexError) as e:
        logger.warning("Unrecognized payload shape: %s", e)
        return {"status": "ignored"}

    if not text:
        return {"status": "ignored_non_text"}

    if store.already_processed(message_id):
        logger.info("Skipping already-processed message_id=%s (Meta retry)", message_id)
        return {"status": "duplicate_ignored"}
    store.mark_processed(message_id)

    background_tasks.add_task(_process_message, from_number, text, message_id)
    return {"status": "accepted"}


def _process_message(phone: str, text: str, message_id: str):
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


def handle_customer_message(phone: str, text: str):
    store.log_message(phone, "in", text)

    customer = store.get_customer(phone)

    # First contact / no company on file yet -> try to resolve it from the sheet
    if not customer or not customer.get("company_name"):
        candidate = try_extract_company_name(text)
        rep = None
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
    reply = generate_reply(text, rep, history=history)

    escalate = needs_escalation(text)
    if escalate:
        reply += "\n\nI've flagged this for your sales representative to follow up personally."
        _notify_escalation(phone, text, customer)

    _send(phone, reply, escalated=escalate)


def _send(phone: str, message: str, escalated: bool = False):
    try:
        send_text_message(phone, message)
        store.log_message(phone, "out", message, escalated=escalated)
    except WhatsAppError as e:
        logger.error("Failed to send WhatsApp message to %s: %s", phone, e)


def _notify_escalation(customer_phone: str, message: str, customer: dict | None):
    if not config.ESCALATION_NOTIFY_NUMBERS:
        return
    company = customer["company_name"] if customer else "Unknown company"
    rep_note = f" (rep: {customer.get('rep_name')})" if customer and customer.get("rep_name") else ""
    alert = (
        f"Escalation needed{rep_note}\n"
        f"Customer: +{customer_phone} - {company}\n"
        f"Message: {message}"
    )
    for staff_number in config.ESCALATION_NOTIFY_NUMBERS:
        try:
            send_text_message(staff_number, alert)
        except WhatsAppError as e:
            logger.error("Failed to send escalation alert to %s: %s", staff_number, e)

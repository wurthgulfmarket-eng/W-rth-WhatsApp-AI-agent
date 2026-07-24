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
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from config import config
from ai.agent import generate_reply, generate_image_reply, try_extract_company_name, is_auto_reply, is_company_change_signal
from dashboard import router as dashboard_router
from privacy_policy import PRIVACY_POLICY_HTML
from sheets.sheets_client import find_rep_for_company, find_rep_for_phone, is_known_rep_phone
from storage import store
from utils.phone import is_plausible_phone, to_whatsapp_number
from utils.whatsapp_text import sanitize_template_param
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


def _send_day1_followups():
    """Sends a reminder to the SALES REP (not the customer - customers
    shouldn't be pinged twice about the same enquiry) for any lead where
    they haven't replied to the original escalation at all after
    LEAD_FOLLOWUP_HOURS - see storage.store.get_leads_needing_followup for
    the exact eligibility criteria. Any reply from the rep (action taken or
    not) counts as "handled" and stops reminders for that lead until the
    next one. Must use a Meta-approved template (a rep who hasn't engaged
    yet likely hasn't messaged the business number recently either, so this
    fires outside WhatsApp's 24h free-form window by design), so no-ops
    until one is configured."""
    if not config.WHATSAPP_REP_REMINDER_TEMPLATE_NAME:
        logger.info("WHATSAPP_REP_REMINDER_TEMPLATE_NAME not set - skipping day-1 rep reminders")
        return

    leads = store.get_leads_needing_followup()
    logger.info("Day-1 rep reminder: %d lead(s) eligible", len(leads))

    for lead in leads:
        company_or_name = sanitize_template_param(lead["company_name"]) or "this customer"
        rep_first_name = sanitize_template_param((lead["rep_name"] or "").split(" ")[0]) or "there"

        components = [{"type": "body", "parameters": [
            {"type": "text", "parameter_name": "rep_name", "text": rep_first_name},
            {"type": "text", "parameter_name": "customer_name", "text": company_or_name},
            {"type": "text", "parameter_name": "customer_phone", "text": lead["phone"]},
        ]}]

        try:
            send_template_message(
                to_whatsapp_number(lead["rep_phone"]),
                config.WHATSAPP_REP_REMINDER_TEMPLATE_NAME,
                config.WHATSAPP_REP_REMINDER_TEMPLATE_LANGUAGE,
                components,
            )
            store.mark_lead_followup_sent(lead["id"])
            logger.info("Sent day-1 rep reminder to lead id=%s rep_phone=%s", lead["id"], lead["rep_phone"])
        except WhatsAppError as e:
            # Leave followup_sent_at NULL so this lead is retried on the
            # next scheduled run instead of being silently dropped.
            logger.error("Day-1 rep reminder failed for lead id=%s rep_phone=%s: %s", lead["id"], lead["rep_phone"], e)


# Sends the day-1 rep reminder for any lead the assigned rep hasn't replied
# to yet - see _send_day1_followups. Route name kept as "send-followups" for
# backwards compatibility with the already-configured external scheduler.
# Intended to be called once a day by an external scheduler (this app has no
# built-in cron; Render's free tier doesn't support Cron Job services), e.g.
# a GitHub Actions scheduled workflow. Protected the same way as
# /admin/rebuild-kb.
@app.post("/admin/send-followups")
def send_followups(token: str, background_tasks: BackgroundTasks):
    if token != config.WHATSAPP_VERIFY_TOKEN:
        return Response(content="Forbidden", status_code=403)
    background_tasks.add_task(_send_day1_followups)
    return {"status": "followups_started"}


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

    # A sales rep replying to an escalation alert must never be routed
    # through the customer AI-reply pipeline. Take this path when we have
    # evidence the number is a rep - either we've actually sent it an
    # escalation before, OR the sheet lists it as someone's Rep Phone (a
    # rep who hasn't had a lead escalated to them yet would otherwise
    # never be recognized) - AND either an exact swipe-to-reply match to a
    # known alert, or the number isn't ALSO a known customer - if it's
    # ambiguous (could be either), default to treating it as a customer
    # message, since silently misfiling a real customer's message is worse
    # than a rep occasionally getting an AI reply to their own text.
    context_id = message.get("context", {}).get("id")
    is_rep_number = store.find_rep_matches_for_phone(from_number)
    if not is_rep_number:
        try:
            is_rep_number = is_known_rep_phone(from_number)
        except Exception:
            logger.exception("Sheet-based rep phone lookup failed for %s", from_number)
    if is_rep_number and (
        context_id is not None or store.get_customer(from_number) is None
    ):
        if msg_type == "text":
            text = message.get("text", {}).get("body", "").strip()
            if text:
                background_tasks.add_task(_process_rep_reply, from_number, text, message_id, context_id)
        else:
            logger.info("Ignoring non-text message from rep phone %s", from_number)
        return {"status": "accepted"}

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
        audio = message.get("audio", {})
        background_tasks.add_task(_process_audio_message, from_number, audio.get("id"), message_id)

    else:
        logger.info("Ignoring unsupported message type '%s' from %s", msg_type, from_number)
        return {"status": "ignored_unsupported_type"}

    return {"status": "accepted"}


def _process_rep_reply(rep_phone: str, text: str, message_id: str, context_id: str | None):
    """Captures a sales rep's WhatsApp reply to an escalation alert and links
    it to the right lead (see storage.store.resolve_rep_reply_lead). This
    must NEVER call _send()/send_text_message() - a rep replying to an
    internal alert must not receive an AI-generated chatbot reply. Also
    deliberately does not write to `conversations` (customer-transcript
    data) so rep replies don't pollute the dashboard's per-customer view."""
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    escalation_attempt_id, lead_id, method = store.resolve_rep_reply_lead(rep_phone, context_id)
    store.record_rep_reply(rep_phone, text, message_id, context_id, escalation_attempt_id, lead_id, method)
    logger.info("Recorded rep reply from %s (lead_id=%s, method=%s)", rep_phone, lead_id, method)


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


def _process_audio_message(phone: str, media_id: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    if not config.GROQ_API_KEY:
        # No transcription configured - keep the old, honest behavior
        # instead of silently dropping the voice note.
        store.log_message(phone, "in", "[voice note]")
        _send(
            phone,
            "Thanks for the voice note! I can't listen to audio yet - could you please type your question, "
            "or send a photo of the product instead? You can also call us at +971 800 98784.",
        )
        return

    if not media_id:
        store.log_message(phone, "in", "[voice note]")
        _send(phone, "Sorry, I couldn't receive that voice note. Could you try sending it again?")
        return

    try:
        from whatsapp.client import download_media
        from ai.transcription_client import transcribe_audio

        audio_bytes = download_media(media_id)
        transcript = transcribe_audio(audio_bytes)
        logger.info("Transcribed voice note from %s: %r", phone, transcript)

        # Prefix so the transcript view shows this originated as a voice
        # note, while the substantive text still flows through the normal
        # AI reply / lead-detection pipeline exactly like a typed message.
        handle_customer_message(phone, f"[voice note] {transcript}")
    except Exception:
        logger.exception("Failed to transcribe/handle voice note from %s", phone)
        store.log_message(phone, "in", "[voice note - transcription failed]")
        _send(
            phone,
            "Sorry, I had trouble understanding that voice note. Could you please type your question, "
            "or contact Würth UAE customer service at +971 800 98784?",
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

    # A known customer saying they've moved companies, or that the company
    # on file is wrong entirely ("now with X", "this number is NOT for X"),
    # means the mapping we have is stale/incorrect - clear it and fall
    # through to the same first-contact lookup below, so either this same
    # message's new company name gets matched immediately, or the customer
    # gets asked for it. Without this, the old company/rep stayed on file
    # forever and every future message kept pointing at the wrong rep.
    if customer and customer.get("company_name") and is_company_change_signal(text):
        logger.info("Company-change signal from %s (was: %s) - clearing stale mapping", phone, customer["company_name"])
        store.upsert_customer(phone, "")
        customer = store.get_customer(phone)

    # A known customer's rep info (name/phone/email) is cached in Postgres
    # from whenever they were first matched - if that's gotten stale (a rep
    # left, their number changed in the sheet), a customer could keep being
    # pointed at the wrong/old rep indefinitely with nothing ever
    # re-checking the sheet. Periodically re-look-up their company and
    # refresh the cached rep fields if the sheet's current data differs.
    if customer and customer.get("company_name"):
        updated_at = customer.get("updated_at")
        is_stale = updated_at is None or (
            datetime.now(timezone.utc) - updated_at
        ).total_seconds() > config.REP_INFO_REFRESH_HOURS * 3600
        if is_stale:
            try:
                fresh = find_rep_for_company(customer["company_name"])
            except Exception:
                fresh = None
                logger.exception("Rep-info refresh lookup failed for company '%s'", customer["company_name"])
            if fresh and (
                fresh["rep_name"] != customer.get("rep_name")
                or fresh["rep_phone"] != customer.get("rep_phone")
                or fresh["rep_email"] != customer.get("rep_email")
            ):
                logger.info(
                    "Refreshing stale rep info for %s (%s): %s/%s -> %s/%s",
                    phone, customer["company_name"], customer.get("rep_name"), customer.get("rep_phone"),
                    fresh["rep_name"], fresh["rep_phone"],
                )
                store.upsert_customer(phone, fresh["company_name"], fresh["rep_name"], fresh["rep_phone"], fresh["rep_email"])
                customer = store.get_customer(phone)
            elif fresh:
                # Data hasn't actually changed - still touch updated_at so
                # we don't re-check the sheet on every message once it's
                # confirmed current, only every REP_INFO_REFRESH_HOURS.
                store.upsert_customer(phone, customer["company_name"], customer.get("rep_name", ""),
                                       customer.get("rep_phone", ""), customer.get("rep_email", ""))
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
            # A newly-identified company (via a company-change signal or on
            # first contact matching the sheet) is itself a signup worth a
            # rep's attention - notify their new rep directly, same as a
            # product-interest lead, so the rep knows this customer just
            # showed up under their book. BUT this must still respect
            # is_auto_reply() - the triggering message can itself be a
            # WhatsApp auto-responder firing back at our own outbound
            # broadcast (which also happens to phone-match a real
            # customer), and that's never a real signup to escalate on.
            history = store.get_recent_history(phone, limit=6)
            reply, is_lead = generate_reply(text, {
                "company_name": rep["company_name"], "rep_name": rep["rep_name"],
                "rep_phone": rep["rep_phone"], "rep_email": rep["rep_email"],
            }, history=history)
            escalate = is_lead or not is_auto_reply(text)
            conversation_id = _send(phone, reply, escalated=escalate)
            if escalate and conversation_id is not None:
                store.get_or_open_lead(phone, conversation_id)
                _notify_escalation(conversation_id, phone, text, customer)
            return
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

    # Second, independent check right before the (paid) escalation WhatsApp
    # send actually fires - generate_reply() already filters auto-replies,
    # but this is cheap insurance against any future code path that sets
    # escalate=True without going through that check, so a templated
    # auto-response can never trigger a real send.
    if escalate and conversation_id is not None and is_auto_reply(text):
        logger.warning("Suppressing escalation for %s - message looks like an auto-reply template: %r", phone, text[:200])
        escalate = False

    if escalate and conversation_id is not None:
        store.get_or_open_lead(phone, conversation_id)
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
                {"type": "text", "parameter_name": "rep_name", "text": sanitize_template_param(target_name or "Würth UAE")},
                {"type": "text", "parameter_name": "customer_phone", "text": customer_phone},
                {"type": "text", "parameter_name": "enquiry_text", "text": sanitize_template_param(message_text)[:1000]},
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

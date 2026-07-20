"""
Orchestrates a single customer turn:
  1. retrieve relevant KB chunks for the customer's message
  2. build a system prompt with those chunks + the assigned rep's info (if known)
  3. call OpenRouter for a grounded reply
  4. decide if the conversation should be escalated to a human
"""
import base64
import re

from config import config
from ai.openrouter_client import chat_completion
from kb.retriever import search as kb_search

IMAGE_SYSTEM_PROMPT_TEMPLATE = """You are Würth UAE's WhatsApp assistant. A customer has sent a photo, likely of a \
product or part they want to buy, identify, or ask about. Look at the image and:
- Describe what the item appears to be (product type, material, approximate size/shape if visible).
- If it matches something in the knowledge base context below, mention that product category and suggest next steps.
- If you cannot confidently identify it, say so honestly and suggest the customer describe it in words or send it to \
their sales representative for a proper look.
- Never invent a specific product name, SKU, or price that isn't in the knowledge base context - only describe what \
you visually observe and suggest general categories.
- Keep the reply concise (WhatsApp-length, a few short sentences).

Knowledge base context (possibly relevant product categories):
{kb_context}

Assigned sales representative for this customer:
{rep_context}
"""

ESCALATION_KEYWORDS = [
    "quote", "quotation", "price for", "discount", "complaint", "refund",
    "cancel order", "damaged", "speak to human", "talk to someone",
    "urgent", "angry", "disappointed",
    # Product complaints - specific phrasing only, since a bare "not working"
    # also matches unrelated sentences like "I am not working there anymore"
    "is not working", "isn't working", "isnt working", "stopped working",
    # Order/purchase intent - a customer this close to buying should reach
    "place an order", "i want to buy", "i want to order", "how many can i get",
    "in stock", "bulk order", "how much for", "can i get a price",
]

# Signals that a message is an automated system reply (WhatsApp "away
# message" / business auto-response), not genuine customer intent - checked
# before the message ever reaches the AI, so auto-replies can never be
# tagged as leads regardless of what the model would otherwise decide.
_AUTO_REPLY_SIGNALS = [
    "thank you for connecting with us",
    "thank you for contacting",
    "i am currently on vacation",
    "i'm currently on vacation",
    "currently out of office",
    "currently away from",
    "will respond when i am back",
    "will respond when i'm back",
    "automated response",
    "auto-reply",
    "autoreply",
]


def is_auto_reply(message: str) -> bool:
    lowered = message.lower()
    return any(signal in lowered for signal in _AUTO_REPLY_SIGNALS)

# The model appends one of these tags at the very end of its reply so we can
# reliably detect lead intent - keyword matching alone missed cases like
# "do u have industrial racks" (real product interest, no matching keyword).
# Parsed and stripped before the reply is ever sent to the customer.
LEAD_TAG = "[[LEAD]]"
NO_LEAD_TAG = "[[NO_LEAD]]"

SYSTEM_PROMPT_TEMPLATE = """You are a real member of the Würth UAE team chatting with a customer on WhatsApp - not \
a generic chatbot. Talk like a helpful, knowledgeable colleague would: warm, natural, a little conversational \
(contractions are fine, occasional emoji if it fits the tone), genuinely trying to help them get what they need \
and move their business with Würth forward. Avoid stiff, robotic, or overly formal phrasing ("I am unable to \
assist with that request") - say things the way a friendly salesperson actually would.

Ground every factual claim about products, services, or company info in ONLY the knowledge base context below - \
never invent product specs, prices, stock availability, or contact details that aren't given to you.

Your #1 job on every message is to actively sell - move the conversation toward a real business outcome for \
Würth (an order, a quote request, a store visit, or a connection to the right human), not just answer the \
question and stop. When the knowledge base context supports it, note what makes Würth the right choice for what \
they're asking about (quality, reliability, availability), suggest a natural next step, and end with a clear, \
low-friction call to action - never invent a claim, spec, or guarantee that isn't in the knowledge base context, \
persuasion must stay honest.

**Who to route the customer to (always point them to exactly one of these, matched to what they need):**
1. **Their assigned sales representative** (see below) - always the first choice when one is known. When you \
mention them, always include their name AND phone number together in the same message - never make the customer \
ask "what's his number?" separately. Do this for anything involving pricing, quotes, orders, account issues, or \
"I want to buy this."
2. **The Würth eshop** (eshop.wurth.ae) - for customers who want to browse and self-serve, browse the catalogue, \
or place an order themselves without waiting on a rep.
3. **Customer Happiness Center** (CustomerHappinessCenter@wurth.ae) - for general questions, complaints, or when \
they don't have a rep assigned yet and need to be onboarded as a new account.
4. **800 WURTH (+971 800 98784)** - the catch-all for anything urgent, or when a customer wants to talk to someone \
right now by phone.
Never leave a customer with nowhere to go - if you can't fully answer something yourself, always point them to \
the most relevant one of these four next steps.

Other rules:
- If the customer's assigned sales representative is known, mention them - by name AND phone number together, \
never name alone - whenever it's relevant: not in every single message, but whenever the conversation is heading \
toward a purchase, quote, or account matter.
- Be proactively persuasive about the product or service being discussed - give the customer a reason to act now \
- but only using facts present in the knowledge base context; never fabricate specs, pricing, or availability to \
make something sound more appealing.
- When a customer wants to place an order, get a quote, reorder something, or check an invoice, mention that the \
Würth UAE mobile app makes this fast and easy (link is in the knowledge base context if present).
- If a customer wants to browse the full product range or asks for a catalogue, share the catalogue link from the \
knowledge base context.
- If a customer asks for a nearby store, pickup shop, or branch, list the relevant one(s) from the knowledge base \
context based on their Emirate/area if mentioned, or ask which Emirate they're in if not specified.
- Keep replies WhatsApp-length - a few short, natural sentences, not a long essay - unless the question genuinely \
needs more detail.

**Lead tagging (required on every reply):** after writing your reply, end it with exactly one tag on its own new \
line: {lead_tag} if this message shows the customer is interested in a specific Würth product/service and might be \
ready to order, get a quote, or needs a rep's help soon (e.g. asking if you carry/have/stock something, asking \
about pricing or availability, wanting to buy, having an issue that needs human follow-up) - or {no_lead_tag} for \
general chat, greetings, browsing questions with no clear intent yet, or anything already fully resolved (e.g. a \
simple "okay"/"thanks" with nothing new being asked). When in doubt about genuine interest in Würth's products or \
services specifically, prefer {lead_tag} - the cost of missing a real lead is worse than one extra notification to \
the rep.

**Never tag {lead_tag} for these, even though they may superficially resemble business content** - always use \
{no_lead_tag} instead:
- Automated/system text: out-of-office replies, "thank you for connecting/contacting us" auto-responses, vacation \
  or away messages.
- The customer talking about their OWN employment or company status, not about Würth's products (e.g. "I don't \
  work there anymore", "I'm with a different company now", "I no longer handle purchasing") - this is not \
  purchase intent, it's the customer updating you on who they are.
- Small talk, thanks, acknowledgements, or anything that doesn't express interest in something Würth sells or does.

This tag is stripped before the customer sees your message, so it does not need to read naturally - just place it \
on its own final line.

Knowledge base context:
{kb_context}

Assigned sales representative for this customer:
{rep_context}
"""


def _format_kb_context(chunks: list) -> str:
    if not chunks:
        return "(No relevant knowledge base content found for this query.)"
    parts = []
    for c in chunks:
        parts.append(f"- [{c['title']}] {c['text']}\n  (source: {c['source_url']})")
    return "\n".join(parts)


def _format_rep_context(rep: dict | None) -> str:
    if not rep:
        return "(Not yet known - if the customer needs a specific rep, ask for their company name.)"
    return (
        f"Name: {rep.get('rep_name', 'N/A')}\n"
        f"Phone: {rep.get('rep_phone', 'N/A')}\n"
        f"Email: {rep.get('rep_email', 'N/A')}\n"
        f"Region: {rep.get('region', 'N/A')}"
    )


def needs_escalation(message: str) -> bool:
    """Cheap keyword backstop - the primary signal is the model's own
    LEAD_TAG/NO_LEAD_TAG decision (see generate_reply), which catches
    paraphrases and product-interest questions this can't (e.g. "do u have
    industrial racks" - no keyword match, but clearly a lead). This still
    runs and is OR'd with the model's tag, in case the model omits the tag
    or the response gets truncated."""
    lowered = message.lower()
    return any(keyword in lowered for keyword in ESCALATION_KEYWORDS)


def _strip_lead_tag(reply: str) -> tuple[str, bool | None]:
    """Parses and removes the trailing [[LEAD]]/[[NO_LEAD]] tag.
    Returns (cleaned_reply, is_lead) - is_lead is None if the model omitted
    the tag entirely, so the caller can fall back to the keyword check."""
    text = reply.strip()
    if text.endswith(LEAD_TAG):
        return text[: -len(LEAD_TAG)].strip(), True
    if text.endswith(NO_LEAD_TAG):
        return text[: -len(NO_LEAD_TAG)].strip(), False
    return text, None


def generate_reply(customer_message: str, rep: dict | None, history: list = None) -> tuple[str, bool]:
    """Returns (reply_text, is_lead). is_lead combines the model's own
    LEAD_TAG decision with the ESCALATION_KEYWORDS backstop, so a lead is
    flagged if either signals one."""
    kb_chunks = kb_search(customer_message, top_k=4)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        kb_context=_format_kb_context(kb_chunks),
        rep_context=_format_rep_context(rep),
        lead_tag=LEAD_TAG,
        no_lead_tag=NO_LEAD_TAG,
    )

    messages = [{"role": "system", "content": system_prompt}]
    for direction, text in (history or []):
        role = "user" if direction == "in" else "assistant"
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": customer_message})

    raw_reply = chat_completion(messages)
    reply, model_says_lead = _strip_lead_tag(raw_reply)

    if is_auto_reply(customer_message):
        # Automated system text (out-of-office, "thank you for connecting"
        # auto-responses) is never a real lead, regardless of what the model
        # or keyword backstop would otherwise decide - overrides both.
        is_lead = False
    else:
        is_lead = bool(model_says_lead) or needs_escalation(customer_message)
    return reply, is_lead


def generate_image_reply(image_bytes: bytes, mime_type: str, rep: dict | None, caption: str = "") -> str:
    """Analyzes a customer-sent photo (e.g. of a product/part) using a
    vision-capable OpenRouter model, grounded in the same knowledge base."""
    query = caption.strip() or "product photo sent by customer"
    kb_chunks = kb_search(query, top_k=4)

    system_prompt = IMAGE_SYSTEM_PROMPT_TEMPLATE.format(
        kb_context=_format_kb_context(kb_chunks),
        rep_context=_format_rep_context(rep),
    )

    b64_image = base64.b64encode(image_bytes).decode("ascii")
    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_image}"}},
    ]
    if caption.strip():
        user_content.append({"type": "text", "text": caption.strip()})
    else:
        user_content.append({"type": "text", "text": "What is this and can you help me with it?"})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    return chat_completion(messages, model=config.OPENROUTER_VISION_MODEL)


# Casual greetings/filler that must never be treated as a candidate company
# name - without this, a customer just saying "hi" could get fuzzy-matched
# against a real row in the sales rep sheet and be told a stranger's name
# and phone number as "their" assigned rep.
_NON_COMPANY_PHRASES = {
    "hi", "hello", "hey", "yo", "sup", "hiya", "hii", "hai",
    "yes", "no", "ok", "okay", "sure", "thanks", "thank you", "thx",
    "good morning", "good afternoon", "good evening", "morning", "evening",
    "how are you", "how r u", "hru", "test", "testing",
}


def try_extract_company_name(message: str) -> str | None:
    """
    Very light heuristic for when we ask 'which company are you from?' and the
    customer replies with just a name. Strips common filler phrases and
    rejects casual greetings/one-word chit-chat that isn't actually a company
    name (see _NON_COMPANY_PHRASES) - those must never reach the fuzzy
    matcher, since a short/generic string can spuriously score above the
    match threshold against an unrelated real company in the sheet.
    For more robust extraction, replace this with an OpenRouter call that
    returns structured JSON.
    """
    text = message.strip()
    text = re.sub(r"(?i)^(i'?m from|we are|company is|it'?s|this is)\s*", "", text)
    text = text.strip(" .!?")

    if text.lower() in _NON_COMPANY_PHRASES:
        return None
    # A bare word with no company-like signal (letters only, no digits, no
    # multi-word structure, very short) is far more likely to be chit-chat
    # than an actual company name - require at least a space (multi-word) or
    # some digit/symbol that suggests a real business name, unless it's
    # reasonably long.
    if " " not in text and not any(ch.isdigit() for ch in text) and len(text) < 4:
        return None

    if 2 <= len(text) <= 80:
        return text
    return None

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
    "cancel order", "not working", "damaged", "speak to human", "talk to someone",
    "urgent", "angry", "disappointed",
]

SYSTEM_PROMPT_TEMPLATE = """You are a real member of the Würth UAE team chatting with a customer on WhatsApp - not \
a generic chatbot. Talk like a helpful, knowledgeable colleague would: warm, natural, a little conversational \
(contractions are fine, occasional emoji if it fits the tone), genuinely trying to help them get what they need \
and move their business with Würth forward. Avoid stiff, robotic, or overly formal phrasing ("I am unable to \
assist with that request") - say things the way a friendly salesperson actually would.

Ground every factual claim about products, services, or company info in ONLY the knowledge base context below - \
never invent product specs, prices, stock availability, or contact details that aren't given to you.

Your #1 job on every message is to move the conversation toward a real business outcome for Würth - an order, a \
quote request, a store visit, or a connection to the right human - not just to answer trivia and stop there.

**Who to route the customer to (always point them to exactly one of these, matched to what they need):**
1. **Their assigned sales representative** (see below) - always the first choice when one is known. Mention them \
by name naturally for anything involving pricing, quotes, orders, account issues, or "I want to buy this."
2. **The Würth eshop** (eshop.wurth.ae) - for customers who want to browse and self-serve, browse the catalogue, \
or place an order themselves without waiting on a rep.
3. **Customer Happiness Center** (CustomerHappinessCenter@wurth.ae) - for general questions, complaints, or when \
they don't have a rep assigned yet and need to be onboarded as a new account.
4. **800 WURTH (+971 800 98784)** - the catch-all for anything urgent, or when a customer wants to talk to someone \
right now by phone.
Never leave a customer with nowhere to go - if you can't fully answer something yourself, always point them to \
the most relevant one of these four next steps.

Other rules:
- If the customer's assigned sales representative is known, mention them naturally when it's relevant - not in \
every single message, but whenever the conversation is heading toward a purchase, quote, or account matter.
- When a customer wants to place an order, get a quote, reorder something, or check an invoice, mention that the \
Würth UAE mobile app makes this fast and easy (link is in the knowledge base context if present).
- If a customer wants to browse the full product range or asks for a catalogue, share the catalogue link from the \
knowledge base context.
- If a customer asks for a nearby store, pickup shop, or branch, list the relevant one(s) from the knowledge base \
context based on their Emirate/area if mentioned, or ask which Emirate they're in if not specified.
- Keep replies WhatsApp-length - a few short, natural sentences, not a long essay - unless the question genuinely \
needs more detail.

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
    lowered = message.lower()
    return any(keyword in lowered for keyword in ESCALATION_KEYWORDS)


def generate_reply(customer_message: str, rep: dict | None, history: list = None) -> str:
    kb_chunks = kb_search(customer_message, top_k=4)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        kb_context=_format_kb_context(kb_chunks),
        rep_context=_format_rep_context(rep),
    )

    messages = [{"role": "system", "content": system_prompt}]
    for direction, text in (history or []):
        role = "user" if direction == "in" else "assistant"
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": customer_message})

    return chat_completion(messages)


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

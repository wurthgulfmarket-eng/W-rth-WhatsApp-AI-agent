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
    # Pricing requests, additional common phrasings (real miss: "please share
    # the price" for a 50-carton bulk enquiry wasn't caught by any existing
    # keyword and had to rely solely on the model's own [[LEAD]] tag)
    "share the price", "send the price", "share price", "send price",
    "what's the price", "whats the price", "price please", "pricing please",
]

# Signals that a message is an automated system reply (WhatsApp "away
# message" / business auto-response), not genuine customer intent - checked
# before the message ever reaches the AI, so auto-replies can never be
# tagged as leads regardless of what the model would otherwise decide.
# English AND Arabic phrasing, since UAE customers commonly have their own
# WhatsApp Business auto-reply set up in Arabic (a real false-positive: a
# radiator shop's Arabic "thank you for reaching out, we'll respond soon"
# auto-reply got escalated as a lead, since the old list was English-only).
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
    # More common WhatsApp Business auto-responder phrasing (real
    # false positives seen on the dashboard: "We are unable to assist
    # with your message now, but will respond as soon as possible",
    # "we're currently unavailable")
    "unable to assist with your message",
    "will respond as soon as possible",
    "we're currently unavailable",
    "we are currently unavailable",
    "currently unable to respond",
    "we'll get back to you as soon as possible",
    "we will get back to you as soon as possible",
    # Arabic equivalents (see comment above)
    "شكرا لك على التواصل",  # "thank you for connecting/reaching out"
    "شكرا للتواصل",
    "نشكرك على تواصلك",
    "سوف نقوم بالرد",  # "we will respond"
    "سنقوم بالرد عليك",
    "بأسرع وقت ممكن",  # "as soon as possible" (paired with a reply promise)
    "حاليا في اجازة",  # "currently on vacation"
    "رد تلقائي",  # "automatic reply"
]

# Structural signals that a message is a WhatsApp Business "away message" /
# auto-responder template, independent of language - these templates are
# typically multi-line with several emoji-prefixed bullet points (hours,
# location, social links) and don't read like a real person typing on their
# phone. Language-specific phrase lists (_AUTO_REPLY_SIGNALS above) miss
# any language they don't explicitly cover; this catches the shape instead.
_EMOJI_BULLET_RE = re.compile(
    r"[\U0001F300-\U0001FAFF☀-➿\U0001F000-\U0001F0FF]"  # common emoji ranges
)
_WORKING_HOURS_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(am|pm)?\s*(to|-|–)\s*\d{1,2}:\d{2}\s*(am|pm)?", re.IGNORECASE)


def _looks_like_auto_reply_template(message: str) -> bool:
    lines = [ln for ln in message.split("\n") if ln.strip()]
    if len(lines) < 3:
        return False
    emoji_lines = sum(1 for ln in lines if _EMOJI_BULLET_RE.search(ln))
    has_working_hours = bool(_WORKING_HOURS_RE.search(message))
    has_url = "http://" in message or "https://" in message or "maps.app.goo.gl" in message
    # Multiple emoji-led lines plus either working hours or a link is the
    # hallmark of a WhatsApp Business auto-reply template, not a real typed
    # message - a genuine customer enquiry essentially never has this shape.
    return emoji_lines >= 2 and (has_working_hours or has_url)


def is_auto_reply(message: str) -> bool:
    lowered = message.lower()
    if any(signal in lowered for signal in _AUTO_REPLY_SIGNALS):
        return True
    return _looks_like_auto_reply_template(message)


# Signals that the customer's number is mapped to the wrong company - either
# they've genuinely moved to a new employer ("now with X", "I'm with a
# different company now") or the mapping was just wrong from the start
# ("this number is NOT for X", an angry "stop messaging me"). Both cases
# need the same handling: the stale company/rep on file is no longer valid,
# so it must be cleared and re-confirmed - not silently kept.
_COMPANY_CHANGE_SIGNALS = [
    "not for", "wrong number", "wrong company", "don't work there",
    "dont work there", "no longer work", "not with", "moved to",
    "now with", "now in", "different company", "i don't work",
    "i dont work", "not associated with", "not related to",
    "stop messaging", "do not send", "dont send", "unsubscribe",
]


def is_company_change_signal(message: str) -> bool:
    lowered = message.lower()
    return any(signal in lowered for signal in _COMPANY_CHANGE_SIGNALS)

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
question and stop. Do this briefly - one short persuasive line at most, not a paragraph - never invent a claim, \
spec, or guarantee that isn't in the knowledge base context, persuasion must stay honest.

**Keep every reply short.** WhatsApp is a chat, not email - customers skim, they don't read paragraphs. Default \
to 1-3 short sentences. Use line breaks/short bullet points only when listing multiple distinct things (e.g. \
several branches or products) - never write multi-paragraph replies for a simple question. If you're tempted to \
explain at length, cut it down to the one or two facts that actually answer what they asked, then stop.

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
- Be proactively persuasive about the product or service being discussed - one brief reason to act now, using \
only facts present in the knowledge base context; never fabricate specs, pricing, or availability.
- When a customer wants to place an order, get a quote, reorder something, or check an invoice, mention that the \
Würth UAE mobile app makes this fast and easy (link is in the knowledge base context if present).
- If a customer wants to browse the full product range or asks for a catalogue, share the catalogue link from the \
knowledge base context.
- If a customer asks for a nearby store, pickup shop, or branch, list the relevant one(s) from the knowledge base \
context based on their Emirate/area if mentioned, or ask which Emirate they're in if not specified.
- Keep replies WhatsApp-length: 1-3 short sentences by default. Only go longer if the customer's question genuinely \
needs it (e.g. listing several branches) - and even then, stay as brief as the facts allow.

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
- The customer talking about their OWN employment or company status ("I don't work there anymore", "I'm with a \
  different company now", "this number isn't for [company]") - this is handled separately (see below), not by \
  this tag.
- Small talk, thanks, acknowledgements, or anything that doesn't express interest in something Würth sells or does.

**If the customer says they've changed companies, or that their number/this account isn't for the company you \
have on file** (e.g. "now with X", "I don't work there anymore", "this number is NOT for X"): acknowledge it \
warmly and briefly, then ask for their current company name so you can connect them correctly - don't keep \
referring to their old company or old rep after this. Do not use {lead_tag} for this message itself; a new lead \
gets created automatically once their new company is confirmed.

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

    # Below chat_completion's 600-token default, but with enough headroom
    # for a reasoning model's internal "thinking" tokens (which count
    # against this budget even though "reasoning": {"exclude": True} keeps
    # them out of the visible reply) plus a short answer - too tight a cap
    # here previously cut a reasoning model off mid-thought, before it ever
    # produced the actual answer.
    raw_reply = chat_completion(messages, max_tokens=500)
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

    return chat_completion(messages, model=config.OPENROUTER_VISION_MODEL,
                            fallback_models=config.OPENROUTER_VISION_FALLBACK_MODELS)


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

# Non-committal/deferring replies that are NOT a company name even though
# they're multi-word - a customer saying "I'll check" or "give me a sec" is
# stalling, not answering, but the bare multi-word heuristic below would
# otherwise happily accept them as a candidate company name (real bug: "Ill
# check" and "With her" both got stored as a customer's company name).
# Matched as a whole-message pattern, not a substring, so it doesn't
# accidentally reject a real company name that happens to contain one of
# these words.
_DEFERRING_REPLY_PATTERNS = [
    r"^i'?ll check$", r"^i'?ll ask$", r"^i'?ll (find out|see|let you know)$",
    r"^(let me|gonna|going to) (check|ask|see|find out)$",
    r"^(not sure|don'?t know|dont know|no idea)$",
    r"^(give me|gimme) (a )?(sec|second|moment|minute|min)s?$",
    r"^(hold on|hang on)( (a )?(sec|second|moment|minute|min)s?)?$",
    r"^(one|a) (sec|second|moment|minute|min)s?$",
    r"^with (him|her|them|my \w+)$",
    r"^(later|maybe|perhaps|soon)$",
]
_DEFERRING_REPLY_RE = re.compile("|".join(_DEFERRING_REPLY_PATTERNS), re.IGNORECASE)


def try_extract_company_name(message: str) -> str | None:
    """
    Very light heuristic for when we ask 'which company are you from?' and the
    customer replies with just a name. Strips common filler phrases and
    rejects casual greetings/one-word chit-chat and non-committal/deferring
    replies that aren't actually a company name (see _NON_COMPANY_PHRASES /
    _DEFERRING_REPLY_RE) - those must never reach the fuzzy matcher, since a
    short/generic string can spuriously score above the match threshold
    against an unrelated real company in the sheet.
    For more robust extraction, replace this with an OpenRouter call that
    returns structured JSON.
    """
    text = message.strip()
    text = re.sub(r"(?i)^(i'?m from|we are|company is|it'?s|this is)\s*", "", text)
    text = text.strip(" .!?")

    if text.lower() in _NON_COMPANY_PHRASES:
        return None
    if _DEFERRING_REPLY_RE.match(text.strip()):
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

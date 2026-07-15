"""
Orchestrates a single customer turn:
  1. retrieve relevant KB chunks for the customer's message
  2. build a system prompt with those chunks + the assigned rep's info (if known)
  3. call OpenRouter for a grounded reply
  4. decide if the conversation should be escalated to a human
"""
import re

from ai.openrouter_client import chat_completion
from kb.retriever import search as kb_search

ESCALATION_KEYWORDS = [
    "quote", "quotation", "price for", "discount", "complaint", "refund",
    "cancel order", "not working", "damaged", "speak to human", "talk to someone",
    "urgent", "angry", "disappointed",
]

SYSTEM_PROMPT_TEMPLATE = """You are Würth UAE's WhatsApp assistant. You help customers with questions about \
Würth's products, services, and company information, using ONLY the knowledge base context provided below. \
Be concise (WhatsApp-length replies, a few short sentences, not long essays). Be friendly and professional.

Rules:
- If the knowledge base context does not contain the answer, say you're not fully sure and that their sales \
representative can help further - do not invent product specs, prices, or stock availability.
- Never make up a sales representative's name or contact details - only use what is given to you below.
- If the customer's assigned sales representative is known, mention them naturally when relevant (e.g. when the \
customer asks for a quote, pricing, order status, or wants to speak to someone).
- Keep replies under 80 words unless the question genuinely requires more detail.

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


def try_extract_company_name(message: str) -> str | None:
    """
    Very light heuristic for when we ask 'which company are you from?' and the
    customer replies with just a name. Strips common filler phrases.
    For more robust extraction, replace this with an OpenRouter call that
    returns structured JSON.
    """
    text = message.strip()
    text = re.sub(r"(?i)^(i'?m from|we are|company is|it'?s|this is)\s*", "", text)
    text = text.strip(" .")
    if 2 <= len(text) <= 80:
        return text
    return None

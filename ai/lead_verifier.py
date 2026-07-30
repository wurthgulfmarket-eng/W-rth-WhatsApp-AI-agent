"""
"Head of WhatsApp Replies" - a second-opinion AI check on top of the
primary lead-detection pipeline in ai/agent.py. Only ever called when the
primary pipeline (the model's own [[LEAD:...]] tag, ESCALATION_KEYWORDS
backstop, and is_auto_reply() override) has ALREADY decided a message is
a lead - this can downgrade that call to "not a lead", never upgrade a
message the primary pipeline decided against.

Grounded with real few-shot examples pulled live from Postgres: genuine
leads from the leads table, and confirmed false positives from
lead_false_positives (populated by the dashboard's "Mark as false
positive" action) - a growing bank built from real production data
instead of a hand-maintained phrase list, which kept missing new
auto-reply phrasing variants no matter how many rounds of patching.
"""
import logging

from config import config
from ai.openrouter_client import chat_completion, OpenRouterError
from storage import store

logger = logging.getLogger("wurth-agent.lead_verifier")

_HEAD_SYSTEM_PROMPT = """You are the Head of WhatsApp Replies at Würth UAE. A junior system has already \
flagged the message below as a genuine sales lead. Your ONLY job is to sanity-check that call: is this \
truly a real customer expressing buying interest, or is it actually an automated WhatsApp Business reply \
(out-of-office, "thank you for contacting us", a welcome/greeting template, business-card-style contact \
info, or similar) that slipped through?

Real leads look like: a person asking about a specific product, requesting a quote/price, wanting to place \
or check an order, reporting an urgent issue, or expressing clear buying intent - in their own words.

Auto-replies look like: generic away-message phrasing, "we'll get back to you" boilerplate, business \
hours/contact-info dumps, or a greeting that reads like a template rather than something a person typed \
to ask for something.

Examples of REAL leads (answer LEAD for messages like these):
{positive_examples}

Examples of AUTO-REPLIES incorrectly flagged before (answer NOT_LEAD for messages like these):
{negative_examples}

Reply with exactly one word: LEAD or NOT_LEAD. Nothing else."""


def _format_examples(examples: list) -> str:
    if not examples:
        return "(none yet)"
    return "\n".join(f"- {text}" for text in examples)


def verify_is_lead(message: str) -> bool:
    """Second-opinion check - only call this when the primary pipeline has
    already decided is_lead=True. Returns True (uphold the lead) unless
    the Head confidently says NOT_LEAD. Fails open (True) on any error,
    ambiguous response, or before enough negative examples exist to ground
    a real decision - a Head-call failure or cold start must never
    silently kill a real lead."""
    try:
        negative_count = store.count_negative_lead_examples()
    except Exception:
        logger.exception("Failed to count negative lead examples - skipping Head check, upholding lead")
        return True

    if negative_count < config.LEAD_VERIFIER_MIN_NEGATIVE_EXAMPLES:
        logger.info(
            "Head of WhatsApp Replies dormant (%d/%d false-positive examples marked) - upholding lead",
            negative_count, config.LEAD_VERIFIER_MIN_NEGATIVE_EXAMPLES,
        )
        return True

    try:
        positive_examples = store.get_recent_positive_lead_examples(config.LEAD_VERIFIER_MAX_POSITIVE_EXAMPLES)
        negative_examples = store.get_recent_negative_lead_examples(config.LEAD_VERIFIER_MAX_NEGATIVE_EXAMPLES)
    except Exception:
        logger.exception("Failed to fetch Head-of-Replies example bank - skipping check, upholding lead")
        return True

    system_prompt = _HEAD_SYSTEM_PROMPT.format(
        positive_examples=_format_examples(positive_examples),
        negative_examples=_format_examples(negative_examples),
    )

    try:
        response = chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_tokens=10,
            model=config.LEAD_VERIFIER_MODEL,
            fallback_models=config.LEAD_VERIFIER_FALLBACK_MODELS,
        )
    except OpenRouterError as e:
        logger.warning("Head-of-Replies call failed, upholding lead: %s", e)
        return True

    verdict_is_not_lead = "NOT_LEAD" in response.upper()
    if verdict_is_not_lead:
        logger.info("Head of WhatsApp Replies overruled a lead call: %r", message[:200])
    return not verdict_is_not_lead

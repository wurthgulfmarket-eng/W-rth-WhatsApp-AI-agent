"""
One-off: re-scans every historical rep_replies row (oldest-first per lead)
through the newly-widened try_extract_rep_outcome_signal() phrase matching
and applies whatever outcome signals it now finds via store.set_lead_outcome
- exactly what would have happened live had this phrase list been in place
from the start.

Needed because ai.agent.try_extract_rep_outcome_signal only runs on a NEW
incoming rep reply (see main.py's _process_rep_reply) - widening the phrase
list only affects replies received after the deploy, so leads whose rep
already wrote things like "Order placed" or "Already connected with..."
before this fix stayed stuck at their old outcome (usually "new") despite
the rep clearly reporting real progress.

Processes replies oldest-to-newest per lead, so a lead ends up at whatever
its LAST real signal was - the same "most recent reply wins" behavior the
live pipeline already has. A reply with no detectable signal is skipped
(never resets a lead back to "new").

Run this in Render's Shell tab (has the correct env vars already loaded):
    python scripts/backfill_rep_outcomes.py
"""
from collections import defaultdict

from ai.agent import try_extract_rep_outcome_signal
from storage import store
from storage.store import _get_conn, _put_conn


def _get_all_rep_replies_oldest_first():
    """All rep_replies rows with a resolved lead_id, oldest-first overall
    (grouped by lead below) - unlike get_rep_replies_list (dashboard-facing,
    most-recent-first, date-range-limited), this needs every row ever, in
    chronological order, to replay history correctly."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lead_id, rep_phone, reply_text, created_at
                FROM rep_replies
                WHERE lead_id IS NOT NULL
                ORDER BY lead_id, created_at ASC
            """)
            return cur.fetchall()
    finally:
        _put_conn(conn)


def run(rows):
    by_lead = defaultdict(list)
    for lead_id, rep_phone, reply_text, created_at in rows:
        by_lead[lead_id].append((rep_phone, reply_text, created_at))

    updated_leads = 0
    applied_signals = 0
    for lead_id, replies in by_lead.items():
        last_outcome = None
        for rep_phone, reply_text, created_at in replies:
            signal = try_extract_rep_outcome_signal(reply_text)
            if not signal:
                continue
            outcome, amount = signal
            store.set_lead_outcome(lead_id, outcome, updated_by=f"backfill:rep:{rep_phone}", amount=amount)
            last_outcome = outcome
            applied_signals += 1
            print(f"  lead_id={lead_id}: {reply_text[:60]!r} -> {outcome}" + (f" (AED {amount})" if amount else ""))
        if last_outcome:
            updated_leads += 1

    print(f"\nDone. {updated_leads} lead(s) had at least one detectable outcome signal "
          f"({applied_signals} total signal(s) applied across their reply history).")
    print("Check the dashboard's Recent Leads / Outcome column for the updated results.")


if __name__ == "__main__":
    rows = _get_all_rep_replies_oldest_first()
    print(f"Found {len(rows)} rep replies with a resolved lead across all time.")
    run(rows)

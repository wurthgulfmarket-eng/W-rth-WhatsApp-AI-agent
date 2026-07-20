"""
Postgres-backed storage (Render's own managed Postgres) - tracks which
company each WhatsApp number belongs to, and logs conversations. Required
for persistence: Render's free tier web service filesystem is ephemeral and
wipes SQLite on every deploy/restart, which is why usage history didn't
survive redeploys before this.

Uses a small connection pool (psycopg2.pool) rather than one connection per
thread, since FastAPI's BackgroundTasks can run on different threads and a
free-tier Postgres instance has a limited max connection count.
"""
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor

from config import config

logger = logging.getLogger("wurth-agent.store")

_pool = None

# scheme://user:password@host:port/dbname - captured with a regex instead of
# urllib.parse.urlsplit() because urlsplit() itself chokes on raw special
# characters (e.g. "<") in an unencoded password before we get a chance to
# encode it - it misreads "<3" as a possible IPv6-bracket host and raises
# ValueError. A regex lets us grab the password as opaque text first.
_DSN_PATTERN = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?P<user>[^:@/]*):(?P<password>.*)@(?P<rest>[^@]+)$")


def _normalized_dsn(raw_url: str) -> str:
    """Re-encodes the password component so special characters (@, <, etc.)
    pasted directly into DATABASE_URL don't break URL parsing."""
    match = _DSN_PATTERN.match(raw_url)
    if not match:
        return raw_url

    scheme, user, password, rest = match.group("scheme", "user", "password", "rest")
    return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{rest}"


def _get_pool():
    global _pool
    if _pool is None:
        if not config.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set - the app needs a Postgres connection string "
                "(e.g. Render's Internal Database URL from its managed Postgres) to "
                "store conversations and customer records."
            )
        dsn = _normalized_dsn(config.DATABASE_URL)
        _pool = pg_pool.ThreadedConnectionPool(1, 10, dsn)
        _init_schema()
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn):
    _pool.putconn(conn)


def _init_schema():
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                phone TEXT PRIMARY KEY,
                company_name TEXT,
                rep_name TEXT,
                rep_phone TEXT,
                rep_email TEXT,
                updated_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                phone TEXT,
                direction TEXT,        -- 'in' or 'out'
                message TEXT,
                escalated INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_phone ON conversations (phone);
            CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations (created_at);

            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                processed_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS escalation_attempts (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                customer_phone TEXT NOT NULL,
                target_type TEXT NOT NULL,        -- 'rep' or 'ops_fallback'
                target_phone TEXT NOT NULL,
                target_name TEXT,
                message_type TEXT NOT NULL,       -- 'template' or 'freeform'
                template_name TEXT,
                success INTEGER NOT NULL,
                whatsapp_message_id TEXT,         -- for future delivery-status webhook correlation
                error_detail TEXT,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_escalation_attempts_conversation ON escalation_attempts (conversation_id);
            CREATE INDEX IF NOT EXISTS idx_escalation_attempts_phone ON escalation_attempts (customer_phone);
            CREATE INDEX IF NOT EXISTS idx_escalation_attempts_created_at ON escalation_attempts (created_at);

            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                first_conversation_id INTEGER REFERENCES conversations(id),
                last_conversation_id INTEGER REFERENCES conversations(id),
                opened_at TIMESTAMPTZ NOT NULL,
                last_activity_at TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                followup_sent_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads (phone);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
            CREATE INDEX IF NOT EXISTS idx_leads_last_activity ON leads (last_activity_at);

            CREATE TABLE IF NOT EXISTS rep_replies (
                id SERIAL PRIMARY KEY,
                rep_phone TEXT NOT NULL,
                reply_text TEXT NOT NULL,
                whatsapp_message_id TEXT NOT NULL,
                context_message_id TEXT,
                escalation_attempt_id INTEGER REFERENCES escalation_attempts(id),
                lead_id INTEGER REFERENCES leads(id),
                resolution_method TEXT NOT NULL,   -- 'context_match' | 'fallback_most_recent' | 'unresolved'
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rep_replies_lead ON rep_replies (lead_id);
            CREATE INDEX IF NOT EXISTS idx_rep_replies_rep_phone ON rep_replies (rep_phone);
            """)

            # One-time backfill: group historical escalated messages into
            # leads using the same rolling-window logic as get_or_open_lead,
            # so leads created before this table existed still show up
            # deduplicated instead of leaving `leads` empty for old data.
            cur.execute("SELECT 1 FROM leads LIMIT 1")
            if cur.fetchone() is None:
                cur.execute(f"""
                    WITH escalated AS (
                        SELECT id, phone, created_at,
                               LAG(created_at) OVER (PARTITION BY phone ORDER BY created_at) AS prev_created_at
                        FROM conversations
                        WHERE direction = 'out' AND escalated = 1
                    ),
                    grouped AS (
                        SELECT id, phone, created_at,
                               SUM(CASE
                                   WHEN prev_created_at IS NULL
                                        OR created_at - prev_created_at > INTERVAL '{config.LEAD_DEDUP_WINDOW_HOURS} hours'
                                   THEN 1 ELSE 0
                               END) OVER (PARTITION BY phone ORDER BY created_at) AS grp
                        FROM escalated
                    )
                    INSERT INTO leads (phone, first_conversation_id, last_conversation_id, opened_at, last_activity_at, status, created_at)
                    SELECT phone,
                           MIN(id) AS first_conversation_id,
                           MAX(id) AS last_conversation_id,
                           MIN(created_at) AS opened_at,
                           MAX(created_at) AS last_activity_at,
                           'closed' AS status,
                           now() AS created_at
                    FROM grouped
                    GROUP BY phone, grp
                """)
        conn.commit()

        # One-time backfill: normalize any customers.rep_phone values stored
        # before to_whatsapp_number() added UAE country-code handling for
        # local-format numbers (e.g. "0501234567"). Without this, a rep's
        # phone on file stays in a format that never matches WhatsApp's own
        # "from" field, so their reply to an escalation silently gets
        # treated as an ordinary customer message instead of being
        # recognized as the rep - a real incident this fixes retroactively
        # for rows already in the database, not just new sheet loads.
        from utils.phone import to_whatsapp_number
        with conn.cursor() as cur:
            cur.execute("SELECT phone, rep_phone FROM customers WHERE rep_phone IS NOT NULL AND rep_phone != ''")
            rows = cur.fetchall()
            for phone, rep_phone in rows:
                normalized = to_whatsapp_number(rep_phone)
                if normalized != rep_phone:
                    cur.execute("UPDATE customers SET rep_phone = %s WHERE phone = %s", (normalized, phone))
        conn.commit()
    finally:
        _pool.putconn(conn)


def already_processed(message_id: str) -> bool:
    """Meta retries webhook delivery if it doesn't get a fast 200 response,
    which can redeliver the same message_id. Use this to avoid double-replying."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
            return cur.fetchone() is not None
    finally:
        _put_conn(conn)


def mark_processed(message_id: str):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processed_messages (message_id, processed_at) VALUES (%s, %s) "
                "ON CONFLICT (message_id) DO NOTHING",
                (message_id, datetime.now(timezone.utc)),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def get_customer(phone: str):
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT phone, company_name, rep_name, rep_phone, rep_email FROM customers WHERE phone = %s",
                (phone,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _put_conn(conn)


def upsert_customer(phone: str, company_name: str, rep_name: str = "", rep_phone: str = "", rep_email: str = ""):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customers (phone, company_name, rep_name, rep_phone, rep_email, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (phone) DO UPDATE SET
                    company_name = EXCLUDED.company_name,
                    rep_name = EXCLUDED.rep_name,
                    rep_phone = EXCLUDED.rep_phone,
                    rep_email = EXCLUDED.rep_email,
                    updated_at = EXCLUDED.updated_at
            """, (phone, company_name, rep_name, rep_phone, rep_email, datetime.now(timezone.utc)))
        conn.commit()
    finally:
        _put_conn(conn)


def log_message(phone: str, direction: str, message: str, escalated: bool = False) -> int:
    """Returns the new row's id, so callers (e.g. escalation notification)
    can attach related records (escalation_attempts) to this conversation."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (phone, direction, message, escalated, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (phone, direction, message, int(escalated), datetime.now(timezone.utc)),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        _put_conn(conn)


def record_escalation_attempt(
    conversation_id: int | None, customer_phone: str, target_type: str, target_phone: str,
    target_name: str | None, message_type: str, template_name: str | None,
    success: bool, whatsapp_message_id: str | None, error_detail: str | None,
):
    """Records one notification attempt (success or failure) to a rep or an
    ops-fallback number, so delivery outcomes are visible on the dashboard
    instead of only appearing in logs (which may not be durably retained)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO escalation_attempts (
                    conversation_id, customer_phone, target_type, target_phone, target_name,
                    message_type, template_name, success, whatsapp_message_id, error_detail, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                conversation_id, customer_phone, target_type, target_phone, target_name,
                message_type, template_name, int(success), whatsapp_message_id, error_detail,
                datetime.now(timezone.utc),
            ))
        conn.commit()
    finally:
        _put_conn(conn)


def get_or_open_lead(phone: str, conversation_id: int) -> int:
    """Groups escalated messages from the same customer into one 'lead' as
    long as they keep coming within LEAD_DEDUP_WINDOW_HOURS of the previous
    one - without this, a single back-and-forth enquiry (e.g. 4 messages in
    5 minutes) shows up as 4 separate leads on the dashboard instead of 1.
    A gap longer than the window closes the old lead and starts a new one,
    e.g. the customer returning days later with an unrelated enquiry."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, last_activity_at FROM leads WHERE phone = %s AND status = 'open' "
                "ORDER BY id DESC LIMIT 1",
                (phone,),
            )
            row = cur.fetchone()
            now = datetime.now(timezone.utc)

            if row:
                lead_id, last_activity_at = row
                window_expired = (now - last_activity_at).total_seconds() > config.LEAD_DEDUP_WINDOW_HOURS * 3600
                if not window_expired:
                    cur.execute(
                        "UPDATE leads SET last_conversation_id = %s, last_activity_at = %s WHERE id = %s",
                        (conversation_id, now, lead_id),
                    )
                    conn.commit()
                    return lead_id
                cur.execute("UPDATE leads SET status = 'closed' WHERE id = %s", (lead_id,))

            cur.execute(
                "INSERT INTO leads (phone, first_conversation_id, last_conversation_id, opened_at, "
                "last_activity_at, status, created_at) VALUES (%s, %s, %s, %s, %s, 'open', %s) RETURNING id",
                (phone, conversation_id, conversation_id, now, now, now),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        _put_conn(conn)


def get_leads_needing_followup():
    """Leads that are still open, have an assigned rep with a phone number,
    haven't had a reminder sent yet, and - critically - the rep has NOT
    replied to the original escalation at all (no rep_replies row for this
    lead). This nudges the REP, not the customer, since customers shouldn't
    be pinged twice about the same enquiry; a rep's first reply (any reply,
    action taken or not) is treated as "handled" and stops further
    reminders for this lead until the next one."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT l.id, l.phone, l.last_activity_at,
                       COALESCE(cu.company_name, '') AS company_name,
                       COALESCE(cu.rep_name, '') AS rep_name,
                       COALESCE(cu.rep_phone, '') AS rep_phone
                FROM leads l
                LEFT JOIN customers cu ON cu.phone = l.phone
                WHERE l.status = 'open'
                  AND l.followup_sent_at IS NULL
                  AND COALESCE(cu.rep_phone, '') != ''
                  AND l.last_activity_at <= now() - (%s || ' hours')::interval
                  AND NOT EXISTS (
                      SELECT 1 FROM rep_replies rr WHERE rr.lead_id = l.id
                  )
            """, (config.LEAD_FOLLOWUP_HOURS,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def mark_lead_followup_sent(lead_id: int):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE leads SET followup_sent_at = %s WHERE id = %s", (datetime.now(timezone.utc), lead_id))
        conn.commit()
    finally:
        _put_conn(conn)


def find_rep_matches_for_phone(phone: str) -> bool:
    """True if we've actually sent this phone number a rep escalation alert
    before - evidence-based, not sheet-membership-based, so a stale or
    overlapping rep_phone entry in the sheet can't misroute a real customer's
    message as a rep reply (see resolve_rep_reply_lead / main.py's routing
    guard for the full ambiguity-handling logic)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM escalation_attempts WHERE target_type = 'rep' AND target_phone = %s LIMIT 1",
                (phone,),
            )
            return cur.fetchone() is not None
    finally:
        _put_conn(conn)


def resolve_rep_reply_lead(rep_phone: str, context_message_id: str | None):
    """Figures out which lead a rep's WhatsApp reply is about. If they used
    swipe-to-reply on the exact escalation alert, context_message_id matches
    a stored escalation_attempts.whatsapp_message_id - an exact match. \
    Otherwise falls back to their most recent still-open lead, which can be
    wrong if they were sent two escalations close together (flagged to the
    caller via resolution_method so the dashboard can label it a best guess).
    Returns (escalation_attempt_id, lead_id, resolution_method)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if context_message_id:
                cur.execute("""
                    SELECT ea.id, l.id
                    FROM escalation_attempts ea
                    JOIN leads l ON ea.customer_phone = l.phone
                        AND ea.conversation_id BETWEEN l.first_conversation_id AND l.last_conversation_id
                    WHERE ea.whatsapp_message_id = %s AND ea.target_type = 'rep' AND ea.target_phone = %s
                """, (context_message_id, rep_phone))
                row = cur.fetchone()
                if row:
                    return row[0], row[1], "context_match"

            cur.execute("""
                SELECT l.id
                FROM escalation_attempts ea
                JOIN leads l ON ea.customer_phone = l.phone
                    AND ea.conversation_id BETWEEN l.first_conversation_id AND l.last_conversation_id
                WHERE ea.target_type = 'rep' AND ea.target_phone = %s AND l.status = 'open'
                ORDER BY ea.created_at DESC LIMIT 1
            """, (rep_phone,))
            row = cur.fetchone()
            if row:
                return None, row[0], "fallback_most_recent"

            return None, None, "unresolved"
    finally:
        _put_conn(conn)


def record_rep_reply(
    rep_phone: str, reply_text: str, whatsapp_message_id: str, context_message_id: str | None,
    escalation_attempt_id: int | None, lead_id: int | None, resolution_method: str,
):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO rep_replies (
                    rep_phone, reply_text, whatsapp_message_id, context_message_id,
                    escalation_attempt_id, lead_id, resolution_method, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                rep_phone, reply_text, whatsapp_message_id, context_message_id,
                escalation_attempt_id, lead_id, resolution_method, datetime.now(timezone.utc),
            ))
        conn.commit()
    finally:
        _put_conn(conn)


def get_recent_history(phone: str, limit: int = 6):
    """Returns recent messages oldest-first, for conversation context."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT direction, message FROM conversations WHERE phone = %s ORDER BY id DESC LIMIT %s",
                (phone, limit),
            )
            rows = cur.fetchall()
            return list(reversed(rows))
    finally:
        _put_conn(conn)


# ---- Dashboard / analytics queries ----
# start/end below are "YYYY-MM-DD" strings (inclusive).

def get_stats(start: str = None, end: str = None):
    conn = _get_conn()
    try:
        where, params = _date_where(start, end)
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='in' {where}", params)
            total_in = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='out' {where}", params)
            total_out = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='out' AND escalated=1 {where}", params)
            escalations = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(DISTINCT phone) FROM conversations WHERE 1=1 {where}", params)
            unique_customers = cur.fetchone()[0]
        return {
            "messages_received": total_in,
            "replies_sent": total_out,
            "escalations": escalations,
            "unique_customers": unique_customers,
        }
    finally:
        _put_conn(conn)


def get_daily_counts(start: str = None, end: str = None):
    """Messages received/sent per day, for a simple trend view."""
    conn = _get_conn()
    try:
        where, params = _date_where(start, end)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT to_char(created_at, 'YYYY-MM-DD') AS day,
                       SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS received,
                       SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS sent
                FROM conversations
                WHERE 1=1 {where}
                GROUP BY day
                ORDER BY day
            """, params)
            return [{"day": row[0], "received": row[1], "sent": row[2]} for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_customers_summary(start: str = None, end: str = None):
    """One row per customer who messaged in the range, with message counts and last activity."""
    conn = _get_conn()
    try:
        where, params = _date_where(start, end)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT c.phone,
                       COALESCE(cu.company_name, ''),
                       COALESCE(cu.rep_name, ''),
                       COUNT(*) AS message_count,
                       MAX(c.created_at) AS last_message_at
                FROM conversations c
                LEFT JOIN customers cu ON cu.phone = c.phone
                WHERE 1=1 {where}
                GROUP BY c.phone, cu.company_name, cu.rep_name
                ORDER BY last_message_at DESC
            """, params)
            keys = ["phone", "company_name", "rep_name", "message_count", "last_message_at"]
            return [dict(zip(keys, row)) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_conversation(phone: str, start: str = None, end: str = None):
    """Full transcript for one customer, oldest first."""
    conn = _get_conn()
    try:
        where, params = _date_where(start, end)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT direction, message, escalated, created_at
                FROM conversations
                WHERE phone = %s {where}
                ORDER BY id ASC
            """, (phone, *params))
            keys = ["direction", "message", "escalated", "created_at"]
            return [dict(zip(keys, row)) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_leads_summary(start: str = None, end: str = None):
    """One row per sales rep, counting how many distinct leads (deduplicated
    customer enquiries, see get_or_open_lead) were opened for them in the
    range, how many distinct customers that represents, their most recent
    lead, and how many of those leads' rep-notification attempts failed
    entirely (a fast signal that a rep's phone number might be wrong).
    Counts DISTINCT leads, not raw escalated messages - a customer's 4-message
    back-and-forth about one enquiry counts once, not 4 times."""
    conn = _get_conn()
    try:
        where, params = _date_where(start, end, column="l.opened_at")
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(NULLIF(cu.rep_name, ''), 'Unassigned'),
                       COUNT(DISTINCT l.id) AS lead_count,
                       COUNT(DISTINCT l.phone) AS customer_count,
                       MAX(l.last_activity_at) AS last_lead_at,
                       COUNT(DISTINCT l.id) FILTER (WHERE attempts.attempt_count > 0 AND attempts.any_success IS NOT TRUE) AS failed_notifications
                FROM leads l
                LEFT JOIN customers cu ON cu.phone = l.phone
                LEFT JOIN LATERAL (
                    SELECT BOOL_OR(success = 1) AS any_success, COUNT(*) AS attempt_count
                    FROM escalation_attempts ea
                    WHERE ea.conversation_id BETWEEN l.first_conversation_id AND l.last_conversation_id
                      AND ea.customer_phone = l.phone
                ) attempts ON true
                WHERE 1=1 {where}
                GROUP BY COALESCE(NULLIF(cu.rep_name, ''), 'Unassigned')
                ORDER BY lead_count DESC
            """, params)
            keys = ["rep_name", "lead_count", "customer_count", "last_lead_at", "failed_notifications"]
            return [dict(zip(keys, row)) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_leads_list(start: str = None, end: str = None):
    """Every deduplicated lead in the range, most recent activity first: the
    customer's original enquiry (the inbound message immediately preceding
    the lead's first escalated reply), who it was routed to, current status
    (open/closed - purely time-based, see get_or_open_lead), and
    delivery_status ("delivered" if any escalation_attempts row across the
    whole lead succeeded, "failed" if attempts exist but none succeeded,
    "pending" if none were recorded at all)."""
    conn = _get_conn()
    try:
        where, params = _date_where(start, end, column="l.last_activity_at")
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT l.last_activity_at,
                       l.phone,
                       COALESCE(cu.company_name, ''),
                       COALESCE(NULLIF(cu.rep_name, ''), 'Unassigned'),
                       COALESCE(
                           (SELECT in_msg.message
                            FROM conversations in_msg
                            WHERE in_msg.phone = l.phone
                              AND in_msg.direction = 'in'
                              AND in_msg.id < l.first_conversation_id
                            ORDER BY in_msg.id DESC
                            LIMIT 1),
                           ''
                       ) AS enquiry_text,
                       l.status,
                       attempts.any_success,
                       attempts.attempt_count,
                       attempts.attempt_summary,
                       latest_reply.reply_text,
                       latest_reply.created_at,
                       latest_reply.resolution_method
                FROM leads l
                LEFT JOIN customers cu ON cu.phone = l.phone
                LEFT JOIN LATERAL (
                    SELECT
                        BOOL_OR(success = 1) AS any_success,
                        COUNT(*) AS attempt_count,
                        STRING_AGG(DISTINCT target_type || ':' || CASE WHEN success = 1 THEN 'ok' ELSE 'fail' END, ', ') AS attempt_summary
                    FROM escalation_attempts ea
                    WHERE ea.conversation_id BETWEEN l.first_conversation_id AND l.last_conversation_id
                      AND ea.customer_phone = l.phone
                ) attempts ON true
                LEFT JOIN LATERAL (
                    SELECT reply_text, created_at, resolution_method
                    FROM rep_replies rr
                    WHERE rr.lead_id = l.id
                    ORDER BY rr.created_at DESC
                    LIMIT 1
                ) latest_reply ON true
                WHERE 1=1
                {where}
                ORDER BY l.last_activity_at DESC
            """, params)
            keys = ["created_at", "phone", "company_name", "rep_name", "enquiry_text", "status",
                     "any_success", "attempt_count", "attempt_summary",
                     "rep_reply_text", "rep_reply_at", "rep_reply_method"]
            rows = [dict(zip(keys, row)) for row in cur.fetchall()]
            for row in rows:
                if not row["attempt_count"]:
                    row["delivery_status"] = "pending"
                elif row["any_success"]:
                    row["delivery_status"] = "delivered"
                else:
                    row["delivery_status"] = "failed"
            return rows
    finally:
        _put_conn(conn)


def get_rep_replies_list(start: str = None, end: str = None):
    """Every captured rep reply in the range, most recent first, joined
    back to the lead/customer it was resolved against - a dedicated view of
    rep engagement, separate from the main leads table (which shows only
    the latest reply per lead, not the full reply history)."""
    conn = _get_conn()
    try:
        where, params = _date_where(start, end, column="rr.created_at")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT rr.created_at, rr.rep_phone, rr.reply_text, rr.resolution_method,
                       l.id AS lead_id, l.phone AS customer_phone,
                       COALESCE(cu.company_name, '') AS company_name,
                       COALESCE(cu.rep_name, '') AS rep_name
                FROM rep_replies rr
                LEFT JOIN leads l ON l.id = rr.lead_id
                LEFT JOIN customers cu ON cu.phone = l.phone
                WHERE 1=1 {where}
                ORDER BY rr.created_at DESC
            """, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def get_all_messages(start: str = None, end: str = None):
    """All messages in range, for Excel export."""
    conn = _get_conn()
    try:
        where, params = _date_where(start, end)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT c.created_at, c.phone, COALESCE(cu.company_name, ''), c.direction, c.message, c.escalated
                FROM conversations c
                LEFT JOIN customers cu ON cu.phone = c.phone
                WHERE 1=1 {where}
                ORDER BY c.created_at ASC
            """, params)
            keys = ["created_at", "phone", "company_name", "direction", "message", "escalated"]
            return [dict(zip(keys, row)) for row in cur.fetchall()]
    finally:
        _put_conn(conn)


def _date_where(start: str, end: str, column: str = "created_at"):
    clauses, params = [], []
    if start:
        clauses.append(f"{column} >= %s")
        params.append(f"{start}T00:00:00+00:00")
    if end:
        clauses.append(f"{column} <= %s")
        params.append(f"{end}T23:59:59.999999+00:00")
    where = ("AND " + " AND ".join(clauses)) if clauses else ""
    return where, params

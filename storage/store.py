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
            """)
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


def log_message(phone: str, direction: str, message: str, escalated: bool = False):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (phone, direction, message, escalated, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (phone, direction, message, int(escalated), datetime.now(timezone.utc)),
            )
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


def _date_where(start: str, end: str):
    clauses, params = [], []
    if start:
        clauses.append("created_at >= %s")
        params.append(f"{start}T00:00:00+00:00")
    if end:
        clauses.append("created_at <= %s")
        params.append(f"{end}T23:59:59.999999+00:00")
    where = ("AND " + " AND ".join(clauses)) if clauses else ""
    return where, params

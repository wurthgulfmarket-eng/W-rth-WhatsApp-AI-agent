"""
Lightweight SQLite storage - no external DB needed to get started.
Tracks which company each WhatsApp number belongs to, and logs conversations.
Swap for Postgres later by replacing this module if you outgrow SQLite.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

from config import config

_local = threading.local()


def _get_conn():
    if not hasattr(_local, "conn"):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        _local.conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _init_schema(_local.conn)
    return _local.conn


def _init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        phone TEXT PRIMARY KEY,
        company_name TEXT,
        rep_name TEXT,
        rep_phone TEXT,
        rep_email TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        direction TEXT,        -- 'in' or 'out'
        message TEXT,
        escalated INTEGER DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS processed_messages (
        message_id TEXT PRIMARY KEY,
        processed_at TEXT
    );
    """)
    conn.commit()


def already_processed(message_id: str) -> bool:
    """Meta retries webhook delivery if it doesn't get a fast 200 response,
    which can redeliver the same message_id. Use this to avoid double-replying."""
    conn = _get_conn()
    cur = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
    return cur.fetchone() is not None


def mark_processed(message_id: str):
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
        (message_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_customer(phone: str):
    conn = _get_conn()
    cur = conn.execute("SELECT phone, company_name, rep_name, rep_phone, rep_email FROM customers WHERE phone = ?", (phone,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ["phone", "company_name", "rep_name", "rep_phone", "rep_email"]
    return dict(zip(keys, row))


def upsert_customer(phone: str, company_name: str, rep_name: str = "", rep_phone: str = "", rep_email: str = ""):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO customers (phone, company_name, rep_name, rep_phone, rep_email, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            company_name=excluded.company_name,
            rep_name=excluded.rep_name,
            rep_phone=excluded.rep_phone,
            rep_email=excluded.rep_email,
            updated_at=excluded.updated_at
    """, (phone, company_name, rep_name, rep_phone, rep_email, datetime.now(timezone.utc).isoformat()))
    conn.commit()


def log_message(phone: str, direction: str, message: str, escalated: bool = False):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversations (phone, direction, message, escalated, created_at) VALUES (?, ?, ?, ?, ?)",
        (phone, direction, message, int(escalated), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_recent_history(phone: str, limit: int = 6):
    """Returns recent messages oldest-first, for conversation context."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT direction, message FROM conversations WHERE phone = ? ORDER BY id DESC LIMIT ?",
        (phone, limit),
    )
    rows = cur.fetchall()
    return list(reversed(rows))


# ---- Dashboard / analytics queries ----
# created_at is stored as ISO 8601 UTC (e.g. "2026-07-16T10:23:45.123456+00:00").
# start/end below are "YYYY-MM-DD" strings (inclusive), compared as text - this
# works correctly against ISO-formatted timestamps.

def get_stats(start: str = None, end: str = None):
    conn = _get_conn()
    where, params = _date_where(start, end)

    total_in = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='in' {where}", params).fetchone()[0]
    total_out = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='out' {where}", params).fetchone()[0]
    escalations = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='out' AND escalated=1 {where}", params).fetchone()[0]
    unique_customers = conn.execute(f"SELECT COUNT(DISTINCT phone) FROM conversations WHERE 1=1 {where}", params).fetchone()[0]

    return {
        "messages_received": total_in,
        "replies_sent": total_out,
        "escalations": escalations,
        "unique_customers": unique_customers,
    }


def get_daily_counts(start: str = None, end: str = None):
    """Messages received/sent per day, for a simple trend view."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT substr(created_at, 1, 10) AS day,
               SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS received,
               SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS sent
        FROM conversations
        WHERE 1=1 {where}
        GROUP BY day
        ORDER BY day
    """, params)
    return [{"day": row[0], "received": row[1], "sent": row[2]} for row in cur.fetchall()]


def get_customers_summary(start: str = None, end: str = None):
    """One row per customer who messaged in the range, with message counts and last activity."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT c.phone,
               COALESCE(cu.company_name, ''),
               COALESCE(cu.rep_name, ''),
               COUNT(*) AS message_count,
               MAX(c.created_at) AS last_message_at
        FROM conversations c
        LEFT JOIN customers cu ON cu.phone = c.phone
        WHERE 1=1 {where}
        GROUP BY c.phone
        ORDER BY last_message_at DESC
    """, params)
    keys = ["phone", "company_name", "rep_name", "message_count", "last_message_at"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_conversation(phone: str, start: str = None, end: str = None):
    """Full transcript for one customer, oldest first."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT direction, message, escalated, created_at
        FROM conversations
        WHERE phone = ? {where}
        ORDER BY id ASC
    """, (phone, *params))
    keys = ["direction", "message", "escalated", "created_at"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_all_messages(start: str = None, end: str = None):
    """All messages in range, for Excel export."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT c.created_at, c.phone, COALESCE(cu.company_name, ''), c.direction, c.message, c.escalated
        FROM conversations c
        LEFT JOIN customers cu ON cu.phone = c.phone
        WHERE 1=1 {where}
        ORDER BY c.created_at ASC
    """, params)
    keys = ["created_at", "phone", "company_name", "direction", "message", "escalated"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def _date_where(start: str, end: str):
    clauses, params = [], []
    if start:
        clauses.append("created_at >= ?")
        params.append(f"{start}T00:00:00")
    if end:
        clauses.append("created_at <= ?")
        params.append(f"{end}T23:59:59.999999")
    where = ("AND " + " AND ".join(clauses)) if clauses else ""
    return where, params

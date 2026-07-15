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

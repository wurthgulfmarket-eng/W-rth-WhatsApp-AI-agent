"""
One-off manual resend for a specific escalation that failed because
ESCALATION_NOTIFY_NUMBERS was misconfigured (fixed now). Reuses the app's
own _notify_escalation() so the resend is logged identically to a normal
escalation (visible on the dashboard), rather than sending a raw WhatsApp
message out-of-band.

Run this in Render's Shell tab (has the correct env vars already loaded):
    python scripts/resend_escalation.py
"""
from main import _notify_escalation
from storage import store
from storage.store import _get_conn, _put_conn

CUSTOMER_PHONE = "971544548383"
MESSAGE = (
    "Customer sent an image (joint foam spray fire resistant Wurth - 2 box) "
    "and asked: can you give me price please"
)


def _clear_corrupted_company_name():
    """The pre-fix try_extract_company_name() bug stored this customer's
    question text as their company_name. Clear it so their next message
    re-triggers proper company recognition instead of keeping the bad
    stored text."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE customers SET company_name = NULL WHERE phone = %s AND company_name = %s",
                (CUSTOMER_PHONE, "can you give me price please"),
            )
            print(f"cleared corrupted company_name on {cur.rowcount} row(s)")
        conn.commit()
    finally:
        _put_conn(conn)


if __name__ == "__main__":
    _clear_corrupted_company_name()
    customer = store.get_customer(CUSTOMER_PHONE)
    print(f"customer on file: {customer}")
    _notify_escalation(None, CUSTOMER_PHONE, MESSAGE, customer)
    print("Resend attempted - check the dashboard's Recent Leads / escalation delivery status.")

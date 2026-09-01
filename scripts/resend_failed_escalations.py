"""
One-off: resends every currently-open lead whose escalation delivery never
succeeded ("failed" or "pending") to ESCALATION_NOTIFY_NUMBERS - for leads
that got stuck (e.g. because ESCALATION_NOTIFY_NUMBERS was misconfigured at
the time) and never reached anyone.

Run this in Render's Shell tab (has the correct env vars already loaded):
    python scripts/resend_failed_escalations.py

Safe to run more than once - re-notifying an already-delivered lead is the
only real risk, and this only targets ones that are NOT delivered yet.
"""
from main import _notify_escalation
from storage import store

if __name__ == "__main__":
    leads, _ = store.get_leads_list(page_size=None)
    stuck = [l for l in leads if l["status"] == "open" and l["delivery_status"] != "delivered"]

    print(f"Found {len(stuck)} open, undelivered lead(s) out of {len(leads)} total in range.")
    for lead in stuck:
        phone = lead["phone"]
        message = lead["enquiry_text"] or "(no enquiry text on file)"
        customer = store.get_customer(phone)
        print(f"Resending lead #{lead['lead_id']} for {phone}: {message[:60]!r}")
        _notify_escalation(None, phone, message, customer)

    print("Done - check the dashboard's Recent Leads for updated delivery status.")

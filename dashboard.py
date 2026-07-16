"""
Internal analytics dashboard: message/customer stats, a read-only transcript
viewer, and an Excel export - all reading from the same SQLite database the
webhook writes to (storage/store.py). No separate database or sync needed.

Protected by the same WHATSAPP_VERIFY_TOKEN used for /admin/rebuild-kb -
pass it as ?token=... on every request. This is a shared-secret gate, not
per-user auth; treat the token like a password and only share it with staff
who should see customer phone numbers and message content.
"""
import io
from datetime import date, timedelta

from fastapi import APIRouter, Query, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from config import config
from storage import store

router = APIRouter()


def _check_token(token: str):
    return token == config.WHATSAPP_VERIFY_TOKEN


def _default_date_range():
    end = date.today()
    start = end - timedelta(days=30)
    return start.isoformat(), end.isoformat()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(token: str = "", start: str = "", end: str = "", phone: str = ""):
    if not _check_token(token):
        return HTMLResponse("<h3>Forbidden - missing or invalid token</h3>", status_code=403)

    default_start, default_end = _default_date_range()
    start = start or default_start
    end = end or default_end

    stats = store.get_stats(start, end)
    daily = store.get_daily_counts(start, end)
    customers = store.get_customers_summary(start, end)
    transcript = store.get_conversation(phone, start, end) if phone else None

    return HTMLResponse(_render_dashboard_html(token, start, end, stats, daily, customers, phone, transcript))


@router.get("/dashboard/export")
def export_excel(token: str = "", start: str = "", end: str = ""):
    if not _check_token(token):
        return Response("Forbidden", status_code=403)

    default_start, default_end = _default_date_range()
    start = start or default_start
    end = end or default_end

    from openpyxl import Workbook

    wb = Workbook()

    ws = wb.active
    ws.title = "Messages"
    ws.append(["Timestamp (UTC)", "Phone", "Company", "Direction", "Message", "Escalated"])
    for row in store.get_all_messages(start, end):
        ws.append([
            row["created_at"], row["phone"], row["company_name"],
            "Customer" if row["direction"] == "in" else "Bot",
            row["message"], "Yes" if row["escalated"] else "No",
        ])
    for col_letter, width in zip("ABCDEF", [26, 16, 24, 10, 60, 10]):
        ws.column_dimensions[col_letter].width = width

    ws2 = wb.create_sheet("Customers")
    ws2.append(["Phone", "Company", "Sales Rep", "Message Count", "Last Message (UTC)"])
    for row in store.get_customers_summary(start, end):
        ws2.append([row["phone"], row["company_name"], row["rep_name"], row["message_count"], row["last_message_at"]])
    for col_letter, width in zip("ABCDE", [16, 24, 20, 14, 26]):
        ws2.column_dimensions[col_letter].width = width

    ws3 = wb.create_sheet("Daily Summary")
    ws3.append(["Date", "Messages Received", "Replies Sent"])
    for row in store.get_daily_counts(start, end):
        ws3.append([row["day"], row["received"], row["sent"]])
    for col_letter, width in zip("ABC", [14, 18, 14]):
        ws3.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"wurth-whatsapp-report_{start}_to_{end}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_dashboard_html(token, start, end, stats, daily, customers, selected_phone, transcript):
    daily_rows = "".join(
        f"<tr><td>{d['day']}</td><td>{d['received']}</td><td>{d['sent']}</td></tr>" for d in daily
    ) or "<tr><td colspan='3' class='muted'>No data in this range</td></tr>"

    customer_rows = "".join(
        f"""<tr class="{'active' if c['phone'] == selected_phone else ''}">
            <td><a href="?token={token}&start={start}&end={end}&phone={c['phone']}">{_esc(c['phone'])}</a></td>
            <td>{_esc(c['company_name']) or '<span class="muted">-</span>'}</td>
            <td>{_esc(c['rep_name']) or '<span class="muted">-</span>'}</td>
            <td>{c['message_count']}</td>
            <td>{c['last_message_at'][:16].replace('T', ' ')}</td>
        </tr>""" for c in customers
    ) or "<tr><td colspan='5' class='muted'>No customers in this range</td></tr>"

    if transcript is not None:
        if transcript:
            bubbles = "".join(
                f"""<div class="bubble {'in' if m['direction'] == 'in' else 'out'} {'escalated' if m['escalated'] else ''}">
                    <div class="bubble-text">{_esc(m['message'])}</div>
                    <div class="bubble-time">{m['created_at'][:16].replace('T', ' ')}{' &middot; escalated' if m['escalated'] else ''}</div>
                </div>""" for m in transcript
            )
        else:
            bubbles = "<p class='muted'>No messages for this customer in the selected date range.</p>"
        transcript_html = f"""
        <div class="panel">
            <h2>Transcript &middot; {_esc(selected_phone)}</h2>
            <div class="chat-window">{bubbles}</div>
        </div>"""
    else:
        transcript_html = """
        <div class="panel">
            <h2>Transcript</h2>
            <p class="muted">Select a customer from the list to view their conversation.</p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Würth WhatsApp Agent - Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; background: #f5f6f8; color: #1a1a1a; }}
  header {{ background: #c8102e; color: white; padding: 16px 24px; }}
  header h1 {{ margin: 0; font-size: 1.3em; }}
  .container {{ padding: 20px 24px; max-width: 1200px; margin: 0 auto; }}
  .filters {{ background: white; border-radius: 8px; padding: 14px 18px; margin-bottom: 18px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
  .filters label {{ font-size: 0.85em; color: #555; }}
  .filters input {{ padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; }}
  .filters button, .filters a.button {{ background: #c8102e; color: white; border: none; padding: 7px 16px; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 0.9em; display: inline-block; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 18px; }}
  .stat-card {{ background: white; border-radius: 8px; padding: 16px; }}
  .stat-card .value {{ font-size: 1.8em; font-weight: 700; color: #c8102e; }}
  .stat-card .label {{ font-size: 0.85em; color: #666; margin-top: 4px; }}
  .grid {{ display: grid; grid-template-columns: 1.2fr 1fr; gap: 18px; align-items: start; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .panel {{ background: white; border-radius: 8px; padding: 16px 18px; margin-bottom: 18px; }}
  .panel h2 {{ font-size: 1.05em; margin: 0 0 12px 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88em; }}
  th, td {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid #eee; }}
  th {{ color: #666; font-weight: 600; font-size: 0.8em; text-transform: uppercase; }}
  tr.active {{ background: #fdeef0; }}
  tr:hover {{ background: #fafafa; }}
  .muted {{ color: #999; }}
  .chat-window {{ max-height: 500px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }}
  .bubble {{ max-width: 80%; padding: 8px 12px; border-radius: 10px; font-size: 0.9em; }}
  .bubble.in {{ align-self: flex-start; background: #eee; }}
  .bubble.out {{ align-self: flex-end; background: #d6e9ff; }}
  .bubble.escalated {{ border: 1px solid #c8102e; }}
  .bubble-text {{ white-space: pre-wrap; word-break: break-word; }}
  .bubble-time {{ font-size: 0.7em; color: #888; margin-top: 4px; }}
</style>
</head>
<body>
<header><h1>Würth UAE WhatsApp Agent &middot; Dashboard</h1></header>
<div class="container">

  <form class="filters" method="get">
    <input type="hidden" name="token" value="{_esc(token)}">
    <label>From <input type="date" name="start" value="{start}"></label>
    <label>To <input type="date" name="end" value="{end}"></label>
    <button type="submit">Apply</button>
    <a class="button" href="/dashboard/export?token={_esc(token)}&start={start}&end={end}">Export to Excel</a>
  </form>

  <div class="stats">
    <div class="stat-card"><div class="value">{stats['messages_received']}</div><div class="label">Messages received</div></div>
    <div class="stat-card"><div class="value">{stats['replies_sent']}</div><div class="label">Replies sent</div></div>
    <div class="stat-card"><div class="value">{stats['unique_customers']}</div><div class="label">Unique customers</div></div>
    <div class="stat-card"><div class="value">{stats['escalations']}</div><div class="label">Escalations</div></div>
  </div>

  <div class="panel">
    <h2>Daily activity</h2>
    <table>
      <tr><th>Date</th><th>Received</th><th>Sent</th></tr>
      {daily_rows}
    </table>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>Customers ({len(customers)})</h2>
      <table>
        <tr><th>Phone</th><th>Company</th><th>Rep</th><th>Msgs</th><th>Last active</th></tr>
        {customer_rows}
      </table>
    </div>
    {transcript_html}
  </div>

</div>
</body>
</html>"""

"""
Internal analytics dashboard: message/customer stats, a read-only transcript
viewer, and an Excel export - all reading from the same Postgres database the
webhook writes to (storage/store.py).

Protected by a username/password login (DASHBOARD_ADMIN_USERNAME /
DASHBOARD_ADMIN_PASSWORD env vars), backed by a signed session cookie
(itsdangerous) rather than a server-side session store, so login survives
across the app's multiple worker/background threads without extra state.
"""
import io
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import config
from storage import store

router = APIRouter()

SESSION_COOKIE_NAME = "wurth_dashboard_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 12  # 12 hours

LOGO_URL = (
    "https://eshop.wurth.ae/is-bin/intershop.static/WFS/3890-B1-Site/-/en_US/"
    "webkit_bootstrap/dist/img/wuerth-logo.svg"
)


def _serializer():
    return URLSafeTimedSerializer(config.DASHBOARD_SESSION_SECRET, salt="dashboard-session")


def _is_logged_in(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return False
    try:
        data = _serializer().loads(cookie, max_age=SESSION_MAX_AGE_SECONDS)
        return data.get("user") == config.DASHBOARD_ADMIN_USERNAME
    except (BadSignature, SignatureExpired):
        return False


def _default_date_range():
    end = date.today()
    start = end - timedelta(days=30)
    return start.isoformat(), end.isoformat()


@router.get("/dashboard/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    return HTMLResponse(_render_login_html(error))


@router.post("/dashboard/login")
def login_submit(username: str = Form(""), password: str = Form("")):
    if not config.DASHBOARD_ADMIN_USERNAME or not config.DASHBOARD_ADMIN_PASSWORD:
        return HTMLResponse(
            _render_login_html("Dashboard login is not configured yet - set DASHBOARD_ADMIN_USERNAME "
                                "and DASHBOARD_ADMIN_PASSWORD in the app's environment variables."),
            status_code=500,
        )

    if username == config.DASHBOARD_ADMIN_USERNAME and password == config.DASHBOARD_ADMIN_PASSWORD:
        token = _serializer().dumps({"user": username})
        resp = RedirectResponse(url="/dashboard", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE_NAME, token,
            max_age=SESSION_MAX_AGE_SECONDS, httponly=True, samesite="lax",
        )
        return resp

    return HTMLResponse(_render_login_html("Incorrect username or password."), status_code=401)


@router.get("/dashboard/logout")
def logout():
    resp = RedirectResponse(url="/dashboard/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


_TRANSCRIPT_PAGE_SIZE = 20


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, start: str = "", end: str = "", phone: str = "", rep_phone: str = "", page: int = 1):
    if not _is_logged_in(request):
        return RedirectResponse(url="/dashboard/login", status_code=303)

    default_start, default_end = _default_date_range()
    start = start or default_start
    end = end or default_end
    page = max(page, 1)

    stats = store.get_stats(start, end)
    daily = store.get_daily_counts(start, end)
    customers = store.get_customers_summary(start, end)
    leads_summary = store.get_leads_summary(start, end)
    leads_list = store.get_leads_list(start, end)
    rep_replies = store.get_rep_replies_list(start, end)
    reps = store.get_reps_summary(start, end)
    if phone:
        transcript, transcript_total = store.get_conversation(phone, start, end, page=page, page_size=_TRANSCRIPT_PAGE_SIZE)
    else:
        transcript, transcript_total = None, 0
    rep_transcript = store.get_rep_transcript(rep_phone, start, end) if rep_phone else None

    return HTMLResponse(_render_dashboard_html(
        start, end, stats, daily, customers, leads_summary, leads_list, rep_replies, reps,
        phone, transcript, transcript_total, page, rep_phone, rep_transcript,
    ))


@router.get("/dashboard/export")
def export_excel(request: Request, start: str = "", end: str = ""):
    if not _is_logged_in(request):
        return Response("Forbidden - please log in", status_code=403)

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
            str(row["created_at"]), row["phone"], row["company_name"],
            "Customer" if row["direction"] == "in" else "Bot",
            row["message"], "Yes" if row["escalated"] else "No",
        ])
    for col_letter, width in zip("ABCDEF", [26, 16, 24, 10, 60, 10]):
        ws.column_dimensions[col_letter].width = width

    ws2 = wb.create_sheet("Customers")
    ws2.append(["Phone", "Company", "Sales Rep", "Message Count", "Last Message (UTC)"])
    for row in store.get_customers_summary(start, end):
        ws2.append([row["phone"], row["company_name"], row["rep_name"], row["message_count"], str(row["last_message_at"])])
    for col_letter, width in zip("ABCDE", [16, 24, 20, 14, 26]):
        ws2.column_dimensions[col_letter].width = width

    ws3 = wb.create_sheet("Daily Summary")
    ws3.append(["Date", "Messages Received", "Replies Sent"])
    for row in store.get_daily_counts(start, end):
        ws3.append([row["day"], row["received"], row["sent"]])
    for col_letter, width in zip("ABC", [14, 18, 14]):
        ws3.column_dimensions[col_letter].width = width

    ws4 = wb.create_sheet("Sales Leads")
    ws4.append(["Sales Rep", "Leads Generated", "Unique Customers", "Last Lead (UTC)", "Failed Notifications"])
    for row in store.get_leads_summary(start, end):
        ws4.append([row["rep_name"], row["lead_count"], row["customer_count"], str(row["last_lead_at"]), row["failed_notifications"]])
    for col_letter, width in zip("ABCDE", [22, 16, 18, 26, 18]):
        ws4.column_dimensions[col_letter].width = width

    ws5 = wb.create_sheet("Lead Details")
    ws5.append(["Timestamp (UTC)", "Phone", "Company", "Sales Rep", "Customer Enquiry", "Status", "Delivery", "Rep Response", "Response Confidence"])
    for row in store.get_leads_list(start, end):
        response_confidence = {"context_match": "Confirmed", "fallback_most_recent": "Best guess"}.get(row.get("rep_reply_method"), "")
        ws5.append([
            str(row["created_at"]), row["phone"], row["company_name"], row["rep_name"], row["enquiry_text"],
            row["status"], row["delivery_status"], row.get("rep_reply_text") or "", response_confidence,
        ])
    for col_letter, width in zip("ABCDEFGHI", [26, 16, 24, 20, 60, 10, 14, 40, 16]):
        ws5.column_dimensions[col_letter].width = width

    ws6 = wb.create_sheet("Rep Replies")
    ws6.append(["Timestamp (UTC)", "Rep", "Rep Phone", "Customer", "Customer Phone", "Reply", "Match Confidence"])
    _CONFIDENCE_LABELS_XLSX = {"context_match": "Confirmed", "fallback_most_recent": "Best guess", "unresolved": "Unresolved"}
    for row in store.get_rep_replies_list(start, end):
        ws6.append([
            str(row["created_at"]), row["rep_name"], row["rep_phone"], row["company_name"], row["customer_phone"],
            row["reply_text"], _CONFIDENCE_LABELS_XLSX.get(row["resolution_method"], row["resolution_method"]),
        ])
    for col_letter, width in zip("ABCDEFG", [26, 20, 16, 24, 16, 60, 16]):
        ws6.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"wurth-whatsapp-report_{start}_to_{end}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# All timestamps are stored in Postgres as UTC (see storage/store.py's
# datetime.now(timezone.utc) calls) - the dashboard is used by staff in the
# UAE, so every displayed time is converted to Gulf Standard Time (UTC+4,
# no daylight saving) here rather than showing raw UTC clock time.
_DISPLAY_TZ = timezone(timedelta(hours=4))


def _fmt_ts(ts) -> str:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    return str(ts)[:16].replace("T", " ")


_DELIVERY_PILL_LABELS = {"delivered": "Delivered", "failed": "Failed", "pending": "Pending"}
_STATUS_PILL_LABELS = {"open": "Open", "closed": "Closed"}


def _delivery_pill(status: str, summary: str) -> str:
    label = _DELIVERY_PILL_LABELS.get(status, status)
    title = f' title="{_esc(summary)}"' if summary else ""
    return f'<span class="pill {_esc(status)}"{title}>{label}</span>'


def _status_pill(status: str) -> str:
    label = _STATUS_PILL_LABELS.get(status, status)
    return f'<span class="pill status-{_esc(status)}">{label}</span>'


def _rep_reply_cell(reply_text: str | None, reply_at, method: str | None) -> str:
    if not reply_text:
        return '<span class="muted">No reply yet</span>'
    guess_badge = ' <span class="pill guess" title="Best guess - the rep did not reply directly to the alert, so this is their most recent open lead, not a confirmed match">Best guess</span>' if method == "fallback_most_recent" else ""
    truncated = reply_text if len(reply_text) <= 60 else reply_text[:57] + "..."
    return f'<span title="{_esc(reply_text)}">{_esc(truncated)}</span> <span class="muted">{_fmt_ts(reply_at)}</span>{guess_badge}'


_BASE_STYLE = """
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; background: #f5f6f8; color: #1a1a1a; }
  header { background: #c8102e; color: white; padding: 12px 16px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  header img.logo { height: 28px; filter: brightness(0) invert(1); }
  header h1 { margin: 0; font-size: 1.15em; flex: 1; min-width: 0; }
  header a.logout { color: white; text-decoration: underline; font-size: 0.85em; white-space: nowrap; }
"""


def _render_login_html(error: str = "") -> str:
    error_html = f'<p class="error">{_esc(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Würth WhatsApp Agent - Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; background: #f5f6f8; color: #1a1a1a;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 16px; }}
  .card {{ background: white; border-radius: 10px; padding: 32px 28px; width: 100%; max-width: 360px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }}
  .card img {{ height: 32px; display: block; margin: 0 auto 20px auto; }}
  h1 {{ font-size: 1.1em; text-align: center; margin: 0 0 20px 0; }}
  label {{ display: block; font-size: 0.85em; color: #555; margin-bottom: 4px; margin-top: 14px; }}
  input {{ width: 100%; padding: 10px 12px; border: 1px solid #ccc; border-radius: 6px; font-size: 1em; }}
  button {{ width: 100%; margin-top: 20px; background: #c8102e; color: white; border: none; padding: 11px; border-radius: 6px; font-size: 1em; cursor: pointer; }}
  .error {{ color: #c8102e; font-size: 0.85em; margin-top: 12px; text-align: center; }}
</style>
</head>
<body>
  <form class="card" method="post" action="/dashboard/login">
    <img src="{LOGO_URL}" alt="Würth">
    <h1>WhatsApp Agent Dashboard</h1>
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" required autofocus>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button type="submit">Log in</button>
    {error_html}
  </form>
</body>
</html>"""


def _render_pagination(start, end, phone, current_page, total_pages) -> str:
    if total_pages <= 1:
        return ""
    links = []
    for p in range(1, total_pages + 1):
        if p == current_page:
            links.append(f'<span class="page-num active">{p}</span>')
        else:
            links.append(f'<a class="page-num" href="?start={start}&end={end}&phone={phone}&page={p}">{p}</a>')
    return f'<div class="pagination">{"".join(links)}</div>'


def _render_dashboard_html(start, end, stats, daily, customers, leads_summary, leads_list, rep_replies, reps,
                            selected_phone, transcript, transcript_total, transcript_page,
                            selected_rep_phone, rep_transcript):
    daily_rows = "".join(
        f"<tr><td>{d['day']}</td><td>{d['received']}</td><td>{d['sent']}</td></tr>" for d in daily
    ) or "<tr><td colspan='3' class='muted'>No data in this range</td></tr>"

    total_leads = sum(r["lead_count"] for r in leads_summary)

    leads_summary_rows = "".join(
        f"""<tr>
            <td>{_esc(r['rep_name'])}</td>
            <td>{r['lead_count']}</td>
            <td>{r['customer_count']}</td>
            <td>{_fmt_ts(r['last_lead_at'])}</td>
            <td>{r['failed_notifications'] or '<span class="muted">0</span>'}</td>
        </tr>""" for r in leads_summary
    ) or "<tr><td colspan='5' class='muted'>No leads in this range</td></tr>"

    leads_list_rows = "".join(
        f"""<tr>
            <td>{_fmt_ts(l['created_at'])}</td>
            <td>{_esc(l['company_name']) or _esc(l['phone'])}</td>
            <td>{_esc(l['rep_name'])}</td>
            <td>{_esc(l['enquiry_text'])}</td>
            <td>{_status_pill(l['status'])}</td>
            <td>{_delivery_pill(l['delivery_status'], l.get('attempt_summary') or '')}</td>
            <td>{_rep_reply_cell(l.get('rep_reply_text'), l.get('rep_reply_at'), l.get('rep_reply_method'))}</td>
        </tr>""" for l in leads_list
    ) or "<tr><td colspan='7' class='muted'>No leads in this range</td></tr>"

    _CONFIDENCE_LABELS = {"context_match": "Confirmed", "fallback_most_recent": "Best guess", "unresolved": "Unresolved"}
    rep_replies_rows = "".join(
        f"""<tr>
            <td>{_fmt_ts(r['created_at'])}</td>
            <td>{_esc(r['rep_name']) or _esc(r['rep_phone'])}</td>
            <td>{_esc(r['company_name']) or _esc(r['customer_phone']) or '<span class="muted">-</span>'}</td>
            <td>{_esc(r['reply_text'])}</td>
            <td>{_esc(_CONFIDENCE_LABELS.get(r['resolution_method'], r['resolution_method']))}</td>
        </tr>""" for r in rep_replies
    ) or "<tr><td colspan='5' class='muted'>No rep replies in this range</td></tr>"

    customer_rows = "".join(
        f"""<tr class="{'active' if c['phone'] == selected_phone else ''}">
            <td><a href="?start={start}&end={end}&phone={c['phone']}">{_esc(c['phone'])}</a></td>
            <td>{_esc(c['company_name']) or '<span class="muted">-</span>'}</td>
            <td>{_esc(c['rep_name']) or '<span class="muted">-</span>'}</td>
            <td>{c['message_count']}</td>
            <td>{_fmt_ts(c['last_message_at'])}</td>
        </tr>""" for c in customers
    ) or "<tr><td colspan='5' class='muted'>No customers in this range</td></tr>"

    rep_rows = "".join(
        f"""<tr class="{'active' if r['rep_phone'] == selected_rep_phone else ''}">
            <td><a href="?start={start}&end={end}&rep_phone={r['rep_phone']}">{_esc(r['rep_phone'])}</a></td>
            <td>{_esc(r['rep_name']) or '<span class="muted">-</span>'}</td>
            <td>{r['message_count']}</td>
            <td>{_fmt_ts(r['last_activity_at'])}</td>
        </tr>""" for r in reps
    ) or "<tr><td colspan='4' class='muted'>No rep escalations in this range</td></tr>"

    if transcript is not None:
        if transcript:
            bubbles = "".join(
                f"""<div class="bubble {'in' if m['direction'] == 'in' else 'out'} {'escalated' if m['escalated'] else ''}">
                    <div class="bubble-text">{_esc(m['message'])}</div>
                    <div class="bubble-time">{_fmt_ts(m['created_at'])}{' &middot; escalated' if m['escalated'] else ''}</div>
                </div>""" for m in transcript
            )
        else:
            bubbles = "<p class='muted'>No messages for this customer in the selected date range.</p>"
        total_pages = max((transcript_total + _TRANSCRIPT_PAGE_SIZE - 1) // _TRANSCRIPT_PAGE_SIZE, 1)
        pagination_html = _render_pagination(start, end, selected_phone, transcript_page, total_pages)
        transcript_html = f"""
        <div class="panel">
            <h2>Transcript &middot; {_esc(selected_phone)} <span class="muted" style="font-weight:normal">({transcript_total} messages)</span></h2>
            <div class="chat-window">{bubbles}</div>
            {pagination_html}
        </div>"""
    else:
        transcript_html = """
        <div class="panel">
            <h2>Transcript</h2>
            <p class="muted">Select a customer from the list to view their conversation.</p>
        </div>"""

    if rep_transcript is not None:
        if rep_transcript:
            rep_bubbles = "".join(
                f"""<div class="bubble {'out' if m['direction'] == 'out' else 'in'}">
                    <div class="bubble-text">{(_esc(m['company_name']) + ' &mdash; ') if m['company_name'] else ''}{_esc(m['message_text'])}</div>
                    <div class="bubble-time">{_fmt_ts(m['created_at'])}{' &middot; ' + _esc(_CONFIDENCE_LABELS.get(m['extra'], m['extra'])) if m['direction'] == 'in' and m.get('extra') else ''}</div>
                </div>""" for m in rep_transcript
            )
        else:
            rep_bubbles = "<p class='muted'>No escalation activity for this rep in the selected date range.</p>"
        rep_transcript_html = f"""
        <div class="panel">
            <h2>Rep escalation transcript &middot; {_esc(selected_rep_phone)}</h2>
            <div class="chat-window">{rep_bubbles}</div>
        </div>"""
    else:
        rep_transcript_html = """
        <div class="panel">
            <h2>Rep escalation transcript</h2>
            <p class="muted">Select a rep from the list to view their escalation alerts and replies.</p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Würth WhatsApp Agent - Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<style>
{_BASE_STYLE}
  .container {{ padding: 16px; max-width: 1200px; margin: 0 auto; }}
  .filters {{ background: white; border-radius: 8px; padding: 14px 16px; margin-bottom: 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
  .filters label {{ font-size: 0.85em; color: #555; }}
  .filters input {{ padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px; width: 100%; max-width: 160px; }}
  .filters button, .filters a.button {{ background: #c8102e; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 0.9em; display: inline-block; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 16px; }}
  .stat-card {{ background: white; border-radius: 8px; padding: 14px; }}
  .stat-card .value {{ font-size: 1.6em; font-weight: 700; color: #c8102e; }}
  .stat-card .label {{ font-size: 0.8em; color: #666; margin-top: 4px; }}
  .grid {{ display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; align-items: start; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .panel {{ background: white; border-radius: 8px; padding: 14px 16px; margin-bottom: 16px; overflow-x: auto; }}
  .panel h2 {{ font-size: 1em; margin: 0 0 10px 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; min-width: 380px; }}
  th, td {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid #eee; white-space: nowrap; }}
  th {{ color: #666; font-weight: 600; font-size: 0.78em; text-transform: uppercase; }}
  tr.active {{ background: #fdeef0; }}
  tr:hover {{ background: #fafafa; }}
  .muted {{ color: #999; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.78em; font-weight: 600; white-space: nowrap; }}
  .pill.delivered {{ background: #e6f4ea; color: #1a7f37; }}
  .pill.failed {{ background: #fdeaec; color: #c8102e; }}
  .pill.pending {{ background: #f0f0f0; color: #888; }}
  .pill.status-open {{ background: #e8f0fe; color: #1a56c8; }}
  .pill.status-closed {{ background: #f0f0f0; color: #888; }}
  .pill.guess {{ background: #fff4e5; color: #a85d00; }}
  .chat-window {{ max-height: 500px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }}
  .bubble {{ max-width: 85%; padding: 8px 12px; border-radius: 10px; font-size: 0.9em; }}
  .bubble.in {{ align-self: flex-start; background: #eee; }}
  .bubble.out {{ align-self: flex-end; background: #d6e9ff; }}
  .bubble.escalated {{ border: 1px solid #c8102e; }}
  .bubble-text {{ white-space: pre-wrap; word-break: break-word; }}
  .bubble-time {{ font-size: 0.7em; color: #888; margin-top: 4px; }}
  .pagination {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 12px; padding-top: 12px; border-top: 1px solid #eee; }}
  .page-num {{ display: inline-block; min-width: 26px; text-align: center; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; text-decoration: none; color: #333; background: #f0f0f0; }}
  .page-num:hover {{ background: #e2e2e2; }}
  .page-num.active {{ background: #c8102e; color: white; font-weight: 600; }}
  @media (max-width: 600px) {{
    header h1 {{ font-size: 1em; }}
    .filters {{ flex-direction: column; align-items: stretch; }}
    .filters input {{ max-width: none; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<header>
  <img class="logo" src="{LOGO_URL}" alt="Würth">
  <h1>WhatsApp Agent &middot; Dashboard</h1>
  <a class="logout" href="/dashboard/logout">Log out</a>
</header>
<div class="container">

  <form class="filters" method="get">
    <label>From <input type="date" name="start" value="{start}"></label>
    <label>To <input type="date" name="end" value="{end}"></label>
    <button type="submit">Apply</button>
    <a class="button" href="/dashboard/export?start={start}&end={end}">Export to Excel</a>
  </form>

  <div class="stats">
    <div class="stat-card"><div class="value">{stats['messages_received']}</div><div class="label">Messages received</div></div>
    <div class="stat-card"><div class="value">{stats['replies_sent']}</div><div class="label">Replies sent</div></div>
    <div class="stat-card"><div class="value">{stats['unique_customers']}</div><div class="label">Unique customers</div></div>
    <div class="stat-card"><div class="value">{total_leads}</div><div class="label">Sales leads generated</div></div>
  </div>

  <div class="panel">
    <h2>Daily activity</h2>
    <table>
      <tr><th>Date</th><th>Received</th><th>Sent</th></tr>
      {daily_rows}
    </table>
  </div>

  <div class="panel">
    <h2>Sales leads by rep &middot; how AI is helping the team ({total_leads} total)</h2>
    <p class="muted" style="margin-top:-4px">A "lead" is a customer enquiry the AI recognized as purchase intent, a quote/pricing \
request, or an urgent issue, and flagged for the assigned sales rep to follow up on.</p>
    <table>
      <tr><th>Sales Rep</th><th>Leads</th><th>Customers</th><th>Last lead</th><th>Failed notifications</th></tr>
      {leads_summary_rows}
    </table>
  </div>

  <div class="panel">
    <h2>Recent leads</h2>
    <table>
      <tr><th>When</th><th>Customer</th><th>Rep</th><th>Enquiry</th><th>Status</th><th>Delivery</th><th>Rep Response</th></tr>
      {leads_list_rows}
    </table>
  </div>

  <div class="panel">
    <h2>Rep replies ({len(rep_replies)})</h2>
    <p class="muted" style="margin-top:-4px">Every reply a sales rep sent back after being notified of a lead, in order - a dedicated \
view of rep engagement separate from the leads table above (which only shows the latest reply per lead).</p>
    <table>
      <tr><th>When</th><th>Rep</th><th>Customer</th><th>Reply</th><th>Match confidence</th></tr>
      {rep_replies_rows}
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

  <div class="grid">
    <div class="panel">
      <h2>Sales reps ({len(reps)})</h2>
      <table>
        <tr><th>Phone</th><th>Rep</th><th>Msgs</th><th>Last active</th></tr>
        {rep_rows}
      </table>
    </div>
    {rep_transcript_html}
  </div>

</div>
</body>
</html>"""

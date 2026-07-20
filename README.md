# Würth UAE WhatsApp AI Agent

An AI agent that replies to customers on WhatsApp using a knowledge base built
from eshop.wurth.ae, recognizes returning customers automatically, connects
them to their assigned sales representative, and escalates real sales leads
to that rep on WhatsApp with delivery + reply tracking on an internal
dashboard.

```
Customer messages the business number
        -> WhatsApp Cloud API webhook (this app)
        -> auto-recognize by phone (or ask for company name), cache in Postgres
        -> retrieve relevant knowledge base chunks (TF-IDF search)
        -> OpenRouter LLM generates a warm, grounded, persuasive reply
        -> reply includes the assigned rep's name AND phone number
        -> the model's own [[LEAD]]/[[NO_LEAD]] tag decides if this is a real lead
        -> genuine leads (deduplicated per customer, 36h rolling window) are
           escalated to the rep via WhatsApp (template if approved, else
           free-form), with every attempt recorded
        -> if the rep hasn't replied after 24h, they get an automated reminder
        -> when the rep DOES reply, the webhook recognizes their number,
           routes it away from the AI pipeline entirely, and links their
           reply back to the right lead
        -> all of the above is visible on a password-protected /dashboard,
           including a dedicated rep-reply transcript view
```

## What you need to configure (only step that requires your input)

Everything else is already built. You just need to:

1. Copy `.env.example` to `.env` and fill in:
   - `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`
     (from Meta App Dashboard > WhatsApp > API Setup)
   - `OPENROUTER_API_KEY` (from https://openrouter.ai/keys) and optionally
     change `OPENROUTER_MODEL`
   - `GOOGLE_SHEET_ID` (the long ID in your Google Sheet's URL)
   - `DATABASE_URL` (a Postgres connection string - see "Database" below)
2. Put your Google service account key at `credentials/service_account.json`
   (see "Google Sheets setup" below).
3. Run the setup commands below.

That's it — no code changes needed.

---

## 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
# then edit .env with your keys
```

## 3. Database (Postgres)

Conversation history, customer/rep cache, leads, and escalation tracking all
live in Postgres — required because Render's free tier web service filesystem
is ephemeral and wipes any local file (SQLite included) on every deploy or
restart. Use Render's own managed Postgres (New > PostgreSQL in Render's
dashboard, free tier available) and paste its **Internal Database URL** into
`DATABASE_URL`. Tables are created automatically on first connection
(`storage/store.py`'s `_init_schema()`) — no manual migration step.

## 4. Google Sheets setup (Sales Rep directory)

1. Create/reuse a Google Cloud project, enable the **Google Sheets API**.
2. Create a **Service Account**, generate a JSON key, save it as
   `credentials/service_account.json`.
3. Open your Google Sheet, click **Share**, and share it with the service
   account's `client_email` (found inside the JSON key) as **Viewer**.
4. Structure the sheet with this header row (see `data/sample_sheet_template.csv`
   for an example you can import directly):

   | Company Name | Company Phone | Sales Rep Name | Rep Phone | Rep Email | Region |
   |---|---|---|---|---|---|

   `Company Phone` is the customer's own WhatsApp number — set this so
   returning customers are recognized instantly by phone, without having to
   type their company name again. Rep and company phone numbers can be
   entered in local UAE format (e.g. `0501234567`) or full international
   format (e.g. `971501234567`) — both are normalized automatically.

5. Put the Sheet ID (from its URL:
   `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`) into
   `GOOGLE_SHEET_ID` in `.env`.

## 5. Build the knowledge base

The seed knowledge base (`data/knowledge_base_seed.json`) already ships with
~175 entries covering Würth UAE's company info, contact details, branches,
and the full eshop.wurth.ae category/subcategory structure. To (re)generate
the searchable index from it:

```bash
python -m scraper.scrape_kb   # combines the seed with a live crawl attempt
python -m kb.build_index      # builds the TF-IDF search index
```

This creates `data/knowledge_base.json` (the combined seed + crawled text
chunks) and `data/kb_index.pkl` (the search index the agent queries at
runtime). `eshop.wurth.ae`/`wurth.ae` currently block non-browser scraping
(403), so in practice this just re-indexes the seed data — that's expected
and fine. To expand the seed data itself, edit
`data/knowledge_base_seed.json` directly (one entry per category/subcategory/
topic, each with `source_url`, `title`, `text`).

## 6. Run the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

For local testing before you have a public domain, tunnel it with something
like `ngrok http 8000` and use the ngrok URL as your webhook.

## 6b. Deploy live for free on Render

This repo includes `render.yaml` so Render can deploy it automatically.

1. Push this project to a GitHub repo (create one if you haven't: `git init`,
   commit, push to a new repo on your GitHub account).
2. Go to https://dashboard.render.com > **New** > **Blueprint**, connect your
   GitHub account, and pick this repo. Render reads `render.yaml` and creates
   a free web service called `wurth-whatsapp-agent` automatically.
3. Render will ask you to fill in the env vars marked `sync: false` in
   `render.yaml`, plus any not listed there that you want to use (see the
   full list in "Environment variables" below) — at minimum:
   - `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
     `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_VERIFY_TOKEN`
   - `OPENROUTER_API_KEY`
   - `GOOGLE_SHEET_ID`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — since Render's free tier has no file
     upload, paste the **entire contents** of your
     `credentials/service_account.json` file as the value of this one env
     var. The app writes it to disk automatically on startup (see `main.py`).
   - `DATABASE_URL` — your Render Postgres instance's Internal Database URL
   - `DASHBOARD_ADMIN_USERNAME` / `DASHBOARD_ADMIN_PASSWORD` — to enable the
     `/dashboard` login
4. Click **Apply**. Render runs the build command (`pip install -r
   requirements.txt`) and starts the server. The knowledge base is built
   automatically on first startup if `data/kb_index.pkl` is missing (see
   `main.py`'s `_auto_rebuild_kb_if_missing`) — no manual step needed on a
   fresh deploy. You can also trigger a rebuild manually any time:

   ```bash
   curl -X POST "https://<url>/admin/rebuild-kb?token=<verify-token>"
   ```

5. Once deployed, Render gives you a public URL like
   `https://wurth-whatsapp-agent.onrender.com`. That's your webhook base URL
   and your dashboard base URL (`<url>/dashboard`).

**Free tier note:** the free web service sleeps after 15 minutes of no
traffic. The first WhatsApp message after a period of inactivity may take
~30-50 seconds to get a reply while the instance wakes up; after that it's
fast until it goes idle again. The free tier's filesystem is ephemeral —
every deploy or restart wipes `data/knowledge_base.json`/`kb_index.pkl`, but
these auto-rebuild on startup (see step 4 above), and all conversation/lead
data is safe since it lives in Postgres, not the filesystem.

## 7. Connect the WhatsApp webhook

In [Meta App Dashboard](https://developers.facebook.com/apps) > your app >
WhatsApp > Configuration:
- **Callback URL**: `https://<your-render-url>/webhook`
- **Verify token**: same value as `WHATSAPP_VERIFY_TOKEN` you set in Render's
  env vars
- Click **Verify and save** — Meta calls your `/webhook` GET endpoint, which
  checks the token and echoes back the challenge (already implemented in
  `main.py`).
- Subscribe to the **messages** webhook field.

Send a test message to your WhatsApp Business number — you should get an AI
reply grounded in the knowledge base.

## 8. Lead escalation to sales reps

When the AI decides a customer message is a genuine lead (purchase intent, a
quote/pricing request, an urgent issue — see `ai/agent.py`'s
`SYSTEM_PROMPT_TEMPLATE` and `[[LEAD]]`/`[[NO_LEAD]]` tagging), it
automatically WhatsApps the customer's assigned rep with the enquiry details.
This works out of the box using free-form messages, but those only deliver
within WhatsApp's 24-hour customer-service window (i.e. only if the rep has
messaged the business number recently). For reliable delivery any time,
submit a Meta-approved message template (Meta Business Manager > WhatsApp
Manager > Message Templates, category **Utility**) using **named** variable
placeholders (Meta requires `{{variable_name}}`, not `{{1}}`), matching what
the code sends:

```
New enquiry from {{rep_name}} (+{{customer_phone}}) on WhatsApp:
"{{enquiry_text}}"

They may be ready to place an order or need a quote — reach out soon to help them and close this one for your target!
```

Once approved, set `WHATSAPP_ESCALATION_TEMPLATE_NAME` (and optionally
`WHATSAPP_ESCALATION_OPS_TEMPLATE_NAME` for the ops-fallback path, used when
no rep is assigned or the rep send fails — configured via
`ESCALATION_NOTIFY_NUMBERS`) in your env vars. Until set, escalation runs
exactly as today (free-form messages).

## 9. Day-1 rep reminder (optional)

If a lead is escalated and the assigned rep hasn't replied at all within 24h
(`LEAD_FOLLOWUP_HOURS`), an automated reminder can be sent to the rep (not
the customer — customers shouldn't be pinged twice about the same enquiry).
This also needs a Meta-approved template, since a rep who hasn't engaged yet
likely hasn't messaged the business number recently either:

```
Hi {{rep_name}}, quick reminder — {{customer_name}} (+{{customer_phone}}) reached out to Würth UAE and hasn't heard back from you yet. Please follow up when you get a chance.
```

Set `WHATSAPP_REP_REMINDER_TEMPLATE_NAME` once approved. This app has no
built-in scheduler (Render's free tier doesn't support Cron Job services), so
something needs to call the trigger endpoint once a day:

```bash
curl -X POST "https://<url>/admin/send-followups?token=<verify-token>"
```

The included `.github/workflows/send-followups.yml` does this automatically
via a daily GitHub Actions scheduled workflow — set `RENDER_APP_URL` and
`WHATSAPP_VERIFY_TOKEN` as repo secrets (Settings > Secrets and variables >
Actions) to enable it.

## 10. Admin dashboard

Visit `<your-url>/dashboard` and log in with `DASHBOARD_ADMIN_USERNAME` /
`DASHBOARD_ADMIN_PASSWORD`. Shows:

- Message/customer stats and daily activity trend
- **Sales leads by rep** — deduplicated lead counts, unique customers, failed
  notification counts (a fast signal a rep's phone number might be wrong)
- **Recent leads** — one row per deduplicated lead (not per message), with
  status (open/closed), delivery status of the escalation alert, and the
  rep's latest reply (labeled "Confirmed" if matched via WhatsApp
  swipe-to-reply, or "Best guess" if inferred from timing)
- **Rep replies** — every rep reply in the range, not just the latest per
  lead
- **Customers** — click a phone number to view their full chat transcript
- **Sales reps** — click a rep's phone number to view their dedicated
  escalation transcript (alerts sent + replies received, interleaved
  chronologically)
- **Export to Excel** — all of the above as a multi-sheet `.xlsx` download

## Environment variables

See `.env.example` for the full list with inline comments. Grouped summary:

| Group | Variables |
|---|---|
| WhatsApp Cloud API | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_API_VERSION` |
| OpenRouter (AI) | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_VISION_MODEL` |
| Google Sheets | `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` or `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_WORKSHEET_NAME` |
| Database | `DATABASE_URL` (Postgres) |
| Escalation | `FUZZY_MATCH_THRESHOLD`, `ESCALATION_NOTIFY_NUMBERS`, `WHATSAPP_ESCALATION_TEMPLATE_NAME`, `WHATSAPP_ESCALATION_TEMPLATE_LANGUAGE`, `WHATSAPP_ESCALATION_OPS_TEMPLATE_NAME` |
| Lead dedup + rep reminder | `LEAD_DEDUP_WINDOW_HOURS`, `LEAD_FOLLOWUP_HOURS`, `WHATSAPP_REP_REMINDER_TEMPLATE_NAME`, `WHATSAPP_REP_REMINDER_TEMPLATE_LANGUAGE` |
| Dashboard | `DASHBOARD_ADMIN_USERNAME`, `DASHBOARD_ADMIN_PASSWORD`, `DASHBOARD_SESSION_SECRET` |

---

## Project structure

```
main.py                  FastAPI app - webhook handling, escalation flow, rep-reply routing, admin endpoints
config.py                Loads all settings from .env
dashboard.py              /dashboard - stats, leads, rep replies, transcripts, Excel export

scraper/scrape_kb.py     Combines the seed KB with a (usually blocked) live crawl -> data/knowledge_base.json
kb/build_index.py        Builds TF-IDF search index -> data/kb_index.pkl
kb/retriever.py          search(query, top_k) used at runtime
data/knowledge_base_seed.json   Hand-maintained + eshop-sheet-derived KB entries (committed to the repo)

ai/openrouter_client.py  OpenRouter chat completion wrapper, with retry on transient rate-limit/server errors
ai/agent.py              Builds prompts (KB context + rep info), tags leads via [[LEAD]]/[[NO_LEAD]], filters auto-replies

sheets/sheets_client.py  Google Sheets fuzzy company -> rep lookup, phone-based customer recognition

whatsapp/client.py       Sends free-form and template messages via Meta Cloud API

storage/store.py         Postgres: customers, conversations, leads, escalation_attempts, rep_replies

utils/phone.py           Shared phone normalization (adds UAE country code, strips intl dialing prefix)
utils/whatsapp_text.py   Sanitizes text for WhatsApp template parameters (Meta rejects newlines/multi-space)

.github/workflows/send-followups.yml   Daily scheduled trigger for the day-1 rep reminder

data/                    Generated knowledge base + index live here (gitignored except the seed file)
credentials/             Put service_account.json here (gitignored)
```

## Notes on the AI answering approach

- Retrieval is TF-IDF-based (scikit-learn) — no vector database required, fast
  to set up, works well for keyword-heavy product/catalog content. If you
  want closer semantic matching later, swap `kb/build_index.py` and
  `kb/retriever.py` for an embeddings-based vector store; `main.py` and
  `ai/agent.py` don't need to change since they only call `kb_search(query, top_k)`.
- Lead detection combines the model's own `[[LEAD]]`/`[[NO_LEAD]]` tag
  (`ai/agent.py`'s `SYSTEM_PROMPT_TEMPLATE`) with a keyword backstop
  (`ESCALATION_KEYWORDS`) and an auto-reply/out-of-office filter
  (`is_auto_reply`), so genuine product interest gets flagged even without
  matching keywords, while automated system text and casual chit-chat don't.
- Company identification uses a light heuristic + fuzzy match against the
  sheet (`try_extract_company_name` in `ai/agent.py`), with a blocklist for
  greetings and non-committal/deferring replies ("I'll check", "not sure")
  that must never be mistaken for a company name. For messier company names,
  you can upgrade this to call OpenRouter and return structured JSON instead.
- Rep-reply detection (`main.py`'s `receive_webhook`) only routes an inbound
  message away from the AI pipeline when there's real evidence the sender is
  a rep (an escalation was actually sent to that number) — ambiguous cases
  default to treating the sender as a customer, since silently misfiling a
  real customer's message is worse than a rep occasionally getting an AI
  reply to their own text.
- All customer/company mappings, conversation history, leads, and escalation
  tracking live in Postgres (`storage/store.py`) — required for persistence
  on Render's free tier, whose filesystem is wiped on every deploy/restart.

## Compliance reminders

- WhatsApp requires opt-in before marketing messages, and marketing broadcasts
  must use Meta-approved message templates.
- Review UAE PDPL requirements for storing customer phone numbers and company
  data (retention period, access controls).

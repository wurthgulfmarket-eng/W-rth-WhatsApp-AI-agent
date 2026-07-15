# Würth UAE WhatsApp AI Agent

An AI agent that replies to customers on WhatsApp using a knowledge base built
from wurth.ae and eshop.wurth.ae, and tells customers who their assigned
sales representative is (looked up from a Google Sheet).

```
Customer replies to marketing broadcast
        -> WhatsApp Cloud API webhook (this app)
        -> identify company (asks if unknown, caches in SQLite)
        -> retrieve relevant knowledge base chunks (TF-IDF search)
        -> OpenRouter LLM generates a grounded reply
        -> Google Sheets lookup adds the rep's name/phone/email
        -> reply sent back on WhatsApp
        -> pricing/complaint keywords trigger human escalation alert
```

## What you need to configure (only step that requires your input)

Everything else is already built. You just need to:

1. Copy `.env.example` to `.env` and fill in:
   - `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`
     (from Meta App Dashboard > WhatsApp > API Setup)
   - `OPENROUTER_API_KEY` (from https://openrouter.ai/keys) and optionally
     change `OPENROUTER_MODEL`
   - `GOOGLE_SHEET_ID` (the long ID in your Google Sheet's URL)
2. Put your Google service account key at `credentials/service_account.json`
   (see "Google Sheets setup" below).
3. Run the two setup commands below.

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

## 3. Google Sheets setup (Sales Rep directory)

1. Create/reuse a Google Cloud project, enable the **Google Sheets API**.
2. Create a **Service Account**, generate a JSON key, save it as
   `credentials/service_account.json`.
3. Open your Google Sheet, click **Share**, and share it with the service
   account's `client_email` (found inside the JSON key) as **Viewer**.
4. Structure the sheet with this header row (see `data/sample_sheet_template.csv`
   for an example you can import directly):

   | Company Name | Sales Rep Name | Rep Phone | Rep Email | Region |
   |---|---|---|---|---|

5. Put the Sheet ID (from its URL:
   `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`) into
   `GOOGLE_SHEET_ID` in `.env`.

## 4. Build the knowledge base

This crawls `wurth.ae` and `eshop.wurth.ae` and indexes the content. It needs
real internet access to those domains, so run it from your own machine or
server (not a sandboxed environment):

```bash
./build_kb.sh
```

This creates `data/knowledge_base.json` (raw scraped text chunks) and
`data/kb_index.pkl` (the search index the agent queries at runtime). Re-run
this periodically (e.g. weekly cron job) to keep product info current.

If either site blocks scraping or needs JavaScript rendering, ask Würth's web
team for a product feed/sitemap export and feed it into
`scraper/scrape_kb.py`'s `SEED_URLS` — the rest of the pipeline is unaffected.

## 5. Run the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

For local testing before you have a public domain, tunnel it with something
like `ngrok http 8000` and use the ngrok URL as your webhook.

## 5b. Deploy live for free on Render

This repo includes `render.yaml` so Render can deploy it automatically.

1. Push this project to a GitHub repo (create one if you haven't: `git init`,
   commit, push to a new repo on your GitHub account).
2. Go to https://dashboard.render.com > **New** > **Blueprint**, connect your
   GitHub account, and pick this repo. Render reads `render.yaml` and creates
   a free web service called `wurth-whatsapp-agent` automatically.
3. Render will ask you to fill in the env vars marked `sync: false`. Enter:
   - `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
     `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_VERIFY_TOKEN` (same values as
     your local `.env`)
   - `OPENROUTER_API_KEY`
   - `GOOGLE_SHEET_ID`
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — since Render's free tier has no file
     upload, paste the **entire contents** of your
     `credentials/service_account.json` file as the value of this one env
     var. The app writes it to disk automatically on startup (see `main.py`).
   - `ESCALATION_NOTIFY_NUMBERS` (optional)
4. Click **Apply**. Render will run the build command from `render.yaml`:
   `pip install -r requirements.txt && python -m scraper.scrape_kb && python -m kb.build_index`
   — this crawls wurth.ae/eshop.wurth.ae and builds the search index
   automatically during deploy, so you don't need to run it locally.
5. Once deployed, Render gives you a public URL like
   `https://wurth-whatsapp-agent.onrender.com`. That's your webhook base URL.

**Free tier note:** the free web service sleeps after 15 minutes of no
traffic. The first WhatsApp message after a period of inactivity may take
~30-50 seconds to get a reply while the instance wakes up; after that it's
fast until it goes idle again. This is fine for testing/low volume; if you
need always-on, upgrade that one service to Render's cheapest paid plan later
(no other changes needed).

**Updating the knowledge base after launch:** re-run the Render deploy
(Manual Deploy > Deploy latest commit, or just push a commit) to re-crawl and
rebuild the index — do this periodically, e.g. monthly, to keep product info
current.

## 6. Connect the WhatsApp webhook

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
reply grounded in the scraped knowledge base.

## 7. (Optional) Escalation alerts

Set `ESCALATION_NOTIFY_NUMBERS` in `.env` to comma-separated phone numbers
(international format, no `+`) of staff who should get a WhatsApp alert when
a conversation is flagged (pricing requests, complaints, "talk to a human", etc.).

---

## Project structure

```
main.py                  FastAPI app - webhook verification + message handling
config.py                Loads all settings from .env

scraper/scrape_kb.py     Crawls wurth.ae + eshop.wurth.ae -> data/knowledge_base.json
kb/build_index.py        Builds TF-IDF search index -> data/kb_index.pkl
kb/retriever.py          search(query, top_k) used at runtime

ai/openrouter_client.py  OpenRouter chat completion wrapper
ai/agent.py              Builds prompts (KB context + rep info), decides escalation

sheets/sheets_client.py  Google Sheets fuzzy company -> rep lookup

whatsapp/client.py       Sends messages via Meta Cloud API

storage/store.py         SQLite: customer<->company cache + conversation log

data/                    Generated knowledge base, index, and SQLite DB live here
credentials/             Put service_account.json here (gitignored)
```

## Notes on the AI answering approach

- Retrieval is TF-IDF-based (scikit-learn) — no vector database required, fast
  to set up, works well for keyword-heavy product/catalog content. If you
  want closer semantic matching later, swap `kb/build_index.py` and
  `kb/retriever.py` for an embeddings-based vector store; `main.py` and
  `ai/agent.py` don't need to change since they only call `kb_search(query, top_k)`.
- Company identification currently uses a light heuristic + fuzzy match
  against the sheet. For messier company names, you can upgrade
  `try_extract_company_name` in `ai/agent.py` to call OpenRouter and return
  structured JSON instead.
- All customer/company mappings and conversation history live in SQLite at
  `data/app.db` — swap for Postgres if you need multi-instance deployment.

## Compliance reminders

- WhatsApp requires opt-in before marketing messages, and marketing broadcasts
  must use Meta-approved message templates.
- Review UAE PDPL requirements for storing customer phone numbers and company
  data (retention period, access controls).

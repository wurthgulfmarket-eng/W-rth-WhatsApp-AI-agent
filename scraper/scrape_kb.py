"""
Crawls wurth.ae and eshop.wurth.ae and builds a knowledge base of text chunks
saved to data/knowledge_base.json.

Run this locally (not inside a sandboxed/offline environment) since it needs
real internet access to those two domains:

    python -m scraper.scrape_kb

Notes:
- This is a polite, breadth-limited crawler: same-domain only, respects a
  max page count and a delay between requests. Tune the constants below.
- eshop.wurth.ae is the e-commerce catalog - product names, descriptions,
  specs, and categories are the most valuable content there.
- wurth.ae is the corporate/marketing site - company info, services, policies,
  FAQs, branch/contact info.
- If a site blocks scraping or requires JS rendering, export product data via
  their sitemap.xml or ask Würth IT for a product feed/API instead - update
  SEED_URLS accordingly and this script's parsing will still work on the
  fetched HTML.
"""
import json
import time
import re
import sys
from collections import deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.append("..")
from config import config  # noqa: E402

SEED_URLS = [
    "https://www.wurth.ae/",
    "https://eshop.wurth.ae/",
]

ALLOWED_DOMAINS = {"www.wurth.ae", "wurth.ae", "eshop.wurth.ae"}

MAX_PAGES = 300          # total pages to crawl across both sites
REQUEST_DELAY_SEC = 0.7  # be polite - avoid hammering the server
TIMEOUT_SEC = 15
MIN_CHUNK_CHARS = 200    # skip near-empty chunks
MAX_CHUNK_CHARS = 1200   # split long pages into chunks of roughly this size

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WurthKBBot/1.0; +https://www.wurth.ae/)"
}

SKIP_PATH_PATTERNS = re.compile(
    r"(login|signin|cart|checkout|account|wishlist|\.pdf$|\.jpg$|\.png$|\.zip$)",
    re.IGNORECASE,
)


def is_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc not in ALLOWED_DOMAINS:
        return False
    if SKIP_PATH_PATTERNS.search(parsed.path):
        return False
    return True


def clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS):
    """Split into paragraph-ish chunks without cutting mid-sentence too badly."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current.strip())
    return [c for c in chunks if len(c) >= MIN_CHUNK_CHARS]


def extract_title(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def crawl():
    visited = set()
    queue = deque(SEED_URLS)
    kb_entries = []

    session = requests.Session()
    session.headers.update(HEADERS)

    while queue and len(visited) < MAX_PAGES:
        url = queue.popleft()
        if url in visited or not is_allowed(url):
            continue
        visited.add(url)

        try:
            resp = session.get(url, timeout=TIMEOUT_SEC)
            resp.raise_for_status()
            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue
        except requests.RequestException as e:
            print(f"[skip] {url} -> {e}")
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        title = extract_title(soup)
        text = clean_text(soup)

        for chunk in chunk_text(text):
            kb_entries.append({
                "source_url": url,
                "title": title,
                "text": chunk,
            })

        print(f"[ok] {url} -> {len(text)} chars, visited {len(visited)}/{MAX_PAGES}")

        # enqueue same-domain links
        for a in soup.find_all("a", href=True):
            next_url = urljoin(url, a["href"]).split("#")[0]
            if next_url not in visited and is_allowed(next_url):
                queue.append(next_url)

        time.sleep(REQUEST_DELAY_SEC)

    return kb_entries


def main():
    print("Starting crawl of wurth.ae and eshop.wurth.ae ...")
    entries = crawl()
    print(f"Collected {len(entries)} knowledge base chunks from {len({e['source_url'] for e in entries})} pages")

    import os
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.KB_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"Saved knowledge base to {config.KB_JSON_PATH}")
    print("Next step: python -m kb.build_index")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Crawls wurth.ae + eshop.wurth.ae and builds the search index.
# Run this once initially, then periodically (e.g. weekly via cron) to keep it fresh.
set -e
echo "Step 1/2: crawling wurth.ae and eshop.wurth.ae ..."
python -m scraper.scrape_kb
echo "Step 2/2: building search index ..."
python -m kb.build_index
echo "Done. Knowledge base ready at data/kb_index.pkl"

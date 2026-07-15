"""
Builds a TF-IDF retrieval index over data/knowledge_base.json and saves it
to data/kb_index.pkl. This gives fast, dependency-light semantic-ish search
without needing a separate vector database.

Run after scraper/scrape_kb.py:

    python -m kb.build_index

To upgrade later to real embeddings (better relevance), swap this module
for a vector store (e.g. Chroma, pgvector) and embed chunks with any
embeddings model - the rest of the app only needs kb/retriever.py's
`search(query, top_k)` interface to keep working.
"""
import json
import pickle
import sys

from sklearn.feature_extraction.text import TfidfVectorizer

sys.path.append("..")
from config import config  # noqa: E402


def main():
    with open(config.KB_JSON_PATH, "r", encoding="utf-8") as f:
        entries = json.load(f)

    if not entries:
        raise SystemExit("Knowledge base is empty - run scraper/scrape_kb.py first.")

    texts = [f"{e['title']} {e['text']}" for e in entries]

    vectorizer = TfidfVectorizer(
        max_features=50000,
        stop_words="english",
        ngram_range=(1, 2),
    )
    matrix = vectorizer.fit_transform(texts)

    with open(config.KB_INDEX_PATH, "wb") as f:
        pickle.dump({
            "vectorizer": vectorizer,
            "matrix": matrix,
            "entries": entries,
        }, f)

    print(f"Indexed {len(entries)} chunks -> {config.KB_INDEX_PATH}")


if __name__ == "__main__":
    main()

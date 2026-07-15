"""
Loads the TF-IDF index built by kb/build_index.py and answers
`search(query, top_k)` calls with the most relevant knowledge base chunks.
"""
import pickle
import threading

from sklearn.metrics.pairwise import cosine_similarity

from config import config

_lock = threading.Lock()
_state = {"loaded": False, "vectorizer": None, "matrix": None, "entries": None}


def _load():
    with _lock:
        if _state["loaded"]:
            return
        try:
            with open(config.KB_INDEX_PATH, "rb") as f:
                data = pickle.load(f)
            _state.update({
                "vectorizer": data["vectorizer"],
                "matrix": data["matrix"],
                "entries": data["entries"],
                "loaded": True,
            })
        except FileNotFoundError:
            raise RuntimeError(
                "Knowledge base index not found. Run:\n"
                "  python -m scraper.scrape_kb\n"
                "  python -m kb.build_index"
            )


def search(query: str, top_k: int = 4):
    """Returns top_k most relevant chunks as list of dicts with score, title, text, source_url."""
    _load()
    vectorizer = _state["vectorizer"]
    matrix = _state["matrix"]
    entries = _state["entries"]

    query_vec = vectorizer.transform([query])
    scores = cosine_similarity(query_vec, matrix).flatten()
    top_indices = scores.argsort()[::-1][:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] <= 0:
            continue
        entry = entries[idx]
        results.append({
            "score": float(scores[idx]),
            "title": entry["title"],
            "text": entry["text"],
            "source_url": entry["source_url"],
        })
    return results

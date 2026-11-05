"""Retrieval (RAG) for the support assistant and for suggested replies.

  search()          listings closest in meaning to a question, using the embeddings built by the Q&A
                    notebook (eval/chatbot_qa_eval.ipynb). Vehicle make and year in the question nudge
                    the ranking (a listing that names other years drops), and matches below a minimum
                    similarity are left out instead of being passed to the AI.
  hybrid_products() the chatbot's product search: keyword matches from the store plus meaning matches,
                    merged by reciprocal rank fusion.
  similar_answers() earlier questions the team answered (staff replies or corrections) closest in meaning
                    to a new one, with personal details removed.
  complaints()      issue counts from buyer feedback on a listing (feedback labels from week 1).

Every function degrades to "nothing found" when data files or the Gemini key are missing, so the app
keeps working without them. Thresholds: RAG_MIN_SCORE (listings, default 0.55) and RAG_ANSWER_MIN_SCORE
(earlier answers, default 0.70) in backend/.env; eval/rag_calibrate.py suggests values from the test set."""
import json
import os
import re
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
QA = DATA / "qa"
CACHE_DIR = DATA / "rag"
EMBED_MODEL = "gemini-embedding-001"
_index = None
_feedback = None

MAKES = ["honda", "yamaha", "suzuki", "kawasaki", "polaris", "arctic cat", "ktm", "can-am", "coleman", "baja",
         "ricardo", "roketa", "jcl", "coolster", "taotao", "tao tao", "ssr", "kymco", "gy6", "husqvarna",
         "briggs", "kohler", "predator", "carter", "hammerhead", "motovox", "vespa", "harley"]
_MAKE_RE = re.compile(r"\b(" + "|".join(re.escape(m) for m in MAKES) + r")\b")
_YEAR = r"(19[5-9]\d|20[0-3]\d)"


def min_score():
    return float(os.environ.get("RAG_MIN_SCORE", "0.55"))


def answer_min_score():
    return float(os.environ.get("RAG_ANSWER_MIN_SCORE", "0.70"))


def parse_vehicle(text):
    """Makes and model years named in a question or listing: ({"yamaha"}, {2007, ..., 2012})."""
    t = (text or "").lower()
    makes = {m.replace("tao tao", "taotao") for m in _MAKE_RE.findall(t)}
    years = set()
    for a, b in re.findall(_YEAR + r"\s*(?:-|to|thru|through)\s*" + _YEAR, t):
        a, b = int(a), int(b)
        if 0 <= b - a <= 40:
            years.update(range(a, b + 1))
    years.update(int(y) for y in re.findall(r"\b" + _YEAR + r"\b", t))
    return makes, years


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def embed_texts(texts, task):
    """Gemini embeddings for texts (batches of 20), or None without a key."""
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key or not texts:
        return None
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    out = []
    for i in range(0, len(texts), 20):
        res = client.models.embed_content(model=EMBED_MODEL, contents=texts[i:i + 20],
                                          config=types.EmbedContentConfig(task_type=task))
        out += [e.values for e in res.embeddings]
    return out


EMBED = embed_texts   # replaceable in tests


def _unit(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-9)


def _embed_query(text):
    known = QA / "cache" / "embeddings_questions.json"
    if known.exists():
        cached = json.loads(known.read_text())
        if text in cached:
            return _unit(cached[text])
    v = EMBED([text], "RETRIEVAL_QUERY")
    return _unit(v[0]) if v else None


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def _load():
    """Listings, their vectors and the vehicles they name, loaded once (the vector file is large)."""
    global _index
    if _index is not None:
        return _index
    _index = {"rows": [], "vecs": None}
    try:
        import pandas as pd
        listings = pd.read_csv(QA / "listings.csv", dtype=str).fillna("")
        listings = listings.drop(columns=[c for c in ["condition_notes"] if c in listings.columns])
        listings = listings.drop_duplicates("title")
        listings = listings.groupby(listings["description"].str[:120], group_keys=False).head(5).reset_index(drop=True)
        texts = (listings["title"] + ". Condition: " + listings["condition"] + ". "
                 + listings["item_specifics"] + " " + listings["description"]).str[:1200].tolist()
        meta = pd.read_csv(QA / "listings_meta.csv", dtype=str).fillna("").drop_duplicates("title")
        item_ids = dict(zip(meta["title"], meta["item_id"]))
        cache = json.loads((QA / "cache" / "embeddings_kb.json").read_text())
        rows, vecs = [], []
        for i, text in enumerate(texts):
            if text not in cache:
                continue
            title = listings["title"][i]
            item = item_ids.get(title, "")
            makes, years = parse_vehicle(f"{title} {listings['item_specifics'][i]}")
            rows.append({"title": title, "sku": ("EBAY-" + item.replace("|", "-")) if item else "",
                         "text": text, "makes": makes, "years": years})
            vecs.append(cache[text])
        if rows:
            _index = {"rows": rows, "vecs": _unit(vecs)}
    except Exception:
        pass
    return _index


def _fit(row, makes, years):
    """True if the listing names the asked year, False if it names other years only, None if unknown."""
    if years and row["years"]:
        return bool(years & row["years"])
    return None


def search(query, k=3, threshold=None):
    """Listings closest in meaning to the query, nudged by vehicle make and year:
    [{title, sku, link, text, score, fit}], best first, none below the threshold."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        q = _embed_query(query[:2000])
        if q is None:
            return []
        index = _load()
        if index["vecs"] is None:
            return []
        import numpy as np
        scores = index["vecs"] @ q
        makes, years = parse_vehicle(query)
        adjusted = scores.copy()
        if makes or years:
            for i, row in enumerate(index["rows"]):
                if makes & row["makes"]:
                    adjusted[i] += 0.05
                fit = _fit(row, makes, years)
                if fit is True:
                    adjusted[i] += 0.05
                elif fit is False:
                    adjusted[i] -= 0.10
        cut = min_score() if threshold is None else threshold
        out = []
        for i in np.argsort(-adjusted):
            if scores[i] < cut:
                continue
            row = index["rows"][i]
            out.append({"title": row["title"], "sku": row["sku"], "text": row["text"],
                        "link": f"/product?sku={row['sku']}" if row["sku"] else "",
                        "score": round(float(scores[i]), 3), "fit": _fit(row, makes, years)})
            if len(out) == k:
                break
        return out
    except Exception:
        return []


def hybrid_products(query, storefront, brief, k=6):
    """The chatbot's search_products tool: keyword and meaning matches, merged by reciprocal rank fusion."""
    query = (query or "")[:100]
    page = storefront.product_page(query, 1, 10)
    keyword = page["items"]
    meaning = [m for m in search(query, k=10) if m["sku"]]
    if not meaning:
        return {"total_matches": page["total"], "products": [brief(p) for p in keyword[:k]]}
    score = Counter()
    for rank, p in enumerate(keyword):
        score[p["sku"]] += 1 / (60 + rank + 1)
    for rank, m in enumerate(meaning):
        score[m["sku"]] += 1 / (60 + rank + 1)
    by_sku = {p["sku"]: p for p in keyword}
    fits = {m["sku"]: m["fit"] for m in meaning}
    products = []
    for sku, _ in score.most_common():
        product = by_sku.get(sku) or storefront.get_product(sku)
        if not product:
            continue
        item = brief(product)
        if fits.get(sku) is not None:
            item["fits_the_year_asked"] = fits[sku]
        products.append(item)
        if len(products) == k:
            break
    return {"total_matches": max(page["total"], len(products)), "products": products,
            "search": "keyword and meaning"}


# ---------------------------------------------------------------------------
# Earlier answers from the team
# ---------------------------------------------------------------------------

def scrub(text):
    """Removes emails, phone numbers and order numbers before text is indexed or shown as an example."""
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", text or "")
    t = re.sub(r"(?i)\border\s*(?:number|no\.?|#)?\s*:?\s*#?\d{3,}\b", "order [number]", t)
    t = re.sub(r"#\d{4,}\b", "[number]", t)
    t = re.sub(r"\+?\d[\d\s().-]{8,}\d", "[phone]", t)
    return t


def similar_answers(text, pairs, k=3, threshold=None):
    """The pairs ({question, answer}) whose question is closest in meaning to text, scrubbed:
    [{question, answer, score}], none below the threshold. Question vectors are cached in data/rag/."""
    text = (text or "").strip()
    if not text or not pairs:
        return []
    try:
        pairs = [{"question": scrub(p["question"]), "answer": scrub(p["answer"])} for p in pairs
                 if (p.get("question") or "").strip() and (p.get("answer") or "").strip()]
        path = CACHE_DIR / "answers_cache.json"
        cached = json.loads(path.read_text()) if path.exists() else {}
        missing = list(dict.fromkeys(p["question"] for p in pairs if p["question"] not in cached))
        if missing:
            vectors = EMBED(missing, "RETRIEVAL_DOCUMENT")
            if vectors is None:
                return []
            cached.update(dict(zip(missing, vectors)))
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached))
        q = _embed_query(scrub(text)[:2000])
        if q is None:
            return []
        import numpy as np
        scores = _unit([cached[p["question"]] for p in pairs]) @ q
        cut = answer_min_score() if threshold is None else threshold
        seen, out = set(), []
        for i in np.argsort(-scores):
            if scores[i] < cut or pairs[i]["answer"] in seen:
                continue
            seen.add(pairs[i]["answer"])
            out.append({**pairs[i], "score": round(float(scores[i]), 3)})
            if len(out) == k:
                break
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Buyer feedback on a listing
# ---------------------------------------------------------------------------

def _load_feedback():
    global _feedback
    if _feedback is not None:
        return _feedback
    _feedback = {"by_id": {}, "by_title": {}}
    try:
        import pandas as pd
        fb = pd.read_csv(DATA / "pauleagleauctions" / "feedback_clean_pauleagle.csv", dtype=str).fillna("")
        for name in ["feedback_labels_v2.csv", "feedback_labels_llm_both.csv"]:
            if (DATA / name).exists():
                labels = pd.read_csv(DATA / name, dtype=str).fillna("")
                break
        else:
            return _feedback
        col = "label" if "label" in labels.columns else "llm_label"
        fb["uid"] = "Paul Eagle-" + fb["feedback_id"]
        fb = fb.merge(labels[["uid", col]].rename(columns={col: "label"}), on="uid")
        fb = fb[(fb["comment_type"] != "POSITIVE") & (fb["label"] != "positive")]
        for r in fb.itertuples():
            if getattr(r, "listing_id", ""):
                _feedback["by_id"].setdefault(r.listing_id, Counter())[r.label] += 1
            if getattr(r, "listing_title", ""):
                _feedback["by_title"].setdefault(_norm(r.listing_title), Counter())[r.label] += 1
    except Exception:
        pass
    return _feedback


def _norm(title):
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def complaints(sku, title):
    """Issue counts from negative or neutral buyer feedback on this listing: {"fitment": 2, ...}."""
    fb = _load_feedback()
    found = Counter()
    for listing_id, counts in fb["by_id"].items():
        if listing_id and f"-{listing_id}-" in f"-{sku or ''}-":
            found.update(counts)
    if not found:
        found.update(fb["by_title"].get(_norm(title), Counter()))
    return dict(found.most_common())

import csv, json, re
from collections import Counter
from pathlib import Path

DATA = Path(__file__).parent / "data"
RAW = DATA / "feedback_raw.json"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
FIELDS = ["feedback_id", "comment_type", "comment_text", "entered_within",
          "ratings", "listing_id", "listing_title"]

def period(p):
    if not isinstance(p, dict) or "value" not in p:
        return ""
    unit = str(p.get("unit", "")).lower()
    return f"{p['value']} {unit}s" if unit else str(p["value"])

def find_key(obj, names):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and isinstance(v, (str, int)):
                return str(v)
        for v in obj.values():
            f = find_key(v, names)
            if f:
                return f
    elif isinstance(obj, list):
        for v in obj:
            f = find_key(v, names)
            if f:
                return f
    return ""

def scrub(text):
    return PHONE.sub("[phone]", EMAIL.sub("[email]", text))

def clean(entry):
    if entry.get("automatedFeedback"):
        return None
    comment = entry.get("feedbackComment") or {}
    text = " ".join(str(comment.get("commentText") or "").split())
    if not text:
        return None
    ratings = "; ".join(f"{r.get('ratingType')}={r.get('value')}"
                        for r in entry.get("feedbackRatings") or [] if isinstance(r, dict))
    summary = entry.get("orderLineItemSummary") or {}
    return {
        "feedback_id": entry.get("feedbackId", ""),
        "comment_type": entry.get("commentType", ""),
        "comment_text": scrub(text),
        "entered_within": period(entry.get("feedbackEnteredPeriod")),
        "ratings": ratings,
        "listing_id": find_key(summary, {"listingId", "itemId", "legacyItemId"}),
        "listing_title": find_key(summary, {"listingTitle", "title", "itemTitle"}),
    }

def write(path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

def main():
    raw = json.loads(RAW.read_text())
    rows, seen = [], set()
    for entry in raw:
        row = clean(entry)
        if row and row["feedback_id"] not in seen:
            seen.add(row["feedback_id"])
            rows.append(row)
    order = {"NEGATIVE": 0, "NEUTRAL": 1, "POSITIVE": 2}
    rows.sort(key=lambda r: order.get(r["comment_type"], 3))
    issues = [r for r in rows if r["comment_type"] in ("NEGATIVE", "NEUTRAL")]
    write(DATA / "feedback_clean.csv", rows)
    write(DATA / "feedback_issues.csv", issues)
    print(f"Raw entries:          {len(raw)}")
    print(f"Real buyer comments:  {len(rows)}")
    print(f"  by type:            {dict(Counter(r['comment_type'] for r in rows))}")
    print(f"  with listing info:  {sum(1 for r in rows if r['listing_id'] or r['listing_title'])}")
    print(f"Negative + neutral:   {len(issues)}")
    print(f"Wrote {DATA / 'feedback_clean.csv'}")
    print(f"Wrote {DATA / 'feedback_issues.csv'}")

if __name__ == "__main__":
    main()

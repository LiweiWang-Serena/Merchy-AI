"""Builds training data for a "does this message need a person?" classifier.
Run from backend/:  python eval/build_escalation_data.py

Gemini rewrites real store data into buyer messages:
  label 1 (needs a person): negative or neutral feedback -> the message the buyer sent about the problem
  label 0 (the chatbot can answer): positive feedback -> a thank-you message; listings -> pre-sale questions;
          plus policy questions and what-if questions ("what if it arrives damaged?"), which look like
          problems but only ask about a policy.
The 42 complaints and 10 listings used by the Q&A test set (data/qa/qa_test_set_v1.csv) are left out, so the
test stays fair. Every Gemini result is cached in data/finetune/cache/, so re-running never repeats work.
Output: data/finetune/escalation_train.csv (text, label, intent, source). About 80 Gemini requests."""
import json, os, random, sys, time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "data"
OUT = DATA / "finetune"
CACHE = OUT / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
load_dotenv(BACKEND / ".env", override=True)
MODEL = "gemini-3.5-flash-lite"
PAUSE = 4
N_PROBLEMS, N_THANKS, N_LISTINGS = 600, 150, 300
random.seed(7)

_client = None
def client():
    global _client
    if _client is None:
        from google import genai
        key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not key:
            sys.exit("Add GEMINI_API_KEY to backend/.env")
        _client = genai.Client(api_key=key)
    return _client


def ask(system, payload, retries=4):
    for attempt in range(retries):
        try:
            r = client().models.generate_content(
                model=MODEL, contents=json.dumps(payload, ensure_ascii=False),
                config={"system_instruction": system, "temperature": 0.7, "response_mime_type": "application/json"})
            return json.loads(r.text)
        except Exception as e:
            wait = 10 * 2 ** attempt
            print(f"  attempt {attempt + 1} failed ({str(e)[:60]}), retrying in {wait}s")
            time.sleep(wait)
    raise SystemExit("Gemini is busy or out of quota. Run the script again later; finished work is kept.")


def cached(name):
    p = CACHE / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save(name, data):
    (CACHE / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False))


def batched(name, items, system, size):
    """items: {id: text}. Calls Gemini for ids not cached yet, in batches; returns {id: result}."""
    done = cached(name)
    todo = [k for k in items if k not in done]
    print(f"{name}: {len(items) - len(todo)} cached, {len(todo)} to write")
    for i in range(0, len(todo), size):
        part = {k: items[k] for k in todo[i:i + size]}
        done.update({k: v for k, v in ask(system, part).items() if k in part})
        save(name, done)
        time.sleep(PAUSE)
    return done


STORE = "an eBay store that sells motorcycle, ATV and small-engine parts"
PROBLEM_SYSTEM = f"""Each input is real buyer feedback left after a problem with an order from {STORE}.
Write the message that buyer sent the seller AFTER the order arrived (or failed to arrive), describing that same
problem. Vary the style: some short and blunt, some polite, some with typos. Keep the details, remove names,
order numbers and contact details. One to three sentences. Reply with only a JSON object mapping each id to the message."""
THANKS_SYSTEM = f"""Each input is real positive buyer feedback for {STORE}.
Write a short message the buyer might send the seller after the order arrived fine: a thank-you, a quick
confirmation it fits, or a friendly note. It must NOT report a problem or ask for anything to be fixed.
One or two sentences, informal. Reply with only a JSON object mapping each id to the message."""
PRESALE_SYSTEM = f"""For each listing from {STORE}, write two short, realistic questions a buyer might send BEFORE buying:
fit for a specific vehicle and year, condition, what's included, shipping time, or price. Write like real buyers:
short, informal, sometimes with abbreviations (carb, quad, 4 wheeler). Reply with only a JSON object mapping each id
to a list of two questions."""
POLICY_SYSTEM = f"""Write 80 different short questions buyers ask {STORE} about policies before or after buying, with no
problem having happened: shipping speed, carriers, tracking, return windows, who pays return shipping, refunds timing,
international shipping, combining orders, warranty, how to confirm fit, response time. Vary the wording.
Reply with only JSON: {{"questions": ["...", ...]}}"""
WHATIF_SYSTEM = f"""Write 60 different short questions buyers ask {STORE} about what would happen IF something went wrong,
before anything has happened: "what if it arrives damaged?", "if it doesn't fit can I return it?", "what happens if the
package is lost?". They are hypothetical policy questions, not reports of a real problem. Vary the wording.
Reply with only JSON: {{"questions": ["...", ...]}}"""


def main():
    test = pd.read_csv(DATA / "qa" / "qa_test_set_v1.csv", dtype=str).fillna("")
    test_uids = set(test.loc[test["source"] == "feedback", "source_id"])
    test_listing_heads = {t[:100] for t in test.loc[test["source"] == "listing", "source_text"]}

    fb = pd.read_csv(DATA / "pauleagleauctions" / "feedback_clean_pauleagle.csv", dtype={"feedback_id": str}).assign(store="Paul Eagle")
    fb2 = pd.read_csv(DATA / "fellerssmallengines" / "feedback_clean_fellers.csv", dtype={"feedback_id": str}).assign(store="fellers")
    fb = pd.concat([fb, fb2], ignore_index=True)
    fb["uid"] = fb["store"] + "-" + fb["feedback_id"]
    labels = pd.read_csv(DATA / "feedback_labels_llm_both.csv", dtype=str)
    col = "label" if "label" in labels.columns else "llm_label"
    fb = fb.merge(labels[["uid", col]].rename(columns={col: "issue"}), on="uid")
    fb = fb[~fb["uid"].isin(test_uids) & (fb["comment_text"].str.len() >= 20)]

    problems = fb[(fb["comment_type"] != "POSITIVE") & (fb["issue"] != "positive")]
    problems = problems.sample(min(N_PROBLEMS, len(problems)), random_state=7)
    thanks = fb[(fb["comment_type"] == "POSITIVE") & (fb["issue"] == "positive")]
    thanks = thanks.sample(min(N_THANKS, len(thanks)), random_state=7)

    listings = pd.read_csv(DATA / "qa" / "listings.csv", dtype=str).fillna("")
    listings["text"] = (listings["title"] + ". " + listings["item_specifics"] + " " + listings["description"]).str[:800]
    listings = listings[~listings["text"].str[:100].isin(test_listing_heads)].drop_duplicates("title")
    listings = listings.sample(min(N_LISTINGS, len(listings)), random_state=7).reset_index(drop=True)

    p = batched("problems", dict(zip(problems["uid"], problems["comment_text"])), PROBLEM_SYSTEM, 15)
    t = batched("thanks", dict(zip(thanks["uid"], thanks["comment_text"])), THANKS_SYSTEM, 15)
    l = batched("presale", {f"listing-{i}": x for i, x in enumerate(listings["text"])}, PRESALE_SYSTEM, 10)
    pol = cached("policy") or ask(POLICY_SYSTEM, {"count": 80}); save("policy", pol)
    wi = cached("whatif") or ask(WHATIF_SYSTEM, {"count": 60}); save("whatif", wi)

    issue = dict(zip(problems["uid"], problems["issue"]))
    rows = [{"text": v, "label": 1, "intent": issue.get(k, "problem"), "source": "feedback"} for k, v in p.items()]
    rows += [{"text": v, "label": 0, "intent": "thanks", "source": "feedback"} for v in t.values()]
    rows += [{"text": q, "label": 0, "intent": "pre_sale", "source": "listing"} for qs in l.values() for q in qs[:2]]
    rows += [{"text": q, "label": 0, "intent": "policy", "source": "generated"} for q in pol.get("questions", [])]
    rows += [{"text": q, "label": 0, "intent": "what_if", "source": "generated"} for q in wi.get("questions", [])]
    df = pd.DataFrame(rows)
    df["text"] = df["text"].astype(str).str.strip()
    df = df[df["text"].str.len() > 5].drop_duplicates("text").sample(frac=1, random_state=7)
    df.to_csv(OUT / "escalation_train.csv", index=False)
    print(f"\nSaved {len(df)} messages to data/finetune/escalation_train.csv")
    print(df.groupby(["label", "intent"]).size().to_string())


if __name__ == "__main__":
    main()

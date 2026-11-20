"""Builds the classic-RAG inputs for the 70 test questions, the same way Gemini prompt v2 was run:
all store policies + the 3 listings closest in meaning (cached gemini-embedding-001 vectors, no API calls).
Run from backend/:  python eval/make_classic_test.py
Output: data/finetune/classic_test.jsonl (upload it to Colab)."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import retrieval   # noqa: E402

DATA = BACKEND / "data"
PROMPT_V2 = '''You are the customer-support assistant for an eBay store that sells motorcycle and small-engine parts.
Answer the buyer using only the store information below. Never promise anything that is not in it.
Rules:
- Fit questions: say what the listing states about fit, then remind the buyer to match the OEM part number or send their year, make and model so we can double-check before they buy.
- Problems only staff can solve (refund, return, replacement, wrong, damaged, missing or late item): explain the relevant policy, ask for photos or order details if useful, and say a team member will follow up.
- If the store information does not cover the question, say a team member will follow up.
Store information:
{context}'''

facts = json.loads((DATA / "qa" / "store_facts_v1.json").read_text())
facts_text = "\n".join(f"- {k}: {v.replace('(assumed) ', '')}" for k, v in facts.items())
index = retrieval._load()
if index["vecs"] is None:
    sys.exit("No listing vectors found: run the RAG part of the eval notebook first.")
qvecs = json.loads((DATA / "qa" / "cache" / "embeddings_questions.json").read_text())
test = pd.read_csv(DATA / "qa" / "qa_test_set_v1.csv", dtype=str).fillna("")

out = DATA / "finetune" / "classic_test.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
missing = 0
with out.open("w") as f:
    for r in test.itertuples():
        if r.question not in qvecs:
            missing += 1
            continue
        scores = index["vecs"] @ retrieval._unit(qvecs[r.question])
        top = [index["rows"][j]["text"] for j in np.argsort(-scores)[:3]]
        context = facts_text + "\n\nRelevant listings:\n" + "\n".join(f"- {t}" for t in top)
        f.write(json.dumps({"qid": r.qid, "question": r.question,
                            "system": PROMPT_V2.format(context=context)}, ensure_ascii=False) + "\n")
print(f"Saved {len(test) - missing} questions to {out}" + (f" ({missing} had no cached vector)" if missing else ""))

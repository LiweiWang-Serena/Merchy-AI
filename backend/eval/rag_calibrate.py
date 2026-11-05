"""Suggests RAG_MIN_SCORE from the Q&A test set, with no API calls (uses the notebook's cached vectors).
Run from backend/:  python eval/rag_calibrate.py
For questions that need a listing, the right listing's similarity should be above the threshold;
for questions that don't (policy and after-sale), the best listing's similarity should be below it."""
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import retrieval

QA = retrieval.QA
test = pd.read_csv(QA / "qa_test_set_v1.csv", dtype=str).fillna("")
questions = json.loads((QA / "cache" / "embeddings_questions.json").read_text())
index = retrieval._load()
if index["vecs"] is None:
    sys.exit("No listing vectors: run the RAG part of the notebook first.")
texts = [r["text"] for r in index["rows"]]

needs, other = [], []
for r in test.itertuples():
    if r.question not in questions:
        continue
    scores = index["vecs"] @ retrieval._unit(questions[r.question])
    if r.source == "listing" and "isting" in r.key_points:
        hit = [i for i, t in enumerate(texts) if t[:100] == r.source_text[:100]]
        if hit:
            needs.append(float(scores[hit[0]]))
    else:
        other.append(float(scores.max()))

print(f"Need a listing ({len(needs)}): right listing's similarity  min {min(needs):.3f}  median {np.median(needs):.3f}")
print(f"Don't need one ({len(other)}): best listing's similarity  median {np.median(other):.3f}  max {max(other):.3f}")
best = max(np.arange(0.30, 0.90, 0.01),
           key=lambda t: sum(s >= t for s in needs) + sum(s < t for s in other))
kept = sum(s >= best for s in needs) / len(needs)
dropped = sum(s < best for s in other) / len(other)
print(f"\nSuggested RAG_MIN_SCORE={best:.2f}: keeps the right listing for {kept:.0%} of listing questions, "
      f"and passes no listing for {dropped:.0%} of the others.")
print("Put it in backend/.env as RAG_MIN_SCORE=... and restart the app. With ~30 questions, treat it as a starting point.")

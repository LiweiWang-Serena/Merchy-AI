"""Runs the same ReAct agent (eval/merchy_react.py) with Gemini as its brain on the 70 test questions,
so Gemini ReAct can be graded next to Gemini v2 (classic RAG), base Qwen ReAct and LLW ReAct.

Run from backend/:
    python eval/run_react_gemini.py                              # teacher model, gemini-3.1-flash-lite
    python eval/run_react_gemini.py --model gemini-3.5-flash-lite
About 3 Gemini calls per question. Each answer is saved as soon as it finishes, so re-running continues.
Output: data/qa/replies_react-gemini.csv (graded like the other runs) and data/finetune/traces_react-gemini.jsonl."""
import argparse, json, sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merchy_react import MerchyAgent                 # noqa: E402
from build_react_traces import make_gemini, DATA     # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.1-flash-lite")
    ap.add_argument("--pause", type=float, default=4.0)
    a = ap.parse_args()

    run = "react-gemini"
    replies_path = DATA / "qa" / f"replies_{run}.csv"
    traces_path = DATA / "finetune" / f"traces_{run}.jsonl"
    test = pd.read_csv(DATA / "qa" / "qa_test_set_v1.csv", dtype=str).fillna("")
    done = set(pd.read_csv(replies_path, dtype=str)["qid"]) if replies_path.exists() else set()
    todo = test[~test["qid"].isin(done)]
    print(f"{run}: {len(done)} done, {len(todo)} to run with {a.model}")

    agent = MerchyAgent(DATA / "qa" / "listings.csv", DATA / "qa" / "store_facts_v1.json")
    llm = make_gemini(a.model, a.pause)
    for i, q in enumerate(todo.itertuples(), 1):
        r = agent.run(q.question, llm)
        pd.DataFrame([{"qid": q.qid, "reply": r["answer"], "latency_s": r["seconds"]}]).to_csv(
            replies_path, mode="a", header=not replies_path.exists(), index=False)
        with traces_path.open("a") as f:
            f.write(json.dumps({"qid": q.qid, "question": q.question, "source": q.source, "escalate": q.escalate,
                                "steps": r["steps"], "format_errors": r["format_errors"],
                                "handed_off": r["handed_off"], "gave_up": r.get("gave_up", False),
                                "messages": r["messages"]}, ensure_ascii=False) + "\n")
        print(f"  {i}/{len(todo)}  steps {r['steps']}  handed off {r['handed_off']}")

    t = pd.read_json(traces_path, lines=True).drop_duplicates("qid", keep="last")
    t["escalate"] = t["escalate"].astype(str).str.lower() == "true"
    print(f"\nAverage steps {t.steps.mean():.2f} | format errors {(t.format_errors > 0).sum()} | gave up {t.gave_up.sum()}")
    print(f"After-sale handed off: {t[t.source == 'feedback'].handed_off.sum()} of {(t.source == 'feedback').sum()}")
    print(f"Escalation right (no judge): {(t.handed_off == t.escalate).mean():.0%}")
    print(f"\nNext, in the eval notebook: run_eval('{run}', lambda q: '', {{'answer_model': '{a.model}', 'agent': 'ReAct, 4 tools'}})")


if __name__ == "__main__":
    main()

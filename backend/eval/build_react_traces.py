"""Stage 2: teacher traces for fine-tuning the ReAct agent.
Gemini runs the SAME agent as Qwen (eval/merchy_react.py: same prompt, tools and format) on buyer messages
from data/finetune/escalation_train.csv (built by eval/build_escalation_data.py; the 70 test questions are not in it).
Only good traces are kept: a Final Answer, no format errors, at most 5 steps, and a handoff exactly when the
message needs a person. Every trace is saved as soon as it finishes, so re-running continues where it stopped.

Run from backend/:
    python eval/build_react_traces.py                 # 100 questions, teacher gemini-3.1-flash-lite
    python eval/build_react_traces.py --n 60 --model gemini-3.5-flash-lite
About 3 to 4 Gemini calls per question. Output: data/finetune/react_traces.jsonl (good traces only)."""
import argparse, json, os, sys, time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from merchy_react import MerchyAgent   # noqa: E402

DATA = BACKEND / "data"
OUT = DATA / "finetune"
OUT.mkdir(parents=True, exist_ok=True)
ALL = OUT / "react_traces_all.jsonl"
GOOD = OUT / "react_traces.jsonl"
load_dotenv(BACKEND / ".env", override=True)


def make_gemini(model_name, pause):
    from google import genai
    from google.genai import types
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        sys.exit("Add GEMINI_API_KEY to backend/.env")
    client = genai.Client(api_key=key)

    def step(messages):
        contents = [types.Content(role="user" if m["role"] == "user" else "model",
                                  parts=[types.Part.from_text(text=m["content"])]) for m in messages[1:]]
        for attempt in range(5):
            try:
                r = client.models.generate_content(
                    model=model_name, contents=contents,
                    config={"system_instruction": messages[0]["content"], "temperature": 0,
                            "stop_sequences": ["Observation:"], "max_output_tokens": 600})
                time.sleep(pause)
                return r.text or ""
            except Exception as e:
                msg = str(e)
                if "429" in msg and "day" in msg.lower():
                    raise SystemExit("Daily Gemini quota used up. Run again tomorrow, or pass --model with another model.")
                wait = 15 * (attempt + 1)
                print(f"    Gemini failed ({msg[:60]}), retrying in {wait}s")
                time.sleep(wait)
        raise SystemExit("Gemini keeps failing. Run again later; finished traces are kept.")
    return step


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--model", default="gemini-3.1-flash-lite")
    ap.add_argument("--pause", type=float, default=4.0, help="seconds between calls (15 per minute limit)")
    a = ap.parse_args()

    src = OUT / "escalation_train.csv"
    if not src.exists():
        sys.exit("Run eval/build_escalation_data.py first (it writes the buyer messages).")
    msgs = pd.read_csv(src)
    share = {"problem": 0.5, "pre_sale": 0.3, "policy": 0.1, "what_if": 0.1}
    parts = [msgs[msgs["label"] == 1].sample(min(int(a.n * share["problem"]), (msgs["label"] == 1).sum()), random_state=11)]
    for intent in ["pre_sale", "policy", "what_if"]:
        g = msgs[msgs["intent"] == intent]
        parts.append(g.sample(min(int(a.n * share[intent]), len(g)), random_state=11))
    sample = pd.concat(parts).reset_index(drop=True)

    done = set()
    if ALL.exists():
        done = {json.loads(l)["question"] for l in ALL.open() if l.strip()}
    todo = sample[~sample["text"].isin(done)]
    print(f"{len(sample)} questions: {len(done & set(sample['text']))} done, {len(todo)} to run with {a.model}")

    agent = MerchyAgent(DATA / "qa" / "listings.csv", DATA / "qa" / "store_facts_v1.json")
    llm = make_gemini(a.model, a.pause)
    with ALL.open("a") as f:
        for i, r in enumerate(todo.itertuples(), 1):
            t = agent.run(r.text, llm, max_steps=6)
            needs_person = bool(r.label == 1)
            good = (not t.get("gave_up") and t["format_errors"] == 0 and t["steps"] <= 5
                    and t["handed_off"] == needs_person)
            f.write(json.dumps({"question": r.text, "intent": r.intent, "needs_person": needs_person,
                                "steps": t["steps"], "format_errors": t["format_errors"],
                                "handed_off": t["handed_off"], "good": good, "messages": t["messages"]},
                               ensure_ascii=False) + "\n")
            f.flush()
            print(f"  {i}/{len(todo)}  steps {t['steps']}  {'kept' if good else 'dropped'}")

    rows = [json.loads(l) for l in ALL.open() if l.strip()]
    good = [x for x in rows if x["good"]]
    with GOOD.open("w") as f:
        for x in good:
            f.write(json.dumps({"messages": x["messages"], "intent": x["intent"]}, ensure_ascii=False) + "\n")
    print(f"\n{len(good)} of {len(rows)} traces kept -> data/finetune/react_traces.jsonl")
    print(pd.Series([x["intent"] for x in good]).value_counts().to_string())


if __name__ == "__main__":
    main()

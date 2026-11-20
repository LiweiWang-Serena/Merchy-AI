"""Token usage, latency, sequential throughput and cost per version, from files already saved:
replies_<run>.csv, the ReAct traces and classic_test.jsonl. Nothing is regenerated.

Run from backend/:
    python eval/token_usage.py                 # Gemini tokens counted by the API's count_tokens (cached)
    python eval/token_usage.py --approx        # no API calls: Gemini tokens estimated as characters / 4
Qwen tokens are counted exactly with Qwen's own tokenizer (pip install transformers; downloads a few MB once).

Costs need prices, which change: put current ones from the official pricing pages in PRICES below.
API runs:  cost = input tokens x input price + output tokens x output price.
GPU runs:  cost = GPU price per hour x time spent answering (one question at a time, so an upper bound).
Output: data/qa/usage_summary.csv."""
import argparse, json, os, sys, time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
DATA = BACKEND / "data"
QA, FT = DATA / "qa", DATA / "finetune"
load_dotenv(BACKEND / ".env", override=True)

# ---- fill in current prices (USD); leave None to print tokens and time only ----
PRICES = {
    "gemini-3.5-flash-lite": {"input_per_m": 0.30, "output_per_m": 2.50},
    "gemini-3.1-flash-lite": {"input_per_m": 0.25, "output_per_m": 1.50},
    "gpu_per_hour": 0.526,         # AWS g4dn.xlarge (1x T4), on-demand, us-east-1
}

RUNS = [  # run name, model family, design, model name for pricing, where the prompts come from
    ("facts-plus-listing-rag-prompt-v2", "gemini", "classic RAG", "gemini-3.5-flash-lite", "classic"),
    ("react-gemini",                     "gemini", "ReAct agent", "gemini-3.1-flash-lite", "traces_react-gemini.jsonl"),
    ("qwen7b-classic",                   "qwen",   "classic RAG", "gpu",                   "classic"),
    ("react-qwen7b-base",                "qwen",   "ReAct agent", "gpu",                   "traces_react-qwen7b-base.jsonl"),
    ("react-llw",                        "qwen",   "ReAct agent", "gpu",                   "traces_react-llw.jsonl"),
]


def find(name):
    for p in [FT / name, FT / "cache" / name, QA / name, DATA / "mlartifacts" / name]:
        if p.exists():
            return p
    return None


# ---------------- token counters ----------------
class GeminiCounter:
    def __init__(self, approx):
        self.approx = approx
        self.cache_path = QA / "cache" / "token_counts_gemini.json"
        self.cache = json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        self.client = None
        if not approx:
            from google import genai
            key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
            if not key:
                sys.exit("No GEMINI_API_KEY in backend/.env; run with --approx instead.")
            self.client = genai.Client(api_key=key)

    def count(self, text, model):
        if self.approx or not text:
            return round(len(text or "") / 4)
        k = f"{model}|{hash(text)}|{len(text)}"
        if k not in self.cache:
            for attempt in range(4):
                try:
                    self.cache[k] = self.client.models.count_tokens(model=model, contents=text).total_tokens
                    break
                except Exception as e:
                    time.sleep(5 * (attempt + 1))
            else:
                self.cache[k] = round(len(text) / 4)
            if len(self.cache) % 50 == 0:
                self.save()
        return self.cache[k]

    def save(self):
        self.cache_path.write_text(json.dumps(self.cache))


class QwenCounter:
    def __init__(self):
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")

    def prompt(self, messages):
        ids = self.tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=True)
        if hasattr(ids, "keys"):        # newer transformers return a dict with input_ids
            ids = ids["input_ids"]
        return len(ids)

    def text(self, s):
        return len(self.tok(s or "", add_special_tokens=False)["input_ids"])


def as_text(messages):
    return "\n\n".join(f"{m['role']}: {m['content']}" for m in messages)


# ---------------- per-run usage ----------------
def usage_classic(run, family, model, counter, replies):
    prompts = {q["qid"]: q for q in map(json.loads, open(FT / "classic_test.jsonl"))}
    rows = []
    for r in replies.itertuples():
        q = prompts.get(r.qid)
        if not q:
            continue
        msgs = [{"role": "system", "content": q["system"]}, {"role": "user", "content": q["question"]}]
        if family == "qwen":
            tin, tout = counter.prompt(msgs), counter.text(r.reply)
        else:
            tin, tout = counter.count(as_text(msgs), model), counter.count(r.reply, model)
        rows.append({"qid": r.qid, "calls": 1, "input_tokens": tin, "output_tokens": tout})
    return rows


def usage_react(run, family, model, counter, path):
    rows = []
    for t in map(json.loads, open(path)):
        msgs, tin, tout, calls = t["messages"], 0, 0, 0
        for i, m in enumerate(msgs):
            if m["role"] != "assistant":
                continue
            calls += 1    # every step re-sends the whole conversation so far
            if family == "qwen":
                tin += counter.prompt(msgs[:i]); tout += counter.text(m["content"])
            else:
                tin += counter.count(as_text(msgs[:i]), model); tout += counter.count(m["content"], model)
        rows.append({"qid": t["qid"], "calls": calls, "input_tokens": tin, "output_tokens": tout})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--approx", action="store_true", help="estimate Gemini tokens as characters / 4 (no API calls)")
    a = ap.parse_args()
    if not (FT / "classic_test.jsonl").exists():
        sys.exit("Run eval/make_classic_test.py first (it writes data/finetune/classic_test.jsonl).")
    gem, qwen, out = GeminiCounter(a.approx), None, []
    for run, family, design, model, source in RUNS:
        rp = QA / f"replies_{run}.csv"
        if not rp.exists():
            print(f"skip {run}: no {rp.name}")
            continue
        replies = pd.read_csv(rp, dtype={"qid": str}).fillna("")
        if family == "qwen" and qwen is None:
            qwen = QwenCounter()
        counter = qwen if family == "qwen" else gem
        if source == "classic":
            rows = usage_classic(run, family, model, counter, replies)
        else:
            path = find(source)
            if not path:
                print(f"skip {run}: no {source}")
                continue
            rows = usage_react(run, family, model, counter, path)
        u = pd.DataFrame(rows)
        lat = pd.to_numeric(replies["latency_s"], errors="coerce")
        n = len(u)
        rec = {"run": run, "design": design, "questions": n,
               "calls_per_q": round(u.calls.mean(), 2),
               "input_tokens_per_q": round(u.input_tokens.mean()), "output_tokens_per_q": round(u.output_tokens.mean()),
               "median_latency_s": round(lat.median(), 2),
               "sequential_q_per_hour": round(3600 / lat.mean()) if lat.mean() > 0 else None,
               "cost_per_1000_q_usd": None}
        if model == "gpu" and PRICES["gpu_per_hour"]:
            rec["cost_per_1000_q_usd"] = round(PRICES["gpu_per_hour"] * lat.mean() * 1000 / 3600, 2)
        elif model in PRICES and PRICES[model]["input_per_m"] is not None:
            p = PRICES[model]
            rec["cost_per_1000_q_usd"] = round((u.input_tokens.mean() * p["input_per_m"] +
                                                 u.output_tokens.mean() * p["output_per_m"]) / 1e6 * 1000, 2)
        out.append(rec)
        print(f"done {run}")
    gem.save()
    df = pd.DataFrame(out)
    df.to_csv(QA / "usage_summary.csv", index=False)
    pd.set_option("display.width", 200)
    print("\n" + df.to_string(index=False))
    print("\nGemini tokens:", "estimated (characters / 4)" if a.approx else "counted with count_tokens",
          "| Qwen tokens: Qwen tokenizer | throughput: one question at a time, not concurrent")
    if df["cost_per_1000_q_usd"].isna().all():
        print("Costs are blank: add current prices to PRICES at the top of this file.")
    print("Note: Gemini v2 prompts are rebuilt from classic_test.jsonl (same policies + top 3 listings), so its input count is close but not byte-identical.")


if __name__ == "__main__":
    main()

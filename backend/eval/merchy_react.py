"""A ReAct support agent for the Merchy-AI store: the model thinks step by step, calls tools, then answers.

Shared by every model, so teacher (Gemini) and student (Qwen) run the same prompt, tools and format:
    agent = MerchyAgent(listings_csv, store_facts_json)
    result = agent.run(question, llm)      # llm(messages) -> text of the next assistant step
Each step is one assistant message ("Thought / Action / Action Input", or "Thought / Final Answer");
each tool result goes back as a user message ("Observation: ..."). That multi-turn shape is also the
fine-tuning format: the assistant turns are what the model learns, the observations are context.

Tools: SearchListings (BM25 over the listings, nudged by vehicle make and year), GetProduct, GetPolicy,
HandToStaff (records a handoff; refunds, returns and damaged, wrong or missing items need a person)."""
import json
import re
import time

import pandas as pd
from rank_bm25 import BM25Okapi

MAX_STEPS = 6
HANDOFF_NOTE = "\n\n[The conversation was handed to a person on the team.]"

SYSTEM = """You are the customer-support assistant for an eBay store that sells motorcycle, ATV and small-engine parts.
Work step by step with the tools, then answer the customer.

Tools:
- SearchListings: find listings by part, make, model and year. Input: search words, e.g. carburetor yamaha grizzly 2008
- GetProduct: full details of one listing. Input: a listing id from a search result, e.g. L12
- GetPolicy: the store's policy on shipping, tracking, returns, refunds, damaged items, fitment, condition or contact. Input: the topic
- HandToStaff: give the conversation to a person on the team. Use it for refunds, returns, replacements, and wrong, damaged, missing or late items, or anything the tools can't answer. Input: a one-line summary

Rules:
- Use only facts from tool results. Never promise anything they don't say.
- Fit questions: say what the listing states about fit, then remind the customer to match the OEM part number or send their year, make and model so we can double-check.
- If the customer reports a problem with an order (wrong, damaged, broken, defective, missing or late item, or not as described), call HandToStaff before you write the Final Answer.
- After HandToStaff, still give the relevant policy (look it up) and say a team member will follow up.
- If the customer doesn't say which item they mean, search with their words first, then answer or ask which listing they mean.

Write exactly one step per reply, in this format:
Thought: what to do next
Action: SearchListings, GetProduct, GetPolicy or HandToStaff
Action Input: the input
Then wait for the Observation. When you can answer, write:
Thought: I can answer now.
Final Answer: the reply to the customer, two to five sentences."""

_MAKES = ["honda", "yamaha", "suzuki", "kawasaki", "polaris", "arctic cat", "ktm", "can-am", "coleman", "baja",
          "ricardo", "roketa", "jcl", "coolster", "taotao", "ssr", "kymco", "gy6", "carter", "hammerhead"]
_YEAR = r"(19[5-9]\d|20[0-3]\d)"


def parse_vehicle(text):
    t = (text or "").lower()
    makes = {m for m in _MAKES if re.search(r"\b" + re.escape(m) + r"\b", t)}
    years = set()
    for a, b in re.findall(_YEAR + r"\s*(?:-|to|thru)\s*" + _YEAR, t):
        if 0 <= int(b) - int(a) <= 40:
            years.update(range(int(a), int(b) + 1))
    years.update(int(y) for y in re.findall(r"\b" + _YEAR + r"\b", t))
    return makes, years


def _tokens(text):
    t = (text or "").lower()
    return re.findall(r"[a-z0-9]+", t) + re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", t)


class MerchyAgent:
    def __init__(self, listings_csv, store_facts_json):
        df = pd.read_csv(listings_csv, dtype=str).fillna("")
        df = df.drop_duplicates("title").reset_index(drop=True)
        self.items = [{"id": f"L{i}", "title": r.title, "condition": r.condition,
                       "text": f"{r.title}. Condition: {r.condition}. {r.item_specifics} {r.description}"[:1200]}
                      for i, r in enumerate(df.itertuples())]
        for it in self.items:
            it["makes"], it["years"] = parse_vehicle(it["text"])
        self.bm25 = BM25Okapi([_tokens(it["text"]) for it in self.items])
        facts = json.loads(open(store_facts_json).read())
        self.policies = {k: v.replace("(assumed) ", "") for k, v in facts.items()}

    # ---------- tools ----------
    def search_listings(self, query, k=4):
        scores = self.bm25.get_scores(_tokens(query))
        makes, years = parse_vehicle(query)
        top = max(scores.max(), 1e-9)
        ranked = []
        for i, it in enumerate(self.items):
            s = scores[i] / top
            if makes & it["makes"]:
                s += 0.1
            if years and it["years"]:
                s += 0.15 if years & it["years"] else -0.2
            ranked.append((s, i))
        ranked.sort(reverse=True)
        hits = [self.items[i] for s, i in ranked[:k] if scores[i] > 0]
        if not hits:
            return "No listings found. Try other words (part name, make, model)."
        return "\n".join(f"{it['id']}: {it['title']} (condition: {it['condition']})" for it in hits)

    def get_product(self, ref):
        m = re.search(r"\bL(\d+)\b", ref or "")
        if m and int(m.group(1)) < len(self.items):
            return self.items[int(m.group(1))]["text"]
        return "Unknown listing id. Use an id from SearchListings, like L12."

    def get_policy(self, topic):
        words = set(_tokens(topic))
        aliases = {"handling_time": {"ship", "shipping", "fast", "when", "dispatch"},
                   "carriers": {"carrier", "usps", "ups", "tracking", "track"},
                   "lost_package": {"lost", "missing", "tracking", "arrived", "delivered"},
                   "returns": {"return", "returns", "exchange"}, "refunds": {"refund", "refunds", "money"},
                   "damaged_item": {"damaged", "broken", "cracked", "damage"},
                   "fitment": {"fit", "fitment", "fits", "compatible"}, "condition": {"used", "new", "condition"},
                   "contact": {"contact", "reply", "respond", "message"}}
        hits = [k for k, a in aliases.items() if k in self.policies and (words & a or k.replace("_", " ") in topic.lower())]
        chosen = hits or list(self.policies)
        return "\n".join(f"{k.replace('_', ' ')}: {self.policies[k]}" for k in chosen)

    # ---------- the ReAct loop ----------
    def run(self, question, llm, max_steps=MAX_STEPS):
        """Runs the agent. llm(messages) returns the next assistant step as text.
        Returns {answer, handed_off, steps, format_errors, messages, seconds}."""
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
        handed_off, errors, start = False, 0, time.time()
        for step in range(1, max_steps + 1):
            text = llm(messages)
            text = text.split("Observation:")[0].strip()          # the model must not write the observation
            act, fin = text.find("Action:"), text.find("Final Answer:")
            if act != -1 and fin > act:                            # an action and an answer in one step:
                text = text[:fin].strip()                          # run the action first, answer next step
            messages.append({"role": "assistant", "content": text})
            final = re.search(r"Final Answer:\s*(.+)", text, re.S)
            if final:
                answer = final.group(1).strip()
                return {"answer": answer + (HANDOFF_NOTE if handed_off else ""), "handed_off": handed_off,
                        "steps": step, "format_errors": errors, "messages": messages,
                        "seconds": round(time.time() - start, 2)}
            action = re.search(r"Action:\s*([A-Za-z]+)", text)
            arg = re.search(r"Action Input:\s*(.+)", text, re.S)
            name, value = (action.group(1) if action else ""), (arg.group(1).strip().strip('"') if arg else "")
            if name == "SearchListings":
                obs = self.search_listings(value)
            elif name == "GetProduct":
                obs = self.get_product(value)
            elif name == "GetPolicy":
                obs = self.get_policy(value)
            elif name == "HandToStaff":
                handed_off = True
                obs = ("The team has been notified. Now give the customer the relevant policy and say a team "
                       "member will follow up.")
            else:
                errors += 1
                obs = ("Invalid format. Reply with Thought, Action (SearchListings, GetProduct, GetPolicy or "
                       "HandToStaff) and Action Input, or with Thought and Final Answer.")
            messages.append({"role": "user", "content": f"Observation: {obs}"})
        answer = "Thanks for your patience. A team member will follow up with you shortly."
        return {"answer": answer + HANDOFF_NOTE, "handed_off": True, "steps": max_steps, "format_errors": errors,
                "messages": messages, "seconds": round(time.time() - start, 2), "gave_up": True}

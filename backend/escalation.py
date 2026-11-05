"""Decide when a support conversation must go to a person, as a safety net behind the prompt rule."""
import re

# A customer describing something that already went wrong with an order: staff must act.
_PROBLEM = [
    r"\b(arrived|came|received|got|showed up|delivered)\b.{0,60}\b(broken|cracked|damaged|crushed|bent|leak\w*|wrong|missing|defective|used)\b",
    r"\b(sent|received|got|shipped)\b.{0,60}\binstead\b",
    r"\b(is|was|are|were|it's|its)\s+(\w+\s+)?(broken|cracked|damaged|crushed|bent|defective|missing|faulty)\b",
    r"\b(broke|snapped|cracked|leaked|melted|fell apart)\b",
    r"\b(doesn't|does not|didn't|did not|won't|will not)\s+(\w+\s+)?(work|fit|start|run|seal|match)\b",
    r"\bstopped working\b",
    r"\bwrong\s+(size|part|item|color|colour|one|model)\b",
    r"\b(sent|shipped|gave)\s+(me\s+)?(the\s+|a\s+)?(wrong|different|incorrect)\b",
    r"\bnot as (described|pictured|shown|advertised)\b|\blooks nothing like\b",
    r"\bnever\s+(\w+\s+)?(arrived|came|received|got|showed up|shipped)\b",
    r"\b(hasn't|has not|haven't|have not)\s+(\w+\s+)?(arrived|shipped|come|received|gotten)\b",
    r"\bstill\s+(hasn't|has not|not|no)\b",
    r"\b(lost|stuck)\b.{0,30}\b(package|parcel|order|shipment|tracking|in transit)\b",
    r"\b(i want|i need|i'd like|i would like|give me|send me|requesting)\b.{0,25}\b(refund|money back|replacement|exchange|return label)\b",
    r"\b(need|want) to return\b|\bwhere is my (refund|money|order|package|part)\b|\bno refund\b",
]
# A what-if or a policy question, before anything has gone wrong: just answer.
_IF = r"\b(what if|what happens if|in case|if (the|my|a|an|it|i|this))\b"
_HAPPENED = r"\b(arrived|received|got|came|ordered|bought|paid|sent|opened)\b"
_QUESTION = r"^\s*(can|could|do|does|is|are|what|how|will|would|should|when|which)\b.*\?\s*$"
_PRESALE = r"\b(will|would|does|do|can|could) (this|it|these|that|one)\b.*\?"


def reports_problem(message):
    """True when the message describes an order problem that needs a person (refund, return,
    replacement, wrong, damaged, missing or late item), not a policy question or a what-if."""
    m = (message or "").lower().replace("\u2019", "'")
    if re.search(_IF, m) and (re.match(_QUESTION, m) or not re.search(_HAPPENED, m)):
        return False
    if re.search(_PRESALE, m) and not re.search(_HAPPENED, m):   # "my bike won't start, will this coil fix it?"
        return False
    return any(re.search(p, m) for p in _PROBLEM)

"""
AI customer support for the shop, using the Claude API or the Gemini API.

"Training" the assistant needs no model training. The admin controls:
  1. Settings: the assistant's name, greeting and tone notes.
  2. Knowledge: policies and answers written by the store (shipping,
     returns, fitment help...). The assistant answers from these.
  3. Corrections: on the Support page the admin reviews real
     conversations and writes a better answer where one fell short.
     Corrections are shown to the assistant as examples of the answers
     the store wants.
Facts come from the store's own data through tools: the assistant looks
up products, stock and order status instead of guessing them.

The assistant always presents itself as an AI assistant and never
claims to be a person (California's B.O.T. Act requires this when a bot
helps sell goods, and Anthropic's usage policies require consumer
chatbots to disclose that they're AI). It can hand a conversation to a
person on the team at any time.

When a conversation is handed to a person (the customer asks, or the
assistant can't help), the team is notified (notifications.py), a person
replies from the Support page straight into the customer's chat, and the
assistant stays quiet until the person marks the conversation resolved.

Which AI answers: ANTHROPIC_API_KEY uses Claude, GEMINI_API_KEY uses
Gemini (SUPPORT_PROVIDER picks one if both are set). Everything else
(articles, corrections, tools, handoff, notifications) is shared; only
the small adapter that talks to the API differs. With neither key, the
widget still works in a basic offline mode that answers from the
knowledge articles by keyword.
"""

import json
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime

import db
import notifications
import storefront
from db import get_connection

DEFAULT_MODEL = "claude-haiku-4-5-20251001"   # fast and low cost; claude-sonnet-5-5 for higher quality
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"     # stable; newer Flash models (e.g. gemini-3.8-flash) are short-term releases
MAX_MESSAGE_CHARS = 1000
MAX_MESSAGES_PER_CONVERSATION = 40
MAX_MESSAGES_PER_MINUTE = 12                  # per visitor IP, protects the API bill
MAX_TOOL_ROUNDS = 5
HISTORY_MESSAGES = 20
NOTIFY_AGAIN_MINUTES = 10                     # while waiting, remind the team at most this often
HUMAN_NOTICE = ("A person from our team will reply here. You can keep typing; "
                "they'll see everything you write.")
FMT = "%Y-%m-%d %H:%M:%S"

DEFAULT_SETTINGS = {
    "assistant_name": "Parts Assistant",
    "store_name": "SkuTrail Demo Store",
    "greeting": "Hi! I'm the store's AI assistant. Ask me about parts, fitment, stock or an order, "
                "and I can pass you to a person on our team anytime.",
    "tone": "Warm, friendly and practical, like a helpful person at a parts counter. Short, clear "
            "sentences. Use the customer's first name if they give it. Never pushy.",
    "staff_name": "Our team",
}

SAMPLE_KNOWLEDGE = [
    ("Shipping", "Orders placed by 1 p.m. Pacific on a business day usually ship the same day from "
     "California. Delivery in the US usually takes 2 to 5 business days, and a tracking number is "
     "emailed when the order ships."),
    ("Returns", "Unused parts in their original packaging can be returned within 30 days of delivery "
     "for a refund. Installed parts can't be returned unless they're defective. Customers start a "
     "return by emailing their order number."),
    ("Checking fitment", "Each listing names the machines and engines the part fits. Customers should "
     "compare the model number and engine size on their machine (usually on a sticker, or stamped on "
     "the frame or engine) with the listing. If unsure, they can send their make, model, year and "
     "engine size, and the team confirms before they order."),
    ("Contact and hours", "The team answers email at support@example.com, Monday to Friday, 9 a.m. "
     "to 5 p.m. Pacific, usually within one business day."),
]

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the store's catalog by part name, machine or engine model, or SKU. "
                       "Returns up to 6 matches with price, stock and the product page link.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Words to search for, e.g. 'carburetor kodiak 450'"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_product",
        "description": "Full details for one product by its SKU: price, stock, description and link.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
    {
        "name": "check_order",
        "description": "Look up an order's status. Needs the order number AND the email used at "
                       "checkout; details are returned only when both match.",
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": "integer"}, "email": {"type": "string"}},
            "required": ["order_number", "email"],
        },
    },
    {
        "name": "request_human",
        "description": "Hand the conversation to a person on the team. Use it when the customer asks "
                       "for a person, or when you can't resolve their question. Ask for their email "
                       "first so the team can reply.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "summary": {"type": "string", "description": "One or two sentences on what the customer needs."},
            },
            "required": ["summary"],
        },
    },
]

_recent_by_ip = defaultdict(deque)


class TooMany(Exception):
    """A visitor or conversation hit a limit."""


# ---------------------------------------------------------------------------
# Which AI answers (replaceable in tests)
# ---------------------------------------------------------------------------

def provider():
    """claude, gemini, or None (offline)."""
    chosen = os.environ.get("SUPPORT_PROVIDER", "").strip().lower()
    has_claude = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    has_gemini = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    if chosen == "claude":
        return "claude" if has_claude else None
    if chosen == "gemini":
        return "gemini" if has_gemini else None
    if has_claude:
        return "claude"
    if has_gemini:
        return "gemini"
    return None


def default_client():
    which = provider()
    if which == "claude":
        import anthropic
        return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
    if which == "gemini":
        return GeminiAdapter.from_key(os.environ["GEMINI_API_KEY"].strip(), model())
    return None


CLIENT_FACTORY = default_client


def model():
    default = DEFAULT_GEMINI_MODEL if provider() == "gemini" else DEFAULT_MODEL
    return os.environ.get("SUPPORT_MODEL", "").strip() or default


def ai_ready():
    return provider() is not None or CLIENT_FACTORY is not default_client


class GeminiAdapter:
    """Talks to the Gemini API with the same instructions and tools as Claude.
    Gemini calls the assistant's side "model" (Claude says "assistant"),
    and tool results go back as function-response parts."""

    def __init__(self, client, types, model_name):
        self.client, self.types, self.model = client, types, model_name

    @classmethod
    def from_key(cls, api_key, model_name):
        from google import genai
        from google.genai import types
        return cls(genai.Client(api_key=api_key), types, model_name)

    def respond(self, system, history, message, conversation_id):
        t = self.types
        turns = _merge_turns(history + [{"role": "user", "content": message}])
        contents = [t.Content(role="user" if turn["role"] == "user" else "model",
                              parts=[t.Part.from_text(text=turn["content"])]) for turn in turns]
        config = t.GenerateContentConfig(
            system_instruction=system,
            tools=[t.Tool(function_declarations=[
                {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}
                for tool in TOOLS])],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            calls = response.function_calls or []
            if not calls:
                break
            contents.append(response.candidates[0].content)
            contents.append(t.Content(role="user", parts=[
                self._result(call.name, run_tool(call.name, dict(call.args or {}), conversation_id), getattr(call, "id", None))
                for call in calls]))
        return (response.text or "").strip()

    def _result(self, name, output, call_id):
        try:
            return self.types.Part.from_function_response(name=name, response={"result": output}, id=call_id)
        except TypeError:   # older versions of the library don't take an id
            return self.types.Part.from_function_response(name=name, response={"result": output})


def _merge_turns(turns):
    """Joins back-to-back messages from the same side (a customer may write
    several times while waiting for a person) into one turn."""
    merged = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1] = {"role": turn["role"], "content": merged[-1]["content"] + "\n\n" + turn["content"]}
        else:
            merged.append(dict(turn))
    return merged


# ---------------------------------------------------------------------------
# Settings, knowledge and corrections (what the admin "trains" with)
# ---------------------------------------------------------------------------

def get_settings(conn=None):
    own = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key = 'support_settings'").fetchone()
    finally:
        if own:
            conn.close()
    saved = json.loads(row[0]) if row else {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}


def save_settings(values):
    settings = get_settings()
    for key in DEFAULT_SETTINGS:
        if key in values:
            text = str(values[key] or "").strip()
            if not text:
                raise ValueError(f"{key.replace('_', ' ').capitalize()} can't be empty.")
            settings[key] = text[:1500]
    conn = get_connection()
    try:
        db.upsert_meta(conn, "support_settings", json.dumps(settings))
        conn.commit()
    finally:
        conn.close()
    return settings


def ensure_sample_knowledge(conn):
    """Adds example articles the first time, clearly flagged for editing."""
    if conn.execute("SELECT 1 FROM app_meta WHERE key = 'support_seeded'").fetchone():
        return
    now = _now()
    conn.executemany("INSERT INTO support_knowledge (title, content, is_sample, updated_at) VALUES (?, ?, 1, ?)",
                     [(t, c, now) for t, c in SAMPLE_KNOWLEDGE])
    db.upsert_meta(conn, "support_seeded", now)
    conn.commit()


def list_knowledge(conn=None):
    own = conn is None
    conn = conn or get_connection()
    try:
        ensure_sample_knowledge(conn)
        return [dict(r) for r in conn.execute("SELECT * FROM support_knowledge ORDER BY id")]
    finally:
        if own:
            conn.close()


def save_knowledge(title, content, entry_id=None):
    title, content = (title or "").strip(), (content or "").strip()
    if not title or not content:
        raise ValueError("An article needs a title and some text.")
    if len(content) > 4000:
        raise ValueError("Keep an article under 4,000 characters; split longer topics into several.")
    conn = get_connection()
    try:
        if entry_id:
            changed = conn.execute("UPDATE support_knowledge SET title = ?, content = ?, is_sample = 0, "
                                   "updated_at = ? WHERE id = ?", (title, content, _now(), entry_id)).rowcount
            if not changed:
                raise ValueError("That article doesn't exist any more.")
        else:
            conn.execute("INSERT INTO support_knowledge (title, content, is_sample, updated_at) VALUES (?, ?, 0, ?)",
                         (title, content, _now()))
        conn.commit()
    finally:
        conn.close()
    return list_knowledge()


def delete_knowledge(entry_id):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM support_knowledge WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()
    return list_knowledge()


def corrections(conn, limit=8):
    """The admin's improved answers, paired with the question they answer."""
    rows = conn.execute(
        """SELECT m.id, m.conversation_id, m.correction FROM support_messages m
           WHERE m.correction IS NOT NULL AND m.staff_name IS NULL ORDER BY m.id DESC LIMIT ?""", (limit,)).fetchall()
    pairs = []
    for r in rows:
        question = conn.execute(
            """SELECT content FROM support_messages WHERE conversation_id = ? AND role = 'customer'
               AND id < ? ORDER BY id DESC LIMIT 1""", (r["conversation_id"], r["id"])).fetchone()
        if question:
            pairs.append({"question": question[0], "answer": r["correction"]})
    return pairs


def build_system_prompt(settings, knowledge, examples):
    articles = "\n\n".join(f"## {k['title']}\n{k['content']}" for k in knowledge) or "(No articles yet.)"
    shown = "\n\n".join(f"Customer: {e['question']}\nAnswer the store wants: {e['answer']}" for e in examples)
    return f"""You are {settings['assistant_name']}, the AI customer support assistant for {settings['store_name']}, an online store selling powersports and small-engine parts (also sold on eBay and Amazon).

How to help:
- Be warm and natural, like a helpful person at the parts counter. You are an AI assistant, though: say so if asked, and never claim to be a person.
- Get product, price, stock and order facts from the tools. Never guess them. When you mention a product, include its link path (for example /product?sku=CARB-00123).
- Answer policy questions only from the store articles below. If they don't cover it, say you're not sure and offer to pass the question to a person on the team (request_human).
- Don't promise refunds, discounts, delivery dates or exceptions beyond what the articles say.
- Orders: you need the order number and the email used at checkout before you can look one up. Never share details of anyone else's order.
- Keep answers short: two to four sentences, in the customer's language.
- Stay on topics related to the store and its products.

Tone the store asked for: {settings['tone']}

Store articles:
{articles}
""" + (f"\nExamples of answers the store wants (follow their style and substance):\n\n{shown}\n" if shown else "")


# ---------------------------------------------------------------------------
# Tools: live data the assistant can look up
# ---------------------------------------------------------------------------

def run_tool(name, args, conversation_id):
    try:
        if name == "search_products":
            page = storefront.product_page(str(args.get("query", ""))[:100], 1, 6)
            return {"total_matches": page["total"],
                    "products": [_product_brief(p) for p in page["items"]]}
        if name == "get_product":
            product = storefront.get_product(str(args.get("sku", "")).strip())
            if not product:
                return {"found": False}
            text = re.sub(r"<[^>]+>", " ", product.get("description_html") or "")
            return {"found": True, **_product_brief(product), "description": " ".join(text.split())[:800]}
        if name == "check_order":
            return _check_order(args.get("order_number"), args.get("email"))
        if name == "request_human":
            return _request_human(conversation_id, args.get("email"), args.get("summary"))
    except Exception:
        return {"error": "The lookup failed. Apologize briefly and offer to pass this to a person."}
    return {"error": f"Unknown tool {name}"}


def _product_brief(p):
    qty = p["inventory_qty"]
    return {"sku": p["sku"], "title": p["title"], "price": p["price"],
            "stock": "sold out" if qty <= 0 else f"only {qty} left" if qty <= 3 else f"{qty} in stock",
            "link": f"/product?sku={p['sku']}"}


def _check_order(order_number, email):
    nothing = {"found": False, "note": "No order matches that number and email."}
    try:
        order_id = int(order_number)
    except (TypeError, ValueError):
        return nothing
    conn = get_connection()
    try:
        row = conn.execute("SELECT id, customer_email, status, total, items_json, created_at, paid_at "
                           "FROM orders WHERE id = ?", (order_id,)).fetchone()
    finally:
        conn.close()
    # Same answer for "no such order" and "wrong email", so it can't be used to probe for orders.
    if not row or (row["customer_email"] or "").strip().lower() != str(email or "").strip().lower():
        return nothing
    items = json.loads(row["items_json"] or "[]")
    return {"found": True, "order_number": row["id"], "status": row["status"], "total": row["total"],
            "placed": row["created_at"], "paid": row["paid_at"],
            "items": [{"title": i["title"], "quantity": i["quantity"]} for i in items]}


def _request_human(conversation_id, email, summary):
    email, summary = (email or "").strip()[:200] or None, (summary or "").strip()[:500]
    conn = get_connection()
    try:
        conn.execute("UPDATE support_conversations SET needs_human = 1, resolved_at = NULL, notified_at = ?, "
                     "customer_email = COALESCE(?, customer_email), handoff_summary = ? WHERE id = ?",
                     (_now(), email, summary, conversation_id))
        conn.commit()
    finally:
        conn.close()
    notifications.notify(conversation_id, "A customer asked for a person",
                         [("What they need", summary), ("Email", email or "not given yet")])
    return {"handed_off": True,
            "note": "The team has been notified and will reply by email." if email else
                    "The team has been notified. If the customer hasn't given an email, ask for one."}


# ---------------------------------------------------------------------------
# Chatting
# ---------------------------------------------------------------------------

def check_rate(ip):
    now = time.time()
    recent = _recent_by_ip[ip]
    while recent and now - recent[0] > 60:
        recent.popleft()
    if len(recent) >= MAX_MESSAGES_PER_MINUTE:
        raise TooMany("You're sending messages very quickly. Please wait a moment and try again.")
    recent.append(now)


def chat(conversation_id, message):
    message = (message or "").strip()
    if not message:
        raise ValueError("Type a message first.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Please keep messages under {MAX_MESSAGE_CHARS} characters.")

    conn = get_connection()
    try:
        known = conversation_id and conn.execute(
            "SELECT 1 FROM support_conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not known:
            conversation_id = "conv_" + secrets.token_hex(10)
            conn.execute("INSERT INTO support_conversations (id, started_at, last_message_at) VALUES (?, ?, ?)",
                         (conversation_id, _now(), _now()))
        count = conn.execute("SELECT COUNT(*) FROM support_messages WHERE conversation_id = ? AND role = 'customer'",
                             (conversation_id,)).fetchone()[0]
        if count >= MAX_MESSAGES_PER_CONVERSATION:
            raise TooMany("This conversation has reached its limit. Please start a new one, or email the team.")
        history = [{"role": "user" if r["role"] == "customer" else "assistant",
                    "content": f"[{r['staff_name']}, a person on the team, replied:] {r['content']}"
                               if r["staff_name"] else r["content"]}
                   for r in conn.execute("SELECT role, content, staff_name FROM support_messages "
                                         "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                                         (conversation_id, HISTORY_MESSAGES)).fetchall()][::-1]
        customer_message_id = _save(conn, conversation_id, "customer", message)
        convo = conn.execute("SELECT needs_human, notified_at, customer_email FROM support_conversations "
                             "WHERE id = ?", (conversation_id,)).fetchone()
        waiting = bool(convo["needs_human"])
        remind = waiting and (not convo["notified_at"] or
                              (datetime.now() - datetime.strptime(convo["notified_at"], FMT)).total_seconds()
                              > NOTIFY_AGAIN_MINUTES * 60)
        if remind:
            conn.execute("UPDATE support_conversations SET notified_at = ? WHERE id = ?", (_now(), conversation_id))
        settings, knowledge, examples = get_settings(conn), list_knowledge(conn), corrections(conn)
        conn.commit()
    finally:
        conn.close()

    # Handed to a person: the assistant stays quiet; the person sees the message on the Support page.
    if waiting:
        if remind:
            notifications.notify(conversation_id, "A customer is waiting for a reply",
                                 [("Latest message", message), ("Email", convo["customer_email"])])
        return {"conversation_id": conversation_id, "reply": None, "notice": HUMAN_NOTICE,
                "customer_message_id": customer_message_id, "message_id": None,
                "needs_human": True, "mode": "waiting"}

    client = CLIENT_FACTORY()
    if client is None and wants_person(message):
        # Even without the AI, asking for a person hands the conversation over.
        _request_human(conversation_id, None, f"Asked for a person: {message[:200]}")
        reply, mode = OFFLINE_HANDOFF_REPLY, "offline"
    elif client is None:
        reply, mode = offline_answer(message, knowledge), "offline"
    else:
        system = build_system_prompt(settings, knowledge, examples)
        if hasattr(client, "respond"):       # an adapter, e.g. Gemini
            reply, mode = _ask_adapter(client, system, history, message, conversation_id), "ai"
        else:                                # the Claude client
            reply, mode = _ask_claude(client, conversation_id, history, message, system), "ai"

    conn = get_connection()
    try:
        message_id = _save(conn, conversation_id, "assistant", reply)
        needs_human = conn.execute("SELECT needs_human FROM support_conversations WHERE id = ?",
                                   (conversation_id,)).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"conversation_id": conversation_id, "reply": reply, "message_id": message_id,
            "customer_message_id": customer_message_id, "needs_human": bool(needs_human),
            "notice": HUMAN_NOTICE if needs_human else None, "mode": mode}


TROUBLE_REPLY = ("Sorry, I'm having trouble answering right now. Please try again in a moment, "
                 "or ask me to pass your question to a person on the team.")


def _ask_adapter(client, system, history, message, conversation_id):
    try:
        return client.respond(system, history, message, conversation_id) or \
            "Sorry, I didn't catch that. Could you say it another way?"
    except Exception:
        return TROUBLE_REPLY


def _ask_claude(client, conversation_id, history, message, system):
    messages = history + [{"role": "user", "content": message}]
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.messages.create(model=model(), max_tokens=700, system=system,
                                              tools=TOOLS, messages=messages)
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            results = [{"type": "tool_result", "tool_use_id": block.id,
                        "content": json.dumps(run_tool(block.name, block.input, conversation_id))}
                       for block in response.content if block.type == "tool_use"]
            messages.append({"role": "user", "content": results})
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        return text or "Sorry, I didn't catch that. Could you say it another way?"
    except Exception:
        return TROUBLE_REPLY


OFFLINE_HANDOFF_REPLY = "I've asked a person on our team to help. They'll reply right here in this chat."
WANTS_PERSON = re.compile(r"\b(person|human|agent|representative|someone real|real people)\b|人工|真人", re.IGNORECASE)


def wants_person(message):
    """Offline mode can't understand requests, but it can spot one for a person."""
    return bool(WANTS_PERSON.search(message))


STOPWORDS = {"the", "a", "an", "is", "are", "do", "does", "you", "your", "i", "my", "me", "to", "of", "for",
             "and", "or", "in", "on", "it", "can", "how", "what", "when", "will", "with", "we", "us", "be"}


def offline_answer(message, knowledge):
    """Without the AI service: the best-matching article, or an offer of a person."""
    words = set(re.findall(r"[a-z0-9]+", message.lower())) - STOPWORDS
    best, score = None, 0
    for entry in knowledge:
        text = set(re.findall(r"[a-z0-9]+", f"{entry['title']} {entry['content']}".lower())) - STOPWORDS
        overlap = len(words & text) + 2 * len(words & set(re.findall(r"[a-z0-9]+", entry["title"].lower())))
        if overlap > score:
            best, score = entry, overlap
    if best and score >= 2:
        return best["content"]
    return ("I'm the store's AI assistant, but my AI service isn't connected right now, so I can only answer "
            "from our help articles, and I couldn't find one for that. Would you like a person on our team "
            "to follow up? Just share your email.")


# ---------------------------------------------------------------------------
# Reviewing conversations (admin)
# ---------------------------------------------------------------------------

def list_conversations(limit=40):
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT c.id, c.started_at, c.last_message_at, c.customer_email, c.needs_human, c.handoff_summary,
                      (SELECT COUNT(*) FROM support_messages m WHERE m.conversation_id = c.id) AS message_count,
                      (SELECT content FROM support_messages m WHERE m.conversation_id = c.id AND m.role = 'customer'
                       ORDER BY m.id LIMIT 1) AS first_question,
                      (SELECT CASE WHEN m.staff_name IS NOT NULL THEN 'staff' ELSE m.role END
                       FROM support_messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_by
               FROM support_conversations c ORDER BY c.last_message_at DESC LIMIT ?""", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_conversation(conversation_id):
    conn = get_connection()
    try:
        convo = conn.execute("SELECT * FROM support_conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not convo:
            raise ValueError("That conversation doesn't exist.")
        messages = [dict(r) for r in conn.execute(
            "SELECT * FROM support_messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))]
    finally:
        conn.close()
    return {**dict(convo), "messages": messages}


def messages_after(conversation_id, after_id=0):
    """New messages for the customer's chat window (it asks every few
    seconds while a person is handling the conversation). The random
    conversation id works as the key: only the customer's browser has it."""
    try:
        after_id = int(after_id or 0)
    except (TypeError, ValueError):
        after_id = 0
    conn = get_connection()
    try:
        convo = conn.execute("SELECT needs_human FROM support_conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not convo:
            raise LookupError("That conversation doesn't exist.")
        rows = conn.execute("SELECT id, role, content, staff_name FROM support_messages "
                            "WHERE conversation_id = ? AND id > ? ORDER BY id", (conversation_id, after_id)).fetchall()
    finally:
        conn.close()
    return {"needs_human": bool(convo["needs_human"]),
            "messages": [{"id": r["id"], "role": "staff" if r["staff_name"] else r["role"],
                          "text": r["content"], "staff_name": r["staff_name"]} for r in rows]}


def staff_reply(conversation_id, text):
    """A person on the team replies into the customer's chat. Replying
    takes the conversation over, so the assistant stays quiet."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Write a reply first.")
    if len(text) > 4000:
        raise ValueError("Keep a reply under 4,000 characters.")
    staff_name = get_settings()["staff_name"]
    conn = get_connection()
    try:
        if not conn.execute("SELECT 1 FROM support_conversations WHERE id = ?", (conversation_id,)).fetchone():
            raise ValueError("That conversation doesn't exist.")
        now = _now()
        conn.execute("UPDATE support_conversations SET needs_human = 1, resolved_at = NULL, last_message_at = ? "
                     "WHERE id = ?", (now, conversation_id))
        db.insert_returning_id(conn, "INSERT INTO support_messages (conversation_id, role, content, created_at, "
                                     "staff_name) VALUES (?, 'assistant', ?, ?, ?)",
                               (conversation_id, text, now, staff_name))
        conn.commit()
    finally:
        conn.close()
    return get_conversation(conversation_id)


def resolve(conversation_id):
    """Hands the conversation back to the assistant."""
    conn = get_connection()
    try:
        changed = conn.execute("UPDATE support_conversations SET needs_human = 0, resolved_at = ? WHERE id = ?",
                               (_now(), conversation_id)).rowcount
        if not changed:
            raise ValueError("That conversation doesn't exist.")
        conn.commit()
    finally:
        conn.close()
    return get_conversation(conversation_id)


def review_message(message_id, rating=None, correction=None):
    if rating not in (None, 1, -1):
        raise ValueError("Rating must be 1 (good) or -1 (needs work).")
    correction = (correction or "").strip()[:2000] or None
    conn = get_connection()
    try:
        changed = conn.execute("UPDATE support_messages SET rating = ?, correction = ? WHERE id = ? "
                               "AND role = 'assistant' AND staff_name IS NULL",
                               (rating, correction, message_id)).rowcount
        if not changed:
            raise ValueError("Only the AI assistant's answers can be reviewed.")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _save(conn, conversation_id, role, content):
    now = _now()
    conn.execute("UPDATE support_conversations SET last_message_at = ? WHERE id = ?", (now, conversation_id))
    return db.insert_returning_id(
        conn, "INSERT INTO support_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, now))


def _now():
    return datetime.now().strftime(FMT)

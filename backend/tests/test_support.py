"""
Tests for support_agent.py. No network and no API key: a scripted fake
stands in for the Claude API client, returning the same kinds of content
blocks (text, tool_use) the real Messages API returns.
"""

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))

import checkout
import db
import demo_catalog
import support_agent
from tests.test_new_item import expect_error
from tests.test_store_import import temp_database, admin_client


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id_, name, args):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args)


class FakeClaude:
    """Returns scripted responses in order and records every request."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.calls.append(json.loads(json.dumps(request, default=lambda o: o.__dict__)))
        return self.responses.pop(0)


def response(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


@contextmanager
def claude(fake):
    original = support_agent.CLIENT_FACTORY
    support_agent.CLIENT_FACTORY = lambda: fake
    support_agent._recent_by_ip.clear()
    try:
        yield fake
    finally:
        support_agent.CLIENT_FACTORY = original


@contextmanager
def offline():
    original = support_agent.CLIENT_FACTORY
    support_agent.CLIENT_FACTORY = lambda: None
    support_agent._recent_by_ip.clear()
    try:
        yield
    finally:
        support_agent.CLIENT_FACTORY = original


def test_the_instructions_carry_tone_articles_corrections_and_the_ai_disclosure():
    settings = {**support_agent.DEFAULT_SETTINGS, "tone": "Cheerful and brief."}
    prompt = support_agent.build_system_prompt(
        settings, [{"title": "Returns", "content": "30 days, unused."}],
        [{"question": "Do you ship to Canada?", "answer": "Not yet, sorry! US only for now."}])
    assert "Cheerful and brief." in prompt and "## Returns\n30 days, unused." in prompt
    assert "Not yet, sorry! US only for now." in prompt
    assert "never claim to be a person" in prompt
    assert "Never share details of anyone else's order" in prompt


def test_the_assistant_looks_products_up_and_answers_with_real_data():
    with temp_database():
        demo_catalog.install(300, backup=False)
        fake = FakeClaude(
            response("tool_use", text("Let me check."), tool_use("t1", "search_products", {"query": "carburetor"})),
            response("end_turn", text("We have a few carburetors in stock!")))
        with claude(fake):
            result = support_agent.chat(None, "Do you have carburetors?")
        convo = support_agent.get_conversation(result["conversation_id"])
    assert result["reply"] == "We have a few carburetors in stock!" and result["mode"] == "ai"
    first, second = fake.calls
    assert first["model"] == support_agent.DEFAULT_MODEL and first["tools"] == support_agent.TOOLS
    assert first["messages"] == [{"role": "user", "content": "Do you have carburetors?"}]
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "t1"
    found = json.loads(tool_result["content"])
    assert found["products"] and all("carburetor" in p["title"].lower() for p in found["products"])
    assert found["products"][0]["link"].startswith("/product?sku=")
    assert [m["role"] for m in convo["messages"]] == ["customer", "assistant"]


def test_the_conversation_continues_with_its_history():
    with temp_database():
        fake = FakeClaude(response("end_turn", text("Hi Serena!")), response("end_turn", text("Sure.")))
        with claude(fake):
            first = support_agent.chat(None, "Hi, I'm Serena")
            support_agent.chat(first["conversation_id"], "Can you help me?")
    assert fake.calls[1]["messages"] == [
        {"role": "user", "content": "Hi, I'm Serena"},
        {"role": "assistant", "content": "Hi Serena!"},
        {"role": "user", "content": "Can you help me?"},
    ]


def test_order_lookups_need_the_matching_email_and_reveal_nothing_otherwise():
    with temp_database():
        demo_catalog.install(50, backup=False)
        sku = db.get_connection().execute("SELECT sku FROM products WHERE inventory_qty > 2 LIMIT 1").fetchone()[0]
        session = checkout.create_session([{"sku": sku, "quantity": 1}], "Buyer@Example.com")
        checkout.confirm_payment(session["session_id"], "4242424242424242", "12/30", "123")
        order_id = db.get_connection().execute("SELECT id FROM orders").fetchone()[0]
        wrong = support_agent.run_tool("check_order", {"order_number": order_id, "email": "someone@else.com"}, "c")
        missing = support_agent.run_tool("check_order", {"order_number": 999, "email": "buyer@example.com"}, "c")
        right = support_agent.run_tool("check_order", {"order_number": order_id, "email": "buyer@example.com"}, "c")
    assert wrong == missing == {"found": False, "note": "No order matches that number and email."}
    assert right["found"] and right["status"] == "paid" and right["items"][0]["quantity"] == 1
    assert "card" not in json.dumps(right).lower()


def test_asking_for_a_person_flags_the_conversation_for_the_team():
    with temp_database():
        fake = FakeClaude(
            response("tool_use", tool_use("t1", "request_human", {"email": "a@b.com", "summary": "Wants a bulk quote."})),
            response("end_turn", text("I've passed this to the team; they'll email you.")))
        with claude(fake):
            result = support_agent.chat(None, "Can a person give me a bulk quote? a@b.com")
        convo = support_agent.get_conversation(result["conversation_id"])
    assert result["needs_human"] is True
    assert convo["customer_email"] == "a@b.com" and convo["handoff_summary"] == "Wants a bulk quote."


def test_without_an_api_key_it_answers_from_articles_or_offers_a_person():
    with temp_database(), offline():
        returns = support_agent.chat(None, "What is your return policy for unused parts?")
        unknown = support_agent.chat(None, "Can you recommend a good movie?")
    assert returns["mode"] == "offline" and "30 days" in returns["reply"]
    assert "person on our team" in unknown["reply"]


def test_a_failing_api_gives_a_friendly_answer_instead_of_an_error():
    class Broken:
        messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("network down")))
    with temp_database(), claude(Broken()):
        result = support_agent.chat(None, "Hello?")
    assert "having trouble" in result["reply"]


def test_corrections_become_examples_in_the_next_conversation():
    with temp_database():
        fake = FakeClaude(response("end_turn", text("I think so?")), response("end_turn", text("Not yet.")))
        with claude(fake):
            first = support_agent.chat(None, "Do you ship to Canada?")
            support_agent.review_message(first["message_id"], rating=-1,
                                         correction="Not yet, sorry! We ship within the US for now.")
            support_agent.chat(None, "Shipping to Toronto?")
    assert "Not yet, sorry! We ship within the US for now." in fake.calls[1]["system"]
    assert "Customer: Do you ship to Canada?" in fake.calls[1]["system"]


def test_limits_protect_the_bill():
    with temp_database(), offline():
        expect_error(support_agent.chat, None, "x" * 1001, contains="under 1000")
        expect_error(support_agent.chat, None, "   ", contains="Type a message")
        for _ in range(support_agent.MAX_MESSAGES_PER_MINUTE):
            support_agent.check_rate("1.2.3.4")
        expect_error(support_agent.check_rate, "1.2.3.4", kind=support_agent.TooMany, contains="wait a moment")


def test_the_admin_can_edit_articles_and_settings_and_samples_are_flagged():
    with temp_database():
        articles = support_agent.list_knowledge()
        assert articles and all(a["is_sample"] == 1 for a in articles)
        edited = support_agent.save_knowledge("Shipping", "We ship within 2 business days.", articles[0]["id"])
        assert edited[0]["is_sample"] == 0 and edited[0]["content"] == "We ship within 2 business days."
        support_agent.delete_knowledge(articles[1]["id"])
        assert len(support_agent.list_knowledge()) == len(articles) - 1
        settings = support_agent.save_settings({"assistant_name": "Eagle Helper"})
        assert settings["assistant_name"] == "Eagle Helper" and settings["tone"] == support_agent.DEFAULT_SETTINGS["tone"]
        expect_error(support_agent.save_settings, {"greeting": " "}, contains="can't be empty")


def test_chat_is_public_but_training_needs_sign_in():
    import app as app_module
    with temp_database(), offline():
        public = app_module.app.test_client()
        assert public.get("/api/support/config").status_code == 200
        chat = public.post("/api/support/chat", json={"message": "How long does shipping take?"})
        assert chat.status_code == 200 and "business day" in chat.get_json()["reply"]
        for path in ("/api/support/admin", f"/api/support/conversations/{chat.get_json()['conversation_id']}"):
            assert public.get(path).status_code == 401
        assert public.post("/api/support/knowledge", json={"title": "x", "content": "y"}).status_code == 401
        admin = admin_client()
        data = admin.get("/api/support/admin").get_json()
        assert data["conversations"][0]["first_question"] == "How long does shipping take?"
        review = admin.post(f"/api/support/messages/{chat.get_json()['message_id']}/review",
                            json={"rating": 1, "correction": None})
        assert review.status_code == 200


# ---- Handing over to a person ----

@contextmanager
def slack_recorder():
    import notifications
    from tests.test_marketplace import env
    sent = []
    original, background = notifications.send_slack, notifications.BACKGROUND
    notifications.send_slack, notifications.BACKGROUND = sent.append, False
    try:
        with env(SLACK_WEBHOOK_URL="https://hooks.slack.test/T/B/X", NOTIFY_EMAIL_TO=None, SITE_URL="https://shop.test"):
            yield sent
    finally:
        notifications.send_slack, notifications.BACKGROUND = original, background


def test_a_person_takes_over_the_ai_waits_and_resumes_after_resolving():
    with temp_database(), slack_recorder() as slack:
        support_agent.save_settings({"staff_name": "Serena"})
        fake = FakeClaude(
            response("tool_use", tool_use("t1", "request_human", {"email": "kim@example.com", "summary": "Bulk order."})),
            response("end_turn", text("I've asked the team to jump in.")),
            response("end_turn", text("Glad Serena could help! Anything else?")))
        with claude(fake):
            handoff = support_agent.chat(None, "I need 40 carburetors, can a person help? kim@example.com")
            convo_id = handoff["conversation_id"]
            waiting = support_agent.chat(convo_id, "Are you there?")
            assert len(fake.calls) == 2                          # the assistant stayed quiet
            support_agent.staff_reply(convo_id, "Hi Kim, this is Serena. I can do 40 at $19 each.")
            new = support_agent.messages_after(convo_id, waiting["customer_message_id"])
            support_agent.resolve(convo_id)
            after = support_agent.chat(convo_id, "Thanks, that works!")
    assert handoff["needs_human"] and handoff["notice"] == support_agent.HUMAN_NOTICE
    assert waiting["mode"] == "waiting" and waiting["reply"] is None
    assert new["messages"] == [{"id": new["messages"][0]["id"], "role": "staff", "staff_name": "Serena",
                                "text": "Hi Kim, this is Serena. I can do 40 at $19 each."}]
    assert after["reply"] == "Glad Serena could help! Anything else?" and not after["needs_human"]
    history = fake.calls[2]["messages"]
    assert {"role": "assistant", "content": "[Serena, a person on the team, replied:] Hi Kim, this is Serena. "
            "I can do 40 at $19 each."} in history
    assert len(slack) == 1                                      # one alert; the follow-up came within 10 minutes
    assert "Bulk order." in slack[0] and "kim@example.com" in slack[0]
    assert f"https://shop.test/admin/support?c={convo_id}" in slack[0]


def test_a_waiting_customer_triggers_another_reminder_after_ten_minutes():
    with temp_database(), slack_recorder() as slack, offline():
        convo_id = support_agent.chat(None, "Hello")["conversation_id"]
        support_agent.run_tool("request_human", {"summary": "Wants a refund."}, convo_id)
        conn = db.get_connection()
        conn.execute("UPDATE support_conversations SET notified_at = '2026-01-01 09:00:00' WHERE id = ?", (convo_id,))
        conn.commit()
        conn.close()
        support_agent.chat(convo_id, "Still waiting on my refund")
    assert len(slack) == 2 and "Still waiting on my refund" in slack[1]


def test_email_notifications_go_through_smtp():
    import notifications
    from tests.test_marketplace import env
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def starttls(self):
            sent["tls"] = True
        def login(self, user, password):
            sent["login"] = user
        def send_message(self, message):
            sent["message"] = message

    original_smtp, background = notifications.smtplib.SMTP, notifications.BACKGROUND
    notifications.smtplib.SMTP, notifications.BACKGROUND = FakeSMTP, False
    try:
        with env(SLACK_WEBHOOK_URL=None, NOTIFY_EMAIL_TO="team@shop.test", SMTP_HOST="smtp.test",
                 SMTP_PORT="587", SMTP_USER="bot@shop.test", SMTP_PASSWORD="app-password", NOTIFY_EMAIL_FROM=None):
            used = notifications.notify("conv_x", "A customer asked for a person", [("Email", "a@b.com")])
    finally:
        notifications.smtplib.SMTP, notifications.BACKGROUND = original_smtp, background
    message = sent["message"]
    assert used == ["email"] and sent["server"] == ("smtp.test", 587) and sent["tls"] and sent["login"] == "bot@shop.test"
    assert message["To"] == "team@shop.test" and message["From"] == "bot@shop.test"
    assert message["Subject"] == "Shop chat: A customer asked for a person" and "Email: a@b.com" in message.get_content()


def test_only_the_ai_answers_can_be_reviewed():
    with temp_database(), offline():
        convo_id = support_agent.chat(None, "Hello")["conversation_id"]
        staff = support_agent.staff_reply(convo_id, "Hi, a person here.")["messages"][-1]
        expect_error(support_agent.review_message, staff["id"], -1, "x", contains="AI assistant's answers")


def test_the_chat_window_can_poll_but_replying_and_resolving_need_sign_in():
    import app as app_module
    with temp_database(), offline():
        public = app_module.app.test_client()
        convo_id = public.post("/api/support/chat", json={"message": "Hi"}).get_json()["conversation_id"]
        polled = public.get(f"/api/support/chat/{convo_id}/messages?after=0")
        assert polled.status_code == 200 and len(polled.get_json()["messages"]) == 2
        assert public.get("/api/support/chat/conv_unknown/messages").status_code == 404
        assert public.post(f"/api/support/conversations/{convo_id}/reply", json={"text": "x"}).status_code == 401
        assert public.post(f"/api/support/conversations/{convo_id}/resolve").status_code == 401
        admin = admin_client()
        assert admin.post(f"/api/support/conversations/{convo_id}/reply", json={"text": "Hello from a person"}).status_code == 200
        assert admin.get("/api/support/admin").get_json()["conversations"][0]["last_by"] == "staff"


def test_older_databases_get_the_new_columns():
    # SQLite-specific: on PostgreSQL, every test already runs the
    # ADD COLUMN IF NOT EXISTS path in db.ensure_schema().
    import sqlite3
    with temp_database():
        if db.is_postgres():
            return
        db.forget_schema()
        path = db.DB_PATH
        path.unlink()
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                       role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, rating INTEGER, correction TEXT)""")
        old.commit()
        old.close()
        db.ensure_schema()
        columns = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(support_messages)")]
        assert "staff_name" in columns


def test_asking_for_a_person_works_without_an_api_key_too():
    with temp_database(), slack_recorder() as slack, offline():
        result = support_agent.chat(None, "I'd like to talk to a person, please.")
        waiting = support_agent.chat(result["conversation_id"], "Hello?")
        chinese = support_agent.chat(None, "我想找人工客服")
    assert result["needs_human"] and result["reply"] == support_agent.OFFLINE_HANDOFF_REPLY
    assert waiting["mode"] == "waiting"
    assert chinese["needs_human"]
    assert len(slack) == 2 and "Asked for a person" in slack[0]


# ---- Gemini ----

class FakeGeminiTypes:
    """Stands in for google.genai.types: records what the adapter builds."""
    class Content:
        def __init__(self, role, parts):
            self.role, self.parts = role, parts

    class Part:
        @staticmethod
        def from_text(text):
            return {"text": text}

        @staticmethod
        def from_function_response(name, response, id=None):
            return {"function_response": {"name": name, "response": response, "id": id}}

    class GenerateContentConfig:
        def __init__(self, **settings):
            self.__dict__.update(settings)

    class Tool:
        def __init__(self, function_declarations):
            self.function_declarations = function_declarations

    class AutomaticFunctionCallingConfig:
        def __init__(self, disable):
            self.disable = disable


class FakeGemini:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        return self.responses.pop(0)


def gemini_reply(text=None, calls=()):
    function_calls = [SimpleNamespace(id=i, name=n, args=a) for i, n, a in calls]
    return SimpleNamespace(text=text, function_calls=function_calls or None,
                           candidates=[SimpleNamespace(content=FakeGeminiTypes.Content(
                               "model", [{"function_call": n} for _, n, _ in calls]))])


def test_gemini_answers_with_the_same_articles_tools_and_live_data():
    with temp_database():
        demo_catalog.install(300, backup=False)
        fake = FakeGemini(gemini_reply(calls=[("fc1", "search_products", {"query": "carburetor"})]),
                          gemini_reply(text="Yes, we have carburetors in stock!"))
        adapter = support_agent.GeminiAdapter(fake, FakeGeminiTypes, "gemini-2.5-flash")
        with claude(adapter):
            result = support_agent.chat(None, "Any carburetors?")
    first, second = fake.calls
    assert result["reply"] == "Yes, we have carburetors in stock!" and result["mode"] == "ai"
    assert first["model"] == "gemini-2.5-flash"
    assert [c.role for c in first["contents"]] == ["user"] and first["contents"][0].parts == [{"text": "Any carburetors?"}]
    config = first["config"]
    assert "## Returns" in config.system_instruction and config.automatic_function_calling.disable is True
    declared = config.tools[0].function_declarations
    assert [d["name"] for d in declared] == [t["name"] for t in support_agent.TOOLS]
    assert declared[0]["parameters"] == support_agent.TOOLS[0]["input_schema"]
    sent_back = second["contents"][-1]
    assert sent_back.role == "user"
    response = sent_back.parts[0]["function_response"]
    assert response["name"] == "search_products" and response["id"] == "fc1"
    assert all("carburetor" in p["title"].lower() for p in response["response"]["result"]["products"])


def test_gemini_sees_the_model_side_as_model_and_merged_customer_messages():
    with temp_database(), slack_recorder():
        fake = FakeGemini(gemini_reply(text="Hi! How can I help?"), gemini_reply(text="Got it, thanks."))
        adapter = support_agent.GeminiAdapter(fake, FakeGeminiTypes, "gemini-2.5-flash")
        with claude(adapter):
            convo_id = support_agent.chat(None, "Hello")["conversation_id"]
            support_agent.run_tool("request_human", {"summary": "x"}, convo_id)
            support_agent.chat(convo_id, "First while waiting")      # AI paused: saved, not sent
            support_agent.resolve(convo_id)
            support_agent.chat(convo_id, "Second message")
    contents = fake.calls[1]["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[2].parts == [{"text": "First while waiting\n\nSecond message"}]


def test_which_ai_answers_depends_on_the_keys():
    from tests.test_marketplace import env
    cases = [
        ({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": None, "SUPPORT_PROVIDER": None}, "claude", support_agent.DEFAULT_MODEL),
        ({"ANTHROPIC_API_KEY": None, "GEMINI_API_KEY": "g", "SUPPORT_PROVIDER": None}, "gemini", "gemini-2.5-flash"),
        ({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g", "SUPPORT_PROVIDER": None}, "claude", support_agent.DEFAULT_MODEL),
        ({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g", "SUPPORT_PROVIDER": "gemini"}, "gemini", "gemini-2.5-flash"),
        ({"ANTHROPIC_API_KEY": None, "GEMINI_API_KEY": None, "SUPPORT_PROVIDER": None}, None, support_agent.DEFAULT_MODEL),
    ]
    for settings, expected, model_name in cases:
        with env(SUPPORT_MODEL=None, **settings):
            assert support_agent.provider() == expected, settings
            assert support_agent.model() == model_name, settings
    with env(GEMINI_API_KEY="g", ANTHROPIC_API_KEY=None, SUPPORT_PROVIDER=None, SUPPORT_MODEL="gemini-3.8-flash"):
        assert support_agent.model() == "gemini-3.8-flash"


def test_a_failing_gemini_call_gives_the_friendly_answer():
    class Broken:
        def respond(self, *args):
            raise RuntimeError("quota exceeded")
    with temp_database(), claude(Broken()):
        assert support_agent.chat(None, "Hi")["reply"] == support_agent.TROUBLE_REPLY

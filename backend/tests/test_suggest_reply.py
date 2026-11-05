"""Suggested replies: drafted from the conversation and the articles, and they can't hand off again."""
import support_agent


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def respond(self, system, history, message, conversation_id):
        self.calls.append({"system": system, "history": history, "message": message,
                           "handoff": support_agent.run_tool("request_human", {"summary": "x"}, conversation_id)})
        return "We're sorry it arrived cracked. Please send photos of the part and the box within 7 days."


def _setup(monkeypatch, messages):
    fake = FakeAdapter()
    monkeypatch.setattr(support_agent, "CLIENT_FACTORY", lambda: fake)
    monkeypatch.setattr(support_agent, "get_conversation", lambda cid: {"id": cid, "messages": messages})
    monkeypatch.setattr(support_agent, "_draft_context", lambda: (
        support_agent.DEFAULT_SETTINGS, [{"title": "Damaged items", "content": "Send photos within 7 days."}], []))
    return fake


def test_suggestion_is_drafted_from_the_conversation_without_handing_off(monkeypatch):
    fake = _setup(monkeypatch, [{"role": "customer", "content": "My carburetor arrived cracked, I want a refund",
                                 "staff_name": None}])
    draft = support_agent.suggest_reply("conv_test")
    assert "photos" in draft
    call = fake.calls[0]
    assert "drafting a reply for a person on the team" in call["system"]
    assert "Send photos within 7 days." in call["system"]
    assert "carburetor arrived cracked" in call["message"]
    assert "error" in call["handoff"]                     # the handoff tool is off while drafting
    assert "conv_test" not in support_agent._drafting      # and back on afterwards


def test_no_suggestion_when_the_team_spoke_last(monkeypatch):
    _setup(monkeypatch, [{"role": "customer", "content": "Where is my order?", "staff_name": None},
                         {"role": "assistant", "content": "We're checking now.", "staff_name": "Our team"}])
    try:
        support_agent.suggest_reply("conv_test")
        assert False, "expected a ValueError"
    except ValueError as e:
        assert "anything new" in str(e)

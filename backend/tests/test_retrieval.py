"""Suggested replies use listing search by meaning, and still work when it finds nothing."""
import support_agent


class FakeAdapter:
    def __init__(self):
        self.system = None

    def respond(self, system, history, message, conversation_id):
        self.system = system
        return "That carburetor fits the 2008 Grizzly 450. Please match the OEM part number before you buy."


def _setup(monkeypatch, found):
    fake = FakeAdapter()
    monkeypatch.setattr(support_agent, "CLIENT_FACTORY", lambda: fake)
    monkeypatch.setattr(support_agent, "get_conversation", lambda cid: {"id": cid, "messages": [
        {"role": "customer", "content": "Will this carb fit my 2008 Yamaha Grizzly 450?", "staff_name": None}]})
    monkeypatch.setattr(support_agent, "_draft_context", lambda: (support_agent.DEFAULT_SETTINGS, [], []))
    monkeypatch.setattr(support_agent.retrieval, "search", lambda query, k=3, threshold=None: found)
    monkeypatch.setattr(support_agent.retrieval, "complaints", lambda sku, title: {})
    monkeypatch.setattr(support_agent, "_answer_pairs", lambda staff=True, limit=300: [])
    return fake


def test_the_draft_uses_the_listings_found_and_returns_them(monkeypatch):
    listing = {"title": "CARBURETOR FOR YAMAHA GRIZZLY 450 2007-2012", "sku": "EBAY-v1-1-0",
               "link": "/product?sku=EBAY-v1-1-0", "text": "Fits Grizzly 450 2007 to 2012", "score": 0.81}
    fake = _setup(monkeypatch, [listing])
    result = support_agent.suggest_reply_with_sources("conv_test")
    assert "Grizzly 450" in fake.system and "/product?sku=EBAY-v1-1-0" in fake.system
    source = result["sources"][0]
    assert (source["title"], source["link"], source["score"]) == (listing["title"], listing["link"], 0.81)
    assert "fits" in result["suggestion"]


def test_the_draft_still_works_when_search_finds_nothing(monkeypatch):
    fake = _setup(monkeypatch, [])
    result = support_agent.suggest_reply_with_sources("conv_test")
    assert result["sources"] == [] and "Listings that may be relevant" not in fake.system
    assert support_agent.suggest_reply("conv_test") == result["suggestion"]

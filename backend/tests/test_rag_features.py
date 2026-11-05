"""Retrieval features: vehicle-aware ranking, the similarity threshold, hybrid search, scrubbing,
earlier answers, and listing context. No network: embeddings are replaced by small fixed vectors."""
import numpy as np

import retrieval
import support_agent


def _index(monkeypatch, rows, vecs, query_vec):
    monkeypatch.setattr(retrieval, "_index", {"rows": rows, "vecs": retrieval._unit(vecs)})
    monkeypatch.setattr(retrieval, "_embed_query", lambda text: retrieval._unit(query_vec))


def _row(title, sku):
    makes, years = retrieval.parse_vehicle(title)
    return {"title": title, "sku": sku, "text": title, "makes": makes, "years": years}


def test_parse_vehicle_reads_makes_and_year_ranges():
    makes, years = retrieval.parse_vehicle("CARBURETOR YAMAHA GRIZZLY 450 2007-2012 ATV")
    assert makes == {"yamaha"} and years == set(range(2007, 2013))
    assert retrieval.parse_vehicle("fits my 2015 Honda CRF250")[1] == {2015}


def test_the_year_asked_moves_the_right_listing_up(monkeypatch):
    rows = [_row("CARB YAMAHA GRIZZLY 450 2002-2006", "EBAY-a"), _row("CARB YAMAHA GRIZZLY 450 2007-2012", "EBAY-b")]
    _index(monkeypatch, rows, [[1, 0.02], [1, 0]], [1, 0.01])   # nearly the same meaning
    found = retrieval.search("carb for my 2008 Yamaha Grizzly 450", k=2, threshold=0)
    assert found[0]["sku"] == "EBAY-b" and found[0]["fit"] is True and found[1]["fit"] is False


def test_listings_below_the_threshold_are_left_out(monkeypatch):
    _index(monkeypatch, [_row("IGNITION COIL", "EBAY-c")], [[0, 1]], [1, 0.2])
    assert retrieval.search("how fast do you ship?", threshold=0.55) == []


def test_hybrid_search_merges_keyword_and_meaning_matches(monkeypatch):
    class Store:
        def product_page(self, query, page, size):
            return {"total": 1, "items": [{"sku": "KW-1", "title": "keyword match"}]}
        def get_product(self, sku):
            return {"sku": sku, "title": "meaning match"}
    monkeypatch.setattr(retrieval, "search", lambda q, k=10, threshold=None: [
        {"sku": "EBAY-m", "title": "meaning match", "fit": True}])
    result = retrieval.hybrid_products("carb for my quad", Store(), lambda p: {"sku": p["sku"]})
    assert {p["sku"] for p in result["products"]} == {"KW-1", "EBAY-m"}
    assert [p for p in result["products"] if p["sku"] == "EBAY-m"][0]["fits_the_year_asked"] is True


def test_hybrid_search_is_keyword_only_without_vectors(monkeypatch):
    class Store:
        def product_page(self, query, page, size):
            return {"total": 7, "items": [{"sku": f"KW-{i}"} for i in range(7)]}
    monkeypatch.setattr(retrieval, "search", lambda q, k=10, threshold=None: [])
    result = retrieval.hybrid_products("carburetor", Store(), lambda p: p)
    assert result["total_matches"] == 7 and len(result["products"]) == 6


def test_scrub_removes_personal_details():
    text = retrieval.scrub("Email me at jo@example.com or call 510-555-0199 about order #123456")
    assert "jo@example.com" not in text and "555" not in text and "123456" not in text


def test_similar_answers_finds_the_closest_and_respects_the_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval, "CACHE_DIR", tmp_path)
    vectors = {"My carb arrived cracked": [1, 0], "Do you ship to Canada?": [0, 1]}
    monkeypatch.setattr(retrieval, "EMBED", lambda texts, task: [vectors[t] for t in texts])
    monkeypatch.setattr(retrieval, "_embed_query", lambda text: retrieval._unit([1, 0.1]))
    pairs = [{"question": "My carb arrived cracked", "answer": "Please send photos within 7 days."},
             {"question": "Do you ship to Canada?", "answer": "Not at the moment."}]
    found = retrieval.similar_answers("the carburetor came broken", pairs, k=3, threshold=0.7)
    assert [f["answer"] for f in found] == ["Please send photos within 7 days."]


def test_without_a_key_there_are_no_earlier_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(retrieval, "EMBED", lambda texts, task: None)
    assert retrieval.similar_answers("anything", [{"question": "q", "answer": "a"}]) == []


def test_listing_context_names_the_part_the_customer_is_looking_at(monkeypatch):
    monkeypatch.setattr(support_agent.storefront, "get_product", lambda sku: {
        "sku": sku, "title": "STARTER RELAY YAMAHA TTR225 1999-2004", "price": 12.5,
        "description_html": "<p>Fits TTR225</p>"})
    context = support_agent._listing_context("EBAY-v1-1-0")
    assert "STARTER RELAY YAMAHA TTR225" in context and "Fits TTR225" in context and "<p>" not in context
    assert support_agent._listing_context(None) == ""

"""
Tests for marketplace_import.py and the eBay/Amazon API clients. No
network: API calls are replaced with the demo sample responses, which
have the same shape as the real eBay Browse and Amazon Catalog Items APIs.
"""

import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import marketplace_import
from marketplace_import import parse_marketplace_url, read_marketplace, ebay_facts, amazon_facts
from mock_apis import marketplace_samples as samples
from integrations import ebay_browse, amazon_catalog
from tests.test_new_item import expect_error

EBAY_KEYS = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_SELLER_USERNAME")
AMAZON_KEYS = ("SP_API_LWA_APP_ID", "SP_API_LWA_CLIENT_SECRET", "SP_API_REFRESH_TOKEN")


class env:
    """Temporarily sets (or clears, with None) environment variables."""
    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def no_keys():
    return env(**{k: None for k in EBAY_KEYS + AMAZON_KEYS})


# ---- Links ----

def test_reads_item_numbers_and_asins_from_listing_links():
    assert parse_marketplace_url("https://www.ebay.com/itm/123456789012") == ("ebay", "123456789012")
    assert parse_marketplace_url("https://www.ebay.com/itm/predator-clutch/123456789012?hash=x") == ("ebay", "123456789012")
    assert parse_marketplace_url("https://www.amazon.com/Throttle-Cable/dp/B0ABCDE123/ref=sr_1_1") == ("amazon", "B0ABCDE123")
    assert parse_marketplace_url("https://www.amazon.com/gp/product/b0abcde123") == ("amazon", "B0ABCDE123")
    assert parse_marketplace_url("https://some-shop.example/products/clutch") is None


def test_marketplace_links_that_are_not_single_listings_explain_what_to_copy():
    expect_error(parse_marketplace_url, "https://www.ebay.com/str/someshop", contains="/itm/")
    expect_error(parse_marketplace_url, "https://www.amazon.com/s?k=clutch", contains="/dp/")
    expect_error(parse_marketplace_url, "https://a.co/d/abc123", contains="short links")


# ---- Without API keys ----

def test_sample_links_work_without_keys_and_are_labeled_demo():
    with no_keys():
        ebay = read_marketplace(samples.SAMPLE_URLS["ebay"])
        amazon = read_marketplace(samples.SAMPLE_URLS["amazon"])
    assert ebay["source"] == {"platform": "eBay", "demo": True, "own_listing": False,
                              "seller": "demo-parts-seller"}
    assert amazon["source"]["demo"] is True


def test_real_links_without_keys_ask_for_keys_or_manual_entry():
    with no_keys():
        error = expect_error(read_marketplace, "https://www.ebay.com/itm/123456789012",
                             kind=marketplace_import.NotConnected, contains="EBAY_CLIENT_ID")
        assert error.platform == "eBay"
        expect_error(read_marketplace, "https://www.amazon.com/dp/B0ABCDE123",
                     kind=marketplace_import.NotConnected, contains="SP_API_")


# ---- Facts from eBay ----

def test_ebay_listing_gives_fitment_and_specs():
    facts = ebay_facts(samples.EBAY_ITEM, samples.SAMPLE_URLS["ebay"])
    assert facts["part_name"] == "Centrifugal Clutch 3/4 Bore 10T"
    assert facts["fits"]["brands"] == ["Predator"]
    assert facts["fits"]["engine_sizes"] == ["212cc"]
    assert facts["fits"]["equipment"] == ["Go Kart", "Mini Bike"]
    assert facts["fits"]["model_numbers"] == ["69730"]
    specs = {s["name"]: s["value"] for s in facts["specs"]}
    assert specs == {"Bore Diameter": "3/4 in", "Number of Teeth": "10", "Chain Size": "#35", "Material": "Steel"}
    assert facts["reference"]["price"] == 24.99


def test_ebay_wording_photos_and_identifiers_are_never_taken():
    result = read_marketplace_with(samples.EBAY_ITEM)
    everything = json.dumps(result["facts"]) + json.dumps(result["draft"])
    for theirs in ("only one you'll ever need", "Buy with confidence", "ebayimg", "DP-CL-3410",
                   "000000000017", "DemoParts", "30 days"):
        assert theirs not in everything, theirs
    assert result["own_listing"] is None


def test_own_ebay_listing_can_be_imported_as_is():
    with env(EBAY_SELLER_USERNAME="Demo-Parts-Seller"):
        result = read_marketplace_with(samples.EBAY_ITEM)
    assert result["source"]["own_listing"] is True
    assert result["own_listing"]["images"][0].endswith("clutch-front.jpg")
    assert result["own_listing"]["price"] == 24.99


def read_marketplace_with(item):
    return marketplace_import.ebay_result(item, samples.SAMPLE_URLS["ebay"], demo=True)


# ---- Facts from Amazon ----

def test_amazon_catalog_item_gives_fitment_and_specs_but_not_marketing_bullets():
    facts = amazon_facts(samples.AMAZON_ITEM)
    assert facts["part_name"] == "Throttle Cable"
    assert facts["fits"]["brands"] == ["Predator"]
    assert facts["fits"]["engine_sizes"] == ["212cc", "79cc"]
    assert facts["fits"]["equipment"] == ["Go Kart", "Mini Bike"]
    assert {s["name"]: s["value"] for s in facts["specs"]} == {
        "Material": "Steel", "Color": "Black", "Length": "48 inches"}
    everything = json.dumps(facts)
    assert "riders love" not in everything and "DP-TC-48" not in everything
    assert facts["reference"] == {"site": "amazon.com", "url": "https://www.amazon.com/dp/B0DEMO0001",
                                  "price": None}


# ---- API clients (HTTP stubbed) ----

class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300

    def json(self):
        return self._body


def test_ebay_client_gets_a_token_then_reads_by_item_number():
    import requests
    calls = []
    original_post, original_get = requests.post, requests.get
    requests.post = lambda url, **kw: calls.append(("token", url, kw["data"])) or FakeResponse(
        200, {"access_token": "t0ken", "expires_in": 7200})
    requests.get = lambda url, **kw: calls.append(("item", url, kw["params"], kw["headers"])) or FakeResponse(
        200, samples.EBAY_ITEM)
    try:
        client = ebay_browse.EbayBrowseClient("id", "secret")
        item = client.get_item_by_legacy_id("100000000001")
        client.get_item_by_legacy_id("100000000001")   # token is reused, not re-requested
    finally:
        requests.post, requests.get = original_post, original_get
    assert item["legacyItemId"] == "100000000001"
    assert [c[0] for c in calls] == ["token", "item", "item"]
    assert calls[0][2]["grant_type"] == "client_credentials"
    assert calls[1][2] == {"legacy_item_id": "100000000001"}
    assert calls[1][3]["Authorization"] == "Bearer t0ken"


def test_ebay_client_explains_refusals():
    import requests
    original_post, original_get = requests.post, requests.get
    requests.post = lambda url, **kw: FakeResponse(200, {"access_token": "t", "expires_in": 7200})
    requests.get = lambda url, **kw: FakeResponse(403, {})
    try:
        expect_error(ebay_browse.EbayBrowseClient("id", "s").get_item_by_legacy_id, "1",
                     kind=ebay_browse.EbayApiError, contains="Partner Network")
    finally:
        requests.post, requests.get = original_post, original_get


def test_amazon_client_exchanges_the_refresh_token_then_reads_the_catalog():
    import requests
    calls = []
    original_post, original_get = requests.post, requests.get
    requests.post = lambda url, **kw: calls.append(kw["data"]) or FakeResponse(
        200, {"access_token": "Atza|x", "expires_in": 3600})
    requests.get = lambda url, **kw: calls.append((url, kw["params"], kw["headers"])) or FakeResponse(
        200, samples.AMAZON_ITEM)
    try:
        item = amazon_catalog.AmazonCatalogClient("id", "secret", "Atzr|refresh").get_item("B0DEMO0001")
    finally:
        requests.post, requests.get = original_post, original_get
    assert item["asin"] == "B0DEMO0001"
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[1][0].endswith("/catalog/2022-04-01/items/B0DEMO0001")
    assert calls[1][1]["includedData"] == "summaries,attributes"
    assert calls[1][2]["x-amz-access-token"] == "Atza|x"

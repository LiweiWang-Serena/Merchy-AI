"""
Tests for new_item.py and content_generator.draft_from_facts. No
network: reference_product() has the same shape as a Shopify store's
public /products/<handle>.json, with made-up test values.
"""

import json
import os
import sys
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import inventory
import new_item
from content_generator import draft_from_facts
from new_item import extract_facts, parse_reference_url, clean_facts, save_new_item
from tests.test_store_import import temp_database, admin_client

REF_LINK = "https://other-parts-store.example/products/predator-52cc-auger-fuel-tank"
MARKETING = "The best tank you'll ever buy, guaranteed to make your auger roar!"


def reference_product(**overrides):
    product = {
        "id": 8100000000001,
        "title": "Fuel Gas Tank For Harbor Freight Predator 52cc Earth Auger 56257 57341",
        "handle": "predator-52cc-auger-fuel-tank",
        "vendor": "Other Seller Co",
        "tags": "Predator, 52cc, Earth Auger",
        "body_html": (f"<p>{MARKETING}</p>"
                      "<p>Fits Harbor Freight Predator 52cc earth auger models 56257 and 57341.</p>"
                      "<ul><li>Material: HDPE plastic</li><li>Capacity: 0.9 L</li>"
                      "<li>Shipping: Free 2-day shipping</li><li>Warranty: 1 year</li></ul>"),
        "variants": [{"id": 1, "title": "Default Title", "price": "39.49", "sku": "OTHER-SKU-1",
                      "barcode": "012345678905", "grams": 450}],
        "images": [{"id": 1, "src": "https://cdn.shopify.com/s/files/1/other/tank.jpg"}],
    }
    product.update(overrides)
    return product


def facts_for(product=None):
    return extract_facts(product or reference_product(), "other-parts-store.example", REF_LINK)


def expect_error(fn, *args, kind=ValueError, contains=""):
    try:
        fn(*args)
    except kind as e:
        assert contains in str(e), str(e)
        return e
    raise AssertionError(f"expected {kind.__name__}")


# ---- Facts ----

def test_reads_what_the_part_is_and_what_it_fits():
    facts = facts_for()
    assert facts["part_name"] == "Fuel Gas Tank"
    assert facts["fits"]["brands"] == ["Harbor Freight", "Predator"]
    assert facts["fits"]["engine_sizes"] == ["52cc"]
    assert facts["fits"]["equipment"] == ["Earth Auger"]
    assert facts["fits"]["model_numbers"] == ["56257", "57341"]
    assert facts["reference"]["price"] == 39.49


def test_keeps_product_specs_but_skips_the_sellers_policies():
    specs = {s["name"]: s["value"] for s in facts_for()["specs"]}
    assert specs == {"Material": "HDPE plastic", "Capacity": "0.9 L"}


def test_other_sellers_wording_photos_and_barcode_are_never_taken():
    facts = facts_for()
    draft = draft_from_facts(facts)
    everything = json.dumps(facts) + json.dumps(draft)
    for taken_from_them in ("best tank", "roar", "cdn.shopify.com", "012345678905", "OTHER-SKU-1", "Other Seller Co"):
        assert taken_from_them not in everything, taken_from_them


def test_years_and_model_codes_are_told_apart():
    product = reference_product(title="Clutch Cable fits Honda TRX450R 2004-2005", body_html="", tags="")
    fits = facts_for(product)["fits"]
    assert facts_for(product)["part_name"] == "Clutch Cable"
    assert fits["brands"] == ["Honda"]
    assert fits["model_numbers"] == ["TRX450R"]
    assert fits["years"] == ["2004-2005"]


def test_measurements_are_not_mistaken_for_model_numbers():
    product = reference_product(title="Spark Plug for Predator 212cc Engine 2PCS 14MM GX160", body_html="", tags="")
    assert facts_for(product)["fits"]["model_numbers"] == ["GX160"]


# ---- Copy ----

def test_copy_is_written_from_the_facts():
    draft = draft_from_facts(facts_for())
    assert draft["title"] == "Fuel Gas Tank for Harbor Freight Predator 52cc Earth Auger - Fits 56257, 57341"
    assert "A fuel gas tank designed to fit the Harbor Freight Predator 52cc Earth Auger" in draft["description_text"]
    assert "Material: HDPE plastic" in draft["description_text"]
    assert "Aftermarket part. Not made by or affiliated with Harbor Freight and Predator." in draft["description_text"]


def test_long_titles_drop_the_secondary_brand_before_cutting():
    facts = facts_for()
    facts["fits"]["model_numbers"] = ["56257", "57341", "62001", "62002"]
    title = draft_from_facts(facts)["title"]
    assert len(title) <= 80
    assert title.startswith("Fuel Gas Tank for Predator 52cc Earth Auger")


def test_aftermarket_note_can_be_turned_off_for_genuine_parts():
    facts = facts_for()
    facts["aftermarket"] = False
    assert "Aftermarket" not in draft_from_facts(facts)["description_text"]


def test_everything_in_the_copy_is_escaped():
    facts = clean_facts({"part_name": "<script>alert(1)</script> Tank",
                         "specs": [{"name": "Material", "value": '<img src=x onerror="x()">'}]})
    html = draft_from_facts(facts, extra_notes="<b>note</b>")["description_html"]
    assert "<script>" not in html and "<img" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html


# ---- Links ----

def test_marketplace_links_wait_for_their_apis():
    for link, platform in (("https://www.ebay.com/itm/123456789", "eBay"),
                           ("https://www.amazon.com/dp/B000000000", "Amazon"),
                           ("https://a.co/d/abc123", "Amazon")):
        error = expect_error(parse_reference_url, link, kind=new_item.NeedsApi)
        assert error.platform == platform


def test_own_store_links_point_to_the_full_import():
    with patch.dict(os.environ, {"STORE_DOMAIN": "demo-parts.example"}):
        expect_error(parse_reference_url, "https://demo-parts.example/products/fuel-tank",
                     kind=new_item.OwnStoreLink, contains="imported in full")


def test_without_a_store_domain_importing_explains_the_setting():
    import store_import
    with patch.dict(os.environ, {"STORE_DOMAIN": ""}):
        expect_error(store_import.parse_product_url, "https://demo-parts.example/products/x",
                     kind=ValueError, contains="STORE_DOMAIN")


def test_non_product_pages_switch_to_entering_facts_by_hand():
    expect_error(parse_reference_url, "https://other-parts-store.example/collections/tanks",
                 kind=new_item.ManualEntry, contains="by hand")


def test_private_and_local_addresses_are_refused():
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "169.254.169.254"):
        expect_error(new_item._require_public_host, host, contains="private or local")


# ---- Saving ----

def test_saving_creates_an_unstocked_item_and_keeps_the_reference_price():
    with temp_database():
        payload = {"sku": "FMP-TANK-52", "title": "Fuel Tank for Predator 52cc Earth Auger", "price": 36.95,
                   "facts": facts_for(), "extra_notes": "Ships from California.",
                   "photo_urls": "https://example.org/my-photos/tank-1.jpg"}
        assert save_new_item(payload)["sku"] == "FMP-TANK-52"

        detail = inventory.get_item_detail("FMP-TANK-52", 30)
        assert detail["status"] == "new"
        assert detail["stock"]["on_hand"] == 0
        assert detail["product"]["image"] == "https://example.org/my-photos/tank-1.jpg"
        assert detail["references"][0]["price"] == 39.49
        product_html = inventory.storefront.get_product("FMP-TANK-52")["description_html"]
        assert "Ships from California." in product_html and MARKETING not in product_html

        expect_error(save_new_item, payload, contains="already have an item")


def test_saving_validates_sku_price_title_and_photo_links():
    with temp_database():
        base = {"sku": "FMP-1", "title": "Tank", "price": 10, "facts": {}}
        expect_error(save_new_item, {**base, "sku": "bad sku!"}, contains="SKU")
        expect_error(save_new_item, {**base, "price": 0}, contains="price")
        expect_error(save_new_item, {**base, "title": "x" * 81}, contains="80 characters")
        expect_error(save_new_item, {**base, "photo_urls": ["file:///etc/passwd"]}, contains="https://")


def test_endpoints_with_the_site_response_stubbed():
    import app as app_module
    original = new_item._fetch_product_json
    new_item._fetch_product_json = lambda host, handle: reference_product()
    try:
        with temp_database():
            client = admin_client()
            read = client.post("/api/new-item/read", json={"url": REF_LINK})
            assert read.status_code == 200
            facts = read.get_json()["facts"]
            assert facts["fits"]["model_numbers"] == ["56257", "57341"]

            ebay = client.post("/api/new-item/read", json={"url": "https://www.ebay.com/itm/123456789012"})
            assert ebay.status_code == 400 and ebay.get_json()["manual_entry"] is True
            assert ebay.get_json()["not_connected"] is True

            preview = client.post("/api/new-item/preview", json={"facts": facts, "extra_notes": ""})
            assert preview.get_json()["title"].startswith("Fuel Gas Tank for")

            saved = client.post("/api/new-item", json={"sku": "FMP-TANK-52", "title": "Fuel Tank",
                                                       "price": 36.95, "facts": facts})
            assert saved.status_code == 201
    finally:
        new_item._fetch_product_json = original

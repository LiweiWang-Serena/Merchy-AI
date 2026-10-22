"""
Tests for store_import.py. No network: the store's response is replaced
with sample_product(), which has the same shape as the JSON a Shopify
store serves at /products/<handle>.json. The title and price match the
real listing; the SKU, vendor, IDs and photo URLs are made-up test values.
"""

import contextlib
import os
import os
import sys
from unittest.mock import patch
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import db
import inventory
import storefront
import store_import
from store_import import parse_product_url, product_to_rows, save_products

STORE = "demo-parts.example"
HANDLE = "fuel-gas-tank-for-harbor-freight-predator-52cc-earth-auger-56257-57341"
LINK = f"https://demo-parts.example/collections/predator/products/{HANDLE}"


def sample_product(**overrides):
    product = {
        "id": 7000000000001,
        "title": "Fuel Gas Tank For Harbor Freight Predator 52cc Earth Auger 56257 57341",
        "handle": HANDLE,
        "vendor": "Test Vendor",
        "body_html": "<p>Replacement tank &amp; cap.</p><ul><li>Fits 56257</li><li>Fits 57341</li></ul>",
        "variants": [{"id": 4200000000001, "title": "Default Title", "price": "37.95",
                      "sku": "TEST-TANK-52", "image_id": None}],
        "images": [{"id": 3100000000001, "src": "https://cdn.shopify.com/s/files/1/test/tank-front.jpg"},
                   {"id": 3100000000002, "src": "https://cdn.shopify.com/s/files/1/test/tank-side.jpg"}],
    }
    product.update(overrides)
    return product


@contextlib.contextmanager
def temp_database(demo_data=False):
    """Points the app at a throwaway database, so tests never touch real
    data. Normally a temporary SQLite file; with TEST_DATABASE_URL set to a
    PostgreSQL database, a temporary schema in it (deleted afterwards).
    DATABASE_URL from .env is always ignored here."""
    import uuid
    saved = {"path": db.DB_PATH, "schema": db.PG_SCHEMA,
             "env": {k: os.environ.get(k) for k in ("DEMO_DATA", "DATABASE_URL")}}
    os.environ["DEMO_DATA"] = "true" if demo_data else "false"
    test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "test.db"
        if test_url:
            os.environ["DATABASE_URL"] = test_url
            db.PG_SCHEMA = f"test_{uuid.uuid4().hex[:12]}"
            _postgres_admin(test_url, f"CREATE SCHEMA {db.PG_SCHEMA}")
        else:
            os.environ.pop("DATABASE_URL", None)
        try:
            inventory.ensure_seeded()
            yield
        finally:
            if test_url:
                _postgres_admin(test_url, f"DROP SCHEMA {db.PG_SCHEMA} CASCADE")
            db.forget_schema()
            db.DB_PATH, db.PG_SCHEMA = saved["path"], saved["schema"]
            for key, value in saved["env"].items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _postgres_admin(url, statement):
    import psycopg
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SET lock_timeout = '10s'")
        conn.execute(statement)


def admin_client():
    """A test client that's already signed in to the admin area."""
    import app as app_module
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
    return client


def expect_value_error(fn, *args, contains=""):
    try:
        fn(*args)
    except ValueError as e:
        assert contains in str(e), str(e)
        return
    raise AssertionError("expected ValueError")


# ---- Links ----

def test_accepts_a_product_link_opened_from_a_collection():
    assert parse_product_url(LINK, STORE) == HANDLE


def test_accepts_www_query_strings_and_text_pasted_after_the_link():
    assert parse_product_url("https://www.demo-parts.example/products/fuel-tank-52cc?variant=1", STORE) == "fuel-tank-52cc"
    assert parse_product_url("https://demo-parts.example/products/fuel-tank-52cc那我现在", STORE) == "fuel-tank-52cc"


def test_rejects_other_stores_including_lookalike_domains():
    expect_value_error(parse_product_url, "https://another-shop.com/products/fuel-tank", STORE, contains=STORE)
    expect_value_error(parse_product_url, "https://another-shop.com/products/fuel-tank", STORE,
                       contains="another-shop.com")
    expect_value_error(parse_product_url, "https://demo-parts.example.example.net/products/x", STORE, contains=STORE)


def test_wrong_site_messages_say_where_the_link_came_from():
    expect_value_error(parse_product_url, "https://admin.shopify.com/store/demo/products/1234567890", STORE,
                       contains="Shopify admin")
    expect_value_error(parse_product_url, "https://demo-store.myshopify.com/products/fuel-tank", STORE,
                       contains="myshopify.com")
    expect_value_error(parse_product_url, "https://www.ebay.com/itm/123456789", STORE, contains="eBay")


def test_rejects_pages_that_are_not_products():
    expect_value_error(parse_product_url, "https://demo-parts.example/collections/predator", STORE,
                       contains="/products/")
    expect_value_error(parse_product_url, "demo-parts.example/products/x", STORE, contains="https://")


# ---- Mapping store data to inventory rows ----

def test_single_variant_product_becomes_one_item():
    rows = product_to_rows(sample_product(), STORE)
    assert len(rows) == 1
    row = rows[0]
    assert row["sku"] == "TEST-TANK-52" and row["sku_from_store"] is True
    assert row["title"] == "Fuel Gas Tank For Harbor Freight Predator 52cc Earth Auger 56257 57341"
    assert row["price"] == 37.95
    assert row["images"][0].endswith("tank-front.jpg")
    assert row["source_url"] == f"https://demo-parts.example/products/{HANDLE}"


def test_each_variant_becomes_its_own_sku_with_its_own_photo_first():
    product = sample_product(variants=[
        {"id": 1, "title": "Red", "price": "37.95", "sku": "TANK-RED", "image_id": 3100000000002},
        {"id": 2, "title": "Black", "price": "39.95", "sku": "TANK-BLK", "image_id": None},
    ])
    red, black = product_to_rows(product, STORE)
    assert (red["sku"], black["sku"]) == ("TANK-RED", "TANK-BLK")
    assert red["title"].endswith("(Red)")
    assert red["images"][0].endswith("tank-side.jpg")
    assert black["price"] == 39.95
    assert black["source_url"].endswith("?variant=2")


def test_variant_without_a_sku_gets_a_stable_fallback():
    product = sample_product(variants=[{"id": 99, "title": "Default Title", "price": "10.00", "sku": "", "image_id": None}])
    row = product_to_rows(product, STORE)[0]
    assert row["sku"] == "SHOPIFY-99"
    assert row["sku_from_store"] is False


def test_description_is_reduced_to_escaped_text():
    body = ('<p onclick="steal()">Tank &amp; "cap"</p><script>alert(1)</script>'
            '<img src=x onerror=alert(1)>')
    html = product_to_rows(sample_product(body_html=body), STORE)[0]["description_html"]
    assert html == "<p>Tank &amp; &quot;cap&quot;</p>"


# ---- Saving ----

def test_reimport_updates_details_but_never_touches_stock():
    with temp_database():
        save_products(product_to_rows(sample_product(), STORE))
        inventory.record_movement("TEST-TANK-52", "purchase", 10, unit_cost=18.50, reference="PO 1")
        repriced = [{"id": 4200000000001, "title": "Default Title", "price": "34.95",
                     "sku": "TEST-TANK-52", "image_id": None}]
        result = save_products(product_to_rows(sample_product(variants=repriced), STORE))
        assert result[0]["created"] is False
        product = storefront.get_product("TEST-TANK-52")
        assert product["price"] == 34.95
        assert product["inventory_qty"] == 10


def test_imported_item_is_not_stocked_until_stock_is_recorded():
    with temp_database():
        save_products(product_to_rows(sample_product(), STORE))
        item = inventory.get_overview(30)["items"][0]
        assert item["status"] == "new"
        assert item["on_hand"] == 0
        assert item["image"].startswith("https://cdn.shopify.com/")

        inventory.record_movement("TEST-TANK-52", "purchase", 10, unit_cost=18.50)
        detail = inventory.get_item_detail("TEST-TANK-52", 30)
        assert detail["status"] == "no_sales"
        assert detail["stock"]["on_hand"] == 10
        assert detail["stock"]["avg_cost"] == 18.5
        assert detail["product"]["source_url"].startswith("https://demo-parts.example/products/")


def test_import_endpoint_with_the_store_response_stubbed():
    import app as app_module
    original_fetch = store_import.fetch_product
    store_import.fetch_product = lambda handle, domain=None: sample_product()
    try:
        with temp_database():
            client = admin_client()
            with patch.dict(os.environ, {"STORE_DOMAIN": STORE}):
                ok = client.post("/api/inventory/import", json={"url": LINK})
            assert ok.status_code == 201
            assert ok.get_json()["items"][0]["sku"] == "TEST-TANK-52"
            other_store = client.post("/api/inventory/import", json={"url": "https://another-shop.com/products/x"})
            assert other_store.status_code == 400
    finally:
        store_import.fetch_product = original_fetch

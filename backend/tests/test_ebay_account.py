"""
Tests for the eBay seller-account connection (ebay_account.py and the
two integrations it uses). No network: eBay's token endpoint, Trading API
and Fulfillment API are stubbed with responses in their real shapes.
"""

import base64
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse
sys.path.insert(0, str(Path(__file__).parent.parent))

import ebay_account
import inventory
import store_import
from db import get_connection
from integrations import ebay_oauth
from integrations.ebay_seller import parse_listing, NS
from tests.test_marketplace import env
from tests.test_new_item import expect_error
from tests.test_store_import import temp_database, admin_client

SETTINGS = {"EBAY_ENVIRONMENT": "sandbox", "EBAY_CLIENT_ID": "Serena-App-SBX", "EBAY_CLIENT_SECRET": "SBX-secret",
            "EBAY_RUNAME": "Serena-Serena-App-SBX-abcdef", "EBAY_SCOPES": None}

GETUSER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>
<User><UserID>testuser_serena</UserID></User></GetUserResponse>"""

LISTINGS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>
<ActiveList><ItemArray>
  <Item><ItemID>110000000001</ItemID><SKU>FMP-1</SKU><Title>Carburetor for Yamaha Kodiak 450</Title>
    <QuantityAvailable>7</QuantityAvailable>
    <SellingStatus><CurrentPrice currencyID="USD">39.95</CurrentPrice><QuantitySold>3</QuantitySold></SellingStatus>
    <PictureDetails><GalleryURL>https://i.ebayimg.com/thumbs/carb.jpg</GalleryURL></PictureDetails></Item>
  <Item><ItemID>110000000002</ItemID><Title>Scooter Inner Tube 200x50</Title><Quantity>12</Quantity>
    <SellingStatus><CurrentPrice currencyID="USD">12.50</CurrentPrice><QuantitySold>2</QuantitySold></SellingStatus></Item>
  <Item><ItemID>110000000003</ItemID><Title>Drum Brake Assembly</Title>
    <SellingStatus><CurrentPrice currencyID="USD">45.95</CurrentPrice></SellingStatus>
    <Variations>
      <Variation><SKU>BRK-90</SKU><StartPrice currencyID="USD">45.95</StartPrice><Quantity>5</Quantity>
        <SellingStatus><QuantitySold>1</QuantitySold></SellingStatus>
        <VariationSpecifics><NameValueList><Name>Size</Name><Value>90mm</Value></NameValueList></VariationSpecifics></Variation>
      <Variation><StartPrice currencyID="USD">49.95</StartPrice><Quantity>2</Quantity>
        <SellingStatus><QuantitySold>2</QuantitySold></SellingStatus>
        <VariationSpecifics><NameValueList><Name>Size</Name><Value>110mm</Value></NameValueList></VariationSpecifics></Variation>
    </Variations></Item>
</ItemArray>
<PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages><TotalNumberOfEntries>3</TotalNumberOfEntries></PaginationResult>
</ActiveList></GetMyeBaySellingResponse>"""

ORDERS = {"total": 3, "orders": [
    {"orderId": "01-11111-11111", "creationDate": "2026-09-20T15:04:05.000Z", "orderPaymentStatus": "PAID",
     "cancelStatus": {"cancelState": "NONE_REQUESTED"}, "totalMarketplaceFee": {"value": "3.00", "currency": "USD"},
     "buyer": {"username": "some_buyer"},
     "lineItems": [
         {"lineItemId": "L1", "legacyItemId": "110000000001", "sku": "FMP-1", "title": "Carburetor",
          "quantity": 2, "lineItemCost": {"value": "20.00", "currency": "USD"}},
         {"lineItemId": "L2", "legacyItemId": "110000000002", "title": "Scooter Inner Tube",
          "quantity": 1, "lineItemCost": {"value": "10.00", "currency": "USD"}}]},
    {"orderId": "02-22222-22222", "creationDate": "2026-09-21T10:00:00.000Z", "orderPaymentStatus": "PAID",
     "cancelStatus": {"cancelState": "CANCELED"},
     "lineItems": [{"lineItemId": "L3", "sku": "FMP-1", "quantity": 1, "lineItemCost": {"value": "10.00"}}]},
    {"orderId": "03-33333-33333", "creationDate": "2026-09-22T10:00:00.000Z", "orderPaymentStatus": "PENDING",
     "cancelStatus": {"cancelState": "NONE_REQUESTED"},
     "lineItems": [{"lineItemId": "L4", "sku": "FMP-1", "quantity": 1, "lineItemCost": {"value": "10.00"}}]},
]}


class FakeResponse:
    def __init__(self, status, json_body=None, content=b""):
        self.status_code, self._json, self.content = status, json_body, content
        self.ok = 200 <= status < 300

    def json(self):
        return self._json


@contextmanager
def fake_ebay(token_status=200, token_body=None):
    """Stubs requests.post/get with eBay-shaped responses; yields the call log."""
    import requests
    calls = []
    body = token_body or {"access_token": "v^1.1#access", "expires_in": 7200,
                          "refresh_token": "v^1.1#refresh", "refresh_token_expires_in": 47304000}

    def post(url, **kw):
        if "/identity/v1/oauth2/token" in url:
            calls.append(("token", kw["data"], kw["headers"]))
            return FakeResponse(token_status, body)
        call = kw["headers"]["X-EBAY-API-CALL-NAME"]
        calls.append(("trading", call, kw["headers"]))
        return FakeResponse(200, content=GETUSER_XML if call == "GetUser" else LISTINGS_XML)

    def get(url, **kw):
        calls.append(("orders", kw["params"], kw["headers"]))
        return FakeResponse(200, ORDERS)

    original = requests.post, requests.get
    requests.post, requests.get = post, get
    try:
        yield calls
    finally:
        requests.post, requests.get = original


def connect_directly(access_expires_at=None):
    """Saves a connected account without the consent flow."""
    conn = ebay_account._connect()
    conn.execute("""INSERT INTO ebay_account (environment, username, scopes, refresh_token, refresh_expires_at,
                    access_token, access_expires_at, connected_at) VALUES ('sandbox', 'testuser_serena', ?, 'v^1.1#refresh',
                    ?, 'v^1.1#old', ?, '2026-09-29 09:00:00')""",
                 (" ".join(ebay_oauth.DEFAULT_SCOPES), time.time() + 86400 * 500,
                  access_expires_at if access_expires_at is not None else time.time() + 3600))
    conn.commit()
    conn.close()


def add_product(sku, stock=0, stock_date="2026-09-01 09:00:00"):
    store_import.save_products([{"sku": sku, "sku_from_store": True, "title": f"Item {sku}", "brand": None,
                                 "description_html": "", "images": [], "price": 10.0, "source_url": None}])
    if stock:
        conn = get_connection()
        inventory.ensure_ledger_table(conn)
        conn.execute(inventory.INSERT_SQL, (sku, "purchase", None, stock, 8.0, None, 0.0, None, None, stock_date))
        conn.execute("UPDATE products SET inventory_qty = ? WHERE sku = ?", (stock, sku))
        conn.commit()
        conn.close()


# ---- Consent link and pasted address ----

def test_consent_link_asks_for_read_only_access_and_a_fresh_sign_in():
    with temp_database(), env(**SETTINGS):
        url = urlparse(ebay_account.create_consent_link())
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://auth.sandbox.ebay.com/oauth2/authorize"
    assert params["client_id"] == "Serena-App-SBX"
    assert params["redirect_uri"] == "Serena-Serena-App-SBX-abcdef"
    assert params["response_type"] == "code" and params["prompt"] == "login"
    assert params["scope"].split(" ") == list(ebay_oauth.DEFAULT_SCOPES)
    assert all(s.endswith(("api_scope", ".readonly")) for s in params["scope"].split(" "))
    assert len(params["state"]) >= 24


def test_consent_link_says_which_settings_are_missing():
    with temp_database(), env(**{**SETTINGS, "EBAY_RUNAME": None}):
        expect_error(ebay_account.create_consent_link, contains="EBAY_RUNAME")


def test_reads_the_code_from_the_confirmation_address_or_a_bare_code():
    address = ("https://signin.sandbox.ebay.com/ws/eBayISAPI.dll?ThirdPartyAuthSucessFailure&isAuthSuccessful=true"
               "&code=v%5E1.1%23i%5E1%23f%5E0&expires_in=299&state=abc123")
    assert ebay_oauth.read_code(address) == ("v^1.1#i^1#f^0", "abc123")
    assert ebay_oauth.read_code("v%5E1.1%23i%5E1") == ("v^1.1#i^1", None)
    expect_error(ebay_oauth.read_code, "https://x.example/?isAuthSuccessful=false", contains="wasn't granted")
    expect_error(ebay_oauth.read_code, "", contains="Paste the address")


# ---- Connecting ----

def test_connecting_exchanges_the_code_and_saves_the_account():
    with temp_database(), env(**SETTINGS), fake_ebay() as calls:
        state = parse_qs(urlparse(ebay_account.create_consent_link()).query)["state"][0]
        result = ebay_account.complete(f"https://signin.sandbox.ebay.com/x?code=v%5E1.1%23code&state={state}")
        stored = dict(get_connection().execute("SELECT * FROM ebay_account").fetchone())
        pending = get_connection().execute("SELECT COUNT(*) FROM ebay_pending_consent").fetchone()[0]
    assert result["connected"] and result["username"] == "testuser_serena"
    token_call = calls[0]
    assert token_call[1] == {"grant_type": "authorization_code", "code": "v^1.1#code",
                             "redirect_uri": "Serena-Serena-App-SBX-abcdef"}
    assert token_call[2]["Authorization"] == "Basic " + base64.b64encode(b"Serena-App-SBX:SBX-secret").decode()
    assert calls[1][1] == "GetUser" and calls[1][2]["X-EBAY-API-IAF-TOKEN"] == "v^1.1#access"
    assert stored["refresh_token"] == "v^1.1#refresh" and pending == 0


def test_connecting_rejects_codes_that_did_not_start_here():
    with temp_database(), env(**SETTINGS), fake_ebay():
        expect_error(ebay_account.complete, "https://x.example/?code=abc&state=forged", contains="unknown connection")
        expect_error(ebay_account.complete, "abc", contains="Open eBay from the admin page first")
        ebay_account.create_consent_link()
        expect_error(ebay_account.complete, "https://x.example/?code=abc", True, contains="Open eBay")


def test_an_expired_code_gets_a_clear_message():
    with temp_database(), env(**SETTINGS), fake_ebay(token_status=400, token_body={"error": "invalid_grant"}):
        state = parse_qs(urlparse(ebay_account.create_consent_link()).query)["state"][0]
        expect_error(ebay_account.complete, f"https://x.example/?code=old&state={state}",
                     kind=ebay_oauth.EbayAuthError, contains="about 5 minutes")


def test_an_expired_access_token_is_refreshed_and_then_reused():
    with temp_database(), env(**SETTINGS), fake_ebay() as calls:
        connect_directly(access_expires_at=time.time() - 10)
        first = ebay_account._client()
        second = ebay_account._client()
    token_calls = [c for c in calls if c[0] == "token"]
    assert len(token_calls) == 1
    assert token_calls[0][1]["grant_type"] == "refresh_token"
    assert token_calls[0][1]["scope"] == " ".join(ebay_oauth.DEFAULT_SCOPES)
    assert first.token == second.token == "v^1.1#access"


# ---- Listings ----

def test_listings_with_and_without_skus_and_with_variations():
    import xml.etree.ElementTree as ET
    items = ET.fromstring(LISTINGS_XML).findall("e:ActiveList/e:ItemArray/e:Item", NS)
    records = [r for item in items for r in parse_listing(item)]
    assert [(r["sku"], r["quantity_available"], r["price"]) for r in records] == [
        ("FMP-1", 7, 39.95),
        ("EBAY-110000000002", 10, 12.5),          # no SKU: 12 listed - 2 sold
        ("BRK-90", 4, 45.95),
        ("EBAY-110000000003-2", 0, 49.95),        # variation without its own SKU
    ]
    assert records[2]["title"] == "Drum Brake Assembly (90mm)"


def test_listing_sync_replaces_ebay_rows_and_can_add_missing_items():
    with temp_database(), env(**SETTINGS), fake_ebay():
        add_product("FMP-1")
        connect_directly()
        first = ebay_account.sync_listings(add_missing=False)
        rows = {r["sku"]: r["quantity_available"] for r in
                get_connection().execute("SELECT * FROM listings WHERE channel = 'ebay'")}
        second = ebay_account.sync_listings(add_missing=True)
        added = dict(get_connection().execute("SELECT * FROM products WHERE sku = 'BRK-90'").fetchone())
    assert rows == {"FMP-1": 7, "EBAY-110000000002": 10, "BRK-90": 4, "EBAY-110000000003-2": 0}
    assert first["not_in_catalog_count"] == 3 and not first["added"]
    assert len(second["added"]) == 3 and second["not_in_catalog_count"] == 0
    assert added["source_url"] == "https://sandbox.ebay.com/itm/110000000003"
    assert added["inventory_qty"] == 0          # stock only ever comes from the ledger


def test_channel_sync_uses_the_connected_account_for_ebay():
    from sync import run_sync
    with temp_database(), env(**SETTINGS), fake_ebay():
        connect_directly()
        result = run_sync()
    assert result["ebay_records"] == 4


# ---- Orders ----

def test_order_lines_skip_canceled_and_unpaid_orders_and_split_the_fee():
    lines, skipped = ebay_account.order_lines(ORDERS["orders"])
    assert skipped == {"canceled": 1, "not_paid": 1}
    assert [(l["sku"], l["quantity"], l["unit_price"], l["fee"]) for l in lines] == [
        ("FMP-1", 2, 10.0, 2.0),                  # $3 fee split by line value: 20 of 30
        ("EBAY-110000000002", 1, 10.0, 1.0),
    ]
    assert lines[0]["occurred_at"] == ebay_account._local_time("2026-09-20T15:04:05.000Z")
    assert "some_buyer" not in str(lines)         # buyer details aren't carried over


def test_import_records_each_sale_once_and_explains_what_it_skipped():
    with temp_database():
        add_product("FMP-1", stock=5)
        lines, _ = ebay_account.order_lines(ORDERS["orders"])
        lines.append({**lines[0], "line_item_id": "L9", "order_id": "09-99999-99999", "quantity": 10,
                      "occurred_at": "2026-09-23 12:00:00"})
        first = ebay_account.record_lines(lines)
        second = ebay_account.record_lines(lines)
        on_hand = get_connection().execute("SELECT inventory_qty FROM products WHERE sku = 'FMP-1'").fetchone()[0]
        sale = dict(get_connection().execute(
            "SELECT * FROM inventory_movements WHERE movement_type = 'sale'").fetchone())
    assert [r["sku"] for r in first["recorded"]] == ["FMP-1"]
    assert [r["sku"] for r in first["not_in_catalog"]] == ["EBAY-110000000002"]
    assert first["not_enough_stock"][0]["available"] == 3
    assert second["already_imported"] == 1 and not second["recorded"]
    assert on_hand == 3
    assert (sale["channel"], sale["quantity"], sale["unit_price"], sale["fee"], sale["reference"]) == (
        "ebay", -2, 10.0, 2.0, "eBay order 01-11111-11111")


def test_a_back_dated_sale_cannot_take_stock_below_zero_later():
    rows = [(5, "2026-09-01 09:00:00"), (-5, "2026-09-25 09:00:00")]
    assert ebay_account.lowest_balance_from(rows, "2026-09-20 00:00:00") == 0
    assert ebay_account.lowest_balance_from(rows, "2026-08-01 00:00:00") == 0
    assert ebay_account.lowest_balance_from([(5, "2026-09-01 09:00:00")], "2026-09-20 00:00:00") == 5


def test_orders_are_read_from_the_chosen_date_with_the_user_token():
    with temp_database(), env(**SETTINGS), fake_ebay() as calls:
        add_product("FMP-1", stock=5)
        connect_directly()
        result = ebay_account.import_orders(90)
    order_call = next(c for c in calls if c[0] == "orders")
    assert order_call[1]["filter"].startswith("creationdate:[") and order_call[1]["filter"].endswith("..]")
    assert order_call[2]["Authorization"] == "Bearer v^1.1#old"
    assert result["orders"] == 3 and len(result["recorded"]) == 1


# ---- Endpoints ----

def test_endpoints_need_sign_in_and_explain_missing_setup():
    import app as app_module
    with temp_database(), env(**{**SETTINGS, "EBAY_CLIENT_ID": None}):
        assert app_module.app.test_client().get("/api/ebay/status").status_code == 401
        client = admin_client()
        status = client.get("/api/ebay/status").get_json()
        assert status["missing_settings"] == ["EBAY_CLIENT_ID"] and status["connected"] is False
        resp = client.post("/api/ebay/consent-link")
        assert resp.status_code == 400 and "EBAY_CLIENT_ID" in resp.get_json()["error"]
        assert client.post("/api/ebay/import-orders", json={"days": 5}).status_code == 400


def test_callback_page_is_public_but_only_accepts_codes_started_here():
    import app as app_module
    with temp_database(), env(**SETTINGS):
        resp = app_module.app.test_client().get("/ebay/callback?code=abc&state=forged")
    assert resp.status_code == 400 and b"not connected" in resp.data

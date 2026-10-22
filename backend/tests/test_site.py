"""
Tests for the website as a whole: every page is served from the one
server, the shop is public, and the admin area and its API need sign-in.
"""

import os
import sys
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
from tests.test_store_import import temp_database, admin_client


def test_shop_pages_and_assets_are_public():
    with temp_database():
        client = app_module.app.test_client()
        for path in ("/", "/product?sku=X", "/cart", "/checkout", "/static/store.css", "/static/cart.js"):
            assert client.get(path).status_code == 200, path


def test_admin_pages_send_you_to_sign_in_and_back():
    client = app_module.app.test_client()
    resp = client.get("/admin/reports?period=90")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/login?next=/admin/reports%3Fperiod%3D90"
    assert client.get("/admin/login").status_code == 200


def test_admin_api_needs_sign_in_but_the_shop_api_does_not():
    with temp_database():
        client = app_module.app.test_client()
        for path in ("/api/inventory", "/api/reports", "/api/overview", "/api/report"):
            assert client.get(path).status_code == 401, path
        assert client.post("/api/new-item/read", json={"url": "x"}).status_code == 401
        assert client.post("/api/sync").status_code == 401
        assert client.get("/api/products").status_code == 200
        assert client.get("/api/store/best-sellers").status_code == 200


def test_only_the_right_password_signs_in_and_sign_out_works():
    with temp_database():
        client = app_module.app.test_client()
        assert client.post("/api/admin/login", json={"password": "admin"}).status_code == 401
        password = app_module.app.config["ADMIN_PASSWORD"]
        assert client.post("/api/admin/login", json={"password": password}).status_code == 200
        assert client.get("/api/inventory").status_code == 200
        assert client.get("/admin").status_code == 200
        client.get("/admin/logout")
        assert client.get("/api/inventory").status_code == 401


def test_overview_reports_and_best_sellers_with_demo_data():
    with temp_database(demo_data=True):
        client = admin_client()
        overview = client.get("/api/overview").get_json()
        assert overview["summary"]["sku_count"] == 8
        assert overview["top_sellers"]
        assert any(a["status"] == "out_of_stock" for a in overview["attention"])
        report = client.get("/api/reports?period=90").get_json()
        assert report["period_days"] == 90
        assert [c["class"] for c in report["abc"]] == ["A", "B", "C"]
        best = app_module.app.test_client().get("/api/store/best-sellers").get_json()
        assert best and all(p["inventory_qty"] > 0 for p in best)


def test_marketplace_sample_through_the_endpoint():
    with temp_database():
        client = admin_client()
        resp = client.post("/api/new-item/read", json={"url": "https://www.ebay.com/itm/100000000001"})
        assert resp.status_code == 200
        assert resp.get_json()["source"]["demo"] is True
        with patch.dict(os.environ, {"STORE_DOMAIN": "demo-parts.example"}):
            own = client.post("/api/new-item/read", json={"url": "https://demo-parts.example/products/x"})
        assert own.status_code == 409 and own.get_json()["own_store"] is True


def test_listings_report_works_before_the_first_sync():
    # Regression: a brand-new database has no listings table until a sync
    # runs, and the Listings page used to get a server error.
    with temp_database():
        resp = admin_client().get("/api/report")
        assert resp.status_code == 200
        assert resp.get_json()["summary"]["total_skus_tracked"] == 0


def test_the_react_shop_explains_how_to_build_it_until_it_is_built():
    import shutil
    dist = app_module.REACT_DIST
    had_build = dist.exists()
    client = app_module.app.test_client()
    if not had_build:
        resp = client.get("/react/")
        assert resp.status_code == 404 and b"npm run build" in resp.data
    try:
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<div id=root></div>")
        (dist / "assets" / "app.js").write_text("console.log(1)")
        assert client.get("/react/").status_code == 200
        assert client.get("/react/assets/app.js").data == b"console.log(1)"
        assert client.get("/react/?q=tube&page=2").status_code == 200    # the app handles its own address
        # Nothing outside the build folder can be read, even with ../ in the address.
        secret = dist.parent / "secret-for-test.txt"
        secret.write_text("do not serve")
        for attempt in ("/react/../secret-for-test.txt", "/react/%2e%2e/secret-for-test.txt",
                        "/react/assets/../../secret-for-test.txt"):
            assert b"do not serve" not in client.get(attempt).data, attempt
    finally:
        (dist.parent / "secret-for-test.txt").unlink(missing_ok=True)
        if not had_build:
            shutil.rmtree(dist)

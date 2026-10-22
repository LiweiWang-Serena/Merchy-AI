try:
    # Read settings such as DEMO_DATA and STORE_DOMAIN from backend/.env.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session

from sync import run_sync
from reporting import build_report
from mock_apis import amazon_api
from listing_generator import build_listing_draft
from db import get_connection, ensure_draft_table
import json
import storefront
import checkout
import market_research
import inventory
import store_import
import new_item
import content_generator
import marketplace_import
import reports
import auth
import db
import ebay_account
import notifications
import support_agent
from content_generator import draft_listing_copy

# One server for the whole website: pages, their CSS/JS (under /static)
# and the JSON API (under /api) share one address, so no cross-site
# access needs to be allowed.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="/static")
auth.init_app(app)

PAGES = {
    # Shop (public)
    "/": "store.html",
    "/product": "product.html",
    "/cart": "cart.html",
    "/checkout": "checkout.html",
    # Admin (sign-in required, enforced in auth.py)
    "/admin/login": "login.html",
    "/admin": "overview.html",
    "/admin/inventory": "inventory.html",
    "/admin/item": "inventory-item.html",
    "/admin/add": "new-item.html",
    "/admin/listings": "dashboard.html",
    "/admin/ebay": "ebay.html",
    "/admin/support": "support.html",
    "/admin/reports": "reports.html",
    "/admin/research": "market-research.html",
    "/admin/ebay-draft": "create-listing.html",
}


def _page(filename):
    def view():
        return send_from_directory(FRONTEND_DIR, filename)
    return view


for _route, _filename in PAGES.items():
    app.add_url_rule(_route, endpoint="page_" + _filename.replace(".", "_").replace("-", "_"),
                     view_func=_page(_filename))


# The React + TypeScript version of the shop home page (frontend-react/),
# once it's been built with `npm run build`. Public, like the original shop.
REACT_DIST = FRONTEND_DIR.parent / "frontend-react" / "dist"


@app.route("/react/")
@app.route("/react/<path:path>")
def react_shop(path="index.html"):
    if not (REACT_DIST / "index.html").exists():
        return ("<p>The React version hasn't been built yet. In the frontend-react folder, run "
                "<code>npm install</code> and then <code>npm run build</code>.</p>", 404)
    if (REACT_DIST / path).is_file():
        return send_from_directory(REACT_DIST, path)
    return send_from_directory(REACT_DIST, "index.html")


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    body = request.get_json(silent=True) or {}
    if not auth.check_password(app, body.get("password")):
        return jsonify({"error": "That password isn't right."}), 401
    session.clear()
    session["admin"] = True
    return jsonify({"ok": True})


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")


# ---- eBay account (read-only connection to the store's seller account) ----

def _ebay_errors(action):
    """Runs an eBay action and turns its errors into JSON responses."""
    try:
        return jsonify(action())
    except ebay_account.EbayAuthError as e:
        return jsonify({"error": str(e), "reconnect": True}), 502
    except ebay_account.EbaySellerError as e:
        return jsonify({"error": str(e)}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/ebay/status", methods=["GET"])
def api_ebay_status():
    return jsonify(ebay_account.status())


@app.route("/api/ebay/consent-link", methods=["POST"])
def api_ebay_consent_link():
    return _ebay_errors(lambda: {"url": ebay_account.create_consent_link()})


@app.route("/api/ebay/complete", methods=["POST"])
def api_ebay_complete():
    address = (request.get_json(silent=True) or {}).get("address", "")
    return _ebay_errors(lambda: ebay_account.complete(address))


@app.route("/api/ebay/sync-listings", methods=["POST"])
def api_ebay_sync_listings():
    add_missing = bool((request.get_json(silent=True) or {}).get("add_missing"))
    return _ebay_errors(lambda: ebay_account.sync_listings(add_missing=add_missing))


@app.route("/api/ebay/import-orders", methods=["POST"])
def api_ebay_import_orders():
    body = request.get_json(silent=True) or {}
    days, until_days = body.get("days", 30), body.get("until_days", 0)
    if not all(isinstance(v, int) for v in (days, until_days)):
        return jsonify({"error": "Choose a window within the last two years."}), 400
    return _ebay_errors(lambda: ebay_account.import_orders(days, until_days))


@app.route("/api/ebay/disconnect", methods=["POST"])
def api_ebay_disconnect():
    return jsonify(ebay_account.disconnect())


@app.route("/ebay/callback")
def ebay_callback():
    """Where eBay sends the account owner after they agree, when the
    RuName's auth accepted URL points here (an HTTPS deployment). Public on
    purpose: the owner may not be signed in to this admin area. It only
    accepts codes for consent links made in the admin area in the last 30
    minutes."""
    from markupsafe import escape
    try:
        ebay_account.complete(request.url, require_state=True)
        title, text, code = "eBay account connected", "You can close this tab.", 200
    except (ValueError, ebay_account.EbayAuthError) as e:
        title, text, code = "eBay account not connected", str(e), 400
    page = (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>{escape(title)}</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            f'<body style="font-family: system-ui, sans-serif; max-width: 520px; margin: 18vh auto; '
            f'padding: 0 24px"><h1 style="font-size: 26px">{escape(title)}</h1><p>{escape(text)}</p></body></html>')
    return page, code


# ---- AI customer support (support_agent.py) ----

@app.route("/api/support/config", methods=["GET"])
def api_support_config():
    """Public: what the chat widget needs to greet the customer."""
    settings = support_agent.get_settings()
    return jsonify({"assistant_name": settings["assistant_name"], "greeting": settings["greeting"],
                    "ai_ready": support_agent.ai_ready()})


@app.route("/api/support/chat", methods=["POST"])
def api_support_chat():
    """Public: one customer message in, one reply out."""
    body = request.get_json(silent=True) or {}
    try:
        support_agent.check_rate(request.remote_addr or "unknown")
        return jsonify(support_agent.chat(body.get("conversation_id"), body.get("message")))
    except support_agent.TooMany as e:
        return jsonify({"error": str(e)}), 429
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/support/chat/<conversation_id>/messages", methods=["GET"])
def api_support_messages(conversation_id):
    """Public: new messages for the chat window, including replies from a person."""
    try:
        return jsonify(support_agent.messages_after(conversation_id, request.args.get("after", 0)))
    except LookupError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/support/conversations/<conversation_id>/reply", methods=["POST"])
def api_support_staff_reply(conversation_id):
    text = (request.get_json(silent=True) or {}).get("text")
    return _support_errors(lambda: support_agent.staff_reply(conversation_id, text))


@app.route("/api/support/conversations/<conversation_id>/resolve", methods=["POST"])
def api_support_resolve(conversation_id):
    return _support_errors(lambda: support_agent.resolve(conversation_id))


@app.route("/api/support/admin", methods=["GET"])
def api_support_admin():
    return jsonify({"settings": support_agent.get_settings(), "knowledge": support_agent.list_knowledge(),
                    "conversations": support_agent.list_conversations(),
                    "ai_ready": support_agent.ai_ready(), "model": support_agent.model(),
                    "provider": support_agent.provider(),
                    "notifications": notifications.channels()})


def _support_errors(action):
    try:
        return jsonify(action())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/support/settings", methods=["POST"])
def api_support_settings():
    return _support_errors(lambda: support_agent.save_settings(request.get_json(silent=True) or {}))


@app.route("/api/support/knowledge", methods=["POST"])
def api_support_knowledge_save():
    body = request.get_json(silent=True) or {}
    return _support_errors(lambda: support_agent.save_knowledge(body.get("title"), body.get("content"), body.get("id")))


@app.route("/api/support/knowledge/<int:entry_id>/delete", methods=["POST"])
def api_support_knowledge_delete(entry_id):
    return _support_errors(lambda: support_agent.delete_knowledge(entry_id))


@app.route("/api/support/conversations/<conversation_id>", methods=["GET"])
def api_support_conversation(conversation_id):
    return _support_errors(lambda: support_agent.get_conversation(conversation_id))


@app.route("/api/support/messages/<int:message_id>/review", methods=["POST"])
def api_support_review(message_id):
    body = request.get_json(silent=True) or {}
    return _support_errors(lambda: support_agent.review_message(message_id, body.get("rating"), body.get("correction")))


@app.route("/api/overview", methods=["GET"])
def api_overview():
    """Admin home: last 30 days at a glance, top sellers, items needing attention, recent orders."""
    inventory.ensure_seeded()
    return jsonify(reports.build_overview())


@app.route("/api/reports", methods=["GET"])
def api_reports():
    """Product analysis, profit, sales, purchasing and inventory analysis for the period."""
    inventory.ensure_seeded()
    return jsonify(reports.for_page(reports.build_report(_period_arg())))


@app.route("/api/reports/items.csv", methods=["GET"])
def api_reports_csv():
    """Every item's figures for the period, for opening in Excel or Numbers."""
    from flask import Response
    inventory.ensure_seeded()
    period = _period_arg()
    return Response(reports.items_csv(period), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=items-last-{period}-days.csv"})


@app.route("/api/store/best-sellers", methods=["GET"])
def api_best_sellers():
    """Public: the shop home page's best sellers (in stock only)."""
    inventory.ensure_seeded()
    return jsonify(reports.best_sellers())


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Pull fresh listing data from Amazon and eBay and reconcile it. eBay
    comes from the connected seller account when there is one."""
    return _ebay_errors(run_sync)


@app.route("/api/report", methods=["GET"])
def api_report():
    """Return the latest procurement/inventory report from synced data."""
    return jsonify(build_report())


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/fetch-amazon", methods=["POST"])
def api_fetch_amazon():
    """Given a pasted Amazon listing URL, return the product data for
    that (your own) listing -- title, bullets, and photo URLs."""
    url = (request.get_json() or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    product = amazon_api.fetch_product_by_url(url)
    if not product:
        return jsonify({"error": "Could not find a listing for that URL"}), 404
    return jsonify(product)


@app.route("/api/generate-listing", methods=["POST"])
def api_generate_listing():
    """Fetch the Amazon product data for a URL, generate an eBay-ready
    title + description from it, and save the result as a draft."""
    url = (request.get_json() or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    product = amazon_api.fetch_product_by_url(url)
    if not product:
        return jsonify({"error": "Could not find a listing for that URL"}), 404

    draft = build_listing_draft(product)

    ensure_draft_table()
    conn = get_connection()
    draft["id"] = db.insert_returning_id(
        conn,
        """INSERT INTO draft_listings
           (sku, source_asin, source_url, ebay_title, ebay_description_html, images_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (draft["sku"], draft["source_asin"], draft["source_url"],
         draft["ebay_title"], draft["ebay_description_html"], json.dumps(draft["images"])),
    )
    conn.commit()
    conn.close()

    return jsonify(draft)


@app.route("/api/drafts", methods=["GET"])
def api_drafts():
    """Return previously generated eBay listing drafts, newest first."""
    ensure_draft_table()
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM draft_listings ORDER BY id DESC"
    ).fetchall()
    conn.close()

    drafts = []
    for r in rows:
        d = dict(r)
        d["images"] = json.loads(d.pop("images_json") or "[]")
        drafts.append(d)
    return jsonify(drafts)


@app.route("/api/products", methods=["GET"])
def api_products():
    inventory.ensure_seeded()
    # With ?page=, one page of the shop (searchable); without it, every
    # product, as before.
    if "page" not in request.args:
        return jsonify(storefront.get_all_products())
    return jsonify(storefront.product_page(request.args.get("q", ""), _int_arg("page", 1),
                                           _int_arg("per_page", 24)))


@app.route("/api/products/<sku>", methods=["GET"])
def api_product(sku):
    product = storefront.get_product(sku)
    if not product:
        return jsonify({"error": "Not found"}), 404
    return jsonify(product)


@app.route("/api/checkout/create-session", methods=["POST"])
def api_create_session():
    body = request.get_json() or {}
    items = body.get("items", [])
    email = body.get("email", "").strip()
    if not items:
        return jsonify({"error": "Cart is empty"}), 400
    if not email:
        return jsonify({"error": "Email is required"}), 400
    try:
        session = checkout.create_session(items, email)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(session)


@app.route("/api/checkout/confirm", methods=["POST"])
def api_confirm_payment():
    body = request.get_json() or {}
    result = checkout.confirm_payment(
        session_id=body.get("session_id", ""),
        card_number=body.get("card_number", ""),
        exp=body.get("exp", ""),
        cvc=body.get("cvc", ""),
    )
    status_code = 200 if result["status"] == "success" else 402
    return jsonify(result), status_code


@app.route("/api/market-research/niches", methods=["GET"])
def api_niches():
    from mock_apis.market_data import NICHES
    return jsonify(NICHES)


@app.route("/api/market-research/<niche>", methods=["GET"])
def api_market_research(niche):
    insights = market_research.analyze_niche(niche)
    if not insights:
        return jsonify({"error": "Unknown niche"}), 404
    return jsonify(insights)


@app.route("/api/generate-content", methods=["POST"])
def api_generate_content():
    body = request.get_json() or {}
    niche = body.get("niche")
    product_facts = body.get("product_facts")
    if not niche or not product_facts:
        return jsonify({"error": "Missing 'niche' or 'product_facts'"}), 400

    insights = market_research.analyze_niche(niche)
    if not insights:
        return jsonify({"error": "Unknown niche"}), 404

    draft = draft_listing_copy(product_facts, insights)
    return jsonify({"draft": draft, "insights_used": insights})


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _period_arg():
    try:
        period = int(request.args.get("period", 30))
    except ValueError:
        period = 30
    return period if period in (30, 90) else 30


@app.route("/api/inventory", methods=["GET"])
def api_inventory():
    """All SKUs with stock, sales, profit, and turnover for the period."""
    inventory.ensure_seeded()
    args = request.args
    return jsonify(inventory.list_items(
        _period_arg(), q=args.get("q", ""), status=args.get("status", ""),
        sort=args.get("sort", "inventory_value"), direction=args.get("dir", "desc"),
        page=_int_arg("page", 1), per_page=_int_arg("per_page", 50)))


@app.route("/api/inventory/<sku>", methods=["GET"])
def api_inventory_item(sku):
    """One SKU's purchases/sales/stock reconciliation, profit, turnover,
    channel breakdown, chart series, and recent ledger rows."""
    inventory.ensure_seeded()
    detail = inventory.get_item_detail(sku, _period_arg())
    if not detail:
        return jsonify({"error": "Unknown SKU"}), 404
    return jsonify(detail)


@app.route("/api/inventory/import", methods=["POST"])
def api_import_from_store():
    """Add an item from its product page link on the store (public data:
    name, SKU, price, photos). Stock starts empty until recorded."""
    url = (request.get_json() or {}).get("url", "")
    try:
        result = store_import.import_from_url(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except store_import.StoreError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result), 201


@app.route("/api/new-item/read", methods=["POST"])
def api_new_item_read():
    """Facts from another seller's product link, plus copy written from them."""
    url = (request.get_json() or {}).get("url", "")
    try:
        result = marketplace_import.read_marketplace(url)
        return jsonify(result if result is not None else new_item.read_reference(url))
    except new_item.OwnStoreLink as e:
        return jsonify({"error": str(e), "own_store": True}), 409
    except marketplace_import.MarketplaceError as e:
        return jsonify({"error": str(e)}), 502
    except marketplace_import.NotConnected as e:
        return jsonify({"error": str(e), "manual_entry": True, "platform": e.platform,
                        "not_connected": True}), 400
    except new_item.NeedsApi as e:
        return jsonify({"error": str(e), "manual_entry": True, "platform": e.platform}), 400
    except new_item.ManualEntry as e:
        return jsonify({"error": str(e), "manual_entry": True}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except store_import.StoreError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/new-item/import-own-ebay", methods=["POST"])
def api_import_own_ebay():
    """Imports one of your own eBay listings as-is (photos included)."""
    url = (request.get_json() or {}).get("url", "")
    try:
        return jsonify(marketplace_import.import_own_ebay_listing(url)), 201
    except marketplace_import.MarketplaceError as e:
        return jsonify({"error": str(e)}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/new-item/preview", methods=["POST"])
def api_new_item_preview():
    """Regenerates the title and description from (edited) facts."""
    body = request.get_json() or {}
    facts = new_item.clean_facts(body.get("facts"))
    return jsonify(content_generator.draft_from_facts(facts, str(body.get("extra_notes") or "")[:2000]))


@app.route("/api/new-item", methods=["POST"])
def api_new_item_save():
    """Adds the new item to the catalog with no stock yet."""
    try:
        result = new_item.save_new_item(request.get_json() or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result), 201


@app.route("/api/inventory/<sku>/movements", methods=["POST"])
def api_record_movement(sku):
    """Record stock received from a supplier, or a manual adjustment."""
    body = request.get_json() or {}
    try:
        result = inventory.record_movement(
            sku,
            movement_type=body.get("type"),
            quantity=body.get("quantity"),
            unit_cost=body.get("unit_cost"),
            reference=body.get("reference"),
            note=body.get("note"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result), 201


if __name__ == "__main__":
    inventory.ensure_seeded()
    print("\n  Shop:   http://127.0.0.1:5050/")
    print("  Admin:  http://127.0.0.1:5050/admin")
    print(f"  Database: {db.describe()}")
    if app.config["ADMIN_PASSWORD_GENERATED"]:
        print(f"  Admin password for this run: {app.config['ADMIN_PASSWORD']}")
        print("  (Set ADMIN_PASSWORD in backend/.env to choose a permanent one.)\n")
    app.run(port=5050, debug=False)

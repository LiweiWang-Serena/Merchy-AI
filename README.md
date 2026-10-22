# SkuTrail — Multichannel Inventory, Orders & Procurement Reporting

A demo project simulating a real problem for sellers who list the same
products on **Amazon and eBay**: the two platforms track stock
independently, so quantities drift apart, and someone still has to turn
raw sales data into a procurement decision (what to reorder, what's
about to sell out, what's dead weight).

This project models that end to end: two mock marketplace APIs → a sync
service that reconciles them → a reporting layer that turns the merged
data into low-stock alerts, reorder recommendations, and a sales
velocity view → a small ops dashboard to look at all of it.

It runs as one website from one server:

| Address | Who | What it's for |
|---|---|---|
| `/` | Customers | Products with live stock ("Only 2 left"), best sellers, product pages |
| `/cart`, `/checkout` | Customers | Cart and test-mode checkout; each sale is written to the inventory ledger |
| `/admin` | You (sign-in) | Overview: last 30 days, top sellers, items needing attention, recent orders |
| `/admin/inventory` | You | Every item's stock, value, sales, margin and turnover; click through for its history |
| `/admin/add` | You | Add a product from a link: your store, eBay, Amazon or another Shopify store |
| `/admin/listings` | You | What Amazon and eBay show for each item, and where they disagree |
| `/admin/ebay` | You | Connect the store's eBay seller account; sync its listings and import its orders |
| `/admin/reports` | You | Product analysis, profit, sales, purchasing and inventory analysis; printable |
| `/admin/research` | You | Market research on product niches |

## Why it's built this way

Amazon's SP-API and eBay's Inventory API both require an approved seller
developer account to get real credentials, which isn't practical for a
portfolio project. `mock_apis/amazon_api.py` and `mock_apis/ebay_api.py`
return realistic sample payloads shaped like what those real APIs
return, sharing one product catalog but generating independent stock
levels — so the sync step has genuine discrepancies to reconcile, the
same way it would against real APIs. Swapping in real API calls later
only means rewriting those two files; `sync.py` and `reporting.py`
don't need to change.

## Connecting your real Shopify / Amazon / eBay accounts

(For reading single eBay and Amazon listings on the Add product page,
see "Adding products from eBay and Amazon links" under Running it.)

This project ships with mock data so it runs immediately with no
credentials. To point it at your real store instead:

1. `cp backend/.env.example backend/.env` and fill in the credentials
   for whichever platforms you're connecting (see the setup steps at
   the top of each file in `backend/integrations/` -- they walk through
   exactly where to get each credential).
2. Load the `.env` file before running the app (e.g. with
   `python-dotenv`, already in requirements.txt: add
   `from dotenv import load_dotenv; load_dotenv()` at the top of
   `app.py`, or export the variables in your shell).
3. Set `USE_REAL_APIS=true` in `.env`.
4. Run `python sync.py` -- it now calls `integrations/shopify_client.py`,
   `integrations/ebay_client.py`, and `integrations/amazon_sp_api_client.py`
   instead of the mocks, using the exact same reconciliation and
   reporting logic already built and tested against mock data.

**Setup effort is very different per platform** -- Shopify is minutes
since you already have the store; eBay is a few hours including its
OAuth consent flow; Amazon SP-API can take days to weeks because parts
of it require Amazon's approval. See each client file's docstring for
the exact steps and links.

**A note on Shopify specifically, since it's your real live store**:
`shopify_client.py` only requests `read_products`, `read_inventory`,
and `write_inventory` scopes -- it can't touch orders, customers,
themes, or your Stripe/payment settings. `update_inventory_level()` is
the only write operation, and it's not called automatically anywhere;
you'd wire it in deliberately (e.g. "after an Amazon sale, push the new
quantity to Shopify") rather than it running unattended against your
live store.

## Architecture

```
mock_apis/amazon_api.py  ──┐
mock_apis/ebay_api.py    ──┤──► sync.py ──► SQLite (listings, sync_log) ──► reporting.py ──► dashboard.html
                            │        │
                            │        └─ flags cross-channel stock discrepancies
                            └─ shared product catalog, independent stock/price
```

- **`sync.py`** — pulls both snapshots, writes normalized rows into a
  `listings` table, and flags any SKU where Amazon and eBay quantities
  disagree by more than a threshold (an oversell risk in real life).
- **`reporting.py`** — aggregates the synced listings by SKU across
  channels and computes: low-stock SKUs, fast movers, dead stock, and
  reorder recommendations based on a sales-velocity-vs-days-of-stock
  heuristic.
- **`app.py`** — a small Flask API (`/api/sync`, `/api/report`,
  `/api/fetch-amazon`, `/api/generate-listing`, `/api/drafts`) so the
  dashboard can trigger a sync, pull the latest report, and draft new
  eBay listings from an Amazon URL.
- **`listing_generator.py`** — turns fetched product data (title, bullet
  points, photos) into an eBay-ready title (respecting eBay's 80-char
  limit) and HTML description.
- **`dashboard.html`** — a static page (Chart.js for the sales-velocity
  chart) that calls the API and renders the ledger.
- **`create-listing.html`** — paste an Amazon listing URL, preview the
  generated eBay title/description/photos, and save it as a draft.
- **`store.html` / `product.html` / `cart.html` / `checkout.html`** —
  the customer-facing storefront: browse products (seeded from the same
  catalog used by the Amazon/eBay mocks and the listing generator), add
  to cart, and check out.
- **`market-research.html`** — pick a category, see structured
  competitive facts about the top-selling listings (price, rating,
  review count, common theme frequency), and generate original listing
  copy for your own product based on those patterns.

### Market research and content generation: the legitimate alternative to scraping

An earlier idea for this project was pasting a competitor's listing URL
and having AI lightly reword their photos/description to avoid
plagiarism detection. That's not something this project does, and it's
worth being explicit about why: the images and description text on a
listing are that seller's copyrighted work, and "rewording to avoid
detection" is still reproducing someone else's creative output, not
producing something original -- it also risks the account doing it
getting suspended once a platform's detection improves.

What `market_research.py` and `content_generator.py` do instead:

- `mock_apis/market_data.py` simulates the kind of **structured,
  non-copyrightable facts** a real market-data source (Amazon's
  Product Advertising API, or a licensed tool like Jungle Scout)
  provides: price, rating, review count, and **keyword frequency**
  (e.g. "'quiet' appears in 40% of top listings"). A statistic about
  text isn't a reproduction of it.
- `market_research.analyze_niche()` turns that into insights: price
  positioning, which themes top sellers lean on, and which recognized
  themes are comparatively underused (a real differentiation signal).
- `content_generator.draft_listing_copy()` writes **original** title
  and description text from those insights plus the seller's own
  product facts -- it never touches another listing's actual wording.
  In production, swap this for a real LLM call (Claude/Gemini) with a
  prompt that explicitly instructs it not to reference or reproduce any
  specific competitor's listing.

### The storefront and checkout

`storefront.py` seeds a `products` table from the same product catalog
used everywhere else in this project, so the storefront, the channel
sync, and the eBay listing generator are all describing one underlying
catalog -- the way a real seller's systems should agree with each other.

`checkout.py` simulates a Stripe Checkout flow closely enough that
swapping in the real `stripe` Python SDK is a small, mechanical change:

- `create_session()` looks up live prices/stock **server-side** (never
  trusts a price the browser sends) and stores a pending order -- this
  is what `stripe.checkout.Session.create(...)` would do in production.
- `confirm_payment()` validates the card against Stripe's own published
  [test card numbers](https://stripe.com/docs/testing) (`4242...` for a
  successful charge, `4000...0002` for a decline, etc.), so the
  success/decline paths behave the same way they would against Stripe's
  real test mode. In production, Stripe handles the actual card entry
  and you'd instead listen for a `checkout.session.completed` webhook.
- On success, it decrements `products.inventory_qty` so the storefront,
  the channel ledger, and procurement report all reflect the sale.

To go from this demo to a real integration: create a Stripe account,
install `stripe`, replace `create_session()`'s body with a real
`stripe.checkout.Session.create(...)` call, and add a webhook endpoint
for `checkout.session.completed` instead of the `/api/checkout/confirm`
route (which exists here only because there's no real Stripe-hosted
page to redirect to in a local demo).

### A note on the "paste a link, generate a listing" feature

This is built for the legitimate version of that workflow: reusing your
**own** Amazon listing's title, bullets, and photos to draft the same
product on eBay. It is not a scraper aimed at other sellers' listings —
copying another seller's or brand's images/description onto your own
listing raises real copyright and marketplace ToS issues. In production,
`mock_apis/amazon_api.py`'s `fetch_product_by_url()` would be replaced
with a call to your own Amazon Seller Central data via the SP-API
Catalog/Listings endpoints, and `listing_generator.py`'s
`generate_ebay_description_html()` is a natural place to plug in an LLM
call (Claude/Gemini) for more natural rewriting instead of the
template-based version used here.

### Inventory: purchases, sales, stock, profit and turnover

`frontend/inventory.html` lists every item with its stock, sales,
margin, turnover and a status (healthy, low stock, out of stock,
overstock, no recent sales). Selecting an item opens
`inventory-item.html`: its photo (or SKU code when there's no photo),
the stock balance for the period, a profit statement, turnover figures,
charts, a per-channel breakdown, a form to record stock received or an
adjustment, and the movement ledger.

**The ledger is the source of truth.** Every change to physical stock is
a row in `inventory_movements`: a purchase (stock received, with unit
cost), a sale (with channel, price and fee), or an adjustment (damage,
count corrections). On-hand quantity is the running total of the ledger,
so it can always be explained and it always balances:

    opening stock + received − sold ± adjusted = closing stock

Website checkout writes its sale rows in the same database transaction
that decrements the storefront's stock, and the item page reports if the
two ever disagree. Marketplace listings are compared against physical
stock too: if eBay lists more units than you actually have, the item
page flags the oversell risk.

**How the numbers are calculated** (`backend/inventory.py`):

- *Cost* uses the moving weighted-average method. Each purchase
  re-averages the unit cost; each sale's cost of goods sold (COGS) is
  its quantity times the average cost at that moment.
- *Net profit* = revenue − COGS − channel fees − write-offs.
- *Turnover* = COGS ÷ average stock value (end-of-day values across the
  period). *Days of inventory* = period length ÷ turnover.
  *Sell-through* = units sold ÷ (opening stock + units received).
- *Sales pace* divides recent sales by the days the item was actually in
  stock, not by 30 calendar days. Otherwise a stockout makes demand look
  lower than it is and shrinks the very reorder that would fix it
  (see `test_stockout_days_do_not_dilute_sales_pace`).
- *Reorder suggestions* appear when less than 14 days of stock remain
  and aim to restore 45 days of stock.

**Fees are sample rates**, set in `FEE_RULES` (Amazon 15%, eBay 13.25%
plus $0.40 per order, Stripe 2.9% plus $0.30). Check each platform's
current fee schedule for your category before relying on the profit
figures.

**Demo data.** On first run, about four months of history is generated
per item, deliberately including an overstocked item, one running low,
one that stopped selling and one that ran out, so every status appears.
Delete `backend/inventory.db` to regenerate it. With real accounts, sale
rows would come from each platform's orders API and purchases from your
supplier invoices; the calculations don't change.

**Adding items from your store by link.** On the inventory page, paste a
product link from the store (set by `STORE_DOMAIN` in `backend/.env`,
set with STORE_DOMAIN in backend/.env) and choose Import item. The backend
reads the product's public storefront data, the same JSON every Shopify
store serves at `/products/<handle>.json`, and adds each variant as its
own SKU with its real name, price and photos. Links from any other store
are rejected, so this imports your own catalog rather than copying other
sellers' listings. Stock, sales and cost aren't public, so an imported
item starts as "Not stocked yet" until you record the stock you have;
live numbers need the Shopify Admin API with the store owner's
authorization. Descriptions are reduced to plain text on import, so no
markup from the store is ever rendered. To track only imported items,
put `DEMO_DATA=false` in `backend/.env` and delete `inventory.db`.

**New items from another seller's link (facts only).** `frontend/new-item.html`
(linked from the inventory page) is for parts you want to sell that you
found at another seller. Paste their product link and the backend reads
only the product's *facts*: what the part is, the brands, engine sizes,
equipment, model numbers and years it fits, its specifications, and the
seller's price. You review and edit those facts, and a title and
description are written fresh from them (`content_generator.draft_from_facts`),
stating only what the facts say, with an aftermarket note so buyers
aren't misled about who made the part. Then you add your own SKU, price
and photos and save it to inventory; the reference price is kept for
comparison on the item page.

What's never taken from the other listing: its wording (the description
is read only to find facts, then discarded), its photos, its barcode (a
UPC identifies *their* product, not yours) and its shipping weight. Links
from Shopify stores are read automatically; eBay and Amazon block
automated page reading, so their facts are entered by hand until their
APIs are connected. Because this fetches links from any site, the
backend refuses links that resolve to private or local network
addresses, re-checks every redirect, and caps the response size.

## Running it

```bash
cd backend
python3 -m venv .venv        # once: create a virtual environment
source .venv/bin/activate    # every new terminal (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
python app.py
```

Works on Python 3.9 and later, including the `python3` that ships with macOS.

The terminal then shows two addresses:

- **Shop:** http://127.0.0.1:5050/
- **Admin:** http://127.0.0.1:5050/admin

The admin area asks for a password. Set your own with `ADMIN_PASSWORD`
in `backend/.env`; if you don't, the terminal prints a new random
password each time the server starts. The shop and checkout stay public.

Everything is served by the Flask server, so open the pages through
these addresses rather than double-clicking the HTML files.

### AI customer support

Every shop page has a chat in the corner, answered by Claude (Claude API)
or Gemini (Gemini API), whichever key is set (`support_agent.py`). It says it's an AI assistant and offers
a person at any time. Admin > Support is where it's "trained":

- **Articles**: policies it may tell customers (shipping, returns,
  fitment help...). It won't answer policy questions they don't cover;
  it offers a person instead. The four starting articles are examples,
  flagged until you edit them.
- **Corrections**: review real conversations, mark answers, and write
  the answer you'd want. Corrections are shown to the assistant as
  examples from then on.
- **Name, greeting and tone.**

Facts come from tools, not from memory: it searches products, reads
price and stock, looks up an order only when the order number and
checkout email both match, and hands a conversation to the team when
asked. Visitors are limited to 12 messages a minute and 40 per
conversation, to protect the API bill.

When a conversation is handed to a person, the AI goes quiet and the
customer is told a person will reply in the chat. The team is alerted by
Slack (`SLACK_WEBHOOK_URL`) and/or email (the `SMTP_` settings), with a
link that opens the conversation; while the customer waits, a reminder
goes out at most every 10 minutes. On the Support page a person replies
straight into the customer's chat window (shown with their name, and the
window checks for replies every few seconds), then marks the
conversation resolved to hand it back to the AI, which sees what the
person said.

Set `ANTHROPIC_API_KEY` (from console.anthropic.com) or `GEMINI_API_KEY`
(from aistudio.google.com) in `backend/.env` to turn it on; with both,
`SUPPORT_PROVIDER` picks one. `SUPPORT_MODEL` chooses the model (defaults:
`claude-haiku-4-5-20251001` for Claude, `gemini-2.5-flash` for Gemini).
Gemini's free tier works for trying it out, but Google uses free-tier
content to improve its products, so turn on billing before real
customers chat. Without a key, the chat answers only with the
best-matching article.

### The shop home page in React + TypeScript

`frontend-react/` has the shop home page rewritten in React + TypeScript
(Vite build), to compare with the plain-JavaScript original. After
`npm install` and `npm run build` there, Flask serves it at `/react/`,
next to the original at `/`; both share the API, stylesheet and cart.
See `frontend-react/README.md`.

### Using PostgreSQL

The app uses SQLite by default (the file `backend/inventory.db`, nothing
to install). Set `DATABASE_URL` and it uses PostgreSQL instead; the rest
of the code is the same either way (see `db.py`). On a Mac, the simplest
PostgreSQL is Postgres.app:

1. Download it from postgresapp.com, move it to Applications, open it and
   click **Initialize**. It runs a server on your Mac with your macOS user
   name and no password.
2. Make its command-line tools available, then open a new Terminal window:
   `sudo mkdir -p /etc/paths.d && echo /Applications/Postgres.app/Contents/Versions/latest/bin | sudo tee /etc/paths.d/postgresapp`
3. Create the database: `createdb channel_ledger`
4. In `backend/.env`: `DATABASE_URL=postgresql://localhost/channel_ledger`
5. Install the driver and check the connection (this uses a throwaway
   schema and doesn't touch any data):
   ```bash
   pip install -r requirements.txt
   python db_check.py
   ```
6. Copy your existing data from SQLite (the SQLite file stays as a backup):
   `python migrate_to_postgres.py`
7. Start the app as usual; the terminal shows which database it's using.

To go back to SQLite, remove the `DATABASE_URL` line.

To run the test suite against PostgreSQL, create a second, empty database
and point `TEST_DATABASE_URL` at it; each test uses its own throwaway
schema there. Tests never use `DATABASE_URL`.

```bash
createdb channel_ledger_test
TEST_DATABASE_URL=postgresql://localhost/channel_ledger_test python -m pytest tests/ -q
```

### Trying it at a real store's scale

```bash
cd backend
python demo_catalog.py              # 10,000 items; or --skus 3000
```

This replaces the database with a large demo catalog: powersports and
small-engine parts with 120 days of history and the sales pattern of a
real multichannel parts seller (a few fast movers, a middle group, and a
long tail that sells a few times a year), plus restocks, stockouts and
overstock. It backs up the current database first as
`inventory-backup-<date-time>.db`; to go back, stop the server and rename
that file to `inventory.db`.

### Built for tens of thousands of items

- **One query, light metrics, a cache.** Lists, the overview and reports
  load the whole ledger in one query and use a light version of the
  per-item calculation that skips the daily chart series. A test checks
  that it gives exactly the same figures as the item page, to the last
  decimal. Results are cached until the ledger changes (any new movement)
  or ten minutes pass.
- **Paging on the server.** The inventory list is searched, filtered by
  status and sorted on the server, 50 items at a time; the shop pages and
  searches in SQL. The page state lives in the address bar, so filtered
  links and the Back button work.
- **Trimmed reports, complete counts.** Long report lists show the top
  rows (100 products, the 100 most urgent reorders, the 20 costliest
  stockouts) with the full count and totals; the CSV export has every item.
- **Imports that don't lock up.** eBay orders are read one month at a time
  (oldest first, with progress on the page) and written in batches;
  channel syncs fetch everything before opening a short write transaction.

At 10,000 items with about 48,000 ledger entries, the inventory list
loads in about 0.1 s from cache (about 1.4 s after a change), reports in
about 0.3 s, and the shop in under 0.2 s.

### Admin sign-in

`auth.py` protects every `/admin` page and every admin API endpoint in
one `before_request` hook, with an explicit list of the few public
endpoints (shop products, best sellers, checkout). A new admin endpoint
is therefore protected automatically; forgetting a decorator can't
expose it. The password check uses a constant-time comparison, the
login cookie is signed and HTTP-only, and the pages and API share one
origin, so no cross-site access is allowed at all.

### Reports

`reports.py` builds everything on the Reports page from the inventory
ledger (the same `compute_metrics` the item pages use, so the numbers
always agree):

- **Sales and profit:** revenue to net profit after fees and write-offs,
  weekly trend, and profit by channel.
- **Product analysis:** every item ranked by revenue with ABC classes
  (A = the items making up the first 80% of revenue, B the next 15%, C
  the rest), compared against each class's share of stock value to show
  cash tied up in slow sellers. Top sellers by units and by profit.
- **Purchasing:** deliveries, units and spend in the period, cost
  changes per item, whether stock is building up or running down
  (spend vs. cost of goods sold), and a reorder plan priced at current
  average cost, with a total budget.
- **Inventory analysis:** stock by status, how long current stock will
  last (by value), excess stock beyond 45 days of sales, and items out
  of stock with their estimated lost sales per week.

### Connecting the store's eBay account

`ebay_account.py` connects the store's own eBay seller account, read-only,
through eBay's OAuth consent flow: the account owner signs in on eBay and
clicks Agree, so nobody shares a password. Once connected:

- **Sync listings** brings in each active listing's quantity and price by
  SKU (Trading API `GetMyeBaySelling`, which also covers listings created
  in Seller Hub). The Listings page and the oversell checks then use the
  real numbers, and listings missing from the catalog can be added.
- **Import orders** records paid eBay orders (Fulfillment API `getOrders`,
  up to two years back) as sales in the inventory ledger, with eBay's
  actual fees. Each order line is recorded once, so re-running is safe.
  Canceled and unpaid orders are skipped. A sale that would take stock
  below zero at the time it happened is skipped and listed, so the stock
  can be recorded first. Buyer names and addresses are never stored.

Setup, starting in eBay's Sandbox (its test environment):

1. At developer.ebay.com, create a Sandbox keyset. Put its App ID and
   Cert ID in `backend/.env` as `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`,
   and set `EBAY_ENVIRONMENT=sandbox`.
2. On the keyset's User Tokens page, choose "Get a Token from eBay via
   Your Application" and add an eBay Redirect URL. Keep eBay's own
   confirmation page as the auth accepted URL: eBay only accepts HTTPS
   addresses and rejects localhost. Turn on OAuth and copy the RuName
   into `EBAY_RUNAME`.
3. Create a Sandbox test user to sign in with, restart the server, and
   open Admin > eBay. After clicking Agree on eBay, paste the address of
   eBay's confirmation page back into the admin page within 5 minutes.

To connect the real store later, the owner agrees on the production
keyset instead (`EBAY_ENVIRONMENT=production` with production keys and
RuName). On an HTTPS deployment, the RuName's accepted URL can point at
`https://your-domain/ebay/callback`, which completes the connection
without copying the address. The refresh token lasts about 18 months and
is stored in `backend/inventory.db`, so keep that file private; the owner
can withdraw access at any time in their eBay account settings.

### Adding products from eBay and Amazon links

`marketplace_import.py` reads eBay links (the item number in `/itm/…`)
through the eBay Browse API and Amazon links (the ASIN in `/dp/…`)
through the SP-API Catalog Items API. The same rule applies as for other
Shopify stores: another seller's listing gives facts only (what the part
fits, specifications, price), never their wording, photos or part
numbers. Your own eBay listings (seller matches `EBAY_SELLER_USERNAME`)
can be imported as they are, photos included.

Until you add API keys to `backend/.env`, two sample links on the Add
product page return demo data, clearly labeled as such:

- **eBay:** a free developer account at developer.ebay.com gives the
  client ID and secret. eBay says production Buy API access is intended
  for partners and may need approval through the eBay Partner Network.
- **Amazon:** only the primary user of an Amazon Professional seller
  account can register for SP-API. The catalog has no prices.

Run the test suite:

```bash
cd backend
pytest tests/ -v
```

You can also run the pipeline without the API, straight from the
command line:

```bash
cd backend
python sync.py        # pulls from both mock APIs, prints discrepancy count
python reporting.py   # prints the full report as JSON
```

## One important design fix worth calling out

Early on, the storefront's own sales and the Amazon/eBay sync were two
disconnected systems: a sale on your own site updated `products`, but
the channel ledger only read from `listings` (populated by syncing
Amazon/eBay). That meant direct website sales were invisible to the
procurement report -- exactly the kind of blind spot a real multi-channel
seller can't afford.

The fix: `checkout.py` now also upserts a `'website'` row per SKU into
the same `listings` table Amazon/eBay sync into, and `sync.py` only
clears the `amazon`/`ebay` rows on each run (not the whole table), so
website sales persist across syncs instead of being wiped. The result:
`reporting.py` treats your own site as a third channel, same as Amazon
and eBay, with no special-casing needed since it already aggregates by
SKU across whatever channels exist.

## What this demonstrates

- Reconciling data from multiple third-party sources into one source of truth
- Turning raw operational data into an actual decision (reorder qty, not just a dashboard number)
- A backend structured so the mock data sources are swappable for real API integrations later
- A small full-stack slice: mock external APIs → sync logic → SQLite → REST API → dashboard UI

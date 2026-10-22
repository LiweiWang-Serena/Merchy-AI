"""
New items from a reference link.

Paste another seller's product link and get that product's *facts*: what
the part is, which brands, engine sizes, equipment and model numbers it
fits, its specifications, and the seller's price. A title and description
are then written fresh from those facts (content_generator.py), and you
add your own photos, SKU and price.

Taken from the reference: facts only. Fitment and part numbers are facts,
and every seller of the same part states the same ones.
Never taken: the other seller's wording and photos. Their description is
read only to find facts in it and is then discarded. Photos, barcodes
(a UPC identifies *their* product, not yours) and shipping weights are
not imported at all.

Links read automatically: product pages on Shopify stores, which serve
their product data publicly at /products/<handle>.json. eBay and Amazon
block automated page reading; their data will come through their APIs
(integrations/), and until then the facts can be entered by hand.

Because this fetches links from any site, the fetch refuses private and
local network addresses (so a pasted link can't make the server probe
your own network), follows at most a few redirects, re-checking each
one, and caps the response size.
"""

import ipaddress
import json
import re
import socket
from datetime import datetime
from urllib.parse import urljoin, urlparse

import content_generator
import store_import
from db import get_connection, ensure_storefront_tables

MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 3
MAX_PHOTOS = 8
SKU_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$")

# Brands and equipment common in powersports and small-engine parts.
# Matching is case-insensitive and tolerates spaces or hyphens
# ("TaoTao", "Tao-Tao"); results keep the spelling below.
BRANDS = [
    "Harbor Freight", "Predator", "Honda", "Yamaha", "Kawasaki", "Suzuki", "Polaris",
    "Arctic Cat", "Can-Am", "Coleman", "Baja", "Tao Tao", "Kazuma", "Coolster", "SSR",
    "Apollo", "Kayo", "Hisun", "Massimo", "Briggs & Stratton", "Tecumseh", "Kohler",
    "Champion", "Generac", "Ryobi", "Craftsman", "Husqvarna", "Stihl", "Echo",
    "Troy-Bilt", "Toro", "DuroMax", "Hammerhead", "Roketa", "Sunl", "Hensim",
]
EQUIPMENT = [
    "Earth Auger", "Auger", "Go Kart", "Mini Bike", "Pit Bike", "Dirt Bike", "ATV", "UTV",
    "Scooter", "Moped", "Motorcycle", "Generator", "Pressure Washer", "Water Pump",
    "Tiller", "Lawn Mower", "Snow Blower", "Chainsaw", "Trimmer", "Leaf Blower",
    "Log Splitter", "Outboard Motor", "Engine",
]
# Spec lines about the other seller's business, not the product.
NOT_PRODUCT_FACTS = {
    "shipping", "delivery", "returns", "return", "return policy", "payment", "warranty",
    "guarantee", "note", "notes", "notice", "warning", "service", "contact", "about us",
    "why buy", "why choose us", "quality", "features", "feature", "description", "tips",
}
FITMENT_WORDS = re.compile(r"\b(fits?|fitment|compatible|replac\w*)\b", re.IGNORECASE)
YEAR = r"(?:19[5-9]\d|20[0-3]\d)"
MEASUREMENT = re.compile(
    r"\d+(\.\d+)?(MM|CM|M|IN|INCH|INCHES|FT|ML|L|OZ|LB|LBS|KG|G|V|W|A|AH|HP|RPM|T|PCS|PC|PACK)"
)


class ManualEntry(ValueError):
    """The link is fine but its facts can't be read automatically; the
    page should switch to entering them by hand."""


class OwnStoreLink(ValueError):
    """The link is from your own store, which can be imported in full."""


class NeedsApi(ManualEntry):
    def __init__(self, platform):
        self.platform = platform
        super().__init__(
            f"{platform} blocks automated page reading, so this link can't be read yet. "
            f"It will work once the {platform} API is connected. For now, enter the facts by hand below."
        )


# ---------------------------------------------------------------------------
# Reading a link
# ---------------------------------------------------------------------------

def parse_reference_url(url):
    """Returns (host, product handle) for a Shopify product page link."""
    text = (url or "").strip()
    if not text:
        raise ValueError("Paste a product link first.")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Paste the full product link, starting with https://")
    host = parsed.hostname.lower()

    platform = _api_only_platform(host)
    if platform:
        raise NeedsApi(platform)
    own = store_import.store_domain()
    if own and host in (own, f"www.{own}"):
        raise OwnStoreLink("That's a link from your own store, so it can be imported in full, "
                           "photos included.")
    match = store_import.HANDLE_PATTERN.search(parsed.path.lower())
    if not match:
        raise ManualEntry("That link isn't a product page on a Shopify store, so its facts "
                          "can't be read automatically. Enter them by hand below.")
    return host, match.group(1)


def read_reference(url):
    host, handle = parse_reference_url(url)
    product = _fetch_product_json(host, handle)
    facts = extract_facts(product, host, f"https://{host}/products/{handle}")
    return {"facts": facts, "draft": content_generator.draft_from_facts(facts)}


def _api_only_platform(host):
    if host in ("a.co", "amzn.to", "amzn.com"):
        return "Amazon"
    names = {"ebay": "eBay", "amazon": "Amazon", "walmart": "Walmart"}
    for label in host.split(".")[:-1]:
        if label in names:
            return names[label]
    return None


def _require_public_host(host):
    """Refuses hosts that resolve to private, loopback, link-local or
    otherwise non-public addresses."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ValueError(f"Couldn't find the site {host}. Check the link.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise ValueError("That link points to a private or local network address, so it can't be read.")


def _fetch_product_json(host, handle):
    import requests  # only needed for live reads

    url = f"https://{host}/products/{handle}.json"
    for _ in range(MAX_REDIRECTS + 1):
        _require_public_host(urlparse(url).hostname)
        try:
            resp = requests.get(url, timeout=store_import.TIMEOUT_SECONDS, allow_redirects=False,
                                stream=True, headers={"User-Agent": store_import.USER_AGENT,
                                                      "Accept": "application/json"})
        except requests.RequestException:
            raise store_import.StoreError(f"Couldn't reach {host}. Check the link and your internet connection.")
        if not resp.is_redirect:
            break
        url = urljoin(url, resp.headers.get("Location", ""))
        resp.close()
        if urlparse(url).scheme != "https":
            raise store_import.StoreError("The site redirected to an insecure address, so it wasn't read.")
    else:
        raise store_import.StoreError("The site redirected too many times, so it wasn't read.")

    try:
        if resp.status_code == 404:
            raise ManualEntry("That page isn't a product on a Shopify store, so its facts can't be "
                              "read automatically. Enter them by hand below.")
        if resp.status_code == 429:
            raise store_import.StoreError("The site is limiting requests right now. Wait a minute and try again.")
        if not resp.ok:
            raise store_import.StoreError(f"The site returned an error (HTTP {resp.status_code}). Try again later.")
        body = resp.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
    finally:
        resp.close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise store_import.StoreError("The site's response was too large to read.")
    try:
        return json.loads(body)["product"]
    except (ValueError, KeyError, TypeError):
        raise ManualEntry("That page isn't a Shopify product page, so its facts can't be read "
                          "automatically. Enter them by hand below.")


# ---------------------------------------------------------------------------
# Facts (pure functions, covered by tests/test_new_item.py)
# ---------------------------------------------------------------------------

def extract_facts(product, site, url):
    title = " ".join((product.get("title") or "").split())
    part_name, fit_text = _split_title(title)
    lines = store_import.html_to_lines(product.get("body_html") or "")
    tags = product.get("tags") or ""
    tags = ", ".join(tags) if isinstance(tags, list) else str(tags)

    fit_blob = " ".join([fit_text] + [l for l in lines if FITMENT_WORDS.search(l)] + [tags])
    search_text = f"{title} {fit_blob}"
    years, without_years = _years(search_text)
    engine_sizes = _unique(f"{n}cc" for n in re.findall(r"\b(\d{2,4})\s?cc\b", search_text, re.IGNORECASE))

    prices = [float(v["price"]) for v in product.get("variants") or [] if v.get("price")]
    return {
        "part_name": part_name,
        "fits": {
            "brands": _find_terms(search_text, BRANDS),
            "engine_sizes": engine_sizes,
            "equipment": _find_terms(search_text, EQUIPMENT),
            "model_numbers": _model_numbers(without_years),
            "years": years,
        },
        "specs": _specs(lines),
        "aftermarket": True,
        "reference": {"site": site, "url": url, "price": round(min(prices), 2) if prices else None},
    }


def clean_facts(facts):
    """Normalizes facts sent back by the page before generating copy or
    saving, so nothing but short plain strings gets through."""
    facts = facts if isinstance(facts, dict) else {}
    fits = facts.get("fits") if isinstance(facts.get("fits"), dict) else {}

    def strings(values, limit=20, length=60):
        values = values if isinstance(values, list) else []
        return _unique(" ".join(str(v).split())[:length] for v in values[:limit] if str(v).strip())

    specs = []
    for spec in (facts.get("specs") if isinstance(facts.get("specs"), list) else [])[:20]:
        if isinstance(spec, dict):
            name = " ".join(str(spec.get("name", "")).split())[:40]
            value = " ".join(str(spec.get("value", "")).split())[:120]
            if name and value:
                specs.append({"name": name, "value": value})

    ref = facts.get("reference") if isinstance(facts.get("reference"), dict) else {}
    ref_url = str(ref.get("url") or "")
    try:
        ref_price = round(float(ref["price"]), 2) if ref.get("price") is not None else None
    except (TypeError, ValueError):
        ref_price = None
    return {
        "part_name": " ".join(str(facts.get("part_name") or "").split())[:120],
        "fits": {key: strings(fits.get(key)) for key in
                 ("brands", "engine_sizes", "equipment", "model_numbers", "years")},
        "specs": specs,
        "aftermarket": facts.get("aftermarket") is not False,
        "reference": {
            "site": " ".join(str(ref.get("site") or "").split())[:120] or None,
            "url": ref_url[:500] if urlparse(ref_url).scheme in ("http", "https") else None,
            "price": ref_price,
        },
    }


def _split_title(title):
    parts = re.split(r"\s+(?:for|fits?|compatible with)\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(" -|,"), parts[1]
    return title, ""


def _find_terms(text, vocabulary):
    """Known terms in order of appearance; longer terms first, so
    "Earth Auger" isn't also counted as "Auger"."""
    found, remaining = [], text
    for term in sorted(vocabulary, key=len, reverse=True):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term).replace(r"\ ", r"[\s-]?") + r"(?![A-Za-z0-9])"
        match = re.search(pattern, remaining, re.IGNORECASE)
        if match:
            found.append((match.start(), term))
            remaining = re.sub(pattern, lambda m: " " * len(m.group(0)), remaining, flags=re.IGNORECASE)
    return [term for _, term in sorted(found)]


def _years(text):
    years = []
    for start, end in re.findall(rf"\b({YEAR})\s*[-–]\s*({YEAR})\b", text):
        years.append(f"{start}-{end}")
    text = re.sub(rf"\b{YEAR}\s*[-–]\s*{YEAR}\b", " ", text)
    years += re.findall(rf"\b({YEAR})\b", text)
    return _unique(years), re.sub(rf"\b{YEAR}\b", " ", text)


def _model_numbers(text):
    found = []
    for token in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", text):
        token = token.upper()
        if re.fullmatch(r"\d{4,7}(?:-\d{4,7})+", token):
            found += token.split("-")                 # "56257-57341" is two item numbers
        elif re.fullmatch(r"\d{4,7}", token):
            found.append(token)                       # item numbers like 56257
        elif (re.search(r"\d", token) and re.search(r"[A-Z]", token) and 3 <= len(token) <= 14
              and not re.fullmatch(r"\d{2,4}CC", token) and not MEASUREMENT.fullmatch(token)
              and not re.fullmatch(r"\d+X\d+", token)):
            found.append(token)                       # model codes like GX160, TRX450R
    return _unique(found)


def _specs(lines):
    specs = []
    for line in lines:
        match = re.fullmatch(r"([A-Za-z][A-Za-z /&()-]{1,30}):\s*(.{1,60})", line)
        if not match:
            continue
        name, value = match.group(1).strip(), match.group(2).strip()
        if name.lower() in NOT_PRODUCT_FACTS or FITMENT_WORDS.search(name):
            continue
        specs.append({"name": name, "value": value})
    return specs[:12]


def _unique(values):
    seen, out = set(), []
    for v in values:
        key = str(v).lower()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------



def save_new_item(payload):
    """Adds the item to your catalog with no stock (record stock received
    on its inventory page). The description is always regenerated here
    from the cleaned facts, never taken from the page."""
    payload = payload if isinstance(payload, dict) else {}
    sku = str(payload.get("sku") or "").strip()
    if not SKU_PATTERN.fullmatch(sku):
        raise ValueError("Choose a SKU of 2 to 40 letters, numbers, dots, dashes or underscores.")
    title = " ".join(str(payload.get("title") or "").split())
    if not title:
        raise ValueError("Add a title.")
    if len(title) > 80:
        raise ValueError("Keep the title to 80 characters or fewer. eBay and many other marketplaces cut off longer titles.")
    try:
        price = round(float(payload.get("price")), 2)
    except (TypeError, ValueError):
        raise ValueError("Enter your price.")
    if not 0 < price < 100_000:
        raise ValueError("Enter a price greater than zero.")

    facts = clean_facts(payload.get("facts"))
    notes = str(payload.get("extra_notes") or "")[:2000]
    photos = _photo_urls(payload.get("photo_urls"))
    draft = content_generator.draft_from_facts(facts, notes)

    ensure_storefront_tables()
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone():
            raise ValueError(f"You already have an item with SKU {sku}. Choose a different SKU.")
        conn.execute(
            """INSERT INTO products (sku, title, brand, description_html, images_json, price,
               inventory_qty, source_url) VALUES (?, ?, NULL, ?, ?, ?, 0, NULL)""",
            (sku, title, draft["description_html"], json.dumps(photos), price))
        ref = facts["reference"]
        conn.execute(
            """INSERT INTO product_references (sku, reference_site, reference_url, reference_price,
               facts_json, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (sku, ref["site"], ref["url"], ref["price"], json.dumps(facts),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    return {"sku": sku, "title": title}


def _photo_urls(value):
    if isinstance(value, str):
        value = value.splitlines()
    urls = [str(u).strip() for u in (value if isinstance(value, list) else []) if str(u).strip()]
    if len(urls) > MAX_PHOTOS:
        raise ValueError(f"Add at most {MAX_PHOTOS} photo links.")
    for u in urls:
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or len(u) > 500:
            raise ValueError("Each photo link should be a full web address starting with https://")
    return urls

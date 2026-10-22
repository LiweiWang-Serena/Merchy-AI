"""
Products from eBay and Amazon links, through their official APIs.

Same rule as new_item.py: another seller's listing gives facts only --
what the part is, what it fits, its specifications and price -- never
their wording, photos or identifiers. The one exception is your own eBay
listing (its seller matches EBAY_SELLER_USERNAME in backend/.env), which
is your content and can be imported as-is, photos included.

Without API keys, only the two demo sample links work, and the page
labels their results as demo data.
"""

import os
import re
from urllib.parse import urlparse

import content_generator
import new_item
import store_import
from integrations.amazon_catalog import AmazonApiError, AmazonCatalogClient, AmazonNotFound
from integrations.ebay_browse import EbayApiError, EbayBrowseClient, EbayNotFound
from mock_apis import marketplace_samples

EBAY_ITEM_PATH = re.compile(r"/itm/(?:[^/]+/)?(\d{9,15})(?:[/?#]|$)")
AMAZON_ASIN_PATH = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)

# eBay item specifics that say what the part fits...
FIT_ASPECTS = {
    "brands": ("compatible equipment make", "compatible make", "compatible brand", "fits make"),
    "equipment": ("compatible equipment type", "compatible vehicle type"),
    "model_numbers": ("compatible model", "compatible equipment model", "fits model"),
    "engine_sizes": ("engine size", "engine displacement", "displacement"),
    "years": ("compatible year", "year", "years"),
}
# ...and ones that identify the other seller's product or state their
# terms. These are never imported.
NOT_IMPORTED_ASPECTS = {
    "brand", "mpn", "manufacturer part number", "upc", "ean", "isbn", "gtin", "type",
    "warranty", "manufacturer warranty", "custom bundle", "modified item",
    "non-domestic product", "california prop 65 warning", "country/region of manufacture",
}
# Amazon catalog attributes read as specifications.
AMAZON_SPECS = {
    "material": "Material", "color": "Color", "size": "Size", "item_length": "Length",
    "capacity": "Capacity", "voltage": "Voltage", "wattage": "Wattage",
    "fuel_type": "Fuel type", "item_shape": "Shape",
}


class NotConnected(new_item.ManualEntry):
    def __init__(self, platform):
        self.platform = platform
        keys = {"eBay": "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET",
                "Amazon": "the SP_API_ credentials"}[platform]
        super().__init__(
            f"The {platform} API isn't connected yet, so this link can't be read. Add {keys} "
            f"to backend/.env, or try the {platform} sample link to see how it works. "
            "For now, enter the facts by hand below.")


class MarketplaceError(Exception):
    """The marketplace API couldn't be reached or refused the request."""


def parse_marketplace_url(url):
    """("ebay", item number) or ("amazon", ASIN) for a single listing;
    None for links from other sites."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    platform = new_item._api_only_platform(host)
    if platform == "eBay":
        match = EBAY_ITEM_PATH.search(parsed.path)
        if not match:
            raise ValueError("That eBay link isn't a single listing. Open the listing itself and "
                             "copy its link; it contains /itm/.")
        return "ebay", match.group(1)
    if platform == "Amazon":
        if host in ("a.co", "amzn.to"):
            raise ValueError("Amazon short links can't be read. Open the product on Amazon and "
                             "copy the full link; it contains /dp/.")
        match = AMAZON_ASIN_PATH.search(parsed.path)
        if not match:
            raise ValueError("That Amazon link isn't a product page. Open the product and copy "
                             "its link; it contains /dp/.")
        return "amazon", match.group(1).upper()
    return None


def read_marketplace(url):
    parsed = parse_marketplace_url(url)
    if parsed is None:
        return None
    platform, ident = parsed
    if platform == "ebay":
        client = EbayBrowseClient.from_env()
        if client:
            try:
                return ebay_result(client.get_item_by_legacy_id(ident), url, demo=False)
            except EbayNotFound as e:
                raise ValueError(str(e))
            except EbayApiError as e:
                raise MarketplaceError(str(e))
        if ident == marketplace_samples.EBAY_SAMPLE_ID:
            return ebay_result(marketplace_samples.EBAY_ITEM, url, demo=True)
        raise NotConnected("eBay")

    client = AmazonCatalogClient.from_env()
    if client:
        try:
            return amazon_result(client.get_item(ident), demo=False)
        except AmazonNotFound as e:
            raise ValueError(str(e))
        except AmazonApiError as e:
            raise MarketplaceError(str(e))
    if ident == marketplace_samples.AMAZON_SAMPLE_ASIN:
        return amazon_result(marketplace_samples.AMAZON_ITEM, demo=True)
    raise NotConnected("Amazon")


# ---------------------------------------------------------------------------
# eBay
# ---------------------------------------------------------------------------

def ebay_result(item, url, demo):
    facts = ebay_facts(item, url)
    own = _is_own_ebay(item)
    preview = None
    if own:
        preview = {
            "title": (item.get("title") or "")[:200],
            "price": facts["reference"]["price"],
            "images": _ebay_images(item),
            "description_html": store_import._html_to_safe_paragraphs(item.get("description") or ""),
        }
    return {
        "facts": facts,
        "draft": content_generator.draft_from_facts(facts),
        "source": {"platform": "eBay", "demo": demo, "own_listing": own,
                   "seller": (item.get("seller") or {}).get("username")},
        "own_listing": preview,
    }


def ebay_facts(item, url):
    title = " ".join((item.get("title") or "").split())
    aspects = [((a.get("name") or "").strip(), (a.get("value") or "").strip())
               for a in item.get("localizedAspects") or []]
    by_name = {}
    for name, value in aspects:
        by_name.setdefault(name.lower(), []).append(value)

    lines = store_import.html_to_lines(item.get("description") or "")
    fit_lines = [line for line in lines if new_item.FITMENT_WORDS.search(line)]
    part_name, fit_text = new_item._split_title(title)
    if part_name == title and by_name.get("type"):
        part_name = by_name["type"][0]
    fits = _fits_from_text(" ".join([title, fit_text] + fit_lines))

    for key, names in FIT_ASPECTS.items():
        for name in names:
            for value in by_name.get(name, []):
                for piece in re.split(r"\s*[,;|]\s*", value):
                    if key == "engine_sizes":
                        size = re.search(r"(\d{2,4})\s*cc", piece, re.IGNORECASE)
                        piece = f"{size.group(1)}cc" if size else piece
                    if piece:
                        fits[key].append(piece)
        fits[key] = new_item._unique(fits[key])

    identifiers = {v.upper() for v in [item.get("mpn"), item.get("gtin")]
                   + by_name.get("manufacturer part number", []) + by_name.get("mpn", []) if v}
    fits["model_numbers"] = [m for m in fits["model_numbers"] if m.upper() not in identifiers]

    fit_aspect_names = {n for names in FIT_ASPECTS.values() for n in names}
    specs = [{"name": name[:40], "value": value[:120]} for name, value in aspects
             if value and name.lower() not in NOT_IMPORTED_ASPECTS and name.lower() not in fit_aspect_names]

    price = (item.get("price") or {}).get("value")
    return new_item.clean_facts({
        "part_name": part_name,
        "fits": fits,
        "specs": specs[:12],
        "aftermarket": True,
        "reference": {"site": "ebay.com", "url": item.get("itemWebUrl") or url,
                      "price": float(price) if price else None},
    })


def _is_own_ebay(item):
    mine = os.environ.get("EBAY_SELLER_USERNAME", "").strip().lower()
    seller = ((item.get("seller") or {}).get("username") or "").strip().lower()
    return bool(mine) and seller == mine


def _ebay_images(item):
    urls = [(item.get("image") or {}).get("imageUrl")]
    urls += [img.get("imageUrl") for img in item.get("additionalImages") or []]
    return [u for u in urls if u][:8]


def import_own_ebay_listing(url):
    """Imports one of your own eBay listings as-is. Re-reads it from eBay
    rather than trusting anything sent by the page."""
    parsed = parse_marketplace_url(url)
    if not parsed or parsed[0] != "ebay":
        raise ValueError("Paste the link of one of your own eBay listings.")
    client = EbayBrowseClient.from_env()
    if not client:
        raise NotConnected("eBay")
    try:
        item = client.get_item_by_legacy_id(parsed[1])
    except EbayNotFound as e:
        raise ValueError(str(e))
    except EbayApiError as e:
        raise MarketplaceError(str(e))
    if not _is_own_ebay(item):
        raise ValueError("That listing belongs to another seller, so only its facts can be used. "
                         "If it's yours, set EBAY_SELLER_USERNAME in backend/.env to your eBay username.")
    price = (item.get("price") or {}).get("value")
    row = {
        "sku": f"EBAY-{item.get('legacyItemId') or parsed[1]}",
        "sku_from_store": False,
        "title": " ".join((item.get("title") or "Untitled listing").split())[:200],
        "brand": item.get("brand") or None,
        "description_html": store_import._html_to_safe_paragraphs(item.get("description") or ""),
        "images": _ebay_images(item),
        "price": round(float(price), 2) if price else 0.0,
        "source_url": item.get("itemWebUrl") or url,
    }
    return {"items": store_import.save_products([row])}


# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------

def amazon_result(item, demo):
    facts = amazon_facts(item)
    return {
        "facts": facts,
        "draft": content_generator.draft_from_facts(facts),
        "source": {"platform": "Amazon", "demo": demo, "own_listing": False, "seller": None},
        "own_listing": None,
    }


def amazon_facts(item):
    summary = (item.get("summaries") or [{}])[0]
    title = " ".join((summary.get("itemName") or "").split())
    attributes = item.get("attributes") or {}
    bullets = [b.get("value", "") for b in attributes.get("bullet_point", []) if isinstance(b, dict)]
    fit_lines = [b for b in bullets if isinstance(b, str) and new_item.FITMENT_WORDS.search(b)]
    part_name, fit_text = new_item._split_title(title)
    fits = _fits_from_text(" ".join([title, fit_text] + fit_lines))

    identifiers = {str(v).upper() for v in (summary.get("modelNumber"), summary.get("partNumber")) if v}
    fits["model_numbers"] = [m for m in fits["model_numbers"] if m.upper() not in identifiers]

    specs = []
    for key, label in AMAZON_SPECS.items():
        values = attributes.get(key)
        if isinstance(values, list) and values and isinstance(values[0], dict) and "value" in values[0]:
            value = values[0]["value"]
            unit = values[0].get("unit")
            specs.append({"name": label, "value": f"{value} {unit}" if unit else str(value)})

    asin = item.get("asin") or ""
    return new_item.clean_facts({
        "part_name": part_name,
        "fits": fits,
        "specs": specs,
        "aftermarket": True,
        "reference": {"site": "amazon.com", "url": f"https://www.amazon.com/dp/{asin}", "price": None},
    })


def _fits_from_text(text):
    years, without_years = new_item._years(text)
    return {
        "brands": new_item._find_terms(text, new_item.BRANDS),
        "engine_sizes": new_item._unique(f"{n}cc" for n in re.findall(r"\b(\d{2,4})\s?cc\b", text, re.IGNORECASE)),
        "equipment": new_item._find_terms(text, new_item.EQUIPMENT),
        "model_numbers": new_item._model_numbers(without_years),
        "years": years,
    }

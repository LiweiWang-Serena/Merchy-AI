"""
Reads a connected seller account. Read-only: nothing on eBay is changed.

  get_username()         Trading API GetUser: whose account this is
  get_active_listings()  Trading API GetMyeBaySelling (ActiveList): every
                         active listing with SKU, quantity and price. Unlike
                         the Inventory API, it also covers listings created
                         in Seller Hub or other tools.
  get_orders(since)      Fulfillment API getOrders: orders up to two years old

The Trading API is eBay's older XML API; it accepts the same OAuth user
token in the X-EBAY-API-IAF-TOKEN header.

Docs: https://developer.ebay.com/devzone/xml/docs/reference/ebay/getmyebayselling.html
      https://developer.ebay.com/api-docs/sell/fulfillment/resources/order/methods/getOrders
"""

import xml.etree.ElementTree as ET

NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
COMPATIBILITY_LEVEL = "1399"
SITE_ID_US = "0"
PAGE_SIZE = 200
TIMEOUT_SECONDS = 20


class EbaySellerError(Exception):
    """eBay returned an error, or couldn't be reached."""


class SellerClient:
    def __init__(self, api_host, access_token):
        self.api_host = api_host
        self.token = access_token

    # ---- Trading API (XML) ----

    def _trading(self, call_name, inner_xml=""):
        import requests

        body = (f'<?xml version="1.0" encoding="utf-8"?>'
                f'<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">{inner_xml}</{call_name}Request>')
        try:
            resp = requests.post(f"{self.api_host}/ws/api.dll", data=body.encode("utf-8"), headers={
                "X-EBAY-API-CALL-NAME": call_name,
                "X-EBAY-API-SITEID": SITE_ID_US,
                "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
                "X-EBAY-API-IAF-TOKEN": self.token,
                "Content-Type": "text/xml",
            }, timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            raise EbaySellerError("Couldn't reach eBay. Check your internet connection.")
        if resp.status_code != 200:
            raise EbaySellerError(f"eBay returned HTTP {resp.status_code} for {call_name}.")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            raise EbaySellerError(f"eBay sent an unreadable response for {call_name}.")
        if root.findtext("e:Ack", namespaces=NS) not in ("Success", "Warning"):
            message = (root.findtext("e:Errors/e:LongMessage", namespaces=NS)
                       or root.findtext("e:Errors/e:ShortMessage", namespaces=NS) or "unknown error")
            raise EbaySellerError(f"eBay returned an error for {call_name}: {message}")
        return root

    def get_username(self):
        return self._trading("GetUser").findtext("e:User/e:UserID", namespaces=NS)

    def get_active_listings(self, max_pages=125):
        listings, page = [], 1
        while True:
            root = self._trading("GetMyeBaySelling", (
                f"<ActiveList><Include>true</Include><Pagination>"
                f"<EntriesPerPage>{PAGE_SIZE}</EntriesPerPage><PageNumber>{page}</PageNumber>"
                f"</Pagination></ActiveList>"))
            active = root.find("e:ActiveList", NS)
            if active is None:
                break
            for item in active.findall("e:ItemArray/e:Item", NS):
                listings.extend(parse_listing(item))
            pages = int(active.findtext("e:PaginationResult/e:TotalNumberOfPages", "1", NS) or 1)
            if page >= min(pages, max_pages):
                break
            page += 1
        return listings

    # ---- Fulfillment API (REST) ----

    def get_orders(self, since_utc, until_utc=None, max_pages=250):
        """Orders created from since_utc (up to until_utc, if given). Timestamps
        are ISO 8601 UTC, such as 2026-09-01T00:00:00.000Z. max_pages x 200
        orders caps one call; import_orders asks for one month at a time."""
        import requests

        orders, offset = [], 0
        for _ in range(max_pages):
            try:
                resp = requests.get(f"{self.api_host}/sell/fulfillment/v1/order",
                                    params={"filter": f"creationdate:[{since_utc}..{until_utc or ''}]",
                                            "limit": PAGE_SIZE, "offset": offset},
                                    headers={"Authorization": f"Bearer {self.token}",
                                             "Accept": "application/json"},
                                    timeout=TIMEOUT_SECONDS)
            except requests.RequestException:
                raise EbaySellerError("Couldn't reach eBay. Check your internet connection.")
            if resp.status_code in (401, 403):
                raise EbaySellerError("eBay refused to share orders. The authorization may be missing "
                                      "the orders permission; disconnect and connect again.")
            if not resp.ok:
                raise EbaySellerError(f"eBay returned HTTP {resp.status_code} when reading orders.")
            data = resp.json()
            batch = data.get("orders") or []
            orders.extend(batch)
            offset += PAGE_SIZE
            if not batch or offset >= int(data.get("total") or 0):
                break
        return orders


def parse_listing(item):
    """One Trading API <Item> -> one record per SKU (a listing with
    variations such as sizes has one SKU per variation)."""
    item_id = _text(item, "ItemID")
    title = _text(item, "Title") or f"eBay listing {item_id}"
    price = _float(_text(item, "SellingStatus/CurrentPrice") or _text(item, "BuyItNowPrice"))
    image = _text(item, "PictureDetails/GalleryURL")

    variations = item.findall("e:Variations/e:Variation", NS)
    if variations:
        records = []
        for i, v in enumerate(variations, start=1):
            specifics = [_text(nv, "Value") for nv in v.findall("e:VariationSpecifics/e:NameValueList", NS)]
            label = ", ".join(s for s in specifics if s)
            available = _int(_text(v, "Quantity")) - _int(_text(v, "SellingStatus/QuantitySold"))
            records.append({"item_id": item_id, "sku": _text(v, "SKU") or f"EBAY-{item_id}-{i}",
                            "title": f"{title} ({label})" if label else title,
                            "quantity_available": max(available, 0),
                            "price": _float(_text(v, "StartPrice")) or price, "image": image})
        return records

    available = _text(item, "QuantityAvailable")
    if available is None:
        available = _int(_text(item, "Quantity")) - _int(_text(item, "SellingStatus/QuantitySold"))
    return [{"item_id": item_id, "sku": _text(item, "SKU") or f"EBAY-{item_id}", "title": title,
             "quantity_available": max(_int(available), 0), "price": price, "image": image}]


def _text(node, path):
    value = node.findtext("/".join(f"e:{part}" for part in path.split("/")), namespaces=NS)
    return value.strip() if value else None


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0

"""
Demo responses for the two sample links on the Add product page, shaped
like the real eBay Browse API and Amazon Catalog Items API responses.
They're only used when the matching API keys aren't set, and the page
labels them as demo data. The listings, sellers and IDs are made up.
"""

EBAY_SAMPLE_ID = "100000000001"
AMAZON_SAMPLE_ASIN = "B0DEMO0001"
SAMPLE_URLS = {
    "ebay": f"https://www.ebay.com/itm/{EBAY_SAMPLE_ID}",
    "amazon": f"https://www.amazon.com/dp/{AMAZON_SAMPLE_ASIN}",
}

EBAY_ITEM = {
    "itemId": f"v1|{EBAY_SAMPLE_ID}|0",
    "legacyItemId": EBAY_SAMPLE_ID,
    "title": "Centrifugal Clutch 3/4 Bore 10T for Predator 212cc Go Kart Mini Bike 69730",
    "shortDescription": "Heavy duty clutch, the only one you'll ever need!",
    "description": "<p>Heavy duty clutch, the only one you'll ever need! Buy with confidence.</p>"
                   "<p>Fits Predator 212cc engines on go karts and mini bikes, item 69730.</p>",
    "price": {"value": "24.99", "currency": "USD"},
    "seller": {"username": "demo-parts-seller", "feedbackPercentage": "99.1"},
    "image": {"imageUrl": "https://i.ebayimg.example/demo/clutch-front.jpg"},
    "additionalImages": [{"imageUrl": "https://i.ebayimg.example/demo/clutch-side.jpg"}],
    "brand": "DemoParts",
    "mpn": "DP-CL-3410",
    "gtin": "000000000017",
    "itemWebUrl": SAMPLE_URLS["ebay"],
    "localizedAspects": [
        {"type": "STRING", "name": "Brand", "value": "DemoParts"},
        {"type": "STRING", "name": "Manufacturer Part Number", "value": "DP-CL-3410"},
        {"type": "STRING", "name": "Type", "value": "Centrifugal Clutch"},
        {"type": "STRING", "name": "Compatible Equipment Type", "value": "Go Kart, Mini Bike"},
        {"type": "STRING", "name": "Compatible Equipment Make", "value": "Predator"},
        {"type": "STRING", "name": "Engine Size", "value": "212 cc"},
        {"type": "STRING", "name": "Bore Diameter", "value": "3/4 in"},
        {"type": "STRING", "name": "Number of Teeth", "value": "10"},
        {"type": "STRING", "name": "Chain Size", "value": "#35"},
        {"type": "STRING", "name": "Material", "value": "Steel"},
        {"type": "STRING", "name": "Warranty", "value": "30 days"},
    ],
}

AMAZON_ITEM = {
    "asin": AMAZON_SAMPLE_ASIN,
    "summaries": [{
        "marketplaceId": "ATVPDKIKX0DER",
        "itemName": "Throttle Cable for Go Kart Mini Bike with Predator 212cc 79cc Engines, 48 Inch",
        "brand": "DemoParts",
        "manufacturer": "DemoParts",
        "modelNumber": "DP-TC-48",
        "partNumber": "DP-TC-48",
    }],
    "attributes": {
        "material": [{"value": "Steel", "language_tag": "en_US", "marketplace_id": "ATVPDKIKX0DER"}],
        "color": [{"value": "Black", "language_tag": "en_US", "marketplace_id": "ATVPDKIKX0DER"}],
        "item_length": [{"value": 48, "unit": "inches", "marketplace_id": "ATVPDKIKX0DER"}],
        "bullet_point": [
            {"value": "Premium quality cable that riders love!", "language_tag": "en_US"},
            {"value": "Fits Predator 212cc and 79cc engines on go karts and mini bikes", "language_tag": "en_US"},
        ],
    },
}

"""
Turns structured competitive facts (price, rating, review count, keyword
frequency) into insights a small seller can actually act on: where to
price, which product themes to emphasize, and where the gaps are.
"""

from collections import Counter
from mock_apis.market_data import get_top_sellers


def analyze_niche(niche: str):
    listings = get_top_sellers(niche)
    if not listings:
        return None

    prices = [l["price"] for l in listings]
    ratings = [l["rating"] for l in listings]

    keyword_counter = Counter()
    for listing in listings:
        keyword_counter.update(listing["emphasized_keywords"])

    total_listings = len(listings)
    top_keywords = [
        {"keyword": kw, "pct_of_top_listings": round(count / total_listings * 100)}
        for kw, count in keyword_counter.most_common()
    ]

    # Simple price-tier segmentation so a seller can see where they'd land.
    sorted_prices = sorted(prices)
    budget_ceiling = sorted_prices[len(sorted_prices) // 3]
    premium_floor = sorted_prices[2 * len(sorted_prices) // 3]

    # "Underused" themes: keywords that show up but not dominantly --
    # a gap a new listing could lean into harder than the pack does.
    underused = [k for k in top_keywords if 15 <= k["pct_of_top_listings"] <= 35]

    return {
        "niche": niche,
        "sample_size": total_listings,
        "price": {
            "min": min(prices),
            "max": max(prices),
            "median": sorted_prices[total_listings // 2],
            "budget_tier_ceiling": budget_ceiling,
            "premium_tier_floor": premium_floor,
        },
        "rating": {
            "average": round(sum(ratings) / total_listings, 2),
            "min_to_compete": round(min(ratings), 1),
        },
        "top_keywords": top_keywords,
        "underused_keywords": underused,
        "recommendation": _build_recommendation(top_keywords, underused, sorted_prices, total_listings),
    }


def _build_recommendation(top_keywords, underused, sorted_prices, n):
    top3 = ", ".join(k["keyword"] for k in top_keywords[:3])
    median_price = sorted_prices[n // 2]

    rec = f"The top-selling listings in this niche most commonly emphasize: {top3}. "
    rec += f"Median price among top sellers is ${median_price:.2f}. "

    if underused:
        gap = underused[0]["keyword"]
        rec += (f"'{gap}' is mentioned by only {underused[0]['pct_of_top_listings']}% "
                f"of top listings despite being a recognized theme in this niche -- "
                f"leaning into it harder in your own listing is a differentiation opportunity.")
    else:
        rec += "No clear underused theme stood out; competing on price or service (fast shipping, warranty) may matter more here."

    return rec

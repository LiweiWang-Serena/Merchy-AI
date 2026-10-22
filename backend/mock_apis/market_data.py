"""
Simulates a legitimate market-research data source: structured facts
about top-selling listings in a category -- price, rating, review count,
and *keyword frequency* extracted from reviews/titles -- not copyrighted
text or images.

This is the same category of data real tools like Jungle Scout or
Helium10 sell access to, and the same category Amazon's own Product
Advertising API (PA-API) returns to registered affiliates. Prices,
ratings, and review counts are facts, not creative works -- they aren't
copyrightable. Keyword *frequency* (e.g. "'quiet' appears in 40% of
5-star reviews") is a statistic about text, not a reproduction of it.

In production, replace `get_top_sellers()` with a real call to PA-API
(for Amazon) or a licensed market-data provider -- never with a scraper
that copies another seller's actual photos or description text.
"""

import random

NICHES = ["carburetors", "atv-tires", "go-kart-clutches", "dirt-bike-chains", "mini-bike-exhaust"]

# Keyword pools represent *themes* commonly seen across a niche's
# listings and reviews -- generic descriptive terms, not lifted phrases
# from any single competitor's copyrighted listing copy.
KEYWORD_POOL = {
    "carburetors": ["easy install", "no leaks", "idles smooth", "bolt-on fit",
                     "better throttle response", "improved fuel efficiency", "quiet"],
    "atv-tires": ["good traction", "puncture resistant", "smooth ride", "long tread life",
                  "fits stock rims", "quiet on pavement", "aggressive tread"],
    "go-kart-clutches": ["smooth engagement", "no slipping", "easy to adjust",
                          "handles high torque", "direct fit", "reduced vibration"],
    "dirt-bike-chains": ["stays tight", "no rust", "quiet operation", "easy to install",
                          "handles jumps well", "correct link count"],
    "mini-bike-exhaust": ["deep sound", "easy bolt-on", "more power", "lightweight",
                           "no rattling", "good heat shield"],
}


def get_top_sellers(niche: str, limit: int = 50):
    """Returns structured competitive facts for the top-selling listings
    in a niche: price, rating, review count, and keyword frequency.
    No copyrighted text or images -- see module docstring."""
    if niche not in NICHES:
        return []

    rng = random.Random(niche)
    keywords = KEYWORD_POOL[niche]
    listings = []

    for i in range(limit):
        price = round(rng.uniform(12, 65), 2)
        rating = round(rng.uniform(3.6, 5.0), 1)
        review_count = rng.randint(15, 4200)

        # Each listing "emphasizes" a couple of the niche's common themes --
        # simulating what you'd learn from reading review keyword frequency,
        # not from copying any one listing's actual sentences.
        emphasized = rng.sample(keywords, k=rng.randint(1, 3))

        listings.append({
            "rank": i + 1,
            "price": price,
            "rating": rating,
            "review_count": review_count,
            "emphasized_keywords": emphasized,
        })

    return sorted(listings, key=lambda l: l["review_count"], reverse=True)

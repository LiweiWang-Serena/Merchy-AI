"""
Turns raw product data (title, bullet points, images) pulled from your
own Amazon listing into a ready-to-publish eBay listing draft.

This module is template-based so the demo runs with no external API
calls. In production, swap `generate_description()` for a call to an
LLM (Claude/Gemini) with a prompt like:

    "Rewrite these product bullet points as an eBay listing description
    in our brand voice: {bullets}"

That's a one-function change -- everything else (title generation,
character-limit handling, draft storage) stays the same.
"""

import re

EBAY_TITLE_MAX_LEN = 80  # eBay's hard limit on listing titles


def generate_ebay_title(product: dict) -> str:
    """Build an SEO-friendly eBay title from the Amazon product data,
    respecting eBay's 80-character title limit."""
    parts = [p for p in [product.get("brand"), product.get("title")] if p]
    title = " - ".join(parts)

    # Pull a couple of the most concrete-sounding bullet keywords (numbers,
    # material words) to help the title surface in eBay search.
    keyword_bullets = [b for b in product.get("bullets", []) if re.search(r"\d", b)]
    if keyword_bullets:
        extra = keyword_bullets[0].split(",")[0]
        candidate = f"{title} | {extra}"
        if len(candidate) <= EBAY_TITLE_MAX_LEN:
            title = candidate

    if len(title) > EBAY_TITLE_MAX_LEN:
        title = title[:EBAY_TITLE_MAX_LEN - 1].rstrip() + "…"
    return title


def generate_ebay_description_html(product: dict) -> str:
    """Turn bullet points into a clean, scannable eBay HTML description.
    Deliberately plain HTML (no inline styles) since eBay strips a lot of
    custom CSS from listing descriptions anyway."""
    bullets_html = "\n".join(f"    <li>{b}</li>" for b in product.get("bullets", []))
    brand_line = f"<p><strong>Brand:</strong> {product['brand']}</p>\n" if product.get("brand") else ""

    return f"""<div>
  <h2>{product['title']}</h2>
  {brand_line}<ul>
{bullets_html}
  </ul>
  <p>Ships from our warehouse within 1 business day. Same item as our
  Amazon listing, sold by the same seller.</p>
</div>"""


def build_listing_draft(product: dict) -> dict:
    """Full draft object: title, description, and the photo set carried
    over from the seller's own Amazon listing."""
    return {
        "sku": product["sku"],
        "source_asin": product["asin"],
        "source_url": product["source_url"],
        "ebay_title": generate_ebay_title(product),
        "ebay_description_html": generate_ebay_description_html(product),
        "images": product.get("images", []),
    }

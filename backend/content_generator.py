"""
Generates original listing copy by combining:
  1. Market insights (which themes top sellers emphasize, price positioning)
  2. The seller's own facts about their specific product

This never touches another listing's actual text -- it's template-based
generation from structured inputs, the same category of task an LLM
call (Claude/Gemini) would do more fluently in production. Swap
`draft_listing_copy()`'s body for a real API call with a prompt like:

    "Write an original eBay listing title and description for
    {product_facts}, emphasizing these themes that top sellers in this
    category use: {top_keywords}. Do not reference any specific
    competitor or reproduce any existing listing's wording."
"""

TITLE_TEMPLATES = [
    "{product_name} - {top_theme} - {fit_note}",
    "{product_name} ({fit_note}) - {top_theme}",
]


def draft_listing_copy(product_facts: dict, insights: dict):
    """
    product_facts: {name, fit_note, price, own_bullets: [str]}
    insights: output of market_research.analyze_niche()
    """
    top_themes = [k["keyword"] for k in insights["top_keywords"][:3]]
    primary_theme = top_themes[0].title() if top_themes else "Reliable Performance"

    title = TITLE_TEMPLATES[0].format(
        product_name=product_facts["name"],
        top_theme=primary_theme,
        fit_note=product_facts["fit_note"],
    )[:80]

    price_note = _price_positioning_note(product_facts["price"], insights["price"])

    bullets = list(product_facts.get("own_bullets", []))
    # Add a theme-driven bullet for each top competitive theme not
    # already covered by the seller's own facts, phrased fresh.
    covered = " ".join(bullets).lower()
    for theme in top_themes:
        if theme.lower() not in covered:
            bullets.append(_theme_to_bullet(theme))

    description_html = f"""<div>
  <h2>{product_facts['name']}</h2>
  <p>{price_note}</p>
  <ul>
{chr(10).join(f"    <li>{b}</li>" for b in bullets)}
  </ul>
</div>"""

    return {
        "title": title,
        "description_html": description_html,
        "themes_used": top_themes,
    }


def _price_positioning_note(price, price_insights):
    if price <= price_insights["budget_tier_ceiling"]:
        return f"Priced at ${price:.2f} — among the most affordable options in this category."
    if price >= price_insights["premium_tier_floor"]:
        return f"Priced at ${price:.2f} — a premium option backed by quality you can rely on."
    return f"Priced at ${price:.2f} — solid mid-range value for this category."


def _theme_to_bullet(theme: str) -> str:
    """Turns a generic theme keyword into an original, freshly-worded
    bullet point -- this is exactly the step a real LLM call would do
    with more natural variation."""
    phrasing = {
        "easy install": "Designed for a straightforward installation with commonly available tools",
        "no leaks": "Precision-fit seals help prevent leaks after installation",
        "quiet": "Engineered to run quietly under normal operating conditions",
        "good traction": "Tread pattern designed to maintain grip across varied terrain",
        "smooth engagement": "Engages smoothly without the grabbiness of lower-quality alternatives",
        "long tread life": "Built for extended tread life under regular use",
    }
    return phrasing.get(theme, f"Known for {theme} based on category-wide buyer feedback")


# ---------------------------------------------------------------------------
# Listings written from product facts (new items from a reference link)
# ---------------------------------------------------------------------------

from html import escape as _escape

TITLE_LIMIT = 80


def draft_from_facts(facts: dict, extra_notes: str = ""):
    """Writes a title and description from facts alone: part name,
    fitment, part numbers and specs. It states only what the facts say
    (no invented claims like "heavy duty" or "OEM quality") plus general
    buying guidance, so the copy is both original and accurate.

    Returns the description as HTML (Shopify, eBay) and as plain text
    (TikTok Shop and other plain-text fields)."""
    part = (facts.get("part_name") or "").strip() or "Replacement part"
    fits = facts.get("fits") or {}
    brands = list(fits.get("brands") or [])
    engines = list(fits.get("engine_sizes") or [])
    equipment = list(fits.get("equipment") or [])
    models = list(fits.get("model_numbers") or [])
    years = list(fits.get("years") or [])
    specs = [s for s in facts.get("specs") or [] if s.get("name") and s.get("value")]
    notes = [" ".join(p.split()) for p in (extra_notes or "").split("\n") if p.strip()]

    target = " ".join(brands + engines + equipment[:1])
    intro = f"{_article(part)} {_lower_words(part)}"
    if target:
        intro += f" designed to fit the {target}"
        if years:
            intro += f" ({', '.join(years)})"
        if models:
            label = "numbers" if len(models) > 1 else "number"
            intro += f", model or item {label} {_join_and(models)}"
    intro += "."

    fit_rows = [(label, ", ".join(values)) for label, values in (
        ("Brand", brands), ("Engine size", engines), ("Equipment", equipment),
        ("Model or item numbers", models), ("Years", years)) if values]
    check = ("Check the model or item number on your equipment's label and compare it with "
             "the list above. If you aren't sure it fits, send us a message before ordering.")
    disclaimer = (f"Aftermarket part. Not made by or affiliated with {_join_and(brands)}."
                  if brands and facts.get("aftermarket", True) else "")

    html = [f"<p>{_escape(intro)}</p>"]
    text = [intro]
    if fit_rows:
        html.append("<h3>Fits</h3><ul>" + "".join(
            f"<li>{_escape(k)}: {_escape(v)}</li>" for k, v in fit_rows) + "</ul>")
        text.append("Fits:\n" + "\n".join(f"- {k}: {v}" for k, v in fit_rows))
    if specs:
        html.append("<h3>Specifications</h3><ul>" + "".join(
            f"<li>{_escape(s['name'])}: {_escape(s['value'])}</li>" for s in specs) + "</ul>")
        text.append("Specifications:\n" + "\n".join(f"- {s['name']}: {s['value']}" for s in specs))
    for note in notes:
        html.append(f"<p>{_escape(note)}</p>")
        text.append(note)
    html.append(f"<h3>Before you order</h3><p>{_escape(check)}</p>")
    text.append(f"Before you order: {check}")
    if disclaimer:
        html.append(f"<p>{_escape(disclaimer)}</p>")
        text.append(disclaimer)

    return {
        "title": _fit_title(part, brands, engines, equipment, models),
        "description_html": "\n".join(html),
        "description_text": "\n\n".join(text),
    }


def _fit_title(part, brands, engines, equipment, models, limit=TITLE_LIMIT):
    """The most complete title that fits the limit: drops the secondary
    brand first (e.g. "Harbor Freight" when "Predator" is present), then
    the model numbers, before truncating."""
    brand_options = [brands] + ([brands[-1:]] if len(brands) > 1 else [])
    candidates = []
    for brand_set in brand_options:
        lead = " ".join(brand_set + engines + equipment[:1])
        base = f"{part} for {lead}" if lead else part
        if models:
            candidates.append(f"{base} - Fits {', '.join(models)}")
            candidates.append(f"{base} {' '.join(models)}")
    for brand_set in brand_options:
        lead = " ".join(brand_set + engines + equipment[:1])
        candidates.append(f"{part} for {lead}" if lead else part)
    for title in candidates:
        if len(title) <= limit:
            return title
    return candidates[-1][:limit].rsplit(" ", 1)[0]


def _article(phrase):
    return "An" if phrase[:1].lower() in "aeiou" else "A"


def _lower_words(phrase):
    """Lowercases ordinary capitalized words but keeps acronyms and codes
    ("CDI Box" -> "CDI box", "Fuel Gas Tank" -> "fuel gas tank")."""
    return " ".join(w.lower() if w[:1].isupper() and w[1:].islower() else w for w in phrase.split())


def _join_and(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]

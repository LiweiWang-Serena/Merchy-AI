"""
Fetch a seller's active eBay listings through the official Browse API and save them
for the chatbot Q&A evaluation (pre-sale questions and the RAG knowledge base).

Run from the backend folder:
    python fetch_listings.py                         # seller from EBAY_SELLER_USERNAME, 60 listings
    python fetch_listings.py --seller pauleagleauctions --max 100

Needs EBAY_CLIENT_ID, EBAY_CLIENT_SECRET and EBAY_ENVIRONMENT=production in backend/.env.
Uses an application token (client credentials), so no seller login is needed.

Output:
    data/qa/listings.csv        title + text the chatbot can use (read by the eval notebook)
    data/qa/listings_meta.csv   item id, price and URL, for reference
"""
import argparse, base64, html, os, re, sys, time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parent
load_dotenv(BACKEND / '.env')
OUT = BACKEND / 'data' / 'qa'
OUT.mkdir(parents=True, exist_ok=True)

ENV = os.getenv('EBAY_ENVIRONMENT', 'production').lower()
API = 'https://api.ebay.com' if ENV == 'production' else 'https://api.sandbox.ebay.com'
MARKETPLACE = 'EBAY_US'
# eBay Motors > Parts & Accessories. Add more category ids if the store also lists elsewhere.
DEFAULT_CATEGORIES = ['6028']


def get_token():
    cid, secret = os.getenv('EBAY_CLIENT_ID'), os.getenv('EBAY_CLIENT_SECRET')
    if not cid or not secret:
        sys.exit('Add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET to backend/.env')
    auth = base64.b64encode(f'{cid}:{secret}'.encode()).decode()
    r = requests.post(f'{API}/identity/v1/oauth2/token',
                      headers={'Authorization': f'Basic {auth}',
                               'Content-Type': 'application/x-www-form-urlencoded'},
                      data={'grant_type': 'client_credentials',
                            'scope': 'https://api.ebay.com/oauth/api_scope'}, timeout=30)
    if r.status_code != 200:
        sys.exit(f'Token request failed ({r.status_code}): {r.text[:300]}')
    return r.json()['access_token']


def call(session, path, params=None):
    for attempt in range(4):
        r = session.get(f'{API}{path}', params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * 2 ** attempt)
            continue
        sys.exit(f'{path} failed ({r.status_code}): {r.text[:300]}')
    sys.exit(f'{path} kept failing; try again later')


def search(session, seller, categories, max_items):
    found, seen = [], set()
    for cat in categories:
        offset = 0
        while len(found) < max_items:
            data = call(session, '/buy/browse/v1/item_summary/search',
                        {'category_ids': cat, 'filter': f'sellers:{{{seller}}}',
                         'limit': min(200, max_items), 'offset': offset})
            items = data.get('itemSummaries', [])
            for it in items:
                if it['itemId'] not in seen:
                    seen.add(it['itemId'])
                    found.append(it)
            print(f'  category {cat}: {len(found)} listings so far (eBay reports {data.get("total", 0)})')
            if not items or offset + len(items) >= data.get('total', 0):
                break
            offset += len(items)
    return found[:max_items]


def clean_text(s, limit=600):
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))
    return re.sub(r'\s+', ' ', s).strip()[:limit]


def details(session, item_id):
    d = call(session, f'/buy/browse/v1/item/{item_id}')
    aspects = '; '.join(f"{a['name']}: {a['value']}" for a in d.get('localizedAspects', []))
    return {
        'title': d.get('title', ''),
        'condition': d.get('condition', ''),
        'condition_notes': clean_text(d.get('conditionDescription', ''), 300),
        'item_specifics': aspects[:800],
        'description': clean_text(d.get('shortDescription') or d.get('description', '')),
        'category': d.get('categoryPath', ''),
    }, {
        'item_id': item_id, 'title': d.get('title', ''),
        'price': (d.get('price') or {}).get('value', ''),
        'url': d.get('itemWebUrl', ''),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seller', default=os.getenv('EBAY_SELLER_USERNAME', 'pauleagleauctions'))
    ap.add_argument('--max', type=int, default=60, help='how many listings to fetch details for')
    ap.add_argument('--categories', nargs='+', default=DEFAULT_CATEGORIES)
    a = ap.parse_args()

    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {get_token()}',
                            'X-EBAY-C-MARKETPLACE-ID': MARKETPLACE})
    print(f'Searching active listings of {a.seller} ({ENV})')
    summaries = search(session, a.seller, a.categories, a.max)
    if not summaries:
        sys.exit('No listings found. Check the seller username, or pass other --categories.')

    rows, meta = [], []
    for i, it in enumerate(summaries, 1):
        r, m = details(session, it['itemId'])
        rows.append(r); meta.append(m)
        print(f'  details {i}/{len(summaries)}', end='\r')
        time.sleep(0.2)
    print()
    pd.DataFrame(rows).to_csv(OUT / 'listings.csv', index=False)
    pd.DataFrame(meta).to_csv(OUT / 'listings_meta.csv', index=False)
    print(f'Saved {len(rows)} listings to data/qa/listings.csv (and listings_meta.csv)')
    print(pd.DataFrame(rows)[['title', 'condition']].head(5).to_string())


if __name__ == '__main__':
    main()

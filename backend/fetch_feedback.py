"""
Fetch the feedback a seller has RECEIVED on eBay, through eBay's official
Feedback API (getFeedback), using an application token (client credentials,
scope commerce.feedback.readonly). No seller sign-in is needed.

Usage (from backend/, with the virtual environment active):
    python fetch_feedback.py <ebay_username>

Saves the raw API responses to data/feedback_raw.json. That file still holds
buyer user IDs, so it stays on your machine: data/ must be in .gitignore.
Cleaning (dropping user IDs, keeping comment/rating/date/item) is a separate
step once we've seen the real field names.

Needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in backend/.env. Real feedback
only exists in production, so these must be your PRODUCTION keys and
EBAY_ENVIRONMENT must be "production".
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

SCOPE = "https://api.ebay.com/oauth/api_scope/commerce.feedback.readonly"
PAGE_SIZE = 50
OUT_FILE = Path(__file__).parent / "data" / "feedback_raw.json"


def load_env(path=Path(__file__).parent / ".env"):
    """Minimal .env reader, so this script needs no extra packages."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_root():
    env = os.environ.get("EBAY_ENVIRONMENT", "sandbox").strip().lower()
    if env != "production":
        print("Warning: EBAY_ENVIRONMENT is not 'production'. Sandbox has no real feedback.")
        return "https://api.sandbox.ebay.com"
    return "https://api.ebay.com"


def get_app_token(root):
    client_id = os.environ.get("EBAY_CLIENT_ID")
    secret = os.environ.get("EBAY_CLIENT_SECRET")
    if not client_id or not secret:
        sys.exit("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET must be set in backend/.env")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    resp = requests.post(
        f"{root}/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=20,
    )
    if resp.status_code != 200:
        sys.exit(f"Token request failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()["access_token"]


def first_list(payload):
    """The entries come back in a list field; return the first list found."""
    for value in payload.values():
        if isinstance(value, list):
            return value
    return []


def fetch_all(root, token, user_id):
    entries, offset = [], 0
    while True:
        resp = requests.get(
            f"{root}/commerce/feedback/v1/feedback",
            headers={"Authorization": f"Bearer {token}"},
            params={"user_id": user_id, "feedback_type": "FEEDBACK_RECEIVED",
                    "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Request failed at offset {offset} ({resp.status_code}): {resp.text[:500]}")
            break
        page = first_list(resp.json())
        entries.extend(page)
        print(f"  fetched {len(entries)} so far")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.5)  # be gentle with the API
    return entries


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python fetch_feedback.py <ebay_username>")
    load_env()
    root = api_root()
    token = get_app_token(root)
    print("Got an application token. Fetching feedback...")
    entries = fetch_all(root, token, sys.argv[1])

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    print(f"\nSaved {len(entries)} entries to {OUT_FILE}")
    if entries:
        print("\nFields in the first entry (paste these to Claude, not the whole file):")
        print(sorted(entries[0].keys()))


if __name__ == "__main__":
    main()

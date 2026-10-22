"""
eBay Browse API client: reads one listing by the item number in its link
(getItemByLegacyId). It uses an application token from the
client-credentials grant, so no seller login is involved.

Setup (developer.ebay.com, free):
  1. Create an application and copy its Production App ID (client ID)
     and Cert ID (client secret) into backend/.env as EBAY_CLIENT_ID and
     EBAY_CLIENT_SECRET.
  2. Before production keys work, eBay requires you to subscribe to or
     opt out of marketplace account deletion notifications. This app
     stores no eBay user data, so opting out applies.
  3. eBay says production use of its Buy APIs is intended for partners
     and may need approval through the eBay Partner Network. If calls
     come back with 403, that approval is the missing step.

Docs: https://developer.ebay.com/api-docs/buy/browse/resources/item/methods/getItemByLegacyId
"""

import base64
import os
import time

HOSTS = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
SCOPE = "https://api.ebay.com/oauth/api_scope"
TIMEOUT_SECONDS = 10


class EbayApiError(Exception):
    """eBay couldn't be reached or refused the request."""


class EbayNotFound(ValueError):
    """The listing doesn't exist (ended, removed, or a wrong number)."""


class EbayBrowseClient:
    def __init__(self, client_id, client_secret, environment="production", marketplace="EBAY_US"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.host = HOSTS.get(environment, HOSTS["production"])
        self.marketplace = marketplace
        self._token = None
        self._token_expires = 0.0

    @classmethod
    def from_env(cls):
        """Returns a client if the keys are set in .env, otherwise None."""
        client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
        secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
        if not client_id or not secret:
            return None
        return cls(client_id, secret,
                   environment=os.environ.get("EBAY_ENVIRONMENT", "production").strip().lower(),
                   marketplace=os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US").strip())

    def _access_token(self):
        import requests

        if self._token and time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            resp = requests.post(
                f"{self.host}/identity/v1/oauth2/token",
                headers={"Authorization": f"Basic {basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": SCOPE},
                timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            raise EbayApiError("Couldn't reach eBay. Check your internet connection.")
        if resp.status_code != 200:
            raise EbayApiError(f"eBay didn't accept the API keys (HTTP {resp.status_code}). "
                               "Check EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in backend/.env.")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def get_item_by_legacy_id(self, legacy_id):
        import requests

        try:
            resp = requests.get(
                f"{self.host}/buy/browse/v1/item/get_item_by_legacy_id",
                params={"legacy_item_id": legacy_id},
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "X-EBAY-C-MARKETPLACE-ID": self.marketplace},
                timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            raise EbayApiError("Couldn't reach eBay. Check your internet connection.")
        if resp.status_code == 404:
            raise EbayNotFound("eBay couldn't find that listing. It may have ended or been removed.")
        if resp.status_code in (401, 403):
            raise EbayApiError("eBay refused the request. Your keys may not have production access to "
                               "the Browse API yet; that approval goes through the eBay Partner Network.")
        if resp.status_code == 400:
            raise EbayNotFound("eBay couldn't read that listing. Listings with several variations "
                               "(sizes, colors) can't be read by item number; enter the facts by hand.")
        if not resp.ok:
            raise EbayApiError(f"eBay returned an error (HTTP {resp.status_code}). Try again later.")
        return resp.json()

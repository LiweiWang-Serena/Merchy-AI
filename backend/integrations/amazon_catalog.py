"""
Amazon Selling Partner API client: reads catalog facts for one ASIN
(Catalog Items API 2022-04-01, getCatalogItem).

Setup: only the primary user of an Amazon Professional seller account
can register as an SP-API developer. Register a private app in Seller
Central, self-authorize it, and put these in backend/.env:
  SP_API_LWA_APP_ID         Login with Amazon client ID
  SP_API_LWA_CLIENT_SECRET  Login with Amazon client secret
  SP_API_REFRESH_TOKEN      from self-authorizing the app
  SP_API_MARKETPLACE_ID     optional, default ATVPDKIKX0DER (amazon.com)
  SP_API_REGION             optional: na (default), eu or fe
Amazon no longer requires AWS request signing for SP-API, so the AWS
keys in older setup guides aren't used here.

The catalog has no prices; competitor prices would come from the
Product Pricing API, which isn't wired up yet.

Docs: https://developer-docs.amazon.com/sp-api/docs/catalog-items-api-v2022-04-01-reference
"""

import os
import time

ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
TIMEOUT_SECONDS = 10


class AmazonApiError(Exception):
    """Amazon couldn't be reached or refused the request."""


class AmazonNotFound(ValueError):
    """No catalog item for that ASIN in this marketplace."""


class AmazonCatalogClient:
    def __init__(self, client_id, client_secret, refresh_token,
                 marketplace_id="ATVPDKIKX0DER", region="na"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.marketplace_id = marketplace_id
        self.endpoint = ENDPOINTS.get(region, ENDPOINTS["na"])
        self._token = None
        self._token_expires = 0.0

    @classmethod
    def from_env(cls):
        """Returns a client if the credentials are set in .env, otherwise None."""
        values = [os.environ.get(k, "").strip() for k in
                  ("SP_API_LWA_APP_ID", "SP_API_LWA_CLIENT_SECRET", "SP_API_REFRESH_TOKEN")]
        if not all(values):
            return None
        return cls(*values,
                   marketplace_id=os.environ.get("SP_API_MARKETPLACE_ID", "ATVPDKIKX0DER").strip(),
                   region=os.environ.get("SP_API_REGION", "na").strip().lower())

    def _access_token(self):
        import requests

        if self._token and time.time() < self._token_expires - 60:
            return self._token
        try:
            resp = requests.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                "client_id": self.client_id, "client_secret": self.client_secret,
            }, timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            raise AmazonApiError("Couldn't reach Amazon. Check your internet connection.")
        if resp.status_code != 200:
            raise AmazonApiError(f"Amazon didn't accept the credentials (HTTP {resp.status_code}). "
                                 "Check the SP_API_ values in backend/.env.")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def get_item(self, asin):
        import requests

        try:
            resp = requests.get(
                f"{self.endpoint}/catalog/2022-04-01/items/{asin}",
                params={"marketplaceIds": self.marketplace_id, "includedData": "summaries,attributes"},
                headers={"x-amz-access-token": self._access_token()},
                timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            raise AmazonApiError("Couldn't reach Amazon. Check your internet connection.")
        if resp.status_code == 404:
            raise AmazonNotFound("Amazon has no catalog item with that ASIN in this marketplace.")
        if resp.status_code in (401, 403):
            raise AmazonApiError("Amazon refused the request. Check that the app is authorized "
                                 "for the Catalog Items API.")
        if resp.status_code == 429:
            raise AmazonApiError("Amazon is limiting requests right now. Wait a minute and try again.")
        if not resp.ok:
            raise AmazonApiError(f"Amazon returned an error (HTTP {resp.status_code}). Try again later.")
        return resp.json()

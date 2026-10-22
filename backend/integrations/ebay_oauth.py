"""
eBay authorization for a seller account (OAuth authorization code grant).

The account owner signs in on eBay and clicks Agree; eBay then hands back
a one-time code, which this app swaps for tokens. Nobody shares a password.

  consent_url()    the eBay page where the owner signs in and agrees
  read_code()      pulls the code out of the address eBay sends them to
  exchange_code()  one-time code -> access token (2 hours) + refresh token
                   (about 18 months)
  refresh()        refresh token -> new access token

eBay only sends people back to HTTPS addresses and doesn't accept
localhost. A local install therefore keeps eBay's own confirmation page
as the "auth accepted URL": the owner copies that page's address, which
carries the code, into the admin page. A deployed HTTPS site can instead
point the accepted URL at /ebay/callback and skip the copying.

Docs: https://developer.ebay.com/api-docs/static/oauth-authorization-code-grant.html
"""

import base64
import os
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

AUTH_HOSTS = {"production": "https://auth.ebay.com", "sandbox": "https://auth.sandbox.ebay.com"}
API_HOSTS = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}

# Read-only access: the basic scope (used for Trading API calls such as
# GetMyeBaySelling), orders, and inventory. EBAY_SCOPES in .env overrides
# this if eBay assigns your keyset a different set.
DEFAULT_SCOPES = (
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
)
TIMEOUT_SECONDS = 15


class EbayAuthError(Exception):
    """eBay refused a token request, or couldn't be reached."""


class Settings:
    def __init__(self, client_id, client_secret, runame, environment="production", scopes=DEFAULT_SCOPES):
        self.client_id = client_id
        self.client_secret = client_secret
        self.runame = runame
        self.environment = environment if environment in AUTH_HOSTS else "production"
        self.scopes = list(scopes)

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("EBAY_CLIENT_ID", "").strip(),
            os.environ.get("EBAY_CLIENT_SECRET", "").strip(),
            os.environ.get("EBAY_RUNAME", "").strip(),
            os.environ.get("EBAY_ENVIRONMENT", "production").strip().lower(),
            os.environ.get("EBAY_SCOPES", "").split() or DEFAULT_SCOPES,
        )

    def missing(self):
        """Names of the .env settings that still need a value."""
        return [name for name, value in (("EBAY_CLIENT_ID", self.client_id),
                                         ("EBAY_CLIENT_SECRET", self.client_secret),
                                         ("EBAY_RUNAME", self.runame)) if not value]

    @property
    def api_host(self):
        return API_HOSTS[self.environment]

    @property
    def auth_host(self):
        return AUTH_HOSTS[self.environment]


def consent_url(settings, state):
    params = {
        "client_id": settings.client_id,
        "redirect_uri": settings.runame,      # eBay wants the RuName here, not a URL
        "response_type": "code",
        "scope": " ".join(settings.scopes),   # space-separated, then URL-encoded
        "state": state,
        "prompt": "login",                    # always ask which account to use
    }
    return f"{settings.auth_host}/oauth2/authorize?{urlencode(params, quote_via=quote)}"


def read_code(pasted):
    """(code, state) from the address eBay sent the owner to, or from a
    bare code. State is None when only the code was pasted."""
    text = (pasted or "").strip()
    if not text:
        raise ValueError("Paste the address of the eBay page you landed on after clicking Agree.")
    if "code=" not in text and "error=" not in text and "isAuthSuccessful=" not in text:
        return unquote(text), None   # a bare code
    query = urlparse(text).query if "://" in text else text.lstrip("?")
    params = parse_qs(query)
    if params.get("error") or params.get("isAuthSuccessful") == ["false"]:
        raise ValueError("eBay says access wasn't granted. Open eBay again and click Agree.")
    code = (params.get("code") or [None])[0]
    if not code:
        raise ValueError("That address has no code in it. Copy the address of the page eBay "
                         "shows right after you click Agree.")
    return code, (params.get("state") or [None])[0]


def exchange_code(settings, code):
    data = _token_request(settings, {"grant_type": "authorization_code", "code": code,
                                     "redirect_uri": settings.runame})
    now = time.time()
    return {
        "access_token": data["access_token"],
        "access_expires_at": now + int(data.get("expires_in", 7200)),
        "refresh_token": data["refresh_token"],
        "refresh_expires_at": now + int(data.get("refresh_token_expires_in", 47_304_000)),
    }


def refresh(settings, refresh_token, scopes):
    data = _token_request(settings, {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                     "scope": " ".join(scopes)})
    return {"access_token": data["access_token"],
            "access_expires_at": time.time() + int(data.get("expires_in", 7200))}


def _token_request(settings, form):
    import requests

    basic = base64.b64encode(f"{settings.client_id}:{settings.client_secret}".encode()).decode()
    try:
        resp = requests.post(f"{settings.api_host}/identity/v1/oauth2/token",
                             headers={"Authorization": f"Basic {basic}",
                                      "Content-Type": "application/x-www-form-urlencoded"},
                             data=form, timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        raise EbayAuthError("Couldn't reach eBay. Check your internet connection.")
    if resp.status_code == 200:
        return resp.json()
    try:
        body = resp.json()
    except ValueError:
        body = {}
    error = body.get("error", "")
    if error == "invalid_grant" and form["grant_type"] == "authorization_code":
        raise EbayAuthError("eBay didn't accept that code. A code works once and only for about "
                            "5 minutes: open eBay again and paste the new address.")
    if error == "invalid_grant":
        raise EbayAuthError("eBay no longer accepts the saved authorization. It may have expired "
                            "or been withdrawn; connect the account again.")
    if error == "invalid_scope":
        raise EbayAuthError("eBay refused the requested permissions. Check the OAuth scopes listed "
                            "for your keyset and set EBAY_SCOPES in backend/.env to match.")
    if error == "invalid_client":
        raise EbayAuthError("eBay didn't accept the app keys. Check EBAY_CLIENT_ID and "
                            "EBAY_CLIENT_SECRET, and that they match EBAY_ENVIRONMENT.")
    raise EbayAuthError(f"eBay refused the token request (HTTP {resp.status_code}"
                        f"{': ' + error if error else ''}).")

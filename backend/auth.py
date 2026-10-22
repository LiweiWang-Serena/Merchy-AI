"""
Admin login.

Every /admin page and every admin API endpoint requires a signed-in
session. The shop pages and the checkout API stay public.

The password comes from ADMIN_PASSWORD in backend/.env. If it isn't set,
a random one is generated each time the server starts and printed in the
terminal: secure by default, with nothing to configure for local use. A
fixed default such as "admin" would be the first thing anyone tries on a
deployed site.

Protection lives in one before_request hook with an explicit list of
public endpoints, so a new admin endpoint is protected automatically;
forgetting a decorator can't expose it.
"""

import hmac
import os
import secrets
from urllib.parse import quote

from flask import jsonify, redirect, request, session

PUBLIC_API = {
    ("GET", "/api/health"),
    ("GET", "/api/products"),
    ("GET", "/api/store/best-sellers"),
    ("POST", "/api/checkout/create-session"),
    ("POST", "/api/checkout/confirm"),
    ("POST", "/api/admin/login"),
    ("GET", "/api/support/config"),
    ("POST", "/api/support/chat"),
}
PUBLIC_API_PREFIXES = (("GET", "/api/products/"), ("GET", "/api/support/chat/"))


def init_app(app):
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    app.config["ADMIN_PASSWORD_GENERATED"] = not password
    app.config["ADMIN_PASSWORD"] = password or secrets.token_urlsafe(8)
    app.before_request(_require_admin)


def is_admin():
    return session.get("admin") is True


def check_password(app, candidate):
    # Constant-time comparison, so response timing doesn't leak the password.
    return hmac.compare_digest(str(candidate or "").encode(), app.config["ADMIN_PASSWORD"].encode())


def _require_admin():
    path, method = request.path, request.method
    if path.startswith("/api/"):
        public = (method, path) in PUBLIC_API or any(
            method == m and path.startswith(prefix) for m, prefix in PUBLIC_API_PREFIXES)
        if not public and not is_admin():
            return jsonify({"error": "Sign in to the admin area first."}), 401
    elif path.startswith("/admin") and path != "/admin/login" and not is_admin():
        return redirect(f"/admin/login?next={quote(request.full_path.rstrip('?'))}")
    return None

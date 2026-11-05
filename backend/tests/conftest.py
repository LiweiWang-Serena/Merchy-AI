"""Tests run as on CI: none of the developer's local settings from backend/.env apply."""
import pytest

LOCAL_SETTINGS = [
    "SUPPORT_PROVIDER", "SUPPORT_MODEL", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN", "EBAY_SELLER_USERNAME",
    "EBAY_ENVIRONMENT", "EBAY_RUNAME", "EBAY_SCOPES",
    "SP_API_REFRESH_TOKEN", "SP_API_LWA_APP_ID", "SP_API_LWA_CLIENT_SECRET",
    "SP_API_AWS_ACCESS_KEY", "SP_API_AWS_SECRET_KEY", "SP_API_ROLE_ARN", "SP_API_SELLER_ID",
    "SLACK_WEBHOOK_URL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
    "NOTIFY_EMAIL_TO", "NOTIFY_EMAIL_FROM", "USE_REAL_APIS",
]


@pytest.fixture(autouse=True)
def no_local_settings(monkeypatch):
    for name in LOCAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
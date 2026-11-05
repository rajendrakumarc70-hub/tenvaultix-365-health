"""Settings, read from environment variables. See .env.example for the full list."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
LOGIN_HOST = "https://login.microsoftonline.com"

# Read-only Microsoft Graph application permissions the app registration must request.
# Shown to customers on the Connect page, so each one has a plain-English reason.
GRAPH_PERMISSIONS = [
    ("Directory.Read.All", "Read users, admin roles, licenses and company domains."),
    ("AuditLog.Read.All", "See when each person last signed in and who has set up MFA (Business Premium tenants)."),
    ("UserAuthenticationMethod.Read.All", "See which sign-in methods (Authenticator app, phone, passkey) each person has registered."),
    ("Policy.Read.All", "Read security defaults, Conditional Access, app-consent and guest-invite settings."),
    ("RoleManagement.Read.Directory", "Read who holds admin roles."),
    ("Reports.Read.All", "Read Microsoft 365 usage reports to find unused licenses."),
    ("SecurityEvents.Read.All", "Read your Microsoft Secure Score."),
    ("MailboxSettings.Read", "Read inbox rules to spot email being forwarded outside the company. Never reads email content."),
    ("SharePointTenantSettings.Read.All", "Read SharePoint and OneDrive sharing settings."),
    ("DeviceManagementManagedDevices.Read.All", "Read whether Intune-managed devices meet your security rules."),
]

PLANS = {
    # plan id: (display name, days between automatic scans)
    "trial": ("Free trial", 7),
    "essentials": ("Essentials", 7),
    "business": ("Business", 1),
    "partner": ("Partner", 1),
}
TRIAL_DAYS = 14


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    cert_path: str
    cert_thumbprint: str
    base_url: str
    session_secret: str
    database_path: Path
    https_only: bool
    contact_email: str
    booking_url: str

    @property
    def microsoft_configured(self) -> bool:
        has_credential = bool(self.client_secret or (self.cert_path and self.cert_thumbprint))
        return bool(self.client_id and self.base_url and has_credential)

    def client_credential(self):
        """Certificate (preferred in production) or client secret, in the form MSAL expects."""
        if self.cert_path and self.cert_thumbprint:
            return {"private_key": Path(self.cert_path).read_text(), "thumbprint": self.cert_thumbprint}
        return self.client_secret

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


def load_settings() -> Settings:
    session_secret = _env("TVX_SESSION_SECRET")
    if not session_secret:
        if _env("TVX_ENV") == "production":
            raise RuntimeError("TVX_SESSION_SECRET must be set in production.")
        log.warning("TVX_SESSION_SECRET not set; using a random key. Sign-ins won't survive a restart.")
        session_secret = secrets.token_urlsafe(32)
    base_url = _env("TVX_BASE_URL", "http://localhost:8000")
    return Settings(
        client_id=_env("TVX_CLIENT_ID"),
        client_secret=_env("TVX_CLIENT_SECRET"),
        cert_path=_env("TVX_CERT_PATH"),
        cert_thumbprint=_env("TVX_CERT_THUMBPRINT"),
        base_url=base_url,
        session_secret=session_secret,
        database_path=Path(_env("TVX_DB_PATH", str(BASE_DIR / "data" / "health.db"))),
        https_only=base_url.startswith("https://"),
        contact_email=_env("TVX_CONTACT_EMAIL"),
        booking_url=_env("TVX_BOOKING_URL"),
    )

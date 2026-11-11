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
    cert_private_key: str = ""  # PEM text, for hosts that pass secrets as variables (Cloudflare, Render)
    base_url_source: str = "TVX_BASE_URL"  # where base_url came from, shown on /setup
    session_secret_from_env: bool = True  # False when a temporary random key is in use
    version: str = ""  # commit the host deployed, if it tells us (Render does)
    ops_emails: tuple[str, ...] = ()  # Tenvaultix team members who may open /ops (TVX_OPS_EMAILS, comma-separated)
    ops_tenant_id: str = ""  # the Tenvaultix tenant those people sign in from (TVX_OPS_TENANT_ID); both are required

    @property
    def has_certificate(self) -> bool:
        return bool((self.cert_private_key or self.cert_path) and self.cert_thumbprint)

    @property
    def microsoft_configured(self) -> bool:
        has_credential = bool(self.client_secret or self.has_certificate)
        return bool(self.client_id and self.base_url and has_credential)

    def client_credential(self):
        """Certificate (preferred in production) or client secret, in the form MSAL expects."""
        if self.has_certificate:
            key = self.cert_private_key or Path(self.cert_path).read_text()
            return {"private_key": key, "thumbprint": self.cert_thumbprint.replace(":", "").upper()}
        return self.client_secret

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


def load_settings() -> Settings:
    session_secret = _env("TVX_SESSION_SECRET")
    session_secret_from_env = bool(session_secret)
    if not session_secret:
        if _env("TVX_ENV") == "production":
            raise RuntimeError("TVX_SESSION_SECRET must be set in production.")
        log.warning("TVX_SESSION_SECRET not set; using a random key. Sign-ins won't survive a restart.")
        session_secret = secrets.token_urlsafe(32)
    # The public address: set explicitly, or the address Render gives the service, or local development.
    base_url, base_url_source = _env("TVX_BASE_URL").rstrip("/"), "TVX_BASE_URL"
    if not base_url and _env("RENDER_EXTERNAL_URL"):
        base_url, base_url_source = _env("RENDER_EXTERNAL_URL").rstrip("/"), "Render"
    if not base_url:
        base_url, base_url_source = "http://localhost:8000", "default"
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
        cert_private_key=_env("TVX_CERT_PRIVATE_KEY").replace("\\n", "\n"),
        base_url_source=base_url_source,
        session_secret_from_env=session_secret_from_env,
        version=(_env("TVX_VERSION") or _env("RENDER_GIT_COMMIT"))[:7],
        ops_emails=tuple(e.strip().lower() for e in _env("TVX_OPS_EMAILS").split(",") if e.strip()),
        ops_tenant_id=_env("TVX_OPS_TENANT_ID").strip().lower(),
    )

from app.config import load_settings

from .fakes import make_settings


def test_certificate_from_text_is_preferred_over_secret():
    settings = make_settings(cert_private_key="-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
                             cert_thumbprint="ab:cd:ef")
    assert settings.microsoft_configured
    credential = settings.client_credential()
    assert credential["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert credential["thumbprint"] == "ABCDEF"


def test_secret_used_when_no_certificate():
    settings = make_settings(cert_thumbprint="")
    assert settings.client_credential() == "secret"


def test_private_key_from_environment_keeps_line_breaks(monkeypatch):
    monkeypatch.setenv("TVX_SESSION_SECRET", "x")
    monkeypatch.setenv("TVX_CERT_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----")
    monkeypatch.setenv("TVX_CERT_THUMBPRINT", "AB12")
    monkeypatch.setenv("TVX_CLIENT_ID", "client")
    monkeypatch.setenv("TVX_BASE_URL", "https://example.com")
    settings = load_settings()
    assert settings.microsoft_configured
    assert settings.client_credential()["private_key"].count("\n") == 2

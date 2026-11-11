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


def test_render_address_used_when_base_url_not_set(monkeypatch):
    monkeypatch.delenv("TVX_BASE_URL", raising=False)
    monkeypatch.delenv("TVX_SESSION_SECRET", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://tvx-1.onrender.com/")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "9d4b6ffabcdef")
    settings = load_settings()
    assert settings.base_url == "https://tvx-1.onrender.com" and settings.base_url_source == "Render"
    assert settings.https_only and settings.version == "9d4b6ff"
    assert not settings.session_secret_from_env


def test_explicit_base_url_wins(monkeypatch):
    monkeypatch.setenv("TVX_BASE_URL", "https://health.example.com/")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://tvx-1.onrender.com")
    settings = load_settings()
    assert settings.base_url == "https://health.example.com" and settings.base_url_source == "TVX_BASE_URL"

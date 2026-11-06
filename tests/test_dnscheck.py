import pytest

from app.dnscheck import (DnsError, StaticResolver, check_domain, evaluate_dkim, evaluate_dmarc, evaluate_spf,
                          mail_provider, normalize_domain)


@pytest.mark.parametrize("raw, expected", [
    ("example.com", "example.com"),
    ("  Example.COM ", "example.com"),
    ("https://www.example.com/contact?x=1", "example.com"),
    ("sales@example.co.in", "example.co.in"),
    ("example.com:443", "example.com"),
    ("münchen.example", "xn--mnchen-3ya.example"),
    ("localhost", None),
    ("not a domain", None),
    ("", None),
    ("-bad-.com", None),
    ("a" * 64 + ".com", None),
])
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("records, m365, status, fragment", [
    ([], True, "fail", "No SPF record"),
    (["v=spf1 -all", "v=spf1 include:x ~all"], True, "fail", "several SPF records"),
    (["v=spf1 +all"], False, "fail", "every server"),
    (["v=spf1 include:spf.protection.outlook.com -all"], True, "pass", "blocks servers"),
    (["v=spf1 include:spf.protection.outlook.com ~all"], True, "pass", "soft-fails"),
    (["v=spf1 include:mailgun.org -all"], True, "warn", "Microsoft 365 isn't listed"),
    (["v=spf1 include:spf.protection.outlook.com ?all"], True, "warn", "neutral"),
    (["v=spf1 include:spf.protection.outlook.com"], True, "warn", "doesn't end with"),
    (["v=spf1 " + " ".join(f"include:s{i}.example" for i in range(11)) + " -all"], False, "fail", "11 DNS lookups"),
    (["v=spf1 -all"], True, "pass", "never sends email"),
    (["v=spf1 redirect=_spf.example.com"], True, "pass", ""),
    (["google-site-verification=abc"], False, "fail", "No SPF record"),
])
def test_evaluate_spf(records, m365, status, fragment):
    result = evaluate_spf("example.com", records, m365)
    assert result["status"] == status
    assert fragment in result["summary"]


@pytest.mark.parametrize("records, status, fragment", [
    ([], "fail", "No DMARC record"),
    (["v=DMARC1; p=none; rua=mailto:a@example.com"], "warn", "only monitoring"),
    (["v=DMARC1; p=quarantine; rua=mailto:a@example.com"], "pass", "quarantine"),
    (["v=DMARC1; p=reject"], "pass", "reject"),
    (["v=DMARC1; p=reject; pct=25"], "warn", "25%"),
    (["v=DMARC1; rua=mailto:a@example.com"], "fail", "no valid policy"),
    (["v=DMARC1; p=reject", "v=DMARC1; p=none"], "fail", "several DMARC"),
])
def test_evaluate_dmarc(records, status, fragment):
    result = evaluate_dmarc("example.com", records)
    assert result["status"] == status
    assert fragment in result["summary"]


def test_dmarc_without_report_address_adds_a_note():
    result = evaluate_dmarc("example.com", ["v=DMARC1; p=reject"])
    assert any("rua=" in d for d in result["details"])


def test_dkim_for_microsoft_365():
    target = "selector1-example-com._domainkey.example.onmicrosoft.com"
    active = StaticResolver(txt={target: ["v=DKIM1; k=rsa; p=MIIBIjAN"]},
                            cname={"selector1._domainkey.example.com": target})
    assert evaluate_dkim("example.com", active, True)["status"] == "pass"
    waiting = StaticResolver(cname={"selector1._domainkey.example.com": target})
    assert evaluate_dkim("example.com", waiting, True)["status"] == "warn"
    assert evaluate_dkim("example.com", StaticResolver(), True)["status"] == "fail"


def test_dkim_revoked_key_does_not_count():
    revoked = StaticResolver(txt={"selector1._domainkey.example.com": ["v=DKIM1; p="]})
    assert evaluate_dkim("example.com", revoked, True)["status"] == "fail"


def test_dkim_other_providers_check_common_selectors():
    google = StaticResolver(txt={"google._domainkey.example.com": ["v=DKIM1; k=rsa; p=MIIB"]})
    assert evaluate_dkim("example.com", google, False)["status"] == "pass"
    assert evaluate_dkim("example.com", StaticResolver(), False)["status"] == "warn"


def test_mail_provider():
    assert mail_provider(["example-com.mail.protection.outlook.com"]) == "Microsoft 365"
    assert mail_provider(["aspmx.l.google.com"]) == "Google Workspace"
    assert mail_provider(["mx.zoho.in"]) == "Zoho Mail"
    assert mail_provider(["mx1.mimecast.com"]) == "Other"
    assert mail_provider([]) is None


def test_check_domain_detects_microsoft_365_from_tenant_lookup():
    resolver = StaticResolver(mx={"example.com": ["eu-smtp-inbound-1.mimecast.com"]},
                              txt={"example.com": ["v=spf1 include:_netblocks.mimecast.com -all"]})
    report = check_domain("example.com", resolver, tenant_lookup=lambda d: "tenant-id")
    assert report["provider"] == "Other"
    assert report["uses_microsoft_365"] is True
    assert report["results"]["spf"]["status"] == "warn"  # Microsoft 365 missing from SPF


def test_check_domain_reports_dns_outage():
    class Broken(StaticResolver):
        def mx(self, name):
            raise DnsError("timeout")

    report = check_domain("example.com", Broken(), tenant_lookup=None)
    assert report["error"] == "timeout"
    assert report["results"] == {}


def test_system_resolver_works_without_resolv_conf(monkeypatch, tmp_path):
    from app.dnscheck import FALLBACK_NAMESERVERS, SystemResolver

    monkeypatch.delenv("PREFIX", raising=False)
    resolver = SystemResolver(config_file=str(tmp_path / "missing.conf"))
    assert resolver._resolver.nameservers == FALLBACK_NAMESERVERS

    termux = tmp_path / "etc"
    termux.mkdir()
    (termux / "resolv.conf").write_text("nameserver 9.9.9.9\n")
    monkeypatch.setenv("PREFIX", str(tmp_path))
    resolver = SystemResolver(config_file=str(tmp_path / "missing.conf"))
    assert resolver._resolver.nameservers == ["9.9.9.9"]

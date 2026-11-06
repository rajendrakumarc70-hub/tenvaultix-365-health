"""Email security checks that only need public DNS: SPF, DMARC and DKIM.

Used twice: by the free domain check on the home page (no sign-in), and inside a full tenant scan
for every custom domain the tenant owns.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Protocol

import dns.exception
import dns.resolver
import requests

from .config import LOGIN_HOST

DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
M365_SPF_INCLUDE = "include:spf.protection.outlook.com"
COMMON_DKIM_SELECTORS = ["selector1", "selector2", "google", "default", "k1", "s1", "s2", "dkim", "mail"]
SPF_LOOKUP_TERMS = {"include", "a", "mx", "ptr", "exists", "redirect"}
RANK = {"pass": 0, "warn": 1, "fail": 2}


class DnsError(Exception):
    pass


class Resolver(Protocol):
    def txt(self, name: str) -> list[str]: ...
    def mx(self, name: str) -> list[str]: ...
    def cname(self, name: str) -> str | None: ...


FALLBACK_NAMESERVERS = ["1.1.1.1", "8.8.8.8"]


class SystemResolver:
    """Real DNS via dnspython. Missing records come back empty; outages raise DnsError."""

    def __init__(self, timeout: float = 4.0, config_file: str = "/etc/resolv.conf"):
        self._resolver = self._make_resolver(config_file)
        self._resolver.lifetime = timeout

    @staticmethod
    def _make_resolver(config_file: str) -> dns.resolver.Resolver:
        # Android (Termux) and some minimal containers have no /etc/resolv.conf.
        candidates = [config_file]
        if os.environ.get("PREFIX"):
            candidates.append(os.path.join(os.environ["PREFIX"], "etc", "resolv.conf"))
        for path in candidates:
            try:
                return dns.resolver.Resolver(filename=path)
            except dns.resolver.NoResolverConfiguration:
                continue
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = list(FALLBACK_NAMESERVERS)
        return resolver

    def _query(self, name: str, rtype: str):
        try:
            return list(self._resolver.resolve(name, rtype))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as exc:
            raise DnsError(f"DNS lookup for {name} failed: {exc.__class__.__name__}") from exc

    def txt(self, name: str) -> list[str]:
        return [b"".join(r.strings).decode("utf-8", "replace") for r in self._query(name, "TXT")]

    def mx(self, name: str) -> list[str]:
        answers = sorted(self._query(name, "MX"), key=lambda r: r.preference)
        return [str(r.exchange).rstrip(".").lower() for r in answers]

    def cname(self, name: str) -> str | None:
        answers = self._query(name, "CNAME")
        return str(answers[0].target).rstrip(".").lower() if answers else None


class StaticResolver:
    """In-memory DNS answers, for the sample report and tests. TXT lookups follow CNAMEs like real DNS."""

    def __init__(self, txt: dict | None = None, mx: dict | None = None, cname: dict | None = None):
        self._txt, self._mx, self._cname = txt or {}, mx or {}, cname or {}

    def txt(self, name: str) -> list[str]:
        target = self._cname.get(name)
        return list(self._txt.get(target if target else name, []))

    def mx(self, name: str) -> list[str]:
        return list(self._mx.get(name, []))

    def cname(self, name: str) -> str | None:
        return self._cname.get(name)


def normalize_domain(raw: str | None) -> str | None:
    """Accept 'example.com', 'https://www.example.com/x' or 'me@example.com'; return 'example.com'."""
    text = (raw or "").strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.split(r"[/?#:\s]", text, maxsplit=1)[0].strip(".")
    if text.startswith("www."):
        text = text[4:]
    try:
        text = text.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return text if DOMAIN_RE.match(text) else None


def lookup_m365_tenant(domain: str, timeout: float = 5.0) -> str | None:
    """Return the Microsoft Entra tenant ID that owns the domain, or None. Public endpoint, no auth."""
    try:
        resp = requests.get(f"{LOGIN_HOST}/{domain}/v2.0/.well-known/openid-configuration", timeout=timeout)
        if resp.status_code == 200:
            match = re.search(r"/([0-9a-f-]{36})/", resp.json().get("issuer", ""))
            return match.group(1) if match else None
    except (requests.RequestException, ValueError):
        pass
    return None


def mail_provider(mx_hosts: list[str]) -> str | None:
    if not mx_hosts:
        return None
    host = mx_hosts[0]
    if host.endswith("mail.protection.outlook.com") or host.endswith(".mx.microsoft"):
        return "Microsoft 365"
    if host.endswith("google.com") or host.endswith("googlemail.com"):
        return "Google Workspace"
    if ".zoho." in f".{host}":
        return "Zoho Mail"
    return "Other"


def _result(issues: list[tuple[str, str]], record: str | None, passed: str) -> dict:
    """issues: (status, message). The worst one becomes the summary."""
    if not issues:
        return {"status": "pass", "summary": passed, "record": record, "details": []}
    issues = sorted(issues, key=lambda i: -RANK[i[0]])
    return {
        "status": issues[0][0],
        "summary": issues[0][1],
        "record": record,
        "details": [msg for _, msg in issues[1:]],
    }


def evaluate_spf(domain: str, records: list[str], uses_m365: bool) -> dict:
    spf = [r.strip() for r in records if r.strip().lower().startswith("v=spf1")]
    if not spf:
        return _result([("fail", f"No SPF record. Anyone can send email that claims to be from @{domain}.")], None, "")
    if len(spf) > 1:
        return _result(
            [("fail", "There are several SPF records. Receiving servers treat this as an error, so SPF fails for every email.")],
            " | ".join(spf), "",
        )
    record = spf[0]
    terms = [t.lower() for t in record.split()[1:]]
    if terms == ["-all"]:
        return _result([], record, "SPF says this domain never sends email (v=spf1 -all), which is right for a domain you don't send from.")
    names =[re.split(r"[:/=]", t.lstrip("+-~?"), maxsplit=1)[0] for t in terms]
    all_term = next((t for t in reversed(terms) if re.fullmatch(r"[+\-~?]?all", t)), None)
    has_redirect = "redirect" in names
    lookups = sum(1 for n in names if n in SPF_LOOKUP_TERMS)

    issues: list[tuple[str, str]] = []
    if all_term in ("all", "+all"):
        issues.append(("fail", "The SPF record ends with +all, which lets every server in the world send as you."))
    if lookups > 10:
        issues.append(("fail", f"The SPF record needs {lookups} DNS lookups; the limit is 10, so SPF fails."))
    if all_term is None and not has_redirect:
        issues.append(("warn", "The SPF record doesn't end with -all or ~all, so it doesn't block anyone."))
    if all_term == "?all":
        issues.append(("warn", "The SPF record ends with ?all (neutral), which doesn't block forged senders."))
    if uses_m365 and M365_SPF_INCLUDE not in terms and not has_redirect:
        issues.append(("warn", "Microsoft 365 isn't listed in SPF (include:spf.protection.outlook.com), so your own emails can fail checks."))

    if all_term == "~all":
        passed = "SPF is set up. It soft-fails unknown senders (~all), which works well together with a strict DMARC policy."
    else:
        passed = "SPF is set up and blocks servers you haven't listed."
    return _result(issues, record, passed)


def _dmarc_tags(record: str) -> dict[str, str]:
    tags = {}
    for part in record.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            tags[key.strip().lower()] = value.strip()
    return tags


def evaluate_dmarc(domain: str, records: list[str]) -> dict:
    dmarc = [r.strip() for r in records if r.strip().lower().startswith("v=dmarc1")]
    if not dmarc:
        return _result(
            [("fail", f"No DMARC record. Receivers aren't told to reject fake emails that use @{domain}.")], None, ""
        )
    if len(dmarc) > 1:
        return _result([("fail", "There are several DMARC records, so receivers ignore all of them.")], " | ".join(dmarc), "")
    record = dmarc[0]
    tags = _dmarc_tags(record)
    policy = tags.get("p", "").lower()
    issues: list[tuple[str, str]] = []
    if policy not in ("none", "quarantine", "reject"):
        issues.append(("fail", "The DMARC record has no valid policy (p=), so it has no effect."))
    elif policy == "none":
        issues.append(("warn", "DMARC is only monitoring (p=none). Fake emails using your name are still delivered."))
    try:
        pct = int(tags.get("pct", "100"))
    except ValueError:
        pct = 100
    if policy in ("quarantine", "reject") and pct < 100:
        issues.append(("warn", f"DMARC only applies to {pct}% of failing emails (pct={pct})."))
    passed = {
        "reject": "DMARC is set to reject: receivers block emails that fake your domain.",
        "quarantine": "DMARC is set to quarantine: fake emails using your domain go to spam.",
    }.get(policy, "DMARC is set up.")
    result = _result(issues, record, passed)
    if "rua" not in tags:
        result["details"].append("No report address (rua=) is set, so you can't see who sends email in your name.")
    return result


def _dkim_key_published(resolver: Resolver, name: str) -> bool:
    for txt in resolver.txt(name):
        match = re.search(r"(?:^|;)\s*p=([^;\s]*)", txt)
        if match and match.group(1):
            return True
    return False


def evaluate_dkim(domain: str, resolver: Resolver, uses_m365: bool) -> dict:
    if uses_m365:
        names = [f"selector{n}._domainkey.{domain}" for n in (1, 2)]
        if any(_dkim_key_published(resolver, n) for n in names):
            return _result([], None, "DKIM signing is active for Microsoft 365, so your emails carry a digital signature.")
        if any(resolver.cname(n) for n in names):
            return _result(
                [("warn", "DKIM records are in DNS, but signing isn't turned on in Microsoft Defender yet.")], None, ""
            )
        return _result(
            [("fail", "DKIM isn't set up for Microsoft 365. Your emails go out without a digital signature.")], None, ""
        )
    for selector in COMMON_DKIM_SELECTORS:
        if _dkim_key_published(resolver, f"{selector}._domainkey.{domain}"):
            return _result([], None, f"A DKIM key is published (selector '{selector}').")
    return _result(
        [("warn", "We couldn't find DKIM under common selector names. Ask your email provider to confirm it's turned on.")],
        None, "",
    )


def check_domain(domain: str, resolver: Resolver,
                 tenant_lookup: Callable[[str], str | None] | None = lookup_m365_tenant) -> dict:
    report = {"domain": domain, "mx": [], "provider": None, "uses_microsoft_365": False,
              "tenant_id": None, "results": {}, "error": None}
    try:
        report["mx"] = resolver.mx(domain)
        report["provider"] = mail_provider(report["mx"])
        report["tenant_id"] = tenant_lookup(domain) if tenant_lookup else None
        uses_m365 = report["provider"] == "Microsoft 365" or report["tenant_id"] is not None
        report["uses_microsoft_365"] = uses_m365
        report["results"] = {
            "spf": evaluate_spf(domain, resolver.txt(domain), uses_m365),
            "dmarc": evaluate_dmarc(domain, resolver.txt(f"_dmarc.{domain}")),
            "dkim": evaluate_dkim(domain, resolver, uses_m365),
        }
    except DnsError as exc:
        report["error"] = str(exc)
    return report

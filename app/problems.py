"""Server problems people can see: failed sign-ins, approvals, scans, fixes, Azure cost reads and crashes.

Each problem is stored with a short code (a Microsoft AADSTS number, graph-403, crash...) and turned into a
plain-English title and what to do. Public pages show only the time, code and advice; the details (Microsoft's
own message, the tenant) are shown to the Tenvaultix team on /ops and to a company on its own dashboard.
"""
from __future__ import annotations

import re

AADSTS = re.compile(r"AADSTS\d{5,7}")

# code -> (title, what to do)
HINTS: dict[str, tuple[str, str]] = {
    "AADSTS7000215": ("The app's client secret is wrong",
                      "Put the secret's Value (not the Secret ID) in TVX_CLIENT_SECRET in the host's settings, then "
                      "redeploy. If you lost the Value, create a new secret in Entra."),
    "AADSTS7000222": ("The app's client secret has expired",
                      "Create a new client secret in Entra (Certificates & secrets) and update TVX_CLIENT_SECRET."),
    "AADSTS700016": ("Microsoft can't find the app",
                     "Check TVX_CLIENT_ID matches the Application (client) ID, and that the app registration allows "
                     "multiple Entra ID tenants."),
    "AADSTS700027": ("The app's certificate wasn't accepted",
                     "Check TVX_CERT_THUMBPRINT and TVX_CERT_PRIVATE_KEY match the certificate uploaded in Entra."),
    "AADSTS50011": ("The return address isn't registered",
                    "Add this site's /auth/callback and /connect/callback addresses to the app registration in "
                    "Entra, exactly as shown on the setup page."),
    "AADSTS65001": ("Access hasn't been approved yet",
                    "A Global Administrator of that company needs to approve access from the Connect page."),
    "AADSTS65004": ("The approval was cancelled", "Nothing to fix: the person chose not to approve."),
    "AADSTS90094": ("An admin needs to approve", "Only a Global Administrator can approve access for a company."),
    "AADSTS90099": ("An admin needs to approve", "Only a Global Administrator can approve access for a company."),
    "AADSTS50020": ("Wrong kind of account",
                    "Sign in with a work account from the company's Microsoft 365, not a personal account."),
    "AADSTS53003": ("Blocked by the company's Conditional Access",
                    "The company's own sign-in rules blocked this. Their admin can allow the Tenvaultix app."),
    "AADSTS7000112": ("The app is disabled in that company",
                      "Their admin disabled the app in Entra (Enterprise applications). Ask them to enable it."),
    "AADSTS500011": ("The app isn't set up in that company yet",
                     "A Global Administrator needs to connect the company from the Connect page first."),
    "graph-401": ("Microsoft refused the app's access",
                  "The app may have been removed or its approval withdrawn. An admin can reconnect from the "
                  "Connect page."),
    "graph-403": ("A permission is missing",
                  "An admin can approve the latest permissions from the Connect page. For one-click fixes, the "
                  "admin's role may not allow that change."),
    "graph-429": ("Microsoft asked us to slow down", "Nothing to do: the next scan usually works."),
    "graph-5xx": ("Microsoft had a temporary problem", "Nothing to do: try again later."),
    "azure-no-access": ("Azure costs aren't shared yet",
                        "Give the Tenvaultix app the Cost Management Reader role on the Azure subscriptions."),
    "azure-error": ("Azure costs couldn't be read",
                    "Usually temporary. If it keeps happening, check the details and that Cost Management is "
                    "available for the subscription."),
    "crash": ("The app hit an unexpected error", "It's logged. If it keeps happening, check the host's logs."),
}
DEFAULT_HINT = ("Something went wrong", "Check the details, or try again later.")


OAUTH_ERROR = re.compile(r"^[a-z_]{3,40}$")  # access_denied, invalid_request, ...


def code_for(text: str | None, fallback: str) -> str:
    """The AADSTS code in a Microsoft message, if there is one, else `fallback`."""
    match = AADSTS.search(text or "")
    return match.group(0) if match else fallback


def oauth_code(error: str) -> str:
    """An OAuth error name from a URL can be anything; keep it only if it looks like one."""
    return error if OAUTH_ERROR.match(error or "") else "sign-in"


def graph_code(status: int) -> str:
    if status >= 500:
        return "graph-5xx"
    return f"graph-{status}"


def hint(code: str) -> dict:
    title, advice = HINTS.get(code, DEFAULT_HINT)
    return {"code": code, "title": title, "advice": advice}


SECTIONS = {
    "skus": "Licenses", "users": "Users", "activity": "Sign-in activity", "mfa": "MFA registration",
    "roles": "Admin roles", "security_defaults": "Security defaults", "conditional_access": "Conditional Access",
    "authorization_policy": "App and guest settings", "sharepoint": "SharePoint sharing",
    "forwarding": "Mail forwarding rules", "devices": "Devices", "secure_score": "Secure Score",
}


def grouped(rows: list[dict]) -> list[dict]:
    """Newest-first problems -> one line per code: hint, how many times, last seen, latest detail."""
    groups: dict[str, dict] = {}
    for row in rows:
        group = groups.get(row["code"])
        if group is None:
            groups[row["code"]] = {**hint(row["code"]), "area": row["area"], "count": 1, "last": row["at"],
                                   "detail": row.get("detail") or ""}
        else:
            group["count"] += 1
    return list(groups.values())

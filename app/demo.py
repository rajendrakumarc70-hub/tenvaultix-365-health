"""A fictional tenant ("Acme Diagnostics") used for the public sample report and for tests.

The snapshot has the same shape the real collector produces, so the sample report runs through exactly
the same checks and scoring as a customer's tenant. All names and domains are made up (.example domains
can never be real).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .dnscheck import StaticResolver, check_domain
from .licenses import sku_name

DEMO_TENANT_ID = "demo"
DOMAIN = "acmediagnostics.example"
ALIAS_DOMAIN = "acme-labs.example"

FIRST = ["Aarav", "Priya", "Rohan", "Ananya", "Vikram", "Meera", "Arjun", "Kavya", "Rahul", "Sneha", "Karthik",
         "Divya", "Siddharth", "Pooja", "Nikhil", "Isha", "Aditya", "Neha", "Varun", "Riya", "Manish", "Lakshmi",
         "Suresh", "Fatima", "Imran", "Deepa", "Ganesh", "Shreya", "Harish", "Nisha", "Tom", "Sarah", "David",
         "Emma", "Omar"]
LAST = ["Sharma", "Nair", "Iyer", "Reddy", "Menon", "Kapoor", "Das", "Pillai", "Joshi", "Rao", "Gupta", "Verma",
        "Khan", "Patel", "Bose", "Singh", "Kumar", "Shetty", "Mehta", "Chopra", "Agarwal", "Krishnan", "Naidu",
        "Sheikh", "Ali", "Thomas", "Bhat", "Kulkarni", "Desai", "Varma", "Wilson", "Clarke", "Brown", "Hughes",
        "Haddad"]

SKUS = {  # part number: (sku id, total bought)
    "SPB": ("sku-spb", 5),
    "O365_BUSINESS_PREMIUM": ("sku-bstd", 38),
    "EXCHANGESTANDARD": ("sku-exo1", 4),
    "FLOW_FREE": ("sku-flow", 10000),
}
GLOBAL_ADMINS = [0, 1, 3]
NO_MFA = [3, 15, 22]
IDLE_DAYS = {12: 124, 27: None, 33: 97}  # index: days since last activity (None = never)
FORWARDER = 21


def _resolver() -> StaticResolver:
    return StaticResolver(
        txt={
            DOMAIN: ["v=spf1 include:spf.protection.outlook.com -all", "MS=ms12345678"],
            f"_dmarc.{DOMAIN}": ["v=DMARC1; p=none"],
            ALIAS_DOMAIN: ["v=spf1 -all"],
            f"_dmarc.{ALIAS_DOMAIN}": ["v=DMARC1; p=reject; rua=mailto:dmarc@acmediagnostics.example"],
            f"selector1-{ALIAS_DOMAIN.replace('.', '-')}._domainkey.acmediagnostics.onmicrosoft.com": ["v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC"],
        },
        cname={
            f"selector1._domainkey.{ALIAS_DOMAIN}": f"selector1-{ALIAS_DOMAIN.replace('.', '-')}._domainkey.acmediagnostics.onmicrosoft.com",
        },
        mx={
            DOMAIN: [f"{DOMAIN.replace('.', '-')}.mail.protection.outlook.com"],
            ALIAS_DOMAIN: [f"{ALIAS_DOMAIN.replace('.', '-')}.mail.protection.outlook.com"],
        },
    )


def snapshot(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)

    def ago(days: int | None) -> str | None:
        return None if days is None else (now - timedelta(days=days)).isoformat()

    users = []
    for i, (first, last) in enumerate(zip(FIRST, LAST)):
        if i < 5:
            licenses = ["SPB"]
        elif i < 33:
            licenses = ["O365_BUSINESS_PREMIUM"]
        else:
            licenses = ["EXCHANGESTANDARD"]
        idle = IDLE_DAYS.get(i, i % 9)
        users.append({
            "id": f"user-{i}", "name": f"{first} {last}", "upn": f"{first}.{last}@{DOMAIN}".lower(),
            "enabled": True, "type": "Member", "created": ago(400 + i), "license_skus": licenses,
            "has_mailbox": True, "last_activity": ago(idle), "mfa": i not in NO_MFA,
        })
    users.append({"id": "user-reception", "name": "Reception Desk", "upn": f"reception@{DOMAIN}", "enabled": True,
                  "type": "Member", "created": ago(700), "license_skus": [], "has_mailbox": False,
                  "last_activity": ago(2), "mfa": None})
    for n, (name, upn) in enumerate([("Rakesh Menon", "rakesh.menon"), ("Julia Fernandes", "julia.fernandes")]):
        users.append({"id": f"user-left-{n}", "name": name, "upn": f"{upn}@{DOMAIN}", "enabled": False,
                      "type": "Member", "created": ago(900), "license_skus": ["O365_BUSINESS_PREMIUM"],
                      "has_mailbox": True, "last_activity": ago(160 + 40 * n), "mfa": True})
    for name, upn, idle, created in [
        ("Audit Partner", "audit_ca-firm.example#EXT#", 210, 400),
        ("IT Consultant", "consultant_itpartner.example#EXT#", 6, 200),
        ("Lab Supplies Rep", "orders_labsupplies.example#EXT#", None, 320),
    ]:
        users.append({"id": f"guest-{upn[:5]}", "name": name, "upn": f"{upn}@acmediagnostics.onmicrosoft.com",
                      "enabled": True, "type": "Guest", "created": ago(created), "license_skus": [],
                      "has_mailbox": False, "last_activity": ago(idle), "mfa": None})

    consumed = {part: sum(part in u["license_skus"] for u in users) for part in SKUS}
    consumed["FLOW_FREE"] = 3
    skus = [{"sku_id": sid, "part_number": part, "name": sku_name(part), "enabled": total,
             "consumed": consumed[part], "has_mailbox": part != "FLOW_FREE"}
            for part, (sid, total) in SKUS.items()]

    devices = []
    for i in range(34):
        state = "inGracePeriod" if i == 25 else "compliant"
        os_name = "iOS" if i % 5 == 0 else "Android" if i % 7 == 0 else "Windows"
        devices.append({"name": f"ACME-{'PH' if os_name != 'Windows' else 'LT'}-{i + 1:03d}", "os": os_name,
                        "compliance": state, "user": users[i]["upn"]})

    resolver = _resolver()
    return {
        "tenant": {"id": DEMO_TENANT_ID, "name": "Acme Diagnostics Pvt Ltd",
                   "domains": [{"name": DOMAIN, "default": True, "initial": False},
                               {"name": ALIAS_DOMAIN, "default": False, "initial": False},
                               {"name": "acmediagnostics.onmicrosoft.com", "default": False, "initial": True}]},
        "collected_at": now.isoformat(),
        "features": {"entra_p1": True, "entra_p2": False, "intune": True, "exchange": True, "sharepoint": True},
        "unavailable": {},
        "skus": skus,
        "users": users,
        "activity": True, "activity_source": "sign-in logs",
        "mfa": True, "mfa_source": "registration report",
        "roles": [
            {"name": "Global Administrator", "template_id": "62e90394-69f5-4237-9190-012177145e10",
             "members": [f"user-{i}" for i in GLOBAL_ADMINS]},
            {"name": "Exchange Administrator", "template_id": "29232cdf-9323-42fd-ade2-1d097af3e4de",
             "members": ["user-2"]},
            {"name": "Directory Readers", "template_id": "88d8e3e3-8f55-4a1e-953a-9b9898b8876b",
             "members": ["user-7"]},
        ],
        "security_defaults": False,
        "conditional_access": [
            {"name": "Require MFA for admins", "state": "enabled", "include_users": [],
             "include_apps": ["All"], "client_app_types": ["all"], "grant_controls": ["mfa"], "auth_strength": False},
            {"name": "Block legacy authentication", "state": "enabled",
             "include_users": ["All"], "include_apps": ["All"], "client_app_types": ["exchangeActiveSync", "other"],
             "grant_controls": ["block"], "auth_strength": False},
        ],
        "authorization_policy": {
            "allowInvitesFrom": "adminsGuestInvitersAndAllMembers",
            "defaultUserRolePermissions": {
                "allowedToCreateApps": True,
                "permissionGrantPoliciesAssigned": [
                    "ManagePermissionGrantsForSelf.microsoft-user-default-legacy",
                    "ManagePermissionGrantsForOwnedResource.microsoft-dynamically-managed-permissions-for-team",
                ],
            },
        },
        "sharepoint": {"sharing_capability": "externalUserAndGuestSharing"},
        "forwarding": [{"user": f"{users[FORWARDER]['name']} ({users[FORWARDER]['upn']})",
                        "rule": "Copy invoices", "targets": ["invoices.backup@mailbox.example"]}],
        "mailboxes_checked": sum(1 for u in users if u["enabled"] and u["has_mailbox"]),
        "devices": devices,
        "secure_score": {"current": 38.5, "max": 92.0},
        "dns": [check_domain(d, resolver, tenant_lookup=lambda _: DEMO_TENANT_ID) for d in (DOMAIN, ALIAS_DOMAIN)],
    }


# ---------------------------------------------------------------- Azure spend sample (Indian rupees)

AZURE_SERVICES = [  # name, typical cost per day in INR, weekend factor
    ("Virtual Machines", 920, 0.8), ("Azure SQL Database", 360, 0.55), ("Azure App Service", 240, 1.0),
    ("Storage", 215, 1.0), ("Azure Backup", 160, 1.0), ("Microsoft Defender for Cloud", 118, 1.0),
    ("Bandwidth", 62, 0.6), ("Azure Monitor", 44, 0.9), ("Virtual Network", 31, 1.0), ("Azure DNS", 3.2, 1.0),
    ("Key Vault", 1.8, 0.7),
]


def azure_costs(today=None) -> dict:
    """A month of made-up Azure spend, shaped like azure_costs.fetch() returns, with data up to two days ago."""
    from . import azure_costs as costs

    today = today or datetime.now(timezone.utc).date()
    rows = []
    last = max(1, today.day - 2)  # Azure adds costs a day or two late
    for day in range(1, last + 1):
        when = today.replace(day=day)
        weekend = when.weekday() >= 5
        for i, (name, per_day, weekend_factor) in enumerate(AZURE_SERVICES):
            wobble = 1 + (((day * 7 + i * 13) % 11) - 5) / 50  # +-10%, the same every time
            cost = per_day * wobble * (weekend_factor if weekend else 1)
            if name == "Virtual Machines" and day == 6:
                cost += 2400  # someone left a large test VM running for a day
            rows.append({"date": when.isoformat(), "service": name, "cost": round(cost, 2), "currency": "INR"})
    prod = sum(r["cost"] for r in rows if r["service"] not in ("Azure App Service", "Key Vault"))
    subs = [
        {"id": "demo-prod", "name": "Acme Production", "status": "ok", "cost": prod, "currency": "INR"},
        {"id": "demo-dev", "name": "Acme Dev/Test", "status": "ok",
         "cost": sum(r["cost"] for r in rows) - prod, "currency": "INR"},
        {"id": "demo-fin", "name": "Finance sandbox", "status": "no_access", "cost": 0.0, "currency": "",
         "error": "403 AuthorizationFailed"},
    ]
    data = costs.summarise(rows, subs, today)
    data["fetched_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return data

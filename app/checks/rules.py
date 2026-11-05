"""The logic of each check. Every rule reads the tenant snapshot built by collector.py and returns an
Outcome. `needs` lists the snapshot sections a rule depends on; if any is missing (licence, permission
or outage) the engine reports the check as 'not available' with the reason instead of running it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..licenses import sku_name, sku_price
from . import Outcome

GLOBAL_ADMIN_TEMPLATE = "62e90394-69f5-4237-9190-012177145e10"
INACTIVE_DAYS = 90
NEEDS_P1 = "Needs Microsoft Entra ID P1, which comes with Microsoft 365 Business Premium (or E3/E5)."
LEGACY_CLIENTS = {"exchangeActiveSync", "other"}
NONCOMPLIANT = {"noncompliant", "conflict", "error"}


@dataclass
class Rule:
    fn: Callable[[dict], Outcome]
    needs: tuple[str, ...]


RULES: dict[str, Rule] = {}


def rule(check_id: str, *needs: str):
    def register(fn):
        RULES[check_id] = Rule(fn, needs)
        return fn
    return register


# ---------------------------------------------------------------- helpers

def _now(snap: dict) -> datetime:
    return _parse(snap["collected_at"]) or datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days_since(snap: dict, value: str | None) -> int | None:
    dt = _parse(value)
    return None if dt is None else (_now(snap) - dt).days


def _who(user: dict) -> str:
    return f"{user['name']} ({user['upn']})"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _verb(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _all_of(n: int, noun: str, verb_one: str, verb_many: str) -> str:
    """'All 3 staff accounts have' / 'Your only staff account has'."""
    return f"Your only {noun} {verb_one}" if n == 1 else f"All {n} {noun}s {verb_many}"


def _money(amount: float) -> str:
    return f"${amount:,.0f}"


def staff(snap: dict) -> list[dict]:
    """Enabled, licensed member accounts: the people who actually work in the tenant."""
    return [u for u in snap["users"] if u["enabled"] and u["type"] == "Member" and u["license_skus"]]


def admin_roles(snap: dict) -> dict[str, list[str]]:
    """user id -> names of the admin roles they hold."""
    holders: dict[str, list[str]] = {}
    for role in snap["roles"]:
        if "administrator" not in role["name"].lower():
            continue
        for user_id in role["members"]:
            holders.setdefault(user_id, []).append(role["name"])
    return holders


def _inactive(snap: dict, users: list[dict]) -> list[tuple[dict, int | None]]:
    """(user, days since last activity or None for never) for users idle INACTIVE_DAYS+ days."""
    found = []
    for user in users:
        age = _days_since(snap, user.get("created"))
        if age is not None and age < INACTIVE_DAYS:
            continue  # new joiners haven't had the chance to be active yet
        idle = _days_since(snap, user.get("last_activity"))
        if idle is None or idle >= INACTIVE_DAYS:
            found.append((user, idle))
    return found


def _idle_text(idle: int | None) -> str:
    return "no activity recorded" if idle is None else f"last active {idle} days ago"


def _user_license_cost(user: dict) -> tuple[float, list[str]]:
    total, names = 0.0, []
    for part in user["license_skus"]:
        price = sku_price(part)
        if price == 0:
            continue
        total += price or 0
        names.append(sku_name(part))
    return total, names


def _requires_mfa(policy: dict) -> bool:
    return "mfa" in policy["grant_controls"] or policy["auth_strength"]


def _covers_everyone(policy: dict) -> bool:
    return "All" in policy["include_users"] and "All" in policy["include_apps"]


# ---------------------------------------------------------------- sign-in & MFA

@rule("MFA-002", "security_defaults")
def mfa_enforced(snap: dict) -> Outcome:
    if snap["security_defaults"]:
        return Outcome("pass", "Security defaults are on, so everyone has to use MFA.")
    policies = snap.get("conditional_access")
    if policies:
        enforced = [p for p in policies if p["state"] == "enabled" and _requires_mfa(p)]
        everyone = [p for p in enforced if _covers_everyone(p)]
        if everyone:
            return Outcome("pass", f"Conditional Access policy '{everyone[0]['name']}' requires MFA for everyone.")
        if enforced:
            return Outcome(
                "warn",
                "MFA is only required for some people or apps. Everyone else can still sign in with just a password.",
                [f"Policy: {p['name']}" for p in enforced],
            )
        report_only = [p for p in policies if p["state"] == "enabledForReportingButNotEnforced" and _requires_mfa(p)]
        if report_only:
            return Outcome(
                "fail",
                "An MFA policy exists but is only in report-only mode, so nobody is actually asked for MFA.",
                [f"Report-only policy: {p['name']}" for p in report_only],
            )
    summary = "Nothing forces people to use MFA: security defaults are off and no Conditional Access policy requires it."
    if policies is None:
        summary += " Your plan doesn't include Conditional Access, so turning on security defaults is the free fix."
    return Outcome("fail", summary)


@rule("MFA-001", "users", "mfa")
def staff_without_mfa(snap: dict) -> Outcome:
    people = staff(snap)
    if not people:
        return Outcome("info", "We didn't find any licensed staff accounts.")
    without = [u for u in people if u.get("mfa") is False]
    if not without:
        return Outcome("pass", f"{_all_of(len(people), 'staff account', 'has', 'have')} MFA set up.")
    status = "warn" if len(without) / len(people) <= 0.10 else "fail"
    return Outcome(
        status,
        f"{len(without)} of {_plural(len(people), 'staff account')} {_verb(len(without), 'has', 'have')} no MFA method set up.",
        [_who(u) for u in without],
    )


@rule("MFA-003", "security_defaults")
def legacy_auth_blocked(snap: dict) -> Outcome:
    if snap["security_defaults"]:
        return Outcome("pass", "Security defaults block old sign-in methods.")
    policies = snap.get("conditional_access") or []

    def blocks(p: dict) -> bool:
        return ("All" in p["include_users"] and LEGACY_CLIENTS & set(p["client_app_types"])
                and "block" in p["grant_controls"])

    active = [p for p in policies if p["state"] == "enabled" and blocks(p)]
    if active:
        return Outcome("pass", f"Conditional Access policy '{active[0]['name']}' blocks old sign-in methods.")
    summary = ("Old sign-in methods (IMAP, POP, basic SMTP, very old Office apps) aren't blocked. "
               "Attackers use them because they skip MFA.")
    report_only = [p for p in policies if p["state"] == "enabledForReportingButNotEnforced" and blocks(p)]
    evidence = [f"Report-only policy (not enforced): {p['name']}" for p in report_only]
    if report_only:
        summary += " A blocking policy exists but is only in report-only mode."
    return Outcome("fail", summary, evidence)


# ---------------------------------------------------------------- admins & apps

@rule("ADM-001", "users", "roles", "mfa")
def admins_without_mfa(snap: dict) -> Outcome:
    holders = admin_roles(snap)
    admins = [u for u in snap["users"] if u["id"] in holders and u["enabled"]]
    if not admins:
        return Outcome("info", "We didn't find any active admin accounts.")
    without = [u for u in admins if u.get("mfa") is False]
    if not without:
        return Outcome("pass", f"{_all_of(len(admins), 'admin account', 'has', 'have')} MFA set up.")
    return Outcome(
        "fail",
        f"{_plural(len(without), 'admin account')} {'has' if len(without) == 1 else 'have'} no MFA. "
        "One stolen admin password is enough to take over the whole tenant.",
        [f"{_who(u)}: {', '.join(holders[u['id']])}" for u in without],
    )


@rule("ADM-002", "users", "roles")
def global_admin_count(snap: dict) -> Outcome:
    ga_ids = {m for r in snap["roles"]
              if r.get("template_id") == GLOBAL_ADMIN_TEMPLATE or r["name"] == "Global Administrator"
              for m in r["members"]}
    admins = [u for u in snap["users"] if u["id"] in ga_ids and u["enabled"]]
    evidence = [_who(u) for u in admins]
    count = len(admins)
    if count == 0:
        return Outcome("warn", "We couldn't find an active Global Administrator user. If you use groups or "
                               "Privileged Identity Management for admin roles, check this by hand.")
    if count == 1:
        return Outcome("warn", "Only one person is a Global Administrator. If they leave or lose their phone, nobody "
                               "can manage the tenant. Have at least two.", evidence)
    if count >= 5:
        return Outcome("fail", f"{count} people are Global Administrators. Microsoft recommends fewer than five, "
                               "because each one is another way in for attackers.", evidence)
    return Outcome("pass", f"{count} Global Administrators, within Microsoft's recommended range.", evidence)


@rule("ADM-003", "authorization_policy")
def user_consent(snap: dict) -> Outcome:
    perms = snap["authorization_policy"].get("defaultUserRolePermissions") or {}
    assigned = [p for p in perms.get("permissionGrantPoliciesAssigned") or []
                if p.startswith("ManagePermissionGrantsForSelf.")]
    if any(p.endswith("microsoft-user-default-legacy") for p in assigned):
        return Outcome("fail", "Any staff member can give an outside app access to their mailbox and files. "
                               "Attackers use fake app-approval links to steal data this way.")
    if assigned:
        return Outcome("pass", "Staff can only approve low-risk apps; everything else needs an admin.")
    return Outcome("pass", "Only admins can approve apps that want access to company data.")


@rule("ADM-004", "authorization_policy")
def users_register_apps(snap: dict) -> Outcome:
    perms = snap["authorization_policy"].get("defaultUserRolePermissions") or {}
    if perms.get("allowedToCreateApps", True):
        return Outcome("warn", "Any staff member can register new apps in your tenant.")
    return Outcome("pass", "Only admins and app developers can register apps.")


# ---------------------------------------------------------------- accounts & guests

@rule("ACC-001", "users", "activity")
def inactive_accounts(snap: dict) -> Outcome:
    idle = _inactive(snap, staff(snap))
    if not idle:
        return Outcome("pass", f"Every licensed account has been used in the last {INACTIVE_DAYS} days.")
    return Outcome(
        "fail",
        f"{_plural(len(idle), 'account')} can still sign in but {'hasn' if len(idle) == 1 else 'haven'}'t "
        f"been used for {INACTIVE_DAYS}+ days.",
        [f"{_who(u)}: {_idle_text(d)}" for u, d in idle],
    )


@rule("ACC-002", "authorization_policy")
def guest_invites(snap: dict) -> Outcome:
    setting = snap["authorization_policy"].get("allowInvitesFrom", "everyone")
    if setting == "everyone":
        return Outcome("fail", "Anyone in your tenant, even existing guests, can invite outside people in.")
    return {
        "adminsGuestInvitersAndAllMembers": Outcome("pass", "Staff can invite guests; guests can't invite others."),
        "adminsAndGuestInviters": Outcome("pass", "Only admins and approved guest inviters can invite guests."),
        "none": Outcome("pass", "Nobody can invite guests."),
    }.get(setting, Outcome("pass", "Guest invitations are restricted."))


@rule("ACC-003", "users", "activity")
def stale_guests(snap: dict) -> Outcome:
    if snap.get("activity_source") != "sign-in logs":
        return Outcome("not_available", NEEDS_P1)
    guests = [u for u in snap["users"] if u["type"] == "Guest" and u["enabled"]]
    if not guests:
        return Outcome("pass", "There are no guest accounts.")
    stale = _inactive(snap, guests)
    if not stale:
        return Outcome("pass", f"{_all_of(len(guests), 'guest', 'signed', 'signed')} in during the last {INACTIVE_DAYS} days.")
    return Outcome(
        "fail",
        f"{len(stale)} of {_plural(len(guests), 'guest')} {_verb(len(stale), 'hasn', 'haven')}'t signed in for "
        f"{INACTIVE_DAYS}+ days but still {_verb(len(stale), 'has', 'have')} access.",
        [f"{_who(u)}: {_idle_text(d)}" for u, d in stale],
    )


# ---------------------------------------------------------------- email

def _domains_outcome(snap: dict, key: str, label: str) -> Outcome:
    reports = snap["dns"]
    if not reports:
        return Outcome("info", "You only use the onmicrosoft.com domain, which Microsoft looks after.")
    rank = {"pass": 0, "warn": 1, "fail": 2}
    evidence, worst, problems = [], "pass", 0
    for rep in reports:
        if rep.get("error"):
            evidence.append(f"{rep['domain']}: we couldn't look up DNS records")
            continue
        found = rep["results"][key]
        if found["status"] != "pass":
            problems += 1
            evidence.append(f"{rep['domain']}: {found['summary']}")
            if rank[found["status"]] > rank[worst]:
                worst = found["status"]
    if problems == 0:
        if evidence:
            return Outcome("warn", f"We couldn't check {label} on every domain.", evidence)
        return Outcome("pass", f"{label} is set up correctly on {_plural(len(reports), 'domain')}.")
    return Outcome(worst, f"{label} needs attention on {problems} of {_plural(len(reports), 'domain')}.", evidence)


@rule("EML-001", "dns")
def spf(snap: dict) -> Outcome:
    return _domains_outcome(snap, "spf", "SPF")


@rule("EML-002", "dns")
def dmarc(snap: dict) -> Outcome:
    return _domains_outcome(snap, "dmarc", "DMARC")


@rule("EML-003", "dns")
def dkim(snap: dict) -> Outcome:
    return _domains_outcome(snap, "dkim", "DKIM")


@rule("EML-004", "forwarding")
def external_forwarding(snap: dict) -> Outcome:
    rules = snap["forwarding"]
    checked = snap.get("mailboxes_checked", 0)
    if not rules:
        return Outcome("pass", f"No inbox rules forward email outside the company ({_plural(checked, 'mailbox', 'mailboxes')} checked).")
    return Outcome(
        "fail",
        f"{_plural(len(rules), 'inbox rule')} forward{'s' if len(rules) == 1 else ''} email to outside addresses.",
        [f"{r['user']}: rule '{r['rule']}' sends to {', '.join(r['targets'])}" for r in rules],
    )


# ---------------------------------------------------------------- sharing

@rule("SHR-001", "sharepoint")
def anyone_links(snap: dict) -> Outcome:
    capability = snap["sharepoint"].get("sharing_capability")
    return {
        "externalUserAndGuestSharing": Outcome(
            "fail", "SharePoint and OneDrive allow 'Anyone' links, which open files without signing in."),
        "externalUserSharingOnly": Outcome(
            "pass", "Outsiders can be invited to files, but they must sign in."),
        "existingExternalUserSharingOnly": Outcome(
            "pass", "Files can only be shared with guests who are already in your tenant."),
        "disabled": Outcome("pass", "Sharing outside the company is turned off."),
    }.get(capability, Outcome("info", f"Sharing setting: {capability}"))


# ---------------------------------------------------------------- licenses

@rule("LIC-001", "skus")
def unassigned_licenses(snap: dict) -> Outcome:
    waste, unused_total, evidence = 0.0, 0, []
    for sku in snap["skus"]:
        unused = sku["enabled"] - sku["consumed"]
        price = sku_price(sku["part_number"])
        if unused <= 0 or price == 0:
            continue
        if price is None and sku["enabled"] >= 1000:
            continue  # very large unknown pools are free or trial offers
        unused_total += unused
        if price:
            waste += unused * price
            evidence.append(f"{sku_name(sku['part_number'])}: {unused} of {sku['enabled']} unassigned, about {_money(unused * price)}/month")
        else:
            evidence.append(f"{sku_name(sku['part_number'])}: {unused} of {sku['enabled']} unassigned (price unknown)")
    if not unused_total:
        return Outcome("pass", "Every paid license is assigned to someone.")
    summary = f"{_plural(unused_total, 'paid license')} {'isn' if unused_total == 1 else 'aren'}'t assigned to anyone"
    summary += f", about {_money(waste)} a month." if waste else "."
    return Outcome("fail", summary, evidence, waste)


@rule("LIC-002", "users", "skus")
def licensed_blocked_accounts(snap: dict) -> Outcome:
    waste, evidence = 0.0, []
    for user in snap["users"]:
        if user["enabled"] or not user["license_skus"]:
            continue
        cost, names = _user_license_cost(user)
        if not names:
            continue
        waste += cost
        evidence.append(f"{_who(user)}: {', '.join(names)}")
    if not evidence:
        return Outcome("pass", "No blocked accounts are holding paid licenses.")
    return Outcome(
        "fail",
        f"{_plural(len(evidence), 'blocked account')} still {'has' if len(evidence) == 1 else 'have'} paid licenses"
        + (f", about {_money(waste)} a month." if waste else "."),
        evidence, waste,
    )


@rule("LIC-003", "users", "activity", "skus")
def licensed_inactive_users(snap: dict) -> Outcome:
    waste, evidence = 0.0, []
    for user, idle in _inactive(snap, staff(snap)):
        cost, names = _user_license_cost(user)
        if not names:
            continue
        waste += cost
        evidence.append(f"{_who(user)}: {', '.join(names)}, {_idle_text(idle)}")
    if not evidence:
        return Outcome("pass", f"Everyone with a paid license has used Microsoft 365 in the last {INACTIVE_DAYS} days.")
    return Outcome(
        "fail",
        f"{_plural(len(evidence), 'person', 'people')} with paid licenses {'hasn' if len(evidence) == 1 else 'haven'}'t "
        f"used Microsoft 365 for {INACTIVE_DAYS}+ days" + (f", about {_money(waste)} a month." if waste else "."),
        evidence, waste,
    )


# ---------------------------------------------------------------- devices & overview

@rule("DEV-001", "devices")
def device_compliance(snap: dict) -> Outcome:
    devices = snap["devices"]
    if not devices:
        return Outcome("warn", "Intune is included in your plan, but no devices are enrolled, so laptops and phones aren't checked.")
    bad = [d for d in devices if d["compliance"] in NONCOMPLIANT]
    grace = [d for d in devices if d["compliance"] == "inGracePeriod"]

    def line(d: dict) -> str:
        return f"{d['name']} ({d['os']}), {d['user'] or 'no user'}: {d['compliance']}"

    if bad:
        return Outcome("fail", f"{len(bad)} of {_plural(len(devices), 'managed device')} "
                               f"{_verb(len(bad), 'doesn', 'don')}'t meet your security rules.",
                       [line(d) for d in bad + grace])
    if grace:
        return Outcome("warn", f"{len(grace)} of {_plural(len(devices), 'managed device')} will fall out of compliance soon.",
                       [line(d) for d in grace])
    return Outcome("pass", f"{_all_of(len(devices), 'managed device', 'meets', 'meet')} your security rules.")


@rule("GEN-001", "secure_score")
def secure_score(snap: dict) -> Outcome:
    score = snap["secure_score"]
    if not score.get("max"):
        return Outcome("info", "Microsoft hasn't calculated a Secure Score for this tenant yet.")
    pct = 100 * score["current"] / score["max"]
    return Outcome("info", f"Your Microsoft Secure Score is {pct:.0f}% ({score['current']:.0f} of {score['max']:.0f} points).")

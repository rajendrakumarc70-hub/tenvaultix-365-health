"""Reads a snapshot of a customer's tenant from Microsoft Graph. Read-only, settings and metadata only:
we never read email content or files.

Each section is fetched on its own. If one fails (licence missing, permission missing, outage) the reason
goes into snapshot["unavailable"] and the scan carries on, so one gap never blocks the whole report.
Token failures (consent revoked) do stop the scan, because nothing else would work either.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from .dnscheck import DnsError, Resolver, check_domain
from .graph import GraphClient, GraphError
from .licenses import sku_name

log = logging.getLogger(__name__)

P1_PLANS = {"AAD_PREMIUM", "AAD_PREMIUM_P2"}
P2_PLANS = {"AAD_PREMIUM_P2"}
INTUNE_PLANS = {"INTUNE_A", "INTUNE_SMBIZ", "INTUNE_EDU"}
MAILBOX_PLANS = {"EXCHANGE_S_STANDARD", "EXCHANGE_S_ENTERPRISE", "EXCHANGE_S_ESSENTIALS", "EXCHANGE_S_DESKLESS"}
NOT_MFA = {
    "#microsoft.graph.passwordAuthenticationMethod",
    "#microsoft.graph.emailAuthenticationMethod",
    "#microsoft.graph.temporaryAccessPassAuthenticationMethod",
}
MAX_DOMAINS = 5

NO_PERMISSION = ("We don't have permission to read this. An admin can approve the latest permissions from "
                 "the Connect page.")
NEEDS_P1 = "Needs Microsoft Entra ID P1, which comes with Microsoft 365 Business Premium (or E3/E5)."
NEEDS_INTUNE = "Needs Microsoft Intune, which comes with Microsoft 365 Business Premium (or E3/E5)."
NO_SHAREPOINT = "Your plan doesn't include SharePoint Online."
CONCEALED = ("Microsoft 365 reports are hiding user names. A Global Administrator can show them: Microsoft 365 "
             "admin center > Settings > Org settings > Services > Reports, then turn off the option that "
             "conceals user, group and site names.")


class ReportsConcealed(GraphError):
    """Usage reports came back with hashed user names, so we can't match them to people."""

    def __init__(self):
        super().__init__(200, "reportsConcealed", CONCEALED)


def _reason(exc: Exception) -> str:
    if isinstance(exc, ReportsConcealed):
        return CONCEALED
    if isinstance(exc, GraphError):
        if exc.needs_premium:
            return NEEDS_P1
        if exc.forbidden:
            return NO_PERMISSION
        return f"Microsoft Graph returned an error: {exc.message[:160]}"
    if isinstance(exc, DnsError):
        return str(exc)
    return "Unexpected error while reading this data."


def _iso_date(day: str) -> str | None:
    return f"{day}T00:00:00Z" if day else None


class Collector:
    def __init__(self, graph: GraphClient, resolver: Resolver, tenant_id: str, *,
                 max_mailboxes: int = 500, workers: int = 8, now: datetime | None = None):
        self.graph = graph
        self.resolver = resolver
        self.tenant_id = tenant_id
        self.max_mailboxes = max_mailboxes
        self.workers = workers
        self.snap: dict = {
            "tenant": {"id": tenant_id, "name": None, "domains": []},
            "collected_at": (now or datetime.now(timezone.utc)).isoformat(),
            "features": {}, "unavailable": {},
            "skus": None, "users": None, "activity": None, "activity_source": None,
            "mfa": None, "mfa_source": None, "roles": None, "security_defaults": None,
            "conditional_access": None, "authorization_policy": None, "sharepoint": None,
            "forwarding": None, "mailboxes_checked": 0, "devices": None, "secure_score": None, "dns": None,
        }

    def _step(self, section: str, fn: Callable[[], object]) -> None:
        try:
            self.snap[section] = fn()
        except (GraphError, DnsError) as exc:
            log.info("Section %s unavailable for %s: %s", section, self.tenant_id, exc)
            self.snap[section] = None
            self.snap["unavailable"][section] = _reason(exc)

    def _skip(self, section: str, reason: str) -> None:
        self.snap[section] = None
        self.snap["unavailable"][section] = reason

    def run(self) -> dict:
        self._organization()
        self._step("skus", self._skus)
        self._step("users", self._users)
        self._step("roles", self._roles)
        if self.snap["users"] is not None:
            self._step("activity", self._activity)
            self._step("mfa", self._mfa)
        else:
            for section in ("activity", "mfa", "forwarding"):
                self._skip(section, self.snap["unavailable"].get("users", "User list unavailable."))
        self._step("security_defaults", self._security_defaults)
        if self.snap["features"].get("entra_p1"):
            self._step("conditional_access", self._conditional_access)
        else:
            self._skip("conditional_access", NEEDS_P1)
        self._step("authorization_policy", self._authorization_policy)
        if self.snap["features"].get("sharepoint", True):
            self._step("sharepoint", self._sharepoint)
        else:
            self._skip("sharepoint", NO_SHAREPOINT)
        if self.snap["users"] is not None:
            self._step("forwarding", self._forwarding)
        if self.snap["features"].get("intune"):
            self._step("devices", self._devices)
        else:
            self._skip("devices", NEEDS_INTUNE)
        self._step("secure_score", self._secure_score)
        self._step("dns", self._dns)
        return self.snap

    # ---------------------------------------------------------------- sections

    def _organization(self) -> None:
        org = self.graph.get_all("/organization", {"$select": "id,displayName,verifiedDomains"})[0]
        self.snap["tenant"]["name"] = org.get("displayName")
        self.snap["tenant"]["domains"] = [
            {"name": d["name"].lower(), "default": d.get("isDefault", False), "initial": d.get("isInitial", False)}
            for d in org.get("verifiedDomains", [])
        ]

    def _skus(self) -> list[dict]:
        skus, plans = [], set()
        for sku in self.graph.get_all("/subscribedSkus"):
            if sku.get("capabilityStatus") != "Enabled":
                continue
            sku_plans = {p["servicePlanName"] for p in sku.get("servicePlans", [])
                         if p.get("provisioningStatus") in ("Success", "PendingActivation", "PendingProvisioning")}
            enabled = (sku.get("prepaidUnits") or {}).get("enabled", 0)
            if enabled > 0:
                plans |= sku_plans
            skus.append({
                "sku_id": sku["skuId"], "part_number": sku["skuPartNumber"], "name": sku_name(sku["skuPartNumber"]),
                "enabled": enabled, "consumed": sku.get("consumedUnits", 0),
                "has_mailbox": bool(sku_plans & MAILBOX_PLANS),
            })
        self.snap["features"] = {
            "entra_p1": bool(plans & P1_PLANS),
            "entra_p2": bool(plans & P2_PLANS),
            "intune": bool(plans & INTUNE_PLANS),
            "exchange": bool(plans & MAILBOX_PLANS),
            "sharepoint": any(p.startswith("SHAREPOINT") for p in plans),
        }
        return skus

    def _users(self) -> list[dict]:
        by_id = {s["sku_id"]: s for s in self.snap["skus"] or []}
        users = []
        for u in self.graph.iter_all("/users", {
            "$select": "id,displayName,userPrincipalName,accountEnabled,userType,assignedLicenses,createdDateTime",
            "$top": "999",
        }):
            skus = [by_id[a["skuId"]] for a in u.get("assignedLicenses") or [] if a.get("skuId") in by_id]
            users.append({
                "id": u["id"], "name": u.get("displayName") or u.get("userPrincipalName"),
                "upn": u.get("userPrincipalName", ""), "enabled": bool(u.get("accountEnabled")),
                "type": u.get("userType") or "Member", "created": u.get("createdDateTime"),
                "license_skus": [s["part_number"] for s in skus],
                "has_mailbox": any(s["has_mailbox"] for s in skus),
                "last_activity": None, "mfa": None,
            })
        return users

    def _roles(self) -> list[dict]:
        roles = []
        for role in self.graph.get_all("/directoryRoles", {"$select": "id,displayName,roleTemplateId"}):
            members = self.graph.get_all(f"/directoryRoles/{role['id']}/members", {"$select": "id"})
            roles.append({
                "name": role.get("displayName", ""), "template_id": role.get("roleTemplateId"),
                "members": [m["id"] for m in members if m.get("@odata.type") == "#microsoft.graph.user"],
            })
        return roles

    def _activity(self) -> bool:
        users = {u["id"]: u for u in self.snap["users"]}
        if self.snap["features"].get("entra_p1"):
            try:
                for row in self.graph.iter_all("/users", {"$select": "id,signInActivity", "$top": "120"}):
                    activity = row.get("signInActivity") or {}
                    stamps = [activity.get(k) for k in
                              ("lastSuccessfulSignInDateTime", "lastSignInDateTime", "lastNonInteractiveSignInDateTime")]
                    stamps = [s for s in stamps if s]
                    if row["id"] in users:
                        users[row["id"]]["last_activity"] = max(stamps) if stamps else None
                self.snap["activity_source"] = "sign-in logs"
                return True
            except GraphError as exc:
                if not exc.needs_premium:
                    raise
        # No Entra ID P1: fall back to Microsoft 365 usage reports (last 180 days).
        rows = self.graph.get_report_rows("/reports/getOffice365ActiveUserDetail(period='D180')")
        sample = [r.get("User Principal Name", "") for r in rows[:25]]
        if sample and not any("@" in upn for upn in sample):
            raise ReportsConcealed()
        by_upn = {u["upn"].lower(): u for u in self.snap["users"]}
        for row in rows:
            user = by_upn.get((row.get("User Principal Name") or "").lower())
            if not user:
                continue
            days = [v for k, v in row.items() if k and k.endswith("Last Activity Date") and v]
            user["last_activity"] = _iso_date(max(days)) if days else None
        self.snap["activity_source"] = "usage reports"
        return True

    def _mfa(self) -> bool:
        users = {u["id"]: u for u in self.snap["users"]}
        if self.snap["features"].get("entra_p1"):
            try:
                for row in self.graph.iter_all("/reports/authenticationMethods/userRegistrationDetails"):
                    if row.get("id") in users:
                        users[row["id"]]["mfa"] = bool(row.get("isMfaRegistered"))
                self.snap["mfa_source"] = "registration report"
                return True
            except GraphError as exc:
                if not exc.needs_premium:
                    raise
        # No Entra ID P1: look at each person's registered sign-in methods instead.
        admin_ids = {m for r in self.snap["roles"] or [] for m in r["members"]}
        targets = [u for u in self.snap["users"] if u["enabled"] and u["type"] == "Member"
                   and (u["license_skus"] or u["id"] in admin_ids)][: self.max_mailboxes]

        def methods(user: dict) -> bool:
            found = self.graph.get_all(f"/users/{user['id']}/authentication/methods")
            return any(m.get("@odata.type") not in NOT_MFA for m in found)

        for user, result in self._per_user(methods, targets):
            user["mfa"] = result
        self.snap["mfa_source"] = "sign-in methods"
        return True

    def _security_defaults(self) -> bool:
        return bool(self.graph.get("/policies/identitySecurityDefaultsEnforcementPolicy").get("isEnabled"))

    def _conditional_access(self) -> list[dict]:
        policies = []
        for p in self.graph.get_all("/identity/conditionalAccess/policies"):
            conditions = p.get("conditions") or {}
            grant = p.get("grantControls") or {}
            policies.append({
                "name": p.get("displayName", ""), "state": p.get("state", ""),
                "include_users": (conditions.get("users") or {}).get("includeUsers") or [],
                "include_apps": (conditions.get("applications") or {}).get("includeApplications") or [],
                "client_app_types": conditions.get("clientAppTypes") or [],
                "grant_controls": grant.get("builtInControls") or [],
                "auth_strength": bool(grant.get("authenticationStrength")),
            })
        return policies

    def _authorization_policy(self) -> dict:
        data = self.graph.get("/policies/authorizationPolicy")
        if "value" in data:
            data = data["value"][0]
        return {"allowInvitesFrom": data.get("allowInvitesFrom"),
                "defaultUserRolePermissions": data.get("defaultUserRolePermissions") or {}}

    def _sharepoint(self) -> dict:
        data = self.graph.get("/admin/sharepoint/settings")
        return {"sharing_capability": data.get("sharingCapability")}

    def _forwarding(self) -> list[dict]:
        internal = {d["name"] for d in self.snap["tenant"]["domains"]}
        mailboxes = [u for u in self.snap["users"] if u["enabled"] and u["has_mailbox"]][: self.max_mailboxes]

        def external_rules(user: dict) -> list[dict]:
            found = []
            for rule in self.graph.get_all(f"/users/{user['id']}/mailFolders/inbox/messageRules"):
                if not rule.get("isEnabled", True):
                    continue
                actions = rule.get("actions") or {}
                targets = [
                    r["emailAddress"]["address"]
                    for key in ("forwardTo", "forwardAsAttachmentTo", "redirectTo")
                    for r in actions.get(key) or []
                    if (r.get("emailAddress") or {}).get("address")
                ]
                outside = [t for t in targets if t.rsplit("@", 1)[-1].lower() not in internal]
                if outside:
                    found.append({"user": f"{user['name']} ({user['upn']})", "rule": rule.get("displayName", ""),
                                  "targets": outside})
            return found

        results = self._per_user(external_rules, mailboxes, skip_missing=True)
        self.snap["mailboxes_checked"] = len(results)
        return [rule for _, rules in results for rule in rules]

    def _devices(self) -> list[dict]:
        return [
            {"name": d.get("deviceName", ""), "os": d.get("operatingSystem", ""),
             "compliance": d.get("complianceState", "unknown"), "user": d.get("userPrincipalName")}
            for d in self.graph.iter_all("/deviceManagement/managedDevices", {
                "$select": "deviceName,operatingSystem,complianceState,userPrincipalName"})
        ]

    def _secure_score(self) -> dict:
        scores = self.graph.get("/security/secureScores", {"$top": "1"}).get("value", [])
        if not scores:
            return {"current": 0, "max": 0}
        return {"current": float(scores[0].get("currentScore") or 0), "max": float(scores[0].get("maxScore") or 0)}

    def _dns(self) -> list[dict]:
        custom = [d["name"] for d in self.snap["tenant"]["domains"] if not d["name"].endswith(".onmicrosoft.com")]
        return [check_domain(name, self.resolver, tenant_lookup=lambda _: self.tenant_id)
                for name in custom[:MAX_DOMAINS]]

    # ---------------------------------------------------------------- helpers

    def _per_user(self, fn, users: list[dict], skip_missing: bool = False) -> list[tuple[dict, object]]:
        """Run fn(user) in parallel. Users whose mailbox/record doesn't exist are skipped; if every call is
        refused we raise, so the section is marked unavailable with the right reason."""
        results, errors = [], []

        def call(user: dict):
            try:
                return user, fn(user), None
            except GraphError as exc:
                return user, None, exc

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for user, value, exc in pool.map(call, users):
                if exc is None:
                    results.append((user, value))
                elif exc.forbidden or exc.status >= 500 or not skip_missing:
                    errors.append(exc)
        if errors and not results:
            raise errors[0]
        return results


def collect(graph: GraphClient, resolver: Resolver, tenant_id: str, **kwargs) -> dict:
    return Collector(graph, resolver, tenant_id, **kwargs).run()

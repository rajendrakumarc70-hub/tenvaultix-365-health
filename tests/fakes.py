"""Test doubles: a fake Microsoft Graph tenant and fake Microsoft sign-in."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.dnscheck import StaticResolver
from app.graph import GraphError

TENANT = "11111111-2222-3333-4444-555555555555"
GA = "62e90394-69f5-4237-9190-012177145e10"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def ago(days: int) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGraph:
    """Answers Graph calls from a routing table. A route can be data, an exception, or a callable.
    Routes are looked up as 'path?select' first (so /users can answer two different $selects), then 'path'."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def _lookup(self, path: str, params: dict | None):
        self.calls.append(path)
        keys = [f"{path}?{params['$select']}"] if params and "$select" in params else []
        for key in keys + [path]:
            if key in self.routes:
                value = self.routes[key]
                if isinstance(value, Exception):
                    raise value
                return copy.deepcopy(value() if callable(value) else value)
        raise GraphError(404, "Request_ResourceNotFound", f"No fake for {path}")

    def get(self, path, params=None):
        return self._lookup(path, params)

    def get_all(self, path, params=None, limit=None):
        return list(self._lookup(path, params))

    def iter_all(self, path, params=None, limit=None):
        yield from self._lookup(path, params)

    def get_report_rows(self, path):
        return self._lookup(path, None)


def _plan(name):
    return {"servicePlanName": name, "provisioningStatus": "Success"}


def tenant_routes(premium: bool = True) -> dict:
    """A small tenant: an admin with MFA, a member without MFA who forwards email outside,
    an idle member, a blocked ex-employee who still has a license, and a guest."""
    plans = [_plan("EXCHANGE_S_STANDARD"), _plan("SHAREPOINTSTANDARD")]
    if premium:
        plans += [_plan("AAD_PREMIUM"), _plan("INTUNE_A")]
    sku = "SPB" if premium else "O365_BUSINESS_PREMIUM"
    lic = [{"skuId": "sku-1"}]
    users = [
        {"id": "u1", "displayName": "Ada Admin", "userPrincipalName": "ada@contoso.example", "accountEnabled": True,
         "userType": "Member", "assignedLicenses": lic, "createdDateTime": ago(500)},
        {"id": "u2", "displayName": "Bob Staff", "userPrincipalName": "bob@contoso.example", "accountEnabled": True,
         "userType": "Member", "assignedLicenses": lic, "createdDateTime": ago(400)},
        {"id": "u3", "displayName": "Cara Idle", "userPrincipalName": "cara@contoso.example", "accountEnabled": True,
         "userType": "Member", "assignedLicenses": lic, "createdDateTime": ago(300)},
        {"id": "u4", "displayName": "Dan Left", "userPrincipalName": "dan@contoso.example", "accountEnabled": False,
         "userType": "Member", "assignedLicenses": lic, "createdDateTime": ago(900)},
        {"id": "g1", "displayName": "Gina Guest", "userPrincipalName": "gina_partner.example#EXT#@contoso.onmicrosoft.com",
         "accountEnabled": True, "userType": "Guest", "assignedLicenses": [], "createdDateTime": ago(200)},
    ]
    routes = {
        "/organization": [{"id": TENANT, "displayName": "Contoso Ltd", "verifiedDomains": [
            {"name": "contoso.example", "isDefault": True}, {"name": "contoso.onmicrosoft.com", "isInitial": True}]}],
        "/subscribedSkus": [
            {"skuId": "sku-1", "skuPartNumber": sku, "capabilityStatus": "Enabled", "consumedUnits": 4,
             "prepaidUnits": {"enabled": 6}, "servicePlans": plans},
            {"skuId": "sku-free", "skuPartNumber": "FLOW_FREE", "capabilityStatus": "Enabled", "consumedUnits": 1,
             "prepaidUnits": {"enabled": 10000}, "servicePlans": [_plan("FLOW_P2_VIRAL")]},
        ],
        "/users": users,
        "/directoryRoles": [{"id": "role-ga", "displayName": "Global Administrator", "roleTemplateId": GA}],
        "/directoryRoles/role-ga/members": [{"@odata.type": "#microsoft.graph.user", "id": "u1"}],
        "/policies/identitySecurityDefaultsEnforcementPolicy": {"isEnabled": not premium},
        "/policies/authorizationPolicy": {
            "allowInvitesFrom": "everyone",
            "defaultUserRolePermissions": {
                "allowedToCreateApps": False,
                "permissionGrantPoliciesAssigned": ["ManagePermissionGrantsForSelf.microsoft-user-default-low"]}},
        "/admin/sharepoint/settings": {"sharingCapability": "externalUserSharingOnly"},
        "/users/u1/mailFolders/inbox/messageRules": [],
        "/users/u2/mailFolders/inbox/messageRules": [
            {"displayName": "Send to home", "isEnabled": True,
             "actions": {"forwardTo": [{"emailAddress": {"address": "bob.home@mail.example"}}]}},
            {"displayName": "Team copy", "isEnabled": True,
             "actions": {"redirectTo": [{"emailAddress": {"address": "ada@contoso.example"}}]}},
        ],
        "/users/u3/mailFolders/inbox/messageRules": GraphError(404, "MailboxNotEnabledForRESTAPI", "No mailbox"),
        "/security/secureScores": {"value": [{"currentScore": 45.0, "maxScore": 90.0}]},
    }
    if premium:
        routes.update({
            "/users?id,signInActivity": [
                {"id": "u1", "signInActivity": {"lastSignInDateTime": ago(1)}},
                {"id": "u2", "signInActivity": {"lastSuccessfulSignInDateTime": ago(3)}},
                {"id": "u3", "signInActivity": {"lastSignInDateTime": ago(150)}},
                {"id": "u4", "signInActivity": None},
                {"id": "g1", "signInActivity": {"lastSignInDateTime": ago(120)}},
            ],
            "/reports/authenticationMethods/userRegistrationDetails": [
                {"id": "u1", "isMfaRegistered": True}, {"id": "u2", "isMfaRegistered": False},
                {"id": "u3", "isMfaRegistered": True}],
            "/identity/conditionalAccess/policies": [
                {"displayName": "MFA for everyone", "state": "enabled",
                 "conditions": {"users": {"includeUsers": ["All"]}, "applications": {"includeApplications": ["All"]},
                                "clientAppTypes": ["all"]},
                 "grantControls": {"builtInControls": ["mfa"]}}],
            "/deviceManagement/managedDevices": [
                {"deviceName": "LT-1", "operatingSystem": "Windows", "complianceState": "compliant",
                 "userPrincipalName": "ada@contoso.example"},
                {"deviceName": "LT-2", "operatingSystem": "Windows", "complianceState": "noncompliant",
                 "userPrincipalName": "bob@contoso.example"}],
        })
    else:
        authenticator = {"@odata.type": "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod"}
        password = {"@odata.type": "#microsoft.graph.passwordAuthenticationMethod"}
        routes.update({
            "/reports/getOffice365ActiveUserDetail(period='D180')": [
                {"User Principal Name": "ada@contoso.example", "Exchange Last Activity Date": "2026-11-03",
                 "Teams Last Activity Date": "2026-11-04"},
                {"User Principal Name": "bob@contoso.example", "Exchange Last Activity Date": "2026-10-30"},
                {"User Principal Name": "cara@contoso.example", "Exchange Last Activity Date": "2026-05-01"},
            ],
            "/users/u1/authentication/methods": [password, authenticator],
            "/users/u2/authentication/methods": [password],
            "/users/u3/authentication/methods": [password, {"@odata.type": "#microsoft.graph.phoneAuthenticationMethod"}],
        })
    return routes


def tenant_resolver() -> StaticResolver:
    return StaticResolver(
        txt={"contoso.example": ["v=spf1 include:spf.protection.outlook.com -all"],
             "_dmarc.contoso.example": ["v=DMARC1; p=reject; rua=mailto:d@contoso.example"],
             "selector1-contoso-example._domainkey.contoso.onmicrosoft.com": ["v=DKIM1; k=rsa; p=MIGf"]},
        mx={"contoso.example": ["contoso-example.mail.protection.outlook.com"]},
        cname={"selector1._domainkey.contoso.example": "selector1-contoso-example._domainkey.contoso.onmicrosoft.com"},
    )


def make_settings(**overrides) -> Settings:
    values = dict(client_id="client-123", client_secret="secret", cert_path="", cert_thumbprint="",
                  base_url="http://testserver", session_secret="test-secret", database_path=":memory:",
                  https_only=False, contact_email="help@tenvaultix.example", booking_url="")
    values.update(overrides)
    return Settings(**values)


LEGACY_CONSENT = "ManagePermissionGrantsForSelf.microsoft-user-default-legacy"
TEAMS_RSC = "ManagePermissionGrantsForOwnedResource.microsoft-dynamically-managed-permissions-for-team"


def _merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class FakeWriter:
    """Stands in for the admin's delegated Graph client during a one-click fix: settings by path, PATCHes merged in."""

    def __init__(self, settings: dict | None = None, fail: Exception | None = None):
        self.settings = copy.deepcopy(settings or {
            "/policies/authorizationPolicy": {
                "allowInvitesFrom": "everyone",
                "defaultUserRolePermissions": {"allowedToCreateApps": True,
                                               "permissionGrantPoliciesAssigned": [LEGACY_CONSENT, TEAMS_RSC]}},
            "/admin/sharepoint/settings": {"sharingCapability": "externalUserAndGuestSharing"},
        })
        self.fail = fail
        self.patches: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        return copy.deepcopy(self.settings[path])

    def patch(self, path, body):
        if self.fail:
            raise self.fail
        self.patches.append((path, copy.deepcopy(body)))
        _merge(self.settings[path], body)


class FakeAuth:
    """Stands in for Microsoft sign-in: /login redirects straight back, signed in as `user`."""

    def __init__(self, user: dict):
        self.user = user
        self.fix_requests: list[tuple[str, str]] = []
        self.fix_claims: dict | None = None  # override the tid/oid Microsoft "returns" for a fix

    def start_fix(self, session: dict, scope: str, login_hint: str) -> str:
        session["fix_flow"] = {"state": "fixstate"}
        self.fix_requests.append((scope, login_hint))
        return "/auth/callback?code=fix&state=fixstate"

    def finish_fix(self, session: dict, params: dict) -> dict:
        session.pop("fix_flow", None)
        claims = self.fix_claims or {"tid": self.user["tid"], "oid": self.user["oid"]}
        return {"access_token": "delegated-token", **claims}

    def start_login(self, session: dict) -> str:
        session["auth_flow"] = {"state": "s"}
        return "/auth/callback?code=fake&state=s"

    def finish_login(self, session: dict, params: dict) -> dict:
        session.pop("auth_flow", None)
        return dict(self.user)

    def admin_consent_url(self, tenant_id: str, state: str) -> str:
        return f"https://login.example/{tenant_id}/adminconsent?state={state}"

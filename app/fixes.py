"""One-click fixes: an admin approves one change, the app makes it with that admin's own rights, reads the setting back,
and can put it back the way it was.

Scanning stays read-only (application permissions). A fix uses a *delegated* token: Microsoft asks the signed-in admin
to approve one permission just before the change, the app uses the token for that one request and does not keep it, so
Tenvaultix never holds standing write access to a customer's tenant. Microsoft also refuses the change if the admin's
own role isn't allowed to make it.

Only settings that are safe to change and simple to undo are offered here. Fixes that could lock people out (MFA
policies, removing admins) or that live outside Microsoft 365 (DNS records) stay as step-by-step guides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

GRAPH = "https://graph.microsoft.com/"
SELF_CONSENT = "ManagePermissionGrantsForSelf."
LOW_RISK_CONSENT = SELF_CONSENT + "microsoft-user-default-low"
POLICY_ADMINS = "Global Administrator or Privileged Role Administrator"


@dataclass(frozen=True)
class Fix:
    check_id: str
    title: str                      # button and page title
    change: str                     # what we'll change, in plain English
    notice: str                     # what staff will notice
    permission: str                 # the one delegated Graph permission the admin approves
    roles: str                      # Microsoft roles that are allowed to make this change
    path: str                       # Graph resource that is read, patched and read again
    read: Callable[[dict], Any]     # Graph response -> the setting's value
    want: Callable[[Any], Any]      # current value -> the safe value
    body: Callable[[Any], dict]     # value -> PATCH body
    describe: Callable[[Any], str]  # value -> plain English

    @property
    def scope(self) -> str:
        return GRAPH + self.permission


def _policy(doc: dict) -> dict:
    return doc["value"][0] if "value" in doc else doc  # v1.0 returns the object; tolerate the list shape too


def _consent_policies(doc: dict) -> list[str]:
    return list((_policy(doc).get("defaultUserRolePermissions") or {}).get("permissionGrantPoliciesAssigned") or [])


def _restrict_consent(current: list[str]) -> list[str]:
    # Keep the other entries (Teams and chat resource-specific consent); replace only the "for self" policy.
    return [p for p in current if not p.startswith(SELF_CONSENT)] + [LOW_RISK_CONSENT]


def _describe_consent(policies: list[str]) -> str:
    own = [p for p in policies or [] if p.startswith(SELF_CONSENT)]
    if not own:
        return "Only admins can approve apps."
    if any(p.endswith("microsoft-user-default-legacy") for p in own):
        return "Any staff member can approve any outside app."
    return "Staff can approve only low-risk permissions for verified apps; anything else needs an admin."


INVITES = {
    "everyone": "Anyone, including existing guests, can invite outside people.",
    "adminsGuestInvitersAndAllMembers": "Staff can invite guests; guests can't invite others.",
    "adminsAndGuestInviters": "Only admins and Guest Inviters can invite guests.",
    "none": "Nobody can invite guests.",
}
SHARING = {
    "externalUserAndGuestSharing": "Files can be shared with “Anyone” links that open without signing in.",
    "externalUserSharingOnly": "Files can be shared with outside people, but they must sign in.",
    "existingExternalUserSharingOnly": "Files can be shared only with guests already in the tenant.",
    "disabled": "Files can't be shared outside the company.",
}

FIXES: dict[str, Fix] = {f.check_id: f for f in [
    Fix(
        check_id="ADM-003",
        title="Stop staff approving outside apps",
        change="Staff will only be able to approve low-risk permissions for apps from verified publishers. "
               "Anything else will need an admin.",
        notice="Someone who tries to connect an unknown app sees “Need admin approval” instead of an Accept button. "
               "Apps they already use keep working.",
        permission="Policy.ReadWrite.Authorization",
        roles=POLICY_ADMINS,
        path="/policies/authorizationPolicy",
        read=_consent_policies,
        want=_restrict_consent,
        body=lambda value: {"defaultUserRolePermissions": {"permissionGrantPoliciesAssigned": value}},
        describe=_describe_consent,
    ),
    Fix(
        check_id="ADM-004",
        title="Only admins can register apps",
        change="Ordinary staff will no longer be able to create new app registrations in your tenant.",
        notice="Most staff never notice. Anyone who builds apps can be given the Application Developer role.",
        permission="Policy.ReadWrite.Authorization",
        roles=POLICY_ADMINS,
        path="/policies/authorizationPolicy",
        read=lambda doc: (_policy(doc).get("defaultUserRolePermissions") or {}).get("allowedToCreateApps", True),
        want=lambda current: False,
        body=lambda value: {"defaultUserRolePermissions": {"allowedToCreateApps": value}},
        describe=lambda value: "Any staff member can register apps." if value
        else "Only admins and app developers can register apps.",
    ),
    Fix(
        check_id="ACC-002",
        title="Stop guests inviting other guests",
        change="Staff can still invite guests, but guests will no longer be able to invite other outside people.",
        notice="Nothing changes for your staff. Existing guests keep their access.",
        permission="Policy.ReadWrite.Authorization",
        roles=POLICY_ADMINS,
        path="/policies/authorizationPolicy",
        read=lambda doc: _policy(doc).get("allowInvitesFrom", "everyone"),
        want=lambda current: "adminsGuestInvitersAndAllMembers",
        body=lambda value: {"allowInvitesFrom": value},
        describe=lambda value: INVITES.get(value, str(value)),
    ),
    Fix(
        check_id="SHR-001",
        title="Turn off “Anyone” links",
        change="Files can still be shared with people outside the company, but they'll have to sign in to open them.",
        notice="Existing “Anyone” links stop opening while this is off. Undo turns them back on.",
        permission="SharePointTenantSettings.ReadWrite.All",
        roles="Global Administrator or SharePoint Administrator",
        path="/admin/sharepoint/settings",
        read=lambda doc: doc.get("sharingCapability"),
        want=lambda current: "externalUserSharingOnly",
        body=lambda value: {"sharingCapability": value},
        describe=lambda value: SHARING.get(value, str(value)),
    ),
]}

# Shown on the setup and privacy pages: the delegated permissions an admin may be asked for.
FIX_PERMISSIONS = sorted({f.permission for f in FIXES.values()})


def same(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return sorted(a) == sorted(b)
    return a == b


def apply(fix: Fix, graph, value: Any = None) -> dict:
    """Make the change (or set `value`, to undo), then read the setting back.

    `graph` needs get(path) and patch(path, body). Nothing is sent when the setting already has the value."""
    before = fix.read(graph.get(fix.path))
    target = fix.want(before) if value is None else value
    if not same(before, target):
        graph.patch(fix.path, fix.body(target))
    after = fix.read(graph.get(fix.path))
    return {"before": before, "after": after, "target": target, "verified": same(after, target)}

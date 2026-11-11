"""Microsoft sign-in (work accounts only) and the admin-consent link that connects a tenant."""
from __future__ import annotations

from urllib.parse import urlencode

import msal

from .config import GRAPH_SCOPE, LOGIN_HOST, Settings

# Admin roles that can approve application permissions for Microsoft Graph.
CONSENT_ROLES = {
    "62e90394-69f5-4237-9190-012177145e10",  # Global Administrator
    "e8611ab8-c189-46e8-94e1-60213ab1f814",  # Privileged Role Administrator
}

CONSENT_ERRORS = {
    "AADSTS65004": "The approval was cancelled, so nothing was connected.",
    "AADSTS90094": "Only a Global Administrator or Privileged Role Administrator can approve access. "
                   "Ask your admin to sign in here and connect your company.",
    "AADSTS90099": "Only a Global Administrator or Privileged Role Administrator can approve access. "
                   "Ask your admin to sign in here and connect your company.",
    "AADSTS50020": "That account isn't from your company's Microsoft 365. Sign in with your work account.",
}


class LoginError(Exception):
    pass


class MicrosoftAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._app = None

    @property
    def app(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                self.settings.client_id,
                authority=f"{LOGIN_HOST}/organizations",
                client_credential=self.settings.client_credential(),
            )
        return self._app

    def start_login(self, session: dict) -> str:
        flow = self.app.initiate_auth_code_flow(
            scopes=["User.Read"], redirect_uri=self.settings.url("/auth/callback"), prompt="select_account")
        session["auth_flow"] = {k: v for k, v in flow.items() if k != "auth_uri"}  # keep the cookie small
        return flow["auth_uri"]

    def finish_login(self, session: dict, params: dict) -> dict:
        flow = session.pop("auth_flow", None)
        if not flow:
            raise LoginError("Your sign-in took too long or was started in another tab. Please try again.")
        try:
            result = self.app.acquire_token_by_auth_code_flow(flow, params)
        except ValueError as exc:  # state mismatch
            raise LoginError("Sign-in couldn't be verified. Please try again.") from exc
        if "error" in result:
            raise LoginError(result.get("error_description") or result["error"])
        claims = result.get("id_token_claims") or {}
        email = (claims.get("preferred_username") or claims.get("email") or "").lower()
        if not (claims.get("tid") and claims.get("oid") and email):
            raise LoginError("Microsoft didn't return the account details we need.")
        return {"oid": claims["oid"], "tid": claims["tid"], "email": email, "name": claims.get("name") or email}

    # ---- one-click fixes: the admin approves one delegated permission just before the change

    def _fix_app(self) -> msal.ConfidentialClientApplication:
        """A new client with an empty token cache each time, so the admin's token isn't kept after the request."""
        return msal.ConfidentialClientApplication(
            self.settings.client_id,
            authority=f"{LOGIN_HOST}/organizations",
            client_credential=self.settings.client_credential(),
            token_cache=msal.TokenCache(),
        )

    def start_fix(self, session: dict, scope: str, login_hint: str) -> str:
        flow = self._fix_app().initiate_auth_code_flow(
            scopes=[scope], redirect_uri=self.settings.url("/auth/callback"), login_hint=login_hint)
        session["fix_flow"] = {k: v for k, v in flow.items() if k != "auth_uri"}
        return flow["auth_uri"]

    def finish_fix(self, session: dict, params: dict) -> dict:
        flow = session.pop("fix_flow", None)
        if not flow:
            raise LoginError("The approval took too long or was started in another tab. Please try again.")
        try:
            result = self._fix_app().acquire_token_by_auth_code_flow(flow, params)
        except ValueError as exc:  # state mismatch
            raise LoginError("The approval couldn't be verified. Please try again.") from exc
        if "error" in result or "access_token" not in result:
            raise LoginError(result.get("error_description") or result.get("error") or "Microsoft returned no token.")
        claims = result.get("id_token_claims") or {}
        return {"access_token": result["access_token"], "tid": claims.get("tid"), "oid": claims.get("oid")}

    def admin_consent_url(self, tenant_id: str, state: str) -> str:
        query = urlencode({
            "client_id": self.settings.client_id,
            "scope": GRAPH_SCOPE,
            "redirect_uri": self.settings.url("/connect/callback"),
            "state": state,
        })
        return f"{LOGIN_HOST}/{tenant_id}/v2.0/adminconsent?{query}"


def is_consent_admin(graph, user_id: str) -> bool:
    """True if the user holds a role that can approve app access. Also proves our app-only access works."""
    for role in graph.get_all("/directoryRoles", {"$select": "id,roleTemplateId"}):
        if role.get("roleTemplateId") in CONSENT_ROLES:
            members = graph.get_all(f"/directoryRoles/{role['id']}/members", {"$select": "id"})
            if any(m.get("id") == user_id for m in members):
                return True
    return False


def consent_error_message(error: str, description: str) -> str:
    for code, message in CONSENT_ERRORS.items():
        if code in description:
            return message
    if error == "access_denied":
        return "The approval wasn't completed, so nothing was connected."
    return "Microsoft couldn't complete the approval. Please try again, or contact us if it keeps happening."

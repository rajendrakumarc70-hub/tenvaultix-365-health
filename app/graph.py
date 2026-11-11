"""Microsoft Graph access for one customer tenant, using app-only (client credentials) tokens.

The customer's admin approves our app once (admin consent). After that we ask Microsoft for a token
for *their* tenant with our own certificate or secret - we never see or store a customer password.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from typing import Callable, Iterator

import msal
import requests

from .config import GRAPH_SCOPE, LOGIN_HOST, Settings

log = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def needs_premium(self) -> bool:
        text = f"{self.code} {self.message}".lower()
        return "premium" in text or "license" in text

    @property
    def forbidden(self) -> bool:
        return self.status in (401, 403)


class AuthError(Exception):
    """Microsoft refused to issue a token (consent missing or revoked, wrong credentials)."""


_apps: dict[str, msal.ConfidentialClientApplication] = {}
_apps_lock = threading.Lock()


def app_token_provider(settings: Settings, tenant_id: str) -> Callable[[], str]:
    """Return a function that gives a valid Graph token for the tenant (MSAL caches and renews it)."""
    with _apps_lock:
        app = _apps.get(tenant_id)
        if app is None:
            app = msal.ConfidentialClientApplication(
                settings.client_id,
                authority=f"{LOGIN_HOST}/{tenant_id}",
                client_credential=settings.client_credential(),
            )
            _apps[tenant_id] = app

    def get_token() -> str:
        result = app.acquire_token_for_client(scopes=[GRAPH_SCOPE])
        if "access_token" not in result:
            raise AuthError(result.get("error_description") or result.get("error") or "Token request failed")
        return result["access_token"]

    return get_token


class GraphClient:
    def __init__(self, token_provider: Callable[[], str], *, max_retries: int = 4,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30):
        self._token = token_provider
        self._max_retries = max_retries
        self._sleep = sleep
        self._timeout = timeout
        self._local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        return self._local.session

    def _request(self, url: str, params: dict | None = None) -> requests.Response:
        if not url.startswith("https://"):
            url = GRAPH_ROOT + url
        for attempt in range(self._max_retries + 1):
            resp = self._session().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                timeout=self._timeout,
            )
            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                wait = _retry_after(resp, attempt)
                log.info("Graph %s returned %s, retrying in %.1fs", url, resp.status_code, wait)
                self._sleep(wait)
                continue
            if resp.status_code >= 400:
                raise _error_from(resp)
            return resp
        raise GraphError(503, "retriesExhausted", "Microsoft Graph kept asking us to slow down.")

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request(path, params).json()

    def iter_all(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[dict]:
        url, count = path, 0
        while url:
            data = self.get(url, params)
            params = None  # nextLink already carries the query
            for item in data.get("value", []):
                yield item
                count += 1
                if limit and count >= limit:
                    return
            url = data.get("@odata.nextLink")

    def get_all(self, path: str, params: dict | None = None, limit: int | None = None) -> list[dict]:
        return list(self.iter_all(path, params, limit))

    def patch(self, path: str, body: dict) -> None:
        """Change a setting. Only used by one-click fixes, with a delegated token the admin just approved."""
        url = path if path.startswith("https://") else GRAPH_ROOT + path
        resp = self._session().patch(
            url, json=body, timeout=self._timeout,
            headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"})
        if resp.status_code >= 400:
            raise _error_from(resp)

    def get_report_rows(self, path: str) -> list[dict]:
        """Usage reports answer with a redirect to a CSV file. requests follows it and drops our
        Authorization header on the way (the download link is pre-authenticated)."""
        text = self._request(path).content.decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))


def _retry_after(resp: requests.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "")), 60.0)
    except ValueError:
        return min(2.0 ** attempt, 30.0)


def _error_from(resp: requests.Response) -> GraphError:
    code, message = "error", resp.text[:300]
    try:
        err = resp.json().get("error", {})
        code = err.get("code", code)
        message = err.get("message", message)
    except ValueError:
        pass
    return GraphError(resp.status_code, code, message)

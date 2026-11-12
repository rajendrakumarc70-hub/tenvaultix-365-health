"""Connecting right after an approval: fresh tokens on every try, and a clear reason when it still fails."""
import base64
import json

from app import graph
from app.graph import AuthError, GraphError
from app.web import create_app

from .fakes import TENANT, FakeGraph, make_settings, tenant_resolver, tenant_routes
from .test_web import ADMIN, World

REFUSED = GraphError(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")


def jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def test_token_roles_reads_the_permissions_in_a_token():
    assert graph.token_roles(jwt({"roles": ["Policy.Read.All", "Directory.Read.All"]})) == [
        "Directory.Read.All", "Policy.Read.All"]
    assert graph.token_roles(jwt({"scp": "User.Read"})) == []
    assert graph.token_roles("not-a-token") == []


def test_forget_tokens_drops_the_cached_app():
    graph._apps["t-1"] = object()
    graph.forget_tokens("t-1")
    graph.forget_tokens("t-never-seen")
    assert "t-1" not in graph._apps


def retry_world(tmp_path, roles=None, roles_error=None):
    w = World(tmp_path)
    w.factory_calls = 0

    def factory(tid):
        w.factory_calls += 1
        return FakeGraph(w.routes)

    def app_roles(tid):
        if roles_error:
            raise roles_error
        return roles

    w.app = create_app(settings=make_settings(), store=w.store, graph_factory=factory, resolver=tenant_resolver(),
                       auth=w.auth, tenant_lookup=lambda d: None, sleep=lambda s: None, app_roles=app_roles)
    return w


def test_each_try_asks_for_a_fresh_client(tmp_path):
    w = retry_world(tmp_path)
    w.routes = {"/directoryRoles": REFUSED}
    w.connect(w.client(ADMIN))
    assert w.factory_calls == 3  # one per attempt, each with a new token


def test_first_failure_says_almost_there_then_explains(tmp_path):
    w = retry_world(tmp_path, roles=[])
    w.routes = {"/directoryRoles": REFUSED}
    ada = w.client(ADMIN)
    assert "Almost there" in w.connect(ada).text
    page = ada.get("/connect/verify").text
    assert "Not connected yet" in page and "Delegated instead of Application" in page and "graph-403" in page
    assert w.store.get_tenant(TENANT) is None
    assert w.store.list_problems(tenant_id=TENANT)[0]["area"] == "connect"


def test_permissions_granted_but_not_switched_on_yet(tmp_path):
    w = retry_world(tmp_path, roles=["Directory.Read.All", "Policy.Read.All"])
    w.routes = {"/directoryRoles": REFUSED}
    ada = w.client(ADMIN)
    w.connect(ada)
    assert "still switching them on" in ada.get("/connect/verify").text


def test_permissions_missing_the_directory_ones(tmp_path):
    w = retry_world(tmp_path, roles=["Policy.Read.All"])
    w.routes = {"/directoryRoles": REFUSED}
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.get("/connect/verify").text
    assert "1 application permission" in page and "RoleManagement.Read.Directory" in page


def test_token_refused_shows_the_microsoft_hint(tmp_path):
    w = retry_world(tmp_path, roles_error=AuthError("AADSTS7000215: bad secret"))
    w.routes = {"/directoryRoles": AuthError("AADSTS7000229: The client application is missing service principal")}
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.get("/connect/verify").text
    assert "isn&#39;t set up in that company yet" in page and "AADSTS7000229" in page


def test_works_once_microsoft_catches_up_and_counter_resets(tmp_path):
    w = retry_world(tmp_path, roles=[])
    w.routes = {"/directoryRoles": REFUSED}
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.get("/connect/verify")
    w.routes = tenant_routes(premium=True)
    page = ada.get("/connect/verify", follow_redirects=True).text
    assert "Open full report" in page or "Scan in progress" in page or "Connected" in page
    assert w.store.get_tenant(TENANT) is not None


def test_a_new_approval_starts_the_count_again(tmp_path):
    w = retry_world(tmp_path, roles=[])
    w.routes = {"/directoryRoles": REFUSED}
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.get("/connect/verify")
    assert "Almost there" in w.connect(ada).text  # approving again gives Microsoft another chance first

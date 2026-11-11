import re

import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.dnscheck import StaticResolver
from app.graph import AuthError
from app.web import create_app

from .fakes import TENANT, FakeAuth, FakeGraph, FakeWriter, make_settings, tenant_resolver, tenant_routes

ADMIN = {"oid": "u1", "tid": TENANT, "email": "ada@contoso.example", "name": "Ada Admin"}
STAFF = {"oid": "u2", "tid": TENANT, "email": "bob@contoso.example", "name": "Bob Staff"}
OUTSIDER = {"oid": "x1", "tid": "99999999-0000-0000-0000-000000000000", "email": "eve@fabrikam.example", "name": "Eve"}


class World:
    """One app instance plus helpers to sign in as different people."""

    def __init__(self, tmp_path, graph_routes=None, configured=True):
        self.store = Store(tmp_path / "test.db")
        self.routes = graph_routes or tenant_routes(premium=True)
        self.auth = FakeAuth(ADMIN)
        self.writer = FakeWriter()
        self.tokens: list[str] = []
        settings = make_settings() if configured else make_settings(client_id="")
        self.app = create_app(settings=settings, store=self.store, graph_factory=lambda tid: FakeGraph(self.routes),
                              resolver=tenant_resolver(), auth=self.auth, tenant_lookup=lambda d: None,
                              sleep=lambda s: None, delegated_graph=self._writer)

    def _writer(self, token: str) -> FakeWriter:
        self.tokens.append(token)
        return self.writer

    def client(self, user=None) -> TestClient:
        client = TestClient(self.app)
        if user:
            self.auth.user = user
            assert client.get("/login", follow_redirects=True).status_code == 200
        return client

    def connect(self, client: TestClient):
        location = client.get("/connect/start", follow_redirects=False).headers["location"]
        state = re.search(r"state=([^&]+)", location).group(1)
        return client.get(f"/connect/callback?admin_consent=True&tenant={TENANT}&state={state}",
                          follow_redirects=True)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def csrf(client: TestClient) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/app").text).group(1)


def test_public_pages_render(world):
    client = world.client()
    for path in ("/", "/demo", "/connect", "/privacy", "/setup", "/healthz"):
        assert client.get(path).status_code == 200, path
    assert "Acme Diagnostics" in client.get("/demo").text


def test_security_headers(world):
    response = world.client().get("/")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_free_domain_check(tmp_path):
    w = World(tmp_path)
    w.app = create_app(settings=make_settings(), store=w.store, auth=w.auth, tenant_lookup=lambda d: None,
                       resolver=StaticResolver(txt={"example.com": ["v=spf1 -all"]}, mx={"example.com": ["mx.example.com"]}))
    client = w.client()
    page = client.get("/check?domain=https://www.Example.com/about")
    assert page.status_code == 200
    assert "example.com" in page.text and "No DMARC record" in page.text
    assert client.get("/check?domain=nope").status_code == 400


def test_domain_check_is_rate_limited(world):
    client = world.client()
    codes = [client.get("/check?domain=contoso.example").status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_sign_in_not_configured_shows_setup_help(tmp_path):
    page = World(tmp_path, configured=False).client().get("/login")
    assert "set up yet" in page.text and "Setup guide" in page.text


def test_setup_page_shows_which_settings_are_present_without_values(tmp_path):
    page = World(tmp_path, configured=False).client().get("/setup").text
    assert "not configured yet" in page and "missing" in page
    settings = make_settings(client_secret="S3cr3t-value-xyz", session_secret="session-value-xyz")
    app = create_app(settings=settings, store=Store(tmp_path / "s.db"), graph_factory=lambda tid: FakeGraph({}),
                     resolver=tenant_resolver(), auth=FakeAuth(ADMIN), tenant_lookup=lambda d: None,
                     sleep=lambda s: None)
    page = TestClient(app).get("/setup").text
    assert "Microsoft sign-in is <strong>configured</strong>" in page and "client secret set" in page
    assert "S3cr3t-value-xyz" not in page and "session-value-xyz" not in page


def test_dashboard_requires_sign_in(world):
    response = world.client().get("/app", follow_redirects=False)
    assert response.headers["location"] == "/login?next=/app"


def test_connect_flow_runs_first_scan(world):
    client = world.client(ADMIN)
    page = world.connect(client)
    assert "Contoso Ltd" in page.text
    assert "Open full report" in page.text
    tenant = world.store.get_tenant(TENANT)
    assert tenant["plan"] == "trial" and tenant["connected_by"] == ADMIN["email"]
    latest = world.store.latest_scan(TENANT)
    report = client.get(f"/app/report/{latest['id']}")
    assert report.status_code == 200
    assert "Email forwarded outside the company" in report.text
    assert "bob.home@mail.example" in report.text


def test_consent_callback_rejects_bad_state(world):
    client = world.client(ADMIN)
    client.get("/connect/start", follow_redirects=False)
    page = client.get(f"/connect/callback?admin_consent=True&tenant={TENANT}&state=forged")
    assert "expired" in page.text
    assert world.store.get_tenant(TENANT) is None


def test_consent_callback_rejects_other_tenant(world):
    client = world.client(ADMIN)
    location = client.get("/connect/start", follow_redirects=False).headers["location"]
    state = re.search(r"state=([^&]+)", location).group(1)
    page = client.get(f"/connect/callback?admin_consent=True&tenant={OUTSIDER['tid']}&state={state}")
    assert "Different organization" in page.text


def test_consent_error_is_explained(world):
    client = world.client(STAFF)
    location = client.get("/connect/start", follow_redirects=False).headers["location"]
    state = re.search(r"state=([^&]+)", location).group(1)
    page = client.get(f"/connect/callback?error=access_denied&error_description=AADSTS90094%3A+admin+needed&state={state}")
    assert "Global Administrator" in page.text


def test_non_admin_cannot_claim_tenant_by_forging_callback(world):
    client = world.client(STAFF)  # Bob isn't a Global Administrator
    page = world.connect(client)
    assert "An admin needs to connect" in page.text
    assert world.store.get_tenant(TENANT) is None


def test_consent_not_active_yet_offers_retry(tmp_path):
    w = World(tmp_path)
    w.routes = {"/directoryRoles": AuthError("AADSTS7000229: missing service principal")}
    page = w.connect(w.client(ADMIN))
    assert "Almost there" in page.text
    w.routes = tenant_routes(premium=True)
    client = w.client(ADMIN)
    page = w.connect(client)
    assert "Open full report" in page.text


def test_colleague_needs_to_be_added(world):
    world.connect(world.client(ADMIN))
    bob = world.client(STAFF)
    assert "Ask your admin for access" in bob.get("/app").text

    ada = world.client(ADMIN)
    ada.post("/app/team", data={"csrf": csrf(ada), "email": "Bob@Contoso.example"})
    assert world.store.member_role(TENANT, "bob@contoso.example") == "viewer"

    bob = world.client(STAFF)
    page = bob.get("/app")
    assert "Contoso Ltd" in page.text
    assert "Disconnect" not in page.text  # viewers can't disconnect
    bob.post("/app/disconnect", data={"csrf": csrf(bob), "confirm": "DELETE"})
    assert world.store.get_tenant(TENANT) is not None


def test_other_companies_cannot_see_reports(world):
    world.connect(world.client(ADMIN))
    scan_id = world.store.latest_scan(TENANT)["id"]
    eve = world.client(OUTSIDER)
    assert eve.get(f"/app/report/{scan_id}").status_code == 404


def test_posts_need_csrf_token(world):
    ada = world.client(ADMIN)
    world.connect(ada)
    ada.post("/app/team", data={"csrf": "wrong", "email": "x@contoso.example"})
    assert world.store.member_role(TENANT, "x@contoso.example") is None
    ada.post("/app/disconnect", data={"confirm": "DELETE"})
    assert world.store.get_tenant(TENANT) is not None


def test_manual_scans_are_throttled(world):
    ada = world.client(ADMIN)
    world.connect(ada)
    page = ada.post("/app/scan", data={"csrf": csrf(ada)}, follow_redirects=True)
    assert "wait a few minutes" in page.text
    assert len(world.store.list_scans(TENANT)) == 1


def test_disconnect_deletes_everything(world):
    ada = world.client(ADMIN)
    world.connect(ada)
    page = ada.post("/app/disconnect", data={"csrf": csrf(ada), "confirm": "nope"}, follow_redirects=True)
    assert "Type DELETE" in page.text
    page = ada.post("/app/disconnect", data={"csrf": csrf(ada), "confirm": "delete"})
    assert "Disconnected and deleted" in page.text
    assert world.store.get_tenant(TENANT) is None
    assert world.store.list_scans(TENANT) == []


def test_failed_scan_is_shown(tmp_path):
    w = World(tmp_path)
    routes = tenant_routes(premium=True)
    w.routes = routes
    ada = w.client(ADMIN)
    original = dict(routes)
    routes["/organization"] = AuthError("consent revoked")
    w.connect(ada)  # admin check uses /directoryRoles, which still works
    page = ada.get("/app").text
    assert "Microsoft refused access" in page
    routes.update(original)

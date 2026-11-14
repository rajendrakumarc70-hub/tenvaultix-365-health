"""Fixes from the November 2026 security review: sessions that end on the server, no kept Microsoft tokens, the
visitor's real address, upload limits, a team-only setup page, console checks by account ID, token hosts."""
import base64
import hashlib
import json
import time
from datetime import timedelta

import msal
import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import auth as auth_module
from app import fixes
from app import console as console_module
from app.auth import MicrosoftAuth
from app.cli import main as cli
from app.db import Store
from app.graph import GraphClient, GraphError
from app.web import create_app

from .fakes import TENANT, FakeAuth, FakeGraph, make_settings, tenant_resolver
from .test_console import console_world, token
from .test_web import ADMIN, OUTSIDER, STAFF, World, csrf

TRUSTED_PROXIES = "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"  # what the Dockerfile sets


def sign_out(client: TestClient):
    return client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)


# ---------------------------------------------------------------- M1: sessions end on the server

def test_a_copied_cookie_stops_working_after_sign_out(tmp_path):
    w = World(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    copied = ada.cookies.get("tvx_session")
    assert sign_out(ada).headers["location"] == "/"
    thief = TestClient(w.app)
    thief.cookies.set("tvx_session", copied)
    assert thief.get("/app", follow_redirects=False).headers["location"].startswith("/login")


def test_sign_out_everywhere_ends_every_browser(tmp_path):
    w = World(tmp_path)
    laptop = w.client(ADMIN)
    w.connect(laptop)
    phone = w.client(ADMIN)
    assert phone.get("/app").status_code == 200
    laptop.post("/logout/everywhere", data={"csrf": csrf(laptop)})
    assert phone.get("/app", follow_redirects=False).status_code == 303
    assert laptop.get("/app", follow_redirects=False).status_code == 303


def test_sign_out_needs_the_button_not_a_link(tmp_path):
    w = World(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.get("/logout")
    assert page.status_code == 200 and "Sign out on every device" in page.text
    ada.post("/logout", data={"csrf": "forged"})  # another site can't post the right token
    assert ada.get("/app").status_code == 200


def test_cli_can_sign_everyone_out(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    w = World(tmp_path)
    w.store = Store(db)
    w.app = create_app(settings=make_settings(), store=w.store, graph_factory=lambda tid: FakeGraph(w.routes),
                       resolver=tenant_resolver(), auth=w.auth, tenant_lookup=lambda d: None, sleep=lambda s: None)
    ada = w.client(ADMIN)
    assert ada.get("/connect").status_code == 200 and "Sign out" in ada.get("/connect").text
    monkeypatch.setenv("TVX_DB_PATH", str(db))
    assert cli(["sign-out-everyone"]) == 0
    assert ada.get("/app", follow_redirects=False).headers["location"].startswith("/login")


def test_sessions_expire_on_the_server(tmp_path):
    store = Store(tmp_path / "s.db")
    live = store.create_session(ADMIN, hours=1)
    gone = store.create_session(ADMIN, hours=-1)
    assert store.check_session(live)["email"] == ADMIN["email"]
    assert store.check_session(gone) is None and store.check_session("made-up") is None
    with store._conn() as db:  # only a hash of the cookie value is stored
        ids = [r["id"] for r in db.all("SELECT id FROM sessions")]
    assert hashlib.sha256(live.encode()).hexdigest() in ids and live not in ids


def test_console_asks_for_a_fresh_sign_in_after_an_hour(tmp_path, monkeypatch):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    assert ada.get("/admin").status_code == 200
    tok = token(ada, "/admin/security")
    later = console_module.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(console_module, "utcnow", lambda: later)
    r = ada.get("/admin/customers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin/customers"
    ada.post("/admin/security/stop", data={"csrf": tok, "confirm": "STOP"}, follow_redirects=False)
    assert w.store.get_option("emergency_stop") == ""  # a stale console session can't change anything


# ---------------------------------------------------------------- M2: no refresh tokens, nothing kept

class _Resp:
    def __init__(self, data):
        self.status_code, self.text, self.headers = 200, json.dumps(data), {}

    def raise_for_status(self):
        pass


def _b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def test_sign_in_asks_for_no_refresh_token_and_keeps_none(monkeypatch):
    posted, nonce = [], {}

    class Http:
        def get(self, url, **kw):
            return _Resp({"authorization_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
                          "token_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                          "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"})

        def post(self, url, **kw):
            posted.append(kw.get("data") or {})
            id_token = ".".join([_b64({"alg": "none"}), _b64({
                "tid": TENANT, "oid": "u1", "preferred_username": "ada@contoso.example", "aud": "client-123",
                "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "iat": int(time.time()),
                "exp": int(time.time()) + 3600, "nonce": nonce["value"]}), ""])
            return _Resp({"access_token": "AT", "refresh_token": "RT", "id_token": id_token, "expires_in": 3600,
                          "token_type": "Bearer", "client_info": _b64({"uid": "u1", "utid": TENANT})})

        def close(self):
            pass

    caches = []
    real = msal.ConfidentialClientApplication

    def client(*args, **kwargs):
        app = real(*args, http_client=Http(), instance_discovery=False, validate_authority=False, **kwargs)
        caches.append(app.token_cache)
        return app

    monkeypatch.setattr(auth_module.msal, "ConfidentialClientApplication", client)
    auth = MicrosoftAuth(make_settings())
    session = {}
    url = auth.start_login(session)
    assert "offline_access" not in url and "User.Read" in url
    nonce["value"] = hashlib.sha256(session["auth_flow"]["nonce"].encode()).hexdigest()
    assert auth.finish_login(session, {"code": "c", "state": session["auth_flow"]["state"]})["oid"] == "u1"
    assert "offline_access" not in posted[-1].get("scope", "")
    assert len({id(c) for c in caches}) == len(caches) == 2  # a new, empty cache for each step
    assert not hasattr(auth, "_app")  # nothing on the long-lived object holds tokens
    fix_url = auth.start_fix({}, "Policy.ReadWrite.Authorization", "ada@contoso.example")
    assert "offline_access" not in fix_url and "Policy.ReadWrite.Authorization" in fix_url


def test_fix_result_explains_how_to_remove_the_permission(tmp_path):
    w = World(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.post(f"/app/fix/{next(iter(fixes.FIXES))}", data={"csrf": csrf(ada)}, follow_redirects=True)
    fix = w.store.list_fixes(TENANT)
    assert fix, "the fix should have been recorded"
    page = ada.get(f"/app/fixes/{fix[0]['id']}").text
    assert "Remove the permission you approved" in page and "Revoke permission" in page
    assert "appId eq &#39;client-123&#39;" in page or "appId eq 'client-123'" in page


def test_setup_says_not_to_list_fix_permissions(tmp_path):
    page = World(tmp_path).client().get("/setup").text
    assert "Don't add the one-click fix permissions here" in page


# ---------------------------------------------------------------- M3: the visitor's real address

def test_forwarded_for_from_an_untrusted_peer_is_ignored(tmp_path):
    w = console_world(tmp_path)
    client = TestClient(ProxyHeadersMiddleware(w.app, trusted_hosts=TRUSTED_PROXIES))
    codes = [client.get("/check?domain=contoso.example",
                        headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"}).status_code for i in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429  # faking the header no longer resets the limit


def test_the_proxy_header_is_used_when_configured(tmp_path):
    w = World(tmp_path)
    app = create_app(settings=make_settings(client_ip_header="CF-Connecting-IP", ops_emails=("ada@contoso.example",),
                                            ops_tenant_id=TENANT),
                     store=w.store, graph_factory=lambda tid: FakeGraph(w.routes), resolver=tenant_resolver(),
                     auth=FakeAuth(STAFF), tenant_lookup=lambda d: None)
    client = TestClient(app)
    client.get("/login", follow_redirects=True)
    client.get("/admin", headers={"CF-Connecting-IP": "198.51.100.23", "X-Forwarded-For": "8.8.8.8"})
    assert w.store.list_audit(prefix="console.denied")[0]["ip"] == "198.51.100.23"
    assert "198.51.100.23" in client.get("/setup", headers={"CF-Connecting-IP": "198.51.100.23"}).text


# ---------------------------------------------------------------- L4: big uploads

def test_big_requests_are_refused_before_they_are_read(tmp_path):
    client = TestClient(console_world(tmp_path).app)
    big = client.post("/admin/restore", data={"csrf": "x", "confirm": "RESTORE"},
                      files={"backup_file": ("b.json", b"A" * (6 * 1024 * 1024), "application/json")})
    assert big.status_code == 413

    def chunks():  # no Content-Length: counted as it arrives
        for _ in range(7):
            yield b"A" * (1024 * 1024)
    streamed = client.post("/app/team", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert streamed.status_code == 413


# ---------------------------------------------------------------- L1, L2: setup page and console by account ID

def test_setup_is_for_the_team_once_the_console_is_set_up(tmp_path):
    w = console_world(tmp_path)
    anon = TestClient(w.app).get("/setup").text
    assert "client secret set" not in anon and "Running version" not in anon and "Sign in with Microsoft" in anon
    staff = w.client(STAFF).get("/setup").text
    assert "client secret set" not in staff and "bob@contoso.example" in staff  # only their own details
    team = w.client(ADMIN).get("/setup").text
    assert "client secret set" in team and "Running version" in team and "u1" in team


def test_console_by_account_id_ignores_a_renamed_email(tmp_path):
    w = World(tmp_path)
    w.app = create_app(settings=make_settings(ops_emails=("ada@contoso.example",), ops_tenant_id=TENANT,
                                              ops_object_ids=("u1",)),
                       store=w.store, graph_factory=lambda tid: FakeGraph(w.routes), resolver=tenant_resolver(),
                       auth=w.auth, tenant_lookup=lambda d: None)
    impostor = {"oid": "someone-else", "tid": TENANT, "email": "ada@contoso.example", "name": "Not Ada"}
    assert w.client(impostor).get("/admin").status_code == 404
    assert w.client(ADMIN).get("/admin").status_code == 200
    assert w.client(OUTSIDER).get("/admin").status_code == 404


# ---------------------------------------------------------------- L5: the token only goes to Microsoft

def test_next_page_links_to_other_hosts_are_refused():
    class Session:
        def __init__(self):
            self.urls = []

        def request(self, method, url, **kwargs):
            self.urls.append(url)

            class R:
                status_code = 200

                def json(self):
                    return {"value": [{"id": 1}], "@odata.nextLink": "https://evil.example/steal"}
            return R()

    client = GraphClient(lambda: "token", max_retries=0)
    session = Session()
    client._local.session = session
    with pytest.raises(GraphError) as err:
        client.get_all("/users")
    assert err.value.code == "unexpectedHost" and session.urls == ["https://graph.microsoft.com/v1.0/users"]

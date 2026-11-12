"""Server problems: recorded with a code, explained in plain English, shown to the right people only."""
import re

from fastapi.testclient import TestClient

from app import problems
from app.graph import AuthError, GraphError
from app.web import create_app

from .fakes import TENANT, FakeGraph, make_settings, tenant_resolver, tenant_routes
from .test_web import ADMIN, OUTSIDER, STAFF, World, csrf, world  # noqa: F401 (fixture)

BAD_SECRET = ("AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the "
              "client secret value, not the client secret ID. Trace ID: abc")


def test_codes_and_hints():
    assert problems.code_for(BAD_SECRET, "x") == "AADSTS7000215"
    assert problems.code_for("no code here", "sign-in") == "sign-in"
    assert problems.graph_code(503) == "graph-5xx" and problems.graph_code(403) == "graph-403"
    assert "Value" in problems.hint("AADSTS7000215")["advice"]
    assert problems.hint("something-new")["title"] == "Something went wrong"


def test_grouping_counts_repeats_and_keeps_newest():
    rows = [{"code": "A", "area": "x", "at": "2026-11-11T10:00:00", "detail": "new"},
            {"code": "B", "area": "y", "at": "2026-11-11T09:00:00", "detail": ""},
            {"code": "A", "area": "x", "at": "2026-11-11T08:00:00", "detail": "old"}]
    groups = problems.grouped(rows)
    assert [g["code"] for g in groups] == ["A", "B"]
    assert groups[0]["count"] == 2 and groups[0]["detail"] == "new"


def test_store_keeps_only_recent_problems(tmp_path):
    w = World(tmp_path)
    import app.db as db
    old_keep, db.KEEP_PROBLEMS = db.KEEP_PROBLEMS, 5
    try:
        for i in range(8):
            w.store.add_problem("scan", f"c{i}")
    finally:
        db.KEEP_PROBLEMS = old_keep
    codes = [p["code"] for p in w.store.list_problems()]
    assert codes[0] == "c7" and len(codes) <= 6


def test_bad_client_secret_explained_and_shown_on_setup(tmp_path):
    w = World(tmp_path)
    w.auth.login_error = BAD_SECRET
    page = w.client().get("/login", follow_redirects=True)
    assert "client secret is wrong" in page.text and "AADSTS7000215" in page.text
    setup = w.client().get("/setup").text
    assert "Recent problems" in setup and "AADSTS7000215" in setup and "Secret ID" in setup
    assert "Trace ID" not in setup  # Microsoft's own message (details) isn't public


def test_sign_in_error_from_microsoft_is_recorded(tmp_path):
    w = World(tmp_path)
    client = w.client()
    w.auth.start_login = lambda session: (session.update(auth_flow={"state": "s"}),
                                          "/auth/callback?error=invalid_request&error_description="
                                          "AADSTS50011%3A+redirect+mismatch")[1]
    page = client.get("/login", follow_redirects=True)
    assert "return address" in page.text
    assert w.store.list_problems()[0]["code"] == "AADSTS50011"


def test_made_up_errors_are_not_recorded_or_published(tmp_path):
    w = World(tmp_path)
    client = w.client()
    page = client.get("/auth/callback?error=VISIT+evil.example+NOW&error_description=hello")
    assert page.status_code == 200 and w.store.list_problems() == []  # no sign-in was started here
    w.auth.start_login = lambda session: (session.update(auth_flow={"state": "s"}),
                                          "/auth/callback?error=VISIT+evil.example&error_description=x")[1]
    client.get("/login", follow_redirects=True)
    assert w.store.list_problems()[0]["code"] == "sign-in"
    assert "evil.example" not in w.client().get("/setup").text


def test_tenant_problems_stay_off_the_public_page(tmp_path):
    w = World(tmp_path)
    w.store.add_problem("scan", "graph-403", "secret tenant detail", TENANT)
    assert "secret tenant detail" not in w.client().get("/setup").text
    assert "No problems recorded" in w.client().get("/setup").text


def test_failed_scan_shows_in_connection_health(tmp_path):
    w = World(tmp_path)
    ada = w.client(ADMIN)
    original = dict(w.routes)
    w.routes["/organization"] = AuthError("AADSTS7000112: Application is disabled.")
    w.connect(ada)
    w.routes.update(original)
    page = ada.get("/app").text
    assert "Connection health" in page and "The last scan failed" in page
    assert "AADSTS7000112" in page and "disabled the app" in page
    assert "Approve the latest permissions" in page  # owner gets the re-approve button
    assert w.store.list_problems(tenant_id=TENANT)[0]["area"] == "scan"


def test_missing_permission_is_counted_on_the_dashboard(tmp_path):
    w = World(tmp_path)
    w.routes["/admin/sharepoint/settings"] = GraphError(403, "accessDenied", "Insufficient privileges")
    ada = w.client(ADMIN)
    page = w.connect(ada).text
    assert "1 part of your tenant couldn&#39;t be read" in page
    assert "SharePoint sharing" in page
    assert w.store.latest_scan(TENANT)["meta"]["no_permission"] == ["sharepoint"]


def test_healthy_tenant_shows_working(world):
    ada = world.client(ADMIN)
    page = world.connect(ada).text
    assert "Microsoft access is working" in page
    assert "Approve the latest permissions" not in page


def test_crash_shows_friendly_page_and_is_recorded(tmp_path):
    w = World(tmp_path)
    w.store.list_customers = lambda: 1 / 0  # break something the errors page needs
    app = w.app
    ops = create_app(settings=make_settings(ops_emails=("ada@contoso.example",), ops_tenant_id=TENANT), store=w.store,
                     graph_factory=lambda tid: FakeGraph(tenant_routes()), resolver=tenant_resolver(), auth=w.auth,
                     tenant_lookup=lambda d: None, sleep=lambda s: None)
    client = TestClient(ops, raise_server_exceptions=False)
    w.auth.user = ADMIN
    client.get("/login", follow_redirects=True)
    page = client.get("/admin/errors")
    assert page.status_code == 500 and "Something went wrong on our side" in page.text
    assert "ZeroDivisionError" not in page.text
    assert page.headers["Content-Security-Policy"].startswith("default-src 'self'")
    crash = w.store.list_problems()[0]
    assert crash["code"] == "crash" and "ZeroDivisionError" in crash["detail"] and "/admin/errors" in crash["detail"]
    assert app  # the original app is untouched


def ops_world(tmp_path, check=None):
    w = World(tmp_path)
    w.checked = []

    def credential_check(tid):
        w.checked.append(tid)
        if check:
            raise check
    w.app = create_app(settings=make_settings(ops_emails=("ada@contoso.example",), ops_tenant_id=TENANT), store=w.store,
                       graph_factory=lambda tid: FakeGraph(w.routes), resolver=tenant_resolver(), auth=w.auth,
                       tenant_lookup=lambda d: None, sleep=lambda s: None, credential_check=credential_check)
    return w


def test_ops_page_is_for_the_tenvaultix_team_only(tmp_path):
    w = ops_world(tmp_path)
    assert w.client().get("/admin/errors", follow_redirects=False).headers["location"].startswith("/login")
    assert w.client(STAFF).get("/admin/errors").status_code == 404
    assert w.client(OUTSIDER).get("/admin/errors").status_code == 404
    ada = w.client(ADMIN)
    w.connect(ada)
    w.store.add_problem("scan", "graph-403", "Insufficient privileges on /reports", TENANT)
    page = ada.get("/ops", follow_redirects=True).text  # the old address still works
    assert f"/admin/customers/{TENANT}" in page and "Contoso" in page
    assert "Insufficient privileges on /reports" in page  # the team sees details


def test_ops_page_off_when_no_team_emails_set(world):
    assert world.client(ADMIN).get("/admin/errors").status_code == 404


def test_ops_needs_the_tenvaultix_tenant_not_just_the_email(tmp_path):
    w = ops_world(tmp_path)
    impostor = {**ADMIN, "tid": "99999999-0000-0000-0000-000000000000"}  # same email, another tenant
    assert w.client(impostor).get("/admin/errors").status_code == 404
    w.app = create_app(settings=make_settings(ops_emails=("ada@contoso.example",)), store=w.store,
                       graph_factory=lambda tid: FakeGraph(w.routes), resolver=tenant_resolver(), auth=w.auth,
                       tenant_lookup=lambda d: None, sleep=lambda s: None)
    assert w.client(ADMIN).get("/admin/errors").status_code == 404  # no TVX_OPS_TENANT_ID set


def test_ops_credential_test_reports_result(tmp_path):
    w = ops_world(tmp_path)
    ada = w.client(ADMIN)
    page = ada.post("/admin/errors/test", data={"csrf": csrf_from(ada, "/admin/errors")}, follow_redirects=True).text
    assert "credential works" in page and w.checked == [TENANT]

    w = ops_world(tmp_path / "b", check=AuthError(BAD_SECRET))
    ada = w.client(ADMIN)
    page = ada.post("/admin/errors/test", data={"csrf": csrf_from(ada, "/admin/errors")}, follow_redirects=True).text
    assert "credential test failed" in page and "AADSTS7000215" in page
    assert w.store.list_problems()[0]["area"] == "credential"


def test_ops_credential_test_needs_csrf(tmp_path):
    w = ops_world(tmp_path)
    w.client(ADMIN).post("/admin/errors/test", data={"csrf": "wrong"})
    assert w.checked == []


def csrf_from(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)

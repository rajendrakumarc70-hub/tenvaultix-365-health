"""HTTPS-only header, the console activity log, and the emergency stop."""
import re

from fastapi.testclient import TestClient

from app import safety, scheduler
from app.cli import main as cli
from app.db import Store
from app.web import create_app

from .fakes import TENANT, FakeAuth, FakeGraph, make_settings, tenant_resolver, tenant_routes
from .test_azure_costs import FakeArm
from .test_console import console_world, token
from .test_web import ADMIN, STAFF, World


def test_hsts_only_on_https(tmp_path):
    https = create_app(settings=make_settings(base_url="https://health.example", https_only=True),
                       store=Store(tmp_path / "a.db"), auth=FakeAuth(ADMIN), resolver=tenant_resolver(),
                       graph_factory=lambda tid: FakeGraph(tenant_routes()))
    assert TestClient(https).get("/").headers["strict-transport-security"].startswith("max-age=31536000")
    plain = World(tmp_path / "b").client().get("/")
    assert "strict-transport-security" not in plain.headers


def test_console_sign_ins_and_denied_attempts_are_logged_once_per_session(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    ada.get("/admin")
    ada.get("/admin/customers")
    bob = w.client(STAFF)
    bob.get("/admin")
    bob.get("/admin/pricing")
    actions = [a["action"] for a in w.store.list_audit()]
    assert actions.count("console.sign-in") == 1 and actions.count("console.denied") == 1
    denied = w.store.list_audit(prefix="console.denied")[0]
    assert denied["actor"] == "bob@contoso.example" and "/admin" in denied["detail"]
    page = ada.get("/admin/security").text
    assert "aren&#39;t on the team" in page or "aren't on the team" in page
    assert "bob@contoso.example" in page


def test_console_changes_exports_and_restores_are_logged(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.post(f"/admin/customers/{TENANT}/licence", data={
        "csrf": token(ada), "plan": "business", "status": "active", "cycle": "monthly"})
    ada.post("/admin/pricing/essentials", data={
        "csrf": token(ada), "name": "Essentials", "price_inr": "2499", "unit": "month", "scan_days": "7",
        "cta": "trial", "public": "1"})
    ada.get("/admin/customers.csv")
    backup = ada.get("/admin/backup.json")
    ada.post("/admin/restore", data={"csrf": token(ada), "confirm": "RESTORE"},
             files={"backup_file": ("b.json", backup.content, "application/json")})
    log = {a["action"]: a for a in w.store.list_audit()}
    assert "Plan: Free trial → Business" in log["licence.change"]["detail"]
    assert "price_inr" in log["pricing.save"]["detail"]
    assert {"data.export", "data.backup", "data.restore"} <= set(log)
    assert all(a["actor"] == "ada@contoso.example" for a in log.values())
    page = ada.get("/admin/security?show=pricing").text
    assert "pricing.save" in page and "licence.change" not in page


def test_security_page_is_team_only_and_shows_checks(tmp_path):
    w = console_world(tmp_path)
    assert w.client(STAFF).get("/admin/security").status_code == 404
    page = w.client(ADMIN).get("/admin/security").text
    assert "Microsoft credential: client secret" in page and "Emergency stop is off" in page


def test_emergency_stop_pauses_everything_that_uses_microsoft_access(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.post("/admin/security/stop", data={"csrf": token(ada), "confirm": "stop it"},
                    follow_redirects=True).text
    assert "Type STOP" in page and safety.stop_state(make_settings(), w.store) is None
    page = ada.post("/admin/security/stop", data={"csrf": token(ada), "confirm": "STOP"}, follow_redirects=True).text
    assert "Emergency stop is on" in page and w.store.list_audit()[0]["action"] == "security.stop-on"

    dash = ada.get("/app").text
    assert "paused for maintenance" in dash and "disabled" in dash
    scans_before = len(w.store.list_scans(TENANT))
    page = ada.post("/app/scan", data={"csrf": token(ada, "/app")}, follow_redirects=True).text
    assert "Scans are paused" in page and len(w.store.list_scans(TENANT)) == scans_before
    assert "Connecting is paused" in ada.get("/connect/start").text
    page = ada.post("/app/fix/ADM-003", data={"csrf": token(ada, "/app")}).text
    assert "Changing settings is paused" in page and w.auth.fix_requests == []
    assert scheduler.tick(w.store, lambda tid: {}, paused=lambda: True) is None

    page = ada.post("/admin/security/resume", data={"csrf": token(ada), "confirm": "RESUME"},
                    follow_redirects=True).text
    assert "Everything is running again" in page and "paused for maintenance" not in ada.get("/app").text


def test_emergency_stop_blocks_azure_reads(tmp_path):
    w = World(tmp_path)
    arm = FakeArm()
    w.app = create_app(settings=make_settings(), store=w.store, graph_factory=lambda tid: FakeGraph(w.routes),
                       resolver=tenant_resolver(), auth=w.auth, tenant_lookup=lambda d: None, sleep=lambda s: None,
                       arm_factory=lambda tid: arm)
    ada = w.client(ADMIN)
    w.connect(ada)
    safety.turn_on(w.store, "ada")
    page = ada.get("/app/azure").text
    assert "paused for maintenance" in page and arm.posts == []


def test_server_setting_stop_cant_be_lifted_from_the_console(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.app = create_app(settings=make_settings(ops_emails=("ada@contoso.example",), ops_tenant_id=TENANT,
                                              emergency_stop=True),
                       store=w.store, graph_factory=lambda tid: FakeGraph(w.routes), resolver=tenant_resolver(),
                       auth=w.auth, tenant_lookup=lambda d: None, sleep=lambda s: None)
    ada = w.client(ADMIN)
    assert "Connecting is paused" in ada.get("/connect/start").text
    page = ada.post("/admin/security/resume", data={"csrf": token(ada), "confirm": "RESUME"},
                    follow_redirects=True).text
    assert "set on the server" in page and "Emergency stop is on" in page


def test_stop_stays_on_this_server_and_out_of_backups(tmp_path):
    store = Store(tmp_path / "s.db")
    safety.turn_on(store, "ada")
    data = store.export_admin()
    assert "emergency_stop" not in data["options"]
    store.import_admin(data, "ada")
    assert safety.stop_state(make_settings(), store)["by"] == "ada"  # a restore doesn't lift it either


def test_cli_scan_due_respects_the_stop(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    store = Store(db)
    store.create_tenant(TENANT, None, "ada@contoso.example")
    safety.turn_on(store, "ada")
    monkeypatch.setenv("TVX_DB_PATH", str(db))
    monkeypatch.setenv("TVX_CLIENT_ID", "client")
    monkeypatch.setenv("TVX_CLIENT_SECRET", "secret")
    assert cli(["scan-due"]) == 0
    assert "emergency stop is on" in capsys.readouterr().err and store.list_scans(TENANT) == []

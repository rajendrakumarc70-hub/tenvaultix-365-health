"""Automatic scans inside the app, the lease that stops two processes scanning at once, and database backups."""
import io
import sqlite3
from datetime import timedelta

from app import scheduler
from app.cli import main as cli
from app.db import Store

from .fakes import TENANT, FakeGraph, make_settings, tenant_resolver, tenant_routes
from .test_web import ADMIN, World
from app.collector import collect


def collector():
    return lambda tid: collect(FakeGraph(tenant_routes(premium=True)), tenant_resolver(), tid)


def test_tick_scans_due_companies_once(tmp_path):
    store = Store(tmp_path / "s.db")
    store.create_tenant(TENANT, None, "ada@contoso.example")
    assert scheduler.tick(store, collector()) == 1
    assert store.latest_scan(TENANT)["status"] == "done"
    assert scheduler.tick(store, collector()) == 0  # not due again until the plan's interval passes
    assert scheduler.last_run(store)["scans"] == 0


def test_tick_skips_suspended_licences_and_keeps_going_after_a_crash(tmp_path):
    store = Store(tmp_path / "s.db")
    store.create_tenant(TENANT, None, "ada@contoso.example")
    store.create_tenant("other", None, "bob@fabrikam.example")
    store.save_licence(TENANT, {"status": "suspended"}, "ada", "Suspended")

    def boom(tid):
        raise RuntimeError("Graph exploded")
    assert scheduler.tick(store, boom) == 1  # "other" was tried; the crash is recorded, not raised
    assert store.list_scans(TENANT) == []
    assert store.latest_scan("other", status="failed") is not None


def test_only_one_process_holds_the_lease(tmp_path):
    store = Store(tmp_path / "s.db")
    assert store.take_lease("job", "a", timedelta(minutes=30))
    assert not store.take_lease("job", "b", timedelta(minutes=30))
    assert store.take_lease("job", "a", timedelta(minutes=30))  # renewing is fine
    assert store.take_lease(scheduler.LEASE_KEY, "a", timedelta(minutes=30))
    assert scheduler.tick(store, collector(), owner="b") is None  # another process is scanning
    assert store.take_lease("job", "b", timedelta(seconds=-1)) is False
    store.set_option("job", "a|2000-01-01T00:00:00+00:00")  # expired
    assert store.take_lease("job", "b", timedelta(minutes=30))


def test_scheduler_bookkeeping_stays_out_of_console_backups(tmp_path):
    store = Store(tmp_path / "s.db")
    scheduler.tick(store, collector())
    data = store.export_admin()
    assert not any(k.startswith("scheduler_") for k in data["options"])
    store.import_admin(data, "ada")
    assert scheduler.last_run(store) is not None  # restoring doesn't wipe it either


def test_setup_page_shows_scheduler_state(tmp_path):
    w = World(tmp_path)
    assert "TVX_SCHEDULER" in w.client().get("/setup").text and "scan-due" in w.client().get("/setup").text


def test_backup_copies_the_whole_database(tmp_path, monkeypatch, capsysbinary):
    db = tmp_path / "s.db"
    store = Store(db)
    store.create_tenant(TENANT, "Contoso", "ada@contoso.example")
    store.backup_to(tmp_path / "copy.db")
    assert sqlite3.connect(tmp_path / "copy.db").execute("SELECT name FROM tenants").fetchone()[0] == "Contoso"
    stream = io.BytesIO()
    store.backup_to(stream)
    assert stream.getvalue().startswith(b"SQLite format 3")
    monkeypatch.setenv("TVX_DB_PATH", str(db))
    assert cli(["backup", "-"]) == 0
    assert capsysbinary.readouterr().out.startswith(b"SQLite format 3")


def test_restore_replaces_the_database(tmp_path, monkeypatch, capsys):
    db = tmp_path / "live.db"
    store = Store(db)
    store.create_tenant(TENANT, "Contoso", "ada@contoso.example")
    store.backup_to(tmp_path / "copy.db")
    store.delete_tenant(TENANT)
    monkeypatch.setenv("TVX_DB_PATH", str(db))
    assert cli(["restore", str(tmp_path / "copy.db")]) == 1  # needs --yes
    assert cli(["restore", str(tmp_path / "copy.db"), "--yes"]) == 0
    assert Store(db).get_tenant(TENANT)["name"] == "Contoso"
    (tmp_path / "junk.db").write_bytes(b"not a database")
    assert cli(["restore", str(tmp_path / "junk.db"), "--yes"]) == 1
    assert "isn't a Tenvaultix database backup" in capsys.readouterr().err
    assert Store(db).get_tenant(TENANT)["name"] == "Contoso"

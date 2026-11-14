"""The database layer on both engines (run the suite with TVX_TEST_DATABASE_URL for PostgreSQL), moving data
between them, and the scheduler at scale: parallel scans, failure back-off and report clean-up."""
import sqlite3
import threading
import time
from datetime import timedelta

from app import scanner as scanner_module
from app import scheduler
from app.collector import collect
from app.config import load_settings
from app.db import Store

from .fakes import TENANT, FakeGraph, tenant_resolver, tenant_routes

OTHERS = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 6)]


def collector(delay: float = 0.0, live: list | None = None):
    def run(tid):
        if live is not None:
            live.append(1)
            time.sleep(delay)
            live.append(-1)
        return collect(FakeGraph(tenant_routes(premium=True)), tenant_resolver(), tid)
    return run


def test_backup_and_restore_round_trip_keeps_everything(tmp_path):
    first = Store(tmp_path / "first.db")
    first.create_tenant(TENANT, "Contoso", "ada@contoso.example")
    first.add_member(TENANT, "bob@contoso.example")
    scan_id = scheduler.run_scan(first, TENANT, collector())
    first.save_details(TENANT, {"company": "Contoso Ltd", "gstin": "29ABCDE1234F1Z5"}, "ada")
    first.add_audit("ada", "customer.details", "test", "198.51.100.1")
    first.backup_to(tmp_path / "backup.db")
    assert sqlite3.connect(tmp_path / "backup.db").execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1

    second = Store(tmp_path / "second.db")
    second.create_tenant("someone-else", "Old", "x@old.example")  # replaced by the restore
    session = second.create_session({"tid": TENANT, "oid": "u1", "email": "ada@contoso.example"}, hours=1)
    second.restore_from(tmp_path / "backup.db")
    assert second.get_tenant("someone-else") is None
    assert second.get_tenant(TENANT)["name"] == first.get_tenant(TENANT)["name"]
    assert second.list_members(TENANT) == first.list_members(TENANT) and len(first.list_members(TENANT)) == 2
    assert second.get_scan(scan_id)["results"] == first.get_scan(scan_id)["results"]
    assert second.get_account(TENANT)["gstin"] == "29ABCDE1234F1Z5"
    assert second.check_session(session) is None  # a restore signs everyone out
    assert second.start_scan(TENANT) > scan_id  # new rows carry on after the restored ones


def test_restore_accepts_a_backup_from_before_sessions_existed(tmp_path):
    old = Store(tmp_path / "old.db")
    old.create_tenant(TENANT, "Contoso", "ada@contoso.example")
    old.backup_to(tmp_path / "old-backup.db")
    raw = sqlite3.connect(tmp_path / "old-backup.db")
    raw.executescript("DROP TABLE sessions; DROP TABLE leases; DROP TABLE azure_costs;")
    raw.close()
    fresh = Store(tmp_path / "fresh.db")
    fresh.restore_from(tmp_path / "old-backup.db")
    assert fresh.get_tenant(TENANT)["name"] == "Contoso"
    fresh.save_costs(TENANT, {"state": "ok"})  # newer tables still work
    assert fresh.check_session(fresh.create_session({"tid": TENANT, "oid": "u1", "email": "a@b.c"}, 1))


def test_only_one_of_two_servers_gets_the_lease(tmp_path):
    store = Store(tmp_path / "lease.db")
    wins, start = [], threading.Barrier(8)

    def grab(owner):
        start.wait()
        if store.take_lease("job", owner, timedelta(minutes=30)):
            wins.append(owner)
    threads = [threading.Thread(target=grab, args=(f"server-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_scan_times_and_failed_scans_wait_before_retrying(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    store.create_tenant(TENANT, None, "ada@contoso.example")

    def boom(tid):
        raise RuntimeError("Graph exploded")
    assert scheduler.tick(store, boom) == 1
    times = store.scan_times()[TENANT]
    assert times["last_failed"] and not times["last_done"] and not times["running_since"]
    assert scheduler.tick(store, collector()) == 0  # not hammered every 15 minutes
    later = scanner_module.utcnow() + timedelta(hours=7)
    monkeypatch.setattr(scanner_module, "utcnow", lambda: later)
    assert scheduler.tick(store, collector()) == 1
    assert store.latest_scan(TENANT)["status"] == "done"


def test_scheduler_scans_several_companies_at_once(tmp_path):
    store = Store(tmp_path / "many.db")
    for tid in [TENANT, *OTHERS]:
        store.create_tenant(tid, None, f"admin@{tid[:4]}.example")
    live: list[int] = []
    assert scheduler.tick(store, collector(delay=0.2, live=live), workers=3) == 6
    running, most = 0, 0
    for step in live:
        running += step
        most = max(most, running)
    assert most > 1  # scans overlapped
    assert all(store.latest_scan(t)["status"] == "done" for t in [TENANT, *OTHERS])


def test_old_reports_are_pruned_but_the_latest_is_kept(tmp_path):
    store = Store(tmp_path / "p.db")
    store.create_tenant(TENANT, None, "ada@contoso.example")
    ids = [scheduler.run_scan(store, TENANT, collector()) for _ in range(3)]
    with store._conn() as db:
        db.execute("UPDATE scans SET started_at = '2020-01-01T00:00:00+00:00'")
    assert store.prune_scans(timedelta(days=400)) == 2
    assert [s["id"] for s in store.list_scans(TENANT)] == [ids[-1]]
    store.set_option(scheduler.PRUNED_KEY, "2020-01-01T00:00:00+00:00")
    assert scheduler.prune(store, 400) == 0
    assert scheduler.prune(store, 400) is None  # at most once a day


def test_one_customer_lookup_and_engine_name(tmp_path):
    store = Store(tmp_path / "c.db")
    store.create_tenant(TENANT, "Contoso", "ada@contoso.example")
    store.save_details("11111111-0000-0000-0000-000000000000", {"company": "Prospect"}, "ada")
    assert [c["id"] for c in store.list_customers(tenant_id=TENANT)] == [TENANT]
    assert len(store.list_customers()) == 2
    assert store.engine in ("SQLite", "PostgreSQL")


def test_database_url_setting(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("TVX_DATABASE_URL", "postgresql://u:p@db:5432/tenvaultix")
    assert load_settings().database == "postgresql://u:p@db:5432/tenvaultix"
    monkeypatch.delenv("TVX_DATABASE_URL")
    monkeypatch.setenv("TVX_DB_PATH", "/srv/data/health.db")
    assert str(load_settings().database) == "/srv/data/health.db"
    monkeypatch.setenv("RENDER", "true")
    assert load_settings().client_ip_header == "CF-Connecting-IP"

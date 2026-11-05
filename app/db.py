"""SQLite storage: connected tenants, who may see them, and scan results.

Only findings are stored (the check results shown in reports), never raw tenant data. Deleting a
tenant deletes all of its scans and members.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import TRIAL_DAYS

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT,
    plan TEXT NOT NULL DEFAULT 'trial',
    trial_ends TEXT,
    connected_at TEXT NOT NULL,
    connected_by TEXT
);
CREATE TABLE IF NOT EXISTS members (
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    added_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, email)
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    score INTEGER,
    grade TEXT,
    problems INTEGER,
    urgent INTEGER,
    waste REAL,
    results TEXT,
    meta TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS scans_by_tenant ON scans(tenant_id, id);
"""
STALE_SCAN = timedelta(minutes=30)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # ---------------------------------------------------------------- tenants

    def create_tenant(self, tenant_id: str, name: str | None, owner_email: str) -> None:
        now = utcnow()
        with self._conn() as db:
            db.execute(
                "INSERT INTO tenants (id, name, plan, trial_ends, connected_at, connected_by) VALUES (?, ?, 'trial', ?, ?, ?)",
                (tenant_id, name, _iso(now + timedelta(days=TRIAL_DAYS)), _iso(now), owner_email),
            )
            db.execute("INSERT INTO members (tenant_id, email, role, added_at) VALUES (?, ?, 'owner', ?)",
                       (tenant_id, owner_email.lower(), _iso(now)))

    def get_tenant(self, tenant_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None

    def list_tenants(self) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tenants ORDER BY connected_at")]

    def set_tenant_name(self, tenant_id: str, name: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE tenants SET name = ? WHERE id = ?", (name, tenant_id))

    def set_plan(self, tenant_id: str, plan: str) -> bool:
        with self._conn() as db:
            return db.execute("UPDATE tenants SET plan = ? WHERE id = ?", (plan, tenant_id)).rowcount == 1

    def delete_tenant(self, tenant_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))

    # ---------------------------------------------------------------- members

    def member_role(self, tenant_id: str, email: str) -> str | None:
        with self._conn() as db:
            row = db.execute("SELECT role FROM members WHERE tenant_id = ? AND email = ?",
                             (tenant_id, email.lower())).fetchone()
        return row["role"] if row else None

    def list_members(self, tenant_id: str) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT email, role, added_at FROM members WHERE tenant_id = ? ORDER BY role DESC, email", (tenant_id,))]

    def add_member(self, tenant_id: str, email: str, role: str = "viewer") -> None:
        with self._conn() as db:
            db.execute("INSERT OR IGNORE INTO members (tenant_id, email, role, added_at) VALUES (?, ?, ?, ?)",
                       (tenant_id, email.lower(), role, _iso(utcnow())))

    def remove_member(self, tenant_id: str, email: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM members WHERE tenant_id = ? AND email = ? AND role != 'owner'",
                       (tenant_id, email.lower()))

    # ---------------------------------------------------------------- scans

    def start_scan(self, tenant_id: str) -> int:
        with self._conn() as db:
            cur = db.execute("INSERT INTO scans (tenant_id, status, started_at) VALUES (?, 'running', ?)",
                             (tenant_id, _iso(utcnow())))
            return int(cur.lastrowid)

    def finish_scan(self, scan_id: int, results: list[dict], summary: dict, meta: dict) -> None:
        with self._conn() as db:
            db.execute(
                "UPDATE scans SET status='done', finished_at=?, score=?, grade=?, problems=?, urgent=?, waste=?, "
                "results=?, meta=? WHERE id=?",
                (_iso(utcnow()), summary["score"], summary["grade"], summary["problems"], summary["urgent"],
                 summary["waste"], json.dumps(results), json.dumps(meta), scan_id),
            )

    def fail_scan(self, scan_id: int, error: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE scans SET status='failed', finished_at=?, error=? WHERE id=?",
                       (_iso(utcnow()), error, scan_id))

    def _scan(self, row) -> dict | None:
        if not row:
            return None
        scan = dict(row)
        scan["results"] = json.loads(scan["results"]) if scan["results"] else []
        scan["meta"] = json.loads(scan["meta"]) if scan["meta"] else {}
        return scan

    def get_scan(self, scan_id: int) -> dict | None:
        with self._conn() as db:
            return self._scan(db.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone())

    def latest_scan(self, tenant_id: str, status: str = "done") -> dict | None:
        with self._conn() as db:
            return self._scan(db.execute(
                "SELECT * FROM scans WHERE tenant_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                (tenant_id, status)).fetchone())

    def previous_scan(self, tenant_id: str, before_id: int) -> dict | None:
        with self._conn() as db:
            return self._scan(db.execute(
                "SELECT * FROM scans WHERE tenant_id = ? AND status = 'done' AND id < ? ORDER BY id DESC LIMIT 1",
                (tenant_id, before_id)).fetchone())

    def list_scans(self, tenant_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT id, status, started_at, finished_at, score, grade, problems, urgent, waste, error "
                "FROM scans WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (tenant_id, limit))]

    def running_scan(self, tenant_id: str) -> dict | None:
        """The scan in progress, if any. Scans stuck for 30+ minutes (server restarted) are marked failed."""
        scan = self.latest_scan(tenant_id, status="running")
        if not scan:
            return None
        started = datetime.fromisoformat(scan["started_at"])
        if utcnow() - started > STALE_SCAN:
            self.fail_scan(scan["id"], "The scan was interrupted. Please run it again.")
            return None
        return scan

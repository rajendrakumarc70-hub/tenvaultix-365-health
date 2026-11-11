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
CREATE TABLE IF NOT EXISTS fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    check_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before TEXT,
    after TEXT,
    status TEXT NOT NULL,
    error TEXT,
    by_email TEXT NOT NULL,
    at TEXT NOT NULL,
    undo_of INTEGER,
    undone_by INTEGER
);
CREATE INDEX IF NOT EXISTS fixes_by_tenant ON fixes(tenant_id, id);
CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    area TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT,
    tenant_id TEXT
);
CREATE TABLE IF NOT EXISTS azure_costs (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    data TEXT NOT NULL
);
"""
KEEP_PROBLEMS = 500
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
            db.execute("DELETE FROM problems WHERE tenant_id = ?", (tenant_id,))

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

    # ---------------------------------------------------------------- one-click fixes

    def add_fix(self, tenant_id: str, check_id: str, action: str, *, before=None, after=None, status: str,
                by_email: str, error: str | None = None, undo_of: int | None = None) -> int:
        """Record a change (action 'apply' or 'undo'; status 'done', 'unverified' or 'failed')."""
        with self._conn() as db:
            cur = db.execute(
                "INSERT INTO fixes (tenant_id, check_id, action, before, after, status, error, by_email, at, undo_of) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, check_id, action, json.dumps(before), json.dumps(after), status, error,
                 by_email.lower(), _iso(utcnow()), undo_of))
            if undo_of and status != "failed":
                db.execute("UPDATE fixes SET undone_by = ? WHERE id = ? AND tenant_id = ?",
                           (cur.lastrowid, undo_of, tenant_id))
            return int(cur.lastrowid)

    @staticmethod
    def _fix(row) -> dict | None:
        if not row:
            return None
        fix = dict(row)
        fix["before"] = json.loads(fix["before"]) if fix["before"] else None
        fix["after"] = json.loads(fix["after"]) if fix["after"] else None
        return fix

    def get_fix(self, fix_id: int) -> dict | None:
        with self._conn() as db:
            return self._fix(db.execute("SELECT * FROM fixes WHERE id = ?", (fix_id,)).fetchone())

    def list_fixes(self, tenant_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as db:
            return [self._fix(r) for r in db.execute(
                "SELECT * FROM fixes WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (tenant_id, limit))]

    # ---------------------------------------------------------------- problems (server errors people can see)

    def add_problem(self, area: str, code: str, detail: str | None = None, tenant_id: str | None = None) -> None:
        with self._conn() as db:
            db.execute("INSERT INTO problems (at, area, code, detail, tenant_id) VALUES (?, ?, ?, ?, ?)",
                       (_iso(utcnow()), area, code, (detail or "")[:500], tenant_id))
            db.execute("DELETE FROM problems WHERE id <= (SELECT MAX(id) FROM problems) - ?", (KEEP_PROBLEMS,))

    def list_problems(self, limit: int = 20, tenant_id: str | None = None, server_only: bool = False) -> list[dict]:
        with self._conn() as db:
            if server_only:
                rows = db.execute("SELECT * FROM problems WHERE tenant_id IS NULL ORDER BY id DESC LIMIT ?", (limit,))
            elif tenant_id:
                rows = db.execute("SELECT * FROM problems WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                                  (tenant_id, limit))
            else:
                rows = db.execute("SELECT * FROM problems ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    # ---------------------------------------------------------------- Azure costs (cached)

    def save_costs(self, tenant_id: str, data: dict) -> None:
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO azure_costs (tenant_id, fetched_at, data) VALUES (?, ?, ?)",
                       (tenant_id, _iso(utcnow()), json.dumps(data)))

    def get_costs(self, tenant_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT fetched_at, data FROM azure_costs WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if not row:
            return None
        return {"fetched_at": row["fetched_at"], **json.loads(row["data"])}

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

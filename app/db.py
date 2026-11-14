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

from . import licensing

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
CREATE TABLE IF NOT EXISTS accounts (
    tenant_id TEXT PRIMARY KEY,           -- kept after a disconnect: it's Tenvaultix's customer and billing record
    company TEXT, domain TEXT, contact_name TEXT, contact_email TEXT, contact_phone TEXT,
    gstin TEXT, address TEXT, notes TEXT,
    plan TEXT,                            -- licence assigned before the company connects
    status TEXT NOT NULL DEFAULT 'active',  -- active | suspended
    paid_until TEXT,                      -- licence valid until (date); empty = no end date
    seats INTEGER,                        -- user limit; empty = the plan's limit
    cycle TEXT NOT NULL DEFAULT 'monthly',
    price_inr REAL,                       -- agreed price per month; empty = the plan's list price
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS licence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    at TEXT NOT NULL,
    by_email TEXT,
    summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS licence_events_by_tenant ON licence_events(tenant_id, id);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price_inr REAL,
    price_usd REAL,
    unit TEXT NOT NULL DEFAULT 'month',
    max_users INTEGER,
    scan_days INTEGER NOT NULL DEFAULT 7,
    tagline TEXT,
    features TEXT NOT NULL DEFAULT '[]',
    public INTEGER NOT NULL DEFAULT 1,
    featured INTEGER NOT NULL DEFAULT 0,
    cta TEXT NOT NULL DEFAULT 'trial',
    sort INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS options (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS azure_costs (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    data TEXT NOT NULL
);
"""
KEEP_PROBLEMS = 500
KEEP_AUDIT = 5000
LOCAL_OPTIONS = ("scheduler_", "emergency_stop")  # this server's state: never exported or restored
PLAN_FIELDS = ("name", "price_inr", "price_usd", "unit", "max_users", "scan_days", "tagline", "features", "public",
               "featured", "cta", "sort")
DETAIL_FIELDS = ("company", "domain", "contact_name", "contact_email", "contact_phone", "gstin", "address", "notes")
LICENCE_FIELDS = ("plan", "status", "paid_until", "seats", "cycle", "price_inr")
TENANT_SELECT = ("SELECT t.*, a.status AS licence_status, a.paid_until, a.seats, a.price_inr, a.cycle, "
                 "a.company, a.domain FROM tenants t LEFT JOIN accounts a ON a.tenant_id = t.id")
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
            if not db.execute("SELECT COUNT(*) FROM plans").fetchone()[0]:
                for plan in licensing.DEFAULT_PLANS:
                    self._write_plan(db, plan, None)

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
        """A company connects. A licence the Tenvaultix team set up beforehand applies; otherwise a free trial."""
        now = utcnow()
        account = self.get_account(tenant_id)
        plan = (account or {}).get("plan") or "trial"
        trial_ends = _iso(now + timedelta(days=self.trial_days())) if plan == "trial" else None
        with self._conn() as db:
            db.execute(
                "INSERT INTO tenants (id, name, plan, trial_ends, connected_at, connected_by) VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, name, plan, trial_ends, _iso(now), owner_email),
            )
            db.execute("INSERT INTO members (tenant_id, email, role, added_at) VALUES (?, ?, 'owner', ?)",
                       (tenant_id, owner_email.lower(), _iso(now)))

    def get_tenant(self, tenant_id: str) -> dict | None:
        """The tenant, with its licence (licence_status, paid_until, seats...) when the team has set one."""
        with self._conn() as db:
            row = db.execute(TENANT_SELECT + " WHERE t.id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None

    def list_tenants(self) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(TENANT_SELECT + " ORDER BY t.connected_at")]

    def set_tenant_name(self, tenant_id: str, name: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE tenants SET name = ? WHERE id = ?", (name, tenant_id))

    def delete_tenant(self, tenant_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
            db.execute("DELETE FROM problems WHERE tenant_id = ?", (tenant_id,))

    # ---------------------------------------------------------------- plans and prices (edited in the console)

    @staticmethod
    def _write_plan(db, plan: dict, by: str | None) -> None:
        values = [json.dumps(plan.get(f) or []) if f == "features" else
                  int(bool(plan.get(f))) if f in ("public", "featured") else plan.get(f) for f in PLAN_FIELDS]
        db.execute(f"INSERT OR REPLACE INTO plans (id, {', '.join(PLAN_FIELDS)}, updated_at, updated_by) "
                   f"VALUES (?, {', '.join('?' for _ in PLAN_FIELDS)}, ?, ?)",
                   (plan["id"], *values, _iso(utcnow()), by))

    @staticmethod
    def _plan(row) -> dict:
        plan = dict(row)
        plan["features"] = json.loads(plan["features"] or "[]")
        plan["public"], plan["featured"] = bool(plan["public"]), bool(plan["featured"])
        return plan

    def list_plans(self, public_only: bool = False) -> list[dict]:
        query = "SELECT * FROM plans" + (" WHERE public = 1 AND id != 'trial'" if public_only else "")
        with self._conn() as db:
            return [self._plan(r) for r in db.execute(query + " ORDER BY sort, name")]

    def get_plan(self, plan_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return self._plan(row) if row else None

    def save_plan(self, plan: dict, by: str) -> None:
        with self._conn() as db:
            self._write_plan(db, plan, by)

    def plan_in_use(self, plan_id: str) -> int:
        with self._conn() as db:
            return (db.execute("SELECT COUNT(*) FROM tenants WHERE plan = ?", (plan_id,)).fetchone()[0]
                    + db.execute("SELECT COUNT(*) FROM accounts WHERE plan = ? AND tenant_id NOT IN "
                                 "(SELECT id FROM tenants)", (plan_id,)).fetchone()[0])

    def delete_plan(self, plan_id: str) -> bool:
        if plan_id == "trial" or self.plan_in_use(plan_id):
            return False
        with self._conn() as db:
            return db.execute("DELETE FROM plans WHERE id = ?", (plan_id,)).rowcount == 1

    def delete_option(self, key: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM options WHERE key = ?", (key,))

    def get_option(self, key: str, default: str = "") -> str:
        with self._conn() as db:
            row = db.execute("SELECT value FROM options WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_option(self, key: str, value: str) -> None:
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO options (key, value) VALUES (?, ?)", (key, value))

    def take_lease(self, key: str, owner: str, duration: timedelta) -> bool:
        """Claim (or renew) a named lease for `duration`, unless someone else holds an unexpired one."""
        now = utcnow()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM options WHERE key = ?", (key,)).fetchone()
            if row:
                holder, _, until = row["value"].partition("|")
                try:
                    held = holder != owner and datetime.fromisoformat(until) > now
                except ValueError:
                    held = False
                if held:
                    return False
            db.execute("INSERT OR REPLACE INTO options (key, value) VALUES (?, ?)",
                       (key, f"{owner}|{_iso(now + duration)}"))
            return True

    def trial_days(self) -> int:
        value = self.get_option("trial_days")
        return int(value) if value.isdigit() else licensing.DEFAULT_TRIAL_DAYS

    # ---------------------------------------------------------------- customer accounts and licences

    def get_account(self, tenant_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM accounts WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None

    def _ensure_account(self, db, tenant_id: str, by: str | None) -> None:
        now = _iso(utcnow())
        db.execute("INSERT OR IGNORE INTO accounts (tenant_id, created_at, updated_at, updated_by) "
                   "VALUES (?, ?, ?, ?)", (tenant_id, now, now, by))

    def save_details(self, tenant_id: str, details: dict, by: str) -> None:
        fields = [f for f in DETAIL_FIELDS if f in details]
        with self._conn() as db:
            self._ensure_account(db, tenant_id, by)
            db.execute(f"UPDATE accounts SET {', '.join(f + ' = ?' for f in fields)}, updated_at = ?, updated_by = ? "
                       "WHERE tenant_id = ?", (*[details[f] for f in fields], _iso(utcnow()), by, tenant_id))

    def save_licence(self, tenant_id: str, licence: dict, by: str, summary: str) -> None:
        """Set a customer's licence; the connected tenant's plan follows it. `summary` goes in the history."""
        fields = [f for f in LICENCE_FIELDS if f in licence]
        with self._conn() as db:
            self._ensure_account(db, tenant_id, by)
            db.execute(f"UPDATE accounts SET {', '.join(f + ' = ?' for f in fields)}, updated_at = ?, updated_by = ? "
                       "WHERE tenant_id = ?", (*[licence[f] for f in fields], _iso(utcnow()), by, tenant_id))
            if licence.get("plan"):
                db.execute("UPDATE tenants SET plan = ? WHERE id = ?", (licence["plan"], tenant_id))
            if summary:
                db.execute("INSERT INTO licence_events (tenant_id, at, by_email, summary) VALUES (?, ?, ?, ?)",
                           (tenant_id, _iso(utcnow()), by, summary))

    def licence_events(self, tenant_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM licence_events WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (tenant_id, limit))]

    def list_customers(self) -> list[dict]:
        """Every connected company and every customer record set up before connecting, newest first.

        Each row merges the tenant (if connected) with its account: plan, licence fields and contact details."""
        with self._conn() as db:
            tenants = {r["id"]: dict(r) for r in db.execute("SELECT * FROM tenants")}
            accounts = {r["tenant_id"]: dict(r) for r in db.execute("SELECT * FROM accounts")}
            last = {r["tenant_id"]: dict(r) for r in db.execute(
                "SELECT s.tenant_id, s.status, s.started_at, s.score, s.grade, s.meta FROM scans s JOIN "
                "(SELECT tenant_id, MAX(id) AS id FROM scans WHERE status != 'running' GROUP BY tenant_id) m "
                "ON m.id = s.id")}
        rows = []
        for tid in set(tenants) | set(accounts):
            tenant, account = tenants.get(tid), accounts.get(tid) or {}
            scan = last.get(tid)
            users = (licensing.staff_count(json.loads(scan["meta"] or "{}"))
                     if scan and scan["status"] == "done" else None)
            rows.append({
                **{k: v for k, v in account.items() if k not in ("plan", "status")},
                "id": tid, "connected": tenant is not None,
                "name": (tenant or {}).get("name") or account.get("company"),
                "plan": (tenant or {}).get("plan") or account.get("plan") or "trial",
                "trial_ends": (tenant or {}).get("trial_ends"),
                "licence_status": account.get("status"),
                "connected_at": (tenant or {}).get("connected_at"), "connected_by": (tenant or {}).get("connected_by"),
                "created_at": account.get("created_at") or (tenant or {}).get("connected_at"),
                "last_scan": scan, "users": users,
            })
        return sorted(rows, key=lambda r: r["created_at"] or "", reverse=True)

    # ---------------------------------------------------------------- backup (the free host's disk is temporary)

    def backup_to(self, target) -> None:
        """A consistent copy of the whole database (SQLite's online backup), to a path or a binary stream."""
        import os
        import tempfile

        if hasattr(target, "write"):
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                self.backup_to(tmp)
                with open(tmp, "rb") as fh:
                    target.write(fh.read())
                target.flush()
            finally:
                os.unlink(tmp)
            return
        src = sqlite3.connect(self.path, timeout=15)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def restore_from(self, source) -> None:
        """Replace the whole database with a backup made by backup_to (a path or a binary stream)."""
        import os
        import tempfile

        if hasattr(source, "read"):
            fd, tmp = tempfile.mkstemp(suffix=".db")
            with os.fdopen(fd, "wb") as fh:
                fh.write(source.read())
            try:
                self.restore_from(tmp)
            finally:
                os.unlink(tmp)
            return
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            try:
                tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            except sqlite3.DatabaseError:
                tables = set()
            if not {"tenants", "scans"} <= tables:
                raise ValueError("That file isn't a Tenvaultix database backup.")
            dst = sqlite3.connect(self.path, timeout=15)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with self._conn() as db:  # a backup from an older version gets any newer tables and the default plans
            db.executescript(SCHEMA)
            if not db.execute("SELECT COUNT(*) FROM plans").fetchone()[0]:
                for plan in licensing.DEFAULT_PLANS:
                    self._write_plan(db, plan, None)

    def export_admin(self) -> dict:
        with self._conn() as db:
            return {
                "kind": "tenvaultix-console-backup", "version": 1, "exported_at": _iso(utcnow()),
                "plans": [self._plan(r) for r in db.execute("SELECT * FROM plans ORDER BY sort")],
                "options": {r["key"]: r["value"] for r in db.execute("SELECT * FROM options")
                            if not r["key"].startswith(LOCAL_OPTIONS)},
                "accounts": [dict(r) for r in db.execute("SELECT * FROM accounts")],
                "licence_events": [dict(r) for r in db.execute("SELECT * FROM licence_events ORDER BY id")],
            }

    def import_admin(self, data: dict, by: str) -> dict:
        """Put back plans, options, customer records and licence history from export_admin(). Replaces them all."""
        if data.get("kind") != "tenvaultix-console-backup" or data.get("version") != 1:
            raise ValueError("This isn't a Tenvaultix console backup file.")
        plans = [p for p in data.get("plans") or [] if licensing.PLAN_ID.match(str(p.get("id", "")))]
        if not any(p["id"] == "trial" for p in plans):
            raise ValueError("The backup has no plans in it.")
        account_cols = ("tenant_id",) + DETAIL_FIELDS + LICENCE_FIELDS + ("created_at", "updated_at", "updated_by")
        with self._conn() as db:
            db.execute("DELETE FROM plans")
            for plan in plans:
                self._write_plan(db, plan, by)
            for key in [r["key"] for r in db.execute("SELECT key FROM options")]:
                if not key.startswith(LOCAL_OPTIONS):
                    db.execute("DELETE FROM options WHERE key = ?", (key,))
            for key, value in (data.get("options") or {}).items():
                if str(key).startswith(LOCAL_OPTIONS):
                    continue
                db.execute("INSERT INTO options (key, value) VALUES (?, ?)", (str(key)[:64], str(value)[:500]))
            db.execute("DELETE FROM accounts")
            for acc in data.get("accounts") or []:
                if not acc.get("tenant_id"):
                    continue
                row = {c: acc.get(c) for c in account_cols}
                row["status"] = row["status"] or "active"
                row["cycle"] = row["cycle"] or "monthly"
                row["created_at"] = row["created_at"] or _iso(utcnow())
                row["updated_at"] = row["updated_at"] or row["created_at"]
                db.execute(f"INSERT INTO accounts ({', '.join(account_cols)}) VALUES "
                           f"({', '.join('?' for _ in account_cols)})", [row[c] for c in account_cols])
                if row["plan"]:
                    db.execute("UPDATE tenants SET plan = ? WHERE id = ?", (row["plan"], row["tenant_id"]))
            db.execute("DELETE FROM licence_events")
            for ev in data.get("licence_events") or []:
                if ev.get("tenant_id") and ev.get("summary"):
                    db.execute("INSERT INTO licence_events (tenant_id, at, by_email, summary) VALUES (?, ?, ?, ?)",
                               (ev["tenant_id"], ev.get("at") or _iso(utcnow()), ev.get("by_email"),
                                str(ev["summary"])[:500]))
        return {"plans": len(plans), "accounts": len(data.get("accounts") or [])}

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

    # ---------------------------------------------------------------- activity log (who did what in the console)

    def add_audit(self, actor: str | None, action: str, detail: str | None = None, ip: str | None = None) -> None:
        with self._conn() as db:
            db.execute("INSERT INTO audit (at, actor, action, detail, ip) VALUES (?, ?, ?, ?, ?)",
                       (_iso(utcnow()), actor, action, (detail or "")[:500], (ip or "")[:64]))
            db.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id) FROM audit) - ?", (KEEP_AUDIT,))

    def list_audit(self, limit: int = 200, prefix: str | None = None) -> list[dict]:
        with self._conn() as db:
            if prefix:
                rows = db.execute("SELECT * FROM audit WHERE action LIKE ? ORDER BY id DESC LIMIT ?",
                                  (prefix + "%", limit))
            else:
                rows = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
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

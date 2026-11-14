"""Storage: connected tenants, who may see them, scan results, the console's customer records and sign-in sessions.

Two database engines, the same tables and the same code:

- SQLite, a single file (TVX_DB_PATH): development, tests and small installs.
- PostgreSQL (TVX_DATABASE_URL=postgresql://...): production and many customers. Azure Database for PostgreSQL, or
  the `db` container in deploy/docker-compose.yml. Several app servers can share one database.

Only findings are stored (the check results shown in reports), never raw tenant data. Deleting a tenant deletes all
of its scans, fixes, team and cached costs.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import licensing

# {id} and {real} differ between SQLite and PostgreSQL; everything else is the same.
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
    id {id},
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    score INTEGER,
    grade TEXT,
    problems INTEGER,
    urgent INTEGER,
    waste {real},
    results TEXT,
    meta TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS scans_by_tenant ON scans(tenant_id, id);
CREATE TABLE IF NOT EXISTS fixes (
    id {id},
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
    id {id},
    at TEXT NOT NULL,
    area TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT,
    tenant_id TEXT
);
CREATE INDEX IF NOT EXISTS problems_by_tenant ON problems(tenant_id, id);
CREATE TABLE IF NOT EXISTS accounts (
    tenant_id TEXT PRIMARY KEY,           -- kept after a disconnect: it's Tenvaultix's customer and billing record
    company TEXT, domain TEXT, contact_name TEXT, contact_email TEXT, contact_phone TEXT,
    gstin TEXT, address TEXT, notes TEXT,
    plan TEXT,                            -- licence assigned before the company connects
    status TEXT NOT NULL DEFAULT 'active',  -- active | suspended
    paid_until TEXT,                      -- licence valid until (date); empty = no end date
    seats INTEGER,                        -- user limit; empty = the plan's limit
    cycle TEXT NOT NULL DEFAULT 'monthly',
    price_inr {real},                     -- agreed price per month; empty = the plan's list price
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS licence_events (
    id {id},
    tenant_id TEXT NOT NULL,
    at TEXT NOT NULL,
    by_email TEXT,
    summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS licence_events_by_tenant ON licence_events(tenant_id, id);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price_inr {real},
    price_usd {real},
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
    id {id},
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
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,                  -- SHA-256 of the random value in the visitor's cookie, never the value
    tenant_id TEXT NOT NULL,
    user_oid TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(tenant_id, user_oid);
CREATE INDEX IF NOT EXISTS sessions_by_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    until TEXT NOT NULL
);
"""
SQLITE_TYPES = {"id": "INTEGER PRIMARY KEY AUTOINCREMENT", "real": "REAL"}
POSTGRES_TYPES = {"id": "BIGSERIAL PRIMARY KEY", "real": "DOUBLE PRECISION"}

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
SESSION_TOUCH = timedelta(minutes=5)  # how often a session's "last seen" time is written

# Full backups and copies between databases: parents before children. Sessions and leases are never copied.
TABLES = ("plans", "options", "accounts", "licence_events", "tenants", "members", "scans", "fixes", "problems",
          "audit", "azure_costs")
SERIAL_TABLES = ("scans", "fixes", "problems", "licence_events", "audit")
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_database_url(target) -> bool:
    return str(target).startswith(("postgresql://", "postgres://"))


def _dict_row(cursor, row) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


class _Conn:
    """One open connection with the same calls on both engines: '?' placeholders, rows as dicts."""

    def __init__(self, raw, postgres: bool):
        self.raw, self.postgres = raw, postgres

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params=()):
        if self.postgres:
            return self.raw.execute(self._sql(sql), params or None)
        return self.raw.execute(sql, params)

    def executemany(self, sql: str, rows: list) -> None:
        if not rows:
            return
        if self.postgres:
            with self.raw.cursor() as cur:
                cur.executemany(self._sql(sql), rows)
        else:
            self.raw.executemany(sql, rows)

    def one(self, sql: str, params=()) -> dict | None:
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[dict]:
        return list(self.execute(sql, params).fetchall())

    def stream(self, sql: str):
        """Rows one batch at a time, so copying a big table doesn't load it all into memory."""
        if self.postgres:
            with self.raw.cursor(name="tvx_copy") as cur:
                cur.itersize = 200
                cur.execute(sql)
                yield from cur
        else:
            yield from self.raw.execute(sql)

    def script(self, sql: str) -> None:
        if self.postgres:
            self.raw.execute(sql)
        else:
            self.raw.executescript(sql)

    def columns(self, table: str) -> list[str]:
        if self.postgres:
            rows = self.all("SELECT column_name AS name FROM information_schema.columns WHERE table_name = ? "
                            "AND table_schema = current_schema() ORDER BY ordinal_position", (table,))
        else:
            rows = self.all(f"PRAGMA table_info({table})")
        return [r["name"] for r in rows]

    def reset_ids(self) -> None:
        """After rows were copied in with their own ids, new rows continue from the highest one."""
        if self.postgres:
            for table in SERIAL_TABLES:
                self.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                             f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")


def _copy_tables(src: _Conn, dst: _Conn) -> dict[str, int]:
    """Replace every data table in `dst` with the rows in `src`. Everyone is signed out afterwards."""
    for table in reversed(TABLES):
        dst.execute(f"DELETE FROM {table}")
    dst.execute("DELETE FROM sessions")
    counts = {}
    for table in TABLES:
        have = src.columns(table)
        if not have:  # a backup from before this table existed
            continue
        wanted = set(dst.columns(table))
        cols = [c for c in have if c in wanted]
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        batch, count = [], 0
        for row in src.stream(f"SELECT {', '.join(cols)} FROM {table}"):
            batch.append(tuple(row[c] for c in cols))
            count += 1
            if len(batch) >= 500:
                dst.executemany(sql, batch)
                batch = []
        dst.executemany(sql, batch)
        counts[table] = count
    dst.reset_ids()
    return counts


class Store:
    def __init__(self, target: Path | str, *, schema: str | None = None):
        """`target` is a SQLite file path, or a postgresql:// URL. `schema` (PostgreSQL only) keeps the tables in
        their own schema, so tests can share one database."""
        self._setup(target, schema)

    @classmethod
    def sqlite_file(cls, path: Path | str) -> "Store":
        """Always a SQLite file (for backups), whatever the main database is."""
        store = cls.__new__(cls)
        store._setup(path, None)
        return store

    def _setup(self, target: Path | str, schema: str | None) -> None:
        self.postgres = is_database_url(target)
        self._pool = None
        if self.postgres:
            self.url, self.path = str(target), None
            if schema and not SCHEMA_NAME.match(schema):
                raise ValueError(f"Bad schema name {schema!r}")
            self._schema = schema
            self._open_postgres()
        else:
            self.url, self.path = None, str(target)
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            if self.postgres:
                db.execute("SELECT pg_advisory_xact_lock(724001)")  # two servers starting at once
            db.script(SCHEMA.format(**(POSTGRES_TYPES if self.postgres else SQLITE_TYPES)))
            if not db.one("SELECT COUNT(*) AS n FROM plans")["n"]:
                for plan in licensing.DEFAULT_PLANS:
                    self._write_plan(db, plan, None)

    @property
    def engine(self) -> str:
        return "PostgreSQL" if self.postgres else "SQLite"

    # ---------------------------------------------------------------- connections

    def _pg_kwargs(self) -> dict:
        from psycopg.rows import dict_row

        kwargs = {"row_factory": dict_row}
        if self._schema:
            kwargs["options"] = f"-c search_path={self._schema}"
        return kwargs

    def _open_postgres(self) -> None:
        import psycopg

        if self._schema:
            with psycopg.connect(self.url, autocommit=True) as raw:
                raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        size = int(os.environ.get("TVX_DB_POOL", "10") or 0)
        if size > 0:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(self.url, min_size=1, max_size=size, kwargs=self._pg_kwargs(), open=True,
                                        check=ConnectionPool.check_connection, name="tvx")
            atexit.register(self.close)  # close the connections cleanly when the process ends

    def close(self) -> None:
        if self._pool is not None and not self._pool.closed:
            self._pool.close()

    @contextmanager
    def _conn(self):
        """A connection inside one transaction: committed when the block ends, rolled back on an error."""
        if self.postgres:
            if self._pool is not None:
                with self._pool.connection() as raw:
                    yield _Conn(raw, True)
            else:
                import psycopg

                with psycopg.connect(self.url, **self._pg_kwargs()) as raw:
                    yield _Conn(raw, True)
            return
        raw = sqlite3.connect(self.path, timeout=15)
        raw.row_factory = _dict_row
        raw.execute("PRAGMA foreign_keys = ON")
        try:
            yield _Conn(raw, False)
            raw.commit()
        finally:
            raw.close()

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
            return db.one(TENANT_SELECT + " WHERE t.id = ?", (tenant_id,))

    def list_tenants(self) -> list[dict]:
        with self._conn() as db:
            return db.all(TENANT_SELECT + " ORDER BY t.connected_at")

    def set_tenant_name(self, tenant_id: str, name: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE tenants SET name = ? WHERE id = ?", (name, tenant_id))

    def delete_tenant(self, tenant_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
            db.execute("DELETE FROM problems WHERE tenant_id = ?", (tenant_id,))

    # ---------------------------------------------------------------- plans and prices (edited in the console)

    @staticmethod
    def _write_plan(db: _Conn, plan: dict, by: str | None) -> None:
        values = [json.dumps(plan.get(f) or []) if f == "features" else
                  int(bool(plan.get(f))) if f in ("public", "featured") else plan.get(f) for f in PLAN_FIELDS]
        db.execute(f"INSERT INTO plans (id, {', '.join(PLAN_FIELDS)}, updated_at, updated_by) "
                   f"VALUES (?, {', '.join('?' for _ in PLAN_FIELDS)}, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                   + ", ".join(f"{f} = excluded.{f}" for f in PLAN_FIELDS + ("updated_at", "updated_by")),
                   (plan["id"], *values, _iso(utcnow()), by))

    @staticmethod
    def _plan(row: dict) -> dict:
        plan = dict(row)
        plan["features"] = json.loads(plan["features"] or "[]")
        plan["public"], plan["featured"] = bool(plan["public"]), bool(plan["featured"])
        return plan

    def list_plans(self, public_only: bool = False) -> list[dict]:
        query = "SELECT * FROM plans" + (" WHERE public = 1 AND id != 'trial'" if public_only else "")
        with self._conn() as db:
            return [self._plan(r) for r in db.all(query + " ORDER BY sort, name")]

    def get_plan(self, plan_id: str) -> dict | None:
        with self._conn() as db:
            row = db.one("SELECT * FROM plans WHERE id = ?", (plan_id,))
        return self._plan(row) if row else None

    def save_plan(self, plan: dict, by: str) -> None:
        with self._conn() as db:
            self._write_plan(db, plan, by)

    def plan_in_use(self, plan_id: str) -> int:
        with self._conn() as db:
            return (db.one("SELECT COUNT(*) AS n FROM tenants WHERE plan = ?", (plan_id,))["n"]
                    + db.one("SELECT COUNT(*) AS n FROM accounts WHERE plan = ? AND tenant_id NOT IN "
                             "(SELECT id FROM tenants)", (plan_id,))["n"])

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
            row = db.one("SELECT value FROM options WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_option(self, key: str, value: str) -> None:
        with self._conn() as db:
            db.execute("INSERT INTO options (key, value) VALUES (?, ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def take_lease(self, key: str, owner: str, duration: timedelta) -> bool:
        """Claim (or renew) a named lease for `duration`, unless someone else holds an unexpired one.
        One statement, so two servers asking at the same moment can't both get it."""
        now = utcnow()
        with self._conn() as db:
            return db.execute(
                "INSERT INTO leases (name, owner, until) VALUES (?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
                "owner = excluded.owner, until = excluded.until WHERE leases.owner = excluded.owner OR leases.until <= ?",
                (key, owner, _iso(now + duration), _iso(now))).rowcount == 1

    def trial_days(self) -> int:
        value = self.get_option("trial_days")
        return int(value) if value.isdigit() else licensing.DEFAULT_TRIAL_DAYS

    # ---------------------------------------------------------------- sign-in sessions

    def create_session(self, user: dict, hours: float) -> str:
        """Record a sign-in; returns the random value for the visitor's cookie. Only its hash is stored."""
        token = secrets.token_urlsafe(32)
        now = utcnow()
        with self._conn() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(now),))  # tidy up as we go
            db.execute("INSERT INTO sessions (id, tenant_id, user_oid, email, created_at, seen_at, expires_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (_hash(token), user["tid"], user["oid"], user["email"], _iso(now), _iso(now),
                        _iso(now + timedelta(hours=hours))))
        return token

    def check_session(self, token: str | None) -> dict | None:
        """The live session for this cookie value, or None if it ended, expired or never existed."""
        if not token:
            return None
        now = utcnow()
        with self._conn() as db:
            row = db.one("SELECT * FROM sessions WHERE id = ?", (_hash(token),))
            if not row or row["expires_at"] <= _iso(now):
                return None
            if row["seen_at"] <= _iso(now - SESSION_TOUCH):
                db.execute("UPDATE sessions SET seen_at = ? WHERE id = ?", (_iso(now), row["id"]))
        return row

    def end_session(self, token: str | None) -> None:
        if token:
            with self._conn() as db:
                db.execute("DELETE FROM sessions WHERE id = ?", (_hash(token),))

    def end_all_sessions(self) -> int:
        with self._conn() as db:
            return db.execute("DELETE FROM sessions").rowcount

    def end_user_sessions(self, tenant_id: str, user_oid: str) -> int:
        """Sign a person out everywhere: every browser and device."""
        with self._conn() as db:
            return db.execute("DELETE FROM sessions WHERE tenant_id = ? AND user_oid = ?",
                              (tenant_id, user_oid)).rowcount

    # ---------------------------------------------------------------- customer accounts and licences

    def get_account(self, tenant_id: str) -> dict | None:
        with self._conn() as db:
            return db.one("SELECT * FROM accounts WHERE tenant_id = ?", (tenant_id,))

    @staticmethod
    def _ensure_account(db: _Conn, tenant_id: str, by: str | None) -> None:
        now = _iso(utcnow())
        db.execute("INSERT INTO accounts (tenant_id, created_at, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (tenant_id) DO NOTHING", (tenant_id, now, now, by))

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
            return db.all("SELECT * FROM licence_events WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                          (tenant_id, limit))

    def list_customers(self, tenant_id: str | None = None) -> list[dict]:
        """Every connected company and every customer record set up before connecting, newest first (or just the
        one `tenant_id`). Each row merges the tenant (if connected) with its account: plan, licence and contact."""
        one = " WHERE id = ?" if tenant_id else ""
        args = (tenant_id,) if tenant_id else ()
        with self._conn() as db:
            tenants = {r["id"]: r for r in db.all("SELECT * FROM tenants" + one, args)}
            accounts = {r["tenant_id"]: r for r in db.all("SELECT * FROM accounts" + one.replace("id", "tenant_id"),
                                                          args)}
            last = {r["tenant_id"]: r for r in db.all(
                "SELECT s.tenant_id, s.status, s.started_at, s.score, s.grade, s.meta FROM scans s JOIN "
                "(SELECT tenant_id, MAX(id) AS id FROM scans WHERE status != 'running'"
                + (" AND tenant_id = ?" if tenant_id else "") + " GROUP BY tenant_id) m ON m.id = s.id", args)}
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

    # ---------------------------------------------------------------- full backups (always a SQLite file)

    def backup_to(self, target) -> None:
        """A consistent copy of the whole database as a SQLite file, to a path or a binary stream. From PostgreSQL
        too, so one backup format restores anywhere (Azure's own backups remain the main safety net there)."""
        import tempfile

        if hasattr(target, "write"):
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            os.unlink(tmp)
            try:
                self.backup_to(tmp)
                with open(tmp, "rb") as fh:
                    target.write(fh.read())
                target.flush()
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return
        if not self.postgres:
            src = sqlite3.connect(self.path, timeout=15)
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            return
        copy = Store.sqlite_file(target)  # an empty SQLite database with the current tables
        with self._conn() as db, copy._conn() as out:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")  # one consistent moment
            _copy_tables(db, out)

    def restore_from(self, source) -> dict | None:
        """Replace the whole database with a SQLite backup (a path or a binary stream). Into PostgreSQL this is also
        how an existing SQLite database moves across. Everyone is signed out afterwards."""
        import tempfile

        if hasattr(source, "read"):
            fd, tmp = tempfile.mkstemp(suffix=".db")
            with os.fdopen(fd, "wb") as fh:
                fh.write(source.read())
            try:
                return self.restore_from(tmp)
            finally:
                os.unlink(tmp)
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        src.row_factory = _dict_row
        try:
            try:
                tables = {r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            except sqlite3.DatabaseError:
                tables = set()
            if not {"tenants", "scans"} <= tables:
                raise ValueError("That file isn't a Tenvaultix database backup.")
            if self.postgres:
                with self._conn() as db:
                    return _copy_tables(_Conn(src, False), db)
            dst = sqlite3.connect(self.path, timeout=15)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with self._conn() as db:  # a backup from an older version gets any newer tables and the default plans
            db.script(SCHEMA.format(**SQLITE_TYPES))
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM leases")
            if not db.one("SELECT COUNT(*) AS n FROM plans")["n"]:
                for plan in licensing.DEFAULT_PLANS:
                    self._write_plan(db, plan, None)
        return None

    # ---------------------------------------------------------------- console backups (customers, plans, licences)

    def export_admin(self) -> dict:
        with self._conn() as db:
            return {
                "kind": "tenvaultix-console-backup", "version": 1, "exported_at": _iso(utcnow()),
                "plans": [self._plan(r) for r in db.all("SELECT * FROM plans ORDER BY sort")],
                "options": {r["key"]: r["value"] for r in db.all("SELECT * FROM options")
                            if not r["key"].startswith(LOCAL_OPTIONS)},
                "accounts": db.all("SELECT * FROM accounts"),
                "licence_events": db.all("SELECT * FROM licence_events ORDER BY id"),
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
            for key in [r["key"] for r in db.all("SELECT key FROM options")]:
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
            row = db.one("SELECT role FROM members WHERE tenant_id = ? AND email = ?", (tenant_id, email.lower()))
        return row["role"] if row else None

    def list_members(self, tenant_id: str) -> list[dict]:
        with self._conn() as db:
            return db.all("SELECT email, role, added_at FROM members WHERE tenant_id = ? ORDER BY role DESC, email",
                          (tenant_id,))

    def add_member(self, tenant_id: str, email: str, role: str = "viewer") -> None:
        with self._conn() as db:
            db.execute("INSERT INTO members (tenant_id, email, role, added_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT (tenant_id, email) DO NOTHING", (tenant_id, email.lower(), role, _iso(utcnow())))

    def remove_member(self, tenant_id: str, email: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM members WHERE tenant_id = ? AND email = ? AND role != 'owner'",
                       (tenant_id, email.lower()))

    # ---------------------------------------------------------------- scans

    def start_scan(self, tenant_id: str) -> int:
        with self._conn() as db:
            return int(db.execute("INSERT INTO scans (tenant_id, status, started_at) VALUES (?, 'running', ?) "
                                  "RETURNING id", (tenant_id, _iso(utcnow()))).fetchall()[0]["id"])

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

    @staticmethod
    def _scan(row: dict | None) -> dict | None:
        if not row:
            return None
        scan = dict(row)
        scan["results"] = json.loads(scan["results"]) if scan["results"] else []
        scan["meta"] = json.loads(scan["meta"]) if scan["meta"] else {}
        return scan

    def get_scan(self, scan_id: int) -> dict | None:
        with self._conn() as db:
            return self._scan(db.one("SELECT * FROM scans WHERE id = ?", (scan_id,)))

    def latest_scan(self, tenant_id: str, status: str = "done") -> dict | None:
        with self._conn() as db:
            return self._scan(db.one("SELECT * FROM scans WHERE tenant_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                                     (tenant_id, status)))

    def previous_scan(self, tenant_id: str, before_id: int) -> dict | None:
        with self._conn() as db:
            return self._scan(db.one(
                "SELECT * FROM scans WHERE tenant_id = ? AND status = 'done' AND id < ? ORDER BY id DESC LIMIT 1",
                (tenant_id, before_id)))

    def list_scans(self, tenant_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as db:
            return db.all("SELECT id, status, started_at, finished_at, score, grade, problems, urgent, waste, error "
                          "FROM scans WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (tenant_id, limit))

    def scan_times(self, tenant_id: str | None = None) -> dict[str, dict]:
        """For each tenant: when the last finished, failed and running scans started. One light query instead of
        loading every company's latest report, so the scheduler stays quick with thousands of companies."""
        where, args = (" WHERE tenant_id = ?", (tenant_id,)) if tenant_id else ("", ())
        with self._conn() as db:
            rows = db.all(
                "SELECT tenant_id, MAX(CASE WHEN status = 'done' THEN started_at END) AS last_done, "
                "MAX(CASE WHEN status = 'failed' THEN started_at END) AS last_failed, "
                "MAX(CASE WHEN status = 'running' THEN started_at END) AS running_since "
                f"FROM scans{where} GROUP BY tenant_id", args)
        return {r["tenant_id"]: r for r in rows}

    def prune_scans(self, older_than: timedelta) -> int:
        """Delete reports older than `older_than`, always keeping each company's latest finished report."""
        cutoff = _iso(utcnow() - older_than)
        with self._conn() as db:
            return db.execute(
                "DELETE FROM scans WHERE started_at < ? AND status != 'running' AND id NOT IN "
                "(SELECT MAX(id) FROM scans WHERE status = 'done' GROUP BY tenant_id)", (cutoff,)).rowcount

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

    # ---------------------------------------------------------------- one-click fixes

    def add_fix(self, tenant_id: str, check_id: str, action: str, *, before=None, after=None, status: str,
                by_email: str, error: str | None = None, undo_of: int | None = None) -> int:
        """Record a change (action 'apply' or 'undo'; status 'done', 'unverified' or 'failed')."""
        with self._conn() as db:
            fix_id = int(db.execute(
                "INSERT INTO fixes (tenant_id, check_id, action, before, after, status, error, by_email, at, undo_of) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (tenant_id, check_id, action, json.dumps(before), json.dumps(after), status, error,
                 by_email.lower(), _iso(utcnow()), undo_of)).fetchall()[0]["id"])
            if undo_of and status != "failed":
                db.execute("UPDATE fixes SET undone_by = ? WHERE id = ? AND tenant_id = ?", (fix_id, undo_of, tenant_id))
            return fix_id

    @staticmethod
    def _fix(row: dict | None) -> dict | None:
        if not row:
            return None
        fix = dict(row)
        fix["before"] = json.loads(fix["before"]) if fix["before"] else None
        fix["after"] = json.loads(fix["after"]) if fix["after"] else None
        return fix

    def get_fix(self, fix_id: int) -> dict | None:
        with self._conn() as db:
            return self._fix(db.one("SELECT * FROM fixes WHERE id = ?", (fix_id,)))

    def list_fixes(self, tenant_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as db:
            return [self._fix(r) for r in db.all("SELECT * FROM fixes WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                                                 (tenant_id, limit))]

    # ---------------------------------------------------------------- problems (server errors people can see)

    def add_problem(self, area: str, code: str, detail: str | None = None, tenant_id: str | None = None) -> None:
        with self._conn() as db:
            db.execute("INSERT INTO problems (at, area, code, detail, tenant_id) VALUES (?, ?, ?, ?, ?)",
                       (_iso(utcnow()), area, code, (detail or "")[:500], tenant_id))
            db.execute("DELETE FROM problems WHERE id <= (SELECT MAX(id) FROM problems) - ?", (KEEP_PROBLEMS,))

    def list_problems(self, limit: int = 20, tenant_id: str | None = None, server_only: bool = False) -> list[dict]:
        with self._conn() as db:
            if server_only:
                return db.all("SELECT * FROM problems WHERE tenant_id IS NULL ORDER BY id DESC LIMIT ?", (limit,))
            if tenant_id:
                return db.all("SELECT * FROM problems WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (tenant_id, limit))
            return db.all("SELECT * FROM problems ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------------- activity log (who did what in the console)

    def add_audit(self, actor: str | None, action: str, detail: str | None = None, ip: str | None = None) -> None:
        with self._conn() as db:
            db.execute("INSERT INTO audit (at, actor, action, detail, ip) VALUES (?, ?, ?, ?, ?)",
                       (_iso(utcnow()), actor, action, (detail or "")[:500], (ip or "")[:64]))
            db.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id) FROM audit) - ?", (KEEP_AUDIT,))

    def list_audit(self, limit: int = 200, prefix: str | None = None) -> list[dict]:
        with self._conn() as db:
            if prefix:
                return db.all("SELECT * FROM audit WHERE action LIKE ? ORDER BY id DESC LIMIT ?", (prefix + "%", limit))
            return db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------------- Azure costs (cached)

    def save_costs(self, tenant_id: str, data: dict) -> None:
        with self._conn() as db:
            db.execute("INSERT INTO azure_costs (tenant_id, fetched_at, data) VALUES (?, ?, ?) ON CONFLICT (tenant_id) "
                       "DO UPDATE SET fetched_at = excluded.fetched_at, data = excluded.data",
                       (tenant_id, _iso(utcnow()), json.dumps(data)))

    def get_costs(self, tenant_id: str) -> dict | None:
        with self._conn() as db:
            row = db.one("SELECT fetched_at, data FROM azure_costs WHERE tenant_id = ?", (tenant_id,))
        if not row:
            return None
        return {"fetched_at": row["fetched_at"], **json.loads(row["data"])}

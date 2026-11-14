"""Run the whole suite on PostgreSQL as well as SQLite.

    TVX_TEST_DATABASE_URL=postgresql://localhost/tvx_test python -m pytest -q

Each SQLite path a test uses becomes its own PostgreSQL schema (the same path gives the same schema, so a test and
the command line it runs see the same data). The schemas are dropped at the end.
"""
import hashlib
import os

import pytest

PG_URL = os.environ.get("TVX_TEST_DATABASE_URL", "")

if PG_URL:
    os.environ["TVX_DB_POOL"] = "0"  # a connection per call: hundreds of test databases, no idle connections
    from app import db as _db

    _schemas: set[str] = set()
    _real_init = _db.Store.__init__

    def _init(self, target, *, schema=None):
        if not _db.is_database_url(target):
            schema = "t_" + hashlib.sha1(str(target).encode()).hexdigest()[:16]
            _schemas.add(schema)
            target = PG_URL
        _real_init(self, target, schema=schema)

    _db.Store.__init__ = _init

    @pytest.fixture(scope="session", autouse=True)
    def _drop_schemas():
        yield
        import psycopg

        with psycopg.connect(PG_URL, autocommit=True) as conn:
            for schema in _schemas:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

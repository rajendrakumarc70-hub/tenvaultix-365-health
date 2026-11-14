"""Automatic scans inside the web app, so one server needs no separate cron job.

Turned on with TVX_SCHEDULER=on. Every 15 minutes it scans each connected company whose plan interval has passed
(weekly on the trial and Essentials, daily on Business and Partner, or whatever the console's Pricing page says),
skipping companies whose licence has ended or is suspended, and waiting 6 hours before retrying one that failed.
Several companies are scanned at the same time (TVX_SCAN_WORKERS, default 4); Microsoft limits each company
separately, so they don't slow each other down. A lease in the database makes sure only one process does this,
even when the app runs several workers or servers. Once a day it also deletes reports older than
TVX_KEEP_REPORTS_DAYS (each company's latest report is always kept).

`python -m app.cli scan-due` does the same job from cron, for hosts that prefer that.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable

from .db import Store, utcnow
from .scanner import run_scan, scan_due

log = logging.getLogger(__name__)

LEASE_KEY = "scheduler_lease"
LAST_RUN_KEY = "scheduler_last_run"
PRUNED_KEY = "scheduler_pruned_at"
CHECK_EVERY = 15 * 60          # seconds between checks
LEASE_FOR = timedelta(minutes=30)
INSTANCE = uuid.uuid4().hex[:12]


def tick(store: Store, collect: Callable[[str], dict], owner: str = INSTANCE,
         paused: Callable[[], bool] = lambda: False, workers: int = 1, keep_reports: int = 0) -> int | None:
    """Scan every company that's due, `workers` at a time. Returns how many scans ran, or None if the emergency stop
    is on or another process holds the lease. `keep_reports` (days, 0 = forever) prunes old reports once a day."""
    if paused() or not store.take_lease(LEASE_KEY, owner, LEASE_FOR):
        return None
    times = store.scan_times()
    plans = {p["id"]: p for p in store.list_plans()}
    due = []
    for tenant in store.list_tenants():
        try:
            if scan_due(store, tenant, times.get(tenant["id"], {}), plans):
                due.append(tenant["id"])
        except Exception:  # one company must never stop the others
            log.exception("Couldn't check whether %s is due", tenant["id"])

    def scan_one(tenant_id: str) -> int:
        if paused():  # the emergency stop was turned on part-way through
            return 0
        try:
            run_scan(store, tenant_id, collect)  # records its own failures
        except Exception:
            log.exception("Scheduled scan for %s crashed", tenant_id)
        return 1

    ran = 0
    if due:
        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="tvx-scan") as pool:
            for done in as_completed([pool.submit(scan_one, tid) for tid in due]):
                ran += done.result()
                store.take_lease(LEASE_KEY, owner, LEASE_FOR)  # long runs keep the lease
    store.set_option(LAST_RUN_KEY, f"{utcnow().isoformat(timespec='seconds')}|{ran}")
    if ran:
        log.info("Scheduler ran %s scan%s", ran, "" if ran == 1 else "s")
    if keep_reports:
        prune(store, keep_reports)
    return ran


def prune(store: Store, keep_days: int) -> int | None:
    """At most once a day: delete reports older than `keep_days`, keeping each company's latest."""
    last = store.get_option(PRUNED_KEY)
    if last and utcnow() - datetime.fromisoformat(last) < timedelta(days=1):
        return None
    deleted = store.prune_scans(timedelta(days=keep_days))
    store.set_option(PRUNED_KEY, utcnow().isoformat(timespec="seconds"))
    if deleted:
        log.info("Deleted %s report%s older than %s days", deleted, "" if deleted == 1 else "s", keep_days)
    return deleted


def last_run(store: Store) -> dict | None:
    value = store.get_option(LAST_RUN_KEY)
    if not value:
        return None
    at, _, ran = value.partition("|")
    return {"at": at, "scans": int(ran) if ran.isdigit() else 0}


def start(store: Store, collect: Callable[[str], dict], every: int = CHECK_EVERY,
          first_wait: int = 30, paused: Callable[[], bool] = lambda: False, workers: int = 4,
          keep_reports: int = 0) -> threading.Thread:
    def loop() -> None:
        time.sleep(first_wait)  # let the app finish starting
        while True:
            try:
                tick(store, collect, paused=paused, workers=workers, keep_reports=keep_reports)
            except Exception:
                log.exception("Scheduler check failed")
            time.sleep(every)

    thread = threading.Thread(target=loop, name="tvx-scheduler", daemon=True)
    thread.start()
    log.info("Automatic scans are on (checking every %s minutes)", every // 60)
    return thread

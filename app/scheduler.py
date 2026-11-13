"""Automatic scans inside the web app, so one server needs no separate cron job.

Turned on with TVX_SCHEDULER=on. Every 15 minutes it scans each connected company whose plan interval has passed
(weekly on the trial and Essentials, daily on Business and Partner, or whatever the console's Pricing page says),
skipping companies whose licence has ended or is suspended. A lease in the database makes sure only one process
does this, even when the app runs several workers or servers.

`python -m app.cli scan-due` does the same job from cron, for hosts that prefer that.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import timedelta
from typing import Callable

from .db import Store, utcnow
from .scanner import run_scan, scan_due

log = logging.getLogger(__name__)

LEASE_KEY = "scheduler_lease"
LAST_RUN_KEY = "scheduler_last_run"
CHECK_EVERY = 15 * 60          # seconds between checks
LEASE_FOR = timedelta(minutes=30)
INSTANCE = uuid.uuid4().hex[:12]


def tick(store: Store, collect: Callable[[str], dict], owner: str = INSTANCE) -> int | None:
    """Scan every company that's due. Returns how many scans ran, or None if another process holds the lease."""
    if not store.take_lease(LEASE_KEY, owner, LEASE_FOR):
        return None
    ran = 0
    for tenant in store.list_tenants():
        try:
            if scan_due(store, tenant):
                run_scan(store, tenant["id"], collect)  # records its own failures
                ran += 1
        except Exception:  # one company must never stop the others
            log.exception("Scheduled scan for %s crashed", tenant["id"])
        store.take_lease(LEASE_KEY, owner, LEASE_FOR)  # long runs keep the lease
    store.set_option(LAST_RUN_KEY, f"{utcnow().isoformat(timespec='seconds')}|{ran}")
    if ran:
        log.info("Scheduler ran %s scan%s", ran, "" if ran == 1 else "s")
    return ran


def last_run(store: Store) -> dict | None:
    value = store.get_option(LAST_RUN_KEY)
    if not value:
        return None
    at, _, ran = value.partition("|")
    return {"at": at, "scans": int(ran) if ran.isdigit() else 0}


def start(store: Store, collect: Callable[[str], dict], every: int = CHECK_EVERY,
          first_wait: int = 30) -> threading.Thread:
    def loop() -> None:
        time.sleep(first_wait)  # let the app finish starting
        while True:
            try:
                tick(store, collect)
            except Exception:
                log.exception("Scheduler check failed")
            time.sleep(every)

    thread = threading.Thread(target=loop, name="tvx-scheduler", daemon=True)
    thread.start()
    log.info("Automatic scans are on (checking every %s minutes)", every // 60)
    return thread

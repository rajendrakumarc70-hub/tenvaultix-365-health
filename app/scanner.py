"""Runs one scan end to end: collect snapshot -> run checks -> score -> store findings."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from . import blueprint, licensing
from .problems import code_for, graph_code
from .checks import run_checks
from .collector import NO_PERMISSION
from .db import Store, utcnow
from .graph import AuthError, GraphError
from .scoring import summarize

log = logging.getLogger(__name__)

CollectFn = Callable[[str], dict]  # tenant id -> snapshot


def run_scan(store: Store, tenant_id: str, collect: CollectFn, scan_id: int | None = None) -> int:
    scan_id = scan_id or store.start_scan(tenant_id)
    try:
        snapshot = collect(tenant_id)
        results = run_checks(snapshot)
        summary = summarize(results)
        meta = {
            "collected_at": snapshot["collected_at"],
            "features": snapshot.get("features", {}),
            "activity_source": snapshot.get("activity_source"),
            "mfa_source": snapshot.get("mfa_source"),
            "mailboxes_checked": snapshot.get("mailboxes_checked", 0),
            "users": len(snapshot.get("users") or []),
            "domains": [d["name"] for d in snapshot["tenant"].get("domains", [])],
            "facts": blueprint.facts(snapshot),  # counts for the office drawing
            "no_permission": sorted(s for s, why in snapshot.get("unavailable", {}).items() if why == NO_PERMISSION),
        }
        store.finish_scan(scan_id, results, summary, meta)
        if snapshot["tenant"].get("name"):
            store.set_tenant_name(tenant_id, snapshot["tenant"]["name"])
        log.info("Scan %s for %s finished: score %s", scan_id, tenant_id, summary["score"])
    except AuthError as exc:
        log.warning("Scan %s for %s: token refused: %s", scan_id, tenant_id, exc)
        store.add_problem("scan", code_for(str(exc), "graph-401"), str(exc), tenant_id)
        store.fail_scan(scan_id, "Microsoft refused access to your tenant. The app may have been removed, or the "
                                 "approval withdrawn. An admin can reconnect from the Connect page.")
    except GraphError as exc:
        log.warning("Scan %s for %s: Graph error: %s", scan_id, tenant_id, exc)
        store.add_problem("scan", graph_code(exc.status), str(exc), tenant_id)
        store.fail_scan(scan_id, f"Microsoft Graph returned an error ({exc.status}). Please try again later.")
    except Exception as exc:
        log.exception("Scan %s for %s crashed", scan_id, tenant_id)
        store.add_problem("scan", "crash", f"{type(exc).__name__}: {str(exc)[:200]}", tenant_id)
        store.fail_scan(scan_id, "The scan hit an unexpected error. We've logged it; please try again later.")
    return scan_id


def trial_days_left(tenant: dict) -> int | None:
    st = licensing.state(tenant)
    return st["days_left"] if st["key"] in ("trial", "trial_ended") else None


def can_scan(tenant: dict) -> bool:
    """Scans run during a trial and while a licence is valid. Ended or suspended: reports stay, new scans pause."""
    return licensing.state(tenant)["can_scan"]


def scan_interval(store: Store, tenant: dict, plans: dict | None = None) -> timedelta:
    """How often the company's plan scans. `plans` (id -> plan) saves a lookup per company when checking many."""
    if plans is not None:
        plan = plans.get(tenant["plan"]) or plans.get("trial") or {}
    else:
        plan = store.get_plan(tenant["plan"]) or store.get_plan("trial") or {}
    return timedelta(days=plan.get("scan_days") or 7)


RETRY_FAILED_AFTER = timedelta(hours=6)  # a company whose scan failed isn't retried every 15 minutes


def scan_due(store: Store, tenant: dict, times: dict | None = None, plans: dict | None = None) -> bool:
    """Whether the company's next automatic scan is due. `times` is its row from store.scan_times(); the scheduler
    passes it so checking thousands of companies takes one query, not one report load each."""
    if not can_scan(tenant):
        return False
    if times is None:
        times = store.scan_times(tenant["id"]).get(tenant["id"], {})
    now = utcnow()
    if times.get("running_since") and store.running_scan(tenant["id"]):  # also clears a scan stuck after a restart
        return False
    last_done, last_failed = times.get("last_done"), times.get("last_failed")
    if last_failed and (not last_done or last_failed > last_done) \
            and now - datetime.fromisoformat(last_failed) < RETRY_FAILED_AFTER:
        return False
    if not last_done:
        return True
    return now - datetime.fromisoformat(last_done) >= scan_interval(store, tenant, plans) - timedelta(hours=1)

"""Runs one scan end to end: collect snapshot -> run checks -> score -> store findings."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from . import blueprint
from .checks import run_checks
from .config import PLANS
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
        }
        store.finish_scan(scan_id, results, summary, meta)
        if snapshot["tenant"].get("name"):
            store.set_tenant_name(tenant_id, snapshot["tenant"]["name"])
        log.info("Scan %s for %s finished: score %s", scan_id, tenant_id, summary["score"])
    except AuthError as exc:
        log.warning("Scan %s for %s: token refused: %s", scan_id, tenant_id, exc)
        store.fail_scan(scan_id, "Microsoft refused access to your tenant. The app may have been removed, or the "
                                 "approval withdrawn. An admin can reconnect from the Connect page.")
    except GraphError as exc:
        log.warning("Scan %s for %s: Graph error: %s", scan_id, tenant_id, exc)
        store.fail_scan(scan_id, f"Microsoft Graph returned an error ({exc.status}). Please try again later.")
    except Exception:
        log.exception("Scan %s for %s crashed", scan_id, tenant_id)
        store.fail_scan(scan_id, "The scan hit an unexpected error. We've logged it; please try again later.")
    return scan_id


def trial_days_left(tenant: dict) -> int | None:
    if tenant["plan"] != "trial" or not tenant.get("trial_ends"):
        return None
    left = datetime.fromisoformat(tenant["trial_ends"]) - utcnow()
    return max(0, left.days + (1 if left.seconds else 0))


def can_scan(tenant: dict) -> bool:
    """Trials that have ended keep their reports but stop scanning until a plan is chosen."""
    return tenant["plan"] != "trial" or (trial_days_left(tenant) or 0) > 0


def scan_due(store: Store, tenant: dict) -> bool:
    if not can_scan(tenant) or store.running_scan(tenant["id"]):
        return False
    last = store.latest_scan(tenant["id"])
    if not last:
        return True
    interval = timedelta(days=PLANS.get(tenant["plan"], PLANS["trial"])[1])
    return utcnow() - datetime.fromisoformat(last["started_at"]) >= interval - timedelta(hours=1)

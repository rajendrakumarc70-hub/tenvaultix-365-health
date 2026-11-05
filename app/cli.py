"""Command line for the Tenvaultix team.

  python -m app.cli scan-due                 # run every hour (cron, Azure WebJob, GitHub Actions)
  python -m app.cli scan <tenant-id>         # scan one tenant now
  python -m app.cli list                     # connected tenants, plans and last scores
  python -m app.cli set-plan <tenant-id> <plan>   # trial | essentials | business | partner
"""
from __future__ import annotations

import argparse
import logging
import sys

from .collector import collect
from .config import PLANS, load_settings
from .db import Store
from .dnscheck import SystemResolver
from .graph import GraphClient, app_token_provider
from .scanner import run_scan, scan_due, trial_days_left


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan-due", help="scan every tenant whose next scan is due")
    one = sub.add_parser("scan", help="scan one tenant now")
    one.add_argument("tenant_id")
    sub.add_parser("list", help="list connected tenants")
    plan = sub.add_parser("set-plan", help="change a tenant's plan")
    plan.add_argument("tenant_id")
    plan.add_argument("plan", choices=sorted(PLANS))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    store = Store(settings.database_path)

    def collect_tenant(tenant_id: str) -> dict:
        return collect(GraphClient(app_token_provider(settings, tenant_id)), SystemResolver(), tenant_id)

    if args.command == "list":
        for t in store.list_tenants():
            last = store.latest_scan(t["id"])
            left = trial_days_left(t)
            print(f"{t['id']}  {t['name'] or '-':30.30}  {t['plan']:10}"
                  f"{f' ({left}d left)' if left is not None else '':12}"
                  f"  last score: {last['score'] if last else '-'}  owner: {t['connected_by']}")
        return 0

    if args.command == "set-plan":
        if not store.set_plan(args.tenant_id, args.plan):
            print(f"No tenant {args.tenant_id}", file=sys.stderr)
            return 1
        print(f"{args.tenant_id} is now on {args.plan}")
        return 0

    if not settings.microsoft_configured:
        print("Microsoft app credentials aren't configured (see README).", file=sys.stderr)
        return 1

    if args.command == "scan":
        if not store.get_tenant(args.tenant_id):
            print(f"No tenant {args.tenant_id}", file=sys.stderr)
            return 1
        scan = store.get_scan(run_scan(store, args.tenant_id, collect_tenant))
        print(f"Scan {scan['id']}: {scan['status']} score={scan['score']} {scan['error'] or ''}")
        return 0 if scan["status"] == "done" else 1

    failures = 0
    for tenant in store.list_tenants():
        if scan_due(store, tenant):
            scan = store.get_scan(run_scan(store, tenant["id"], collect_tenant))
            failures += scan["status"] != "done"
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

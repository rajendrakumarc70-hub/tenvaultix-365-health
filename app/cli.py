"""Command line for the Tenvaultix team.

  python -m app.cli scan-due                 # run every hour (cron, Azure WebJob, GitHub Actions)
  python -m app.cli scan <tenant-id>         # scan one tenant now
  python -m app.cli list                     # connected tenants, plans and last scores
  python -m app.cli set-plan <tenant-id> <plan>   # a plan id from the console's Pricing page
  python -m app.cli backup <file>            # a safe copy of the whole database ("-" writes it to stdout)
  python -m app.cli restore <file> --yes     # replace the whole database with a backup ("-" reads stdin)
"""
from __future__ import annotations

import argparse
import logging
import sys

from .collector import collect
from .config import load_settings
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
    plan.add_argument("plan")
    backup = sub.add_parser("backup", help="copy the whole database safely while the app runs")
    backup.add_argument("target", help="file to write, or - for standard output")
    restore = sub.add_parser("restore", help="replace the whole database with a backup")
    restore.add_argument("source", help="backup file, or - for standard input")
    restore.add_argument("--yes", action="store_true", help="confirm: everything now in the database is replaced")
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

    if args.command == "backup":
        store.backup_to(sys.stdout.buffer if args.target == "-" else args.target)
        if args.target != "-":
            print(f"Database copied to {args.target}")
        return 0

    if args.command == "restore":
        if not args.yes:
            print("This replaces everything in the database. Add --yes to go ahead.", file=sys.stderr)
            return 1
        try:
            store.restore_from(sys.stdin.buffer if args.source == "-" else args.source)
        except ValueError as exc:
            print(f"Nothing was restored: {exc}", file=sys.stderr)
            return 1
        print("Database restored.", file=sys.stderr)
        return 0

    if args.command == "set-plan":
        if not store.get_plan(args.plan):
            print(f"No plan {args.plan}. Plans: {', '.join(p['id'] for p in store.list_plans())}", file=sys.stderr)
            return 1
        if not store.get_tenant(args.tenant_id):
            print(f"No tenant {args.tenant_id}", file=sys.stderr)
            return 1
        store.save_licence(args.tenant_id, {"plan": args.plan}, "command line", f"Plan set to {args.plan}")
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

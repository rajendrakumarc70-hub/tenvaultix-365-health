"""The Tenvaultix console (/admin): customers, licences, prices, errors and backups, for the Tenvaultix team only.

Only people listed in TVX_OPS_EMAILS who sign in from TVX_OPS_TENANT_ID can open it; everyone else gets "not found".
Every licence change is written to the customer's licence history with who made it, and every console sign-in,
change, export and restore goes in the activity log on the Security page.
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Callable

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from . import licensing, problems, safety
from .db import Store, utcnow
from .dnscheck import normalize_domain
from .licensing import FormError

TENANT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EMAIL = re.compile(r"^[^@\s]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)
GSTIN = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")
UNITS = ["month", "tenant/month", "user/month", "year"]
MAX_BACKUP = 2 * 1024 * 1024
FILTERS = {
    "all": "All", "paying": "Paying", "trial": "On trial", "attention": "Needs attention",
    "ended": "Ended or suspended", "waiting": "Not connected yet",
}


ACTIVITY = {  # filter on the Security page: label, action prefix
    "all": ("Everything", None), "sign-in": ("Sign-ins", "console."), "customers": ("Customers and licences", "customer."),
    "licences": ("Licence changes", "licence."), "pricing": ("Pricing", "pricing."),
    "data": ("Exports and restores", "data."), "security": ("Security", "security."),
}


def add_console(app: FastAPI, *, store: Store, settings, render: Callable, message: Callable, flash: Callable,
                csrf_ok: Callable, is_ops: Callable, record: Callable, credential_check: Callable,
                tenant_lookup: Callable) -> None:

    # ------------------------------------------------------------ helpers

    def audit(request: Request, actor: str | None, action: str, detail: str = "") -> None:
        store.add_audit(actor, action, detail, request.client.host if request.client else None)

    def team(request: Request, path: str):
        """(user, None) for the Tenvaultix team; otherwise (None, the response to send)."""
        user = request.session.get("user")
        if not user:
            return None, RedirectResponse(f"/login?next={path}", status_code=303)
        if not is_ops(user):
            if not request.session.get("console_denied"):  # once per session, so it can't flood the log
                request.session["console_denied"] = True
                audit(request, user["email"], "console.denied", f"Signed in from tenant {user['tid']}, opened {path}")
            return None, message(request, "Page not found", "This page doesn't exist.", status_code=404)
        if not request.session.get("console_seen"):
            request.session["console_seen"] = True
            audit(request, user["email"], "console.sign-in", f"Opened {path}")
        return user, None

    def team_post(request: Request, csrf: str):
        user = request.session.get("user")
        return user if is_ops(user) and csrf_ok(request, csrf) else None

    def page(request: Request, name: str, nav: str, **context):
        return render(request, f"console/{name}", nav=nav, **context)

    def plans_by_id() -> dict[str, dict]:
        return {p["id"]: p for p in store.list_plans()}

    def enrich(row: dict, plans: dict[str, dict]) -> dict:
        plan = plans.get(row["plan"])
        st = licensing.state(row)
        seats = licensing.seat_limit(row, plan)
        users = row.get("users")
        scan = row.get("last_scan") or {}
        over = bool(users and seats and users > seats)
        reasons = []
        if st["key"] == "expiring":
            reasons.append(f"Renew before {st['ends']:%d %b %Y}")
        elif st["key"] == "expired":
            reasons.append("Licence ended; scans paused")
        elif st["key"] == "trial_ended":
            reasons.append("Trial ended; offer a plan")
        elif st["key"] == "suspended":
            reasons.append("Suspended; scans paused")
        elif st["key"] == "trial" and st["days_left"] is not None and st["days_left"] <= 3:
            reasons.append(f"Trial ends in {st['days_left']} day{'s' if st['days_left'] != 1 else ''}")
        if over:
            reasons.append(f"{users:,} staff but {seats:,} seats")
        if scan.get("status") == "failed":
            reasons.append("Last scan failed")
        return {**row, "plan_name": plan["name"] if plan else row["plan"], "state": st, "seats_limit": seats,
                "over_seats": over, "mrr": licensing.monthly_value(row, plan), "reasons": reasons}

    def customers() -> list[dict]:
        plans = plans_by_id()
        return [enrich(r, plans) for r in store.list_customers()]

    def needs_attention(c: dict) -> bool:
        return bool(c["reasons"])

    def matches(c: dict, show: str, q: str) -> bool:
        key = c["state"]["key"]
        if show == "paying" and not (c["mrr"] > 0 or (c["plan"] != "trial" and key in ("active", "expiring"))):
            return False
        if show == "trial" and key != "trial":
            return False
        if show == "attention" and not needs_attention(c):
            return False
        if show == "ended" and key not in ("expired", "trial_ended", "suspended"):
            return False
        if show == "waiting" and c["connected"]:
            return False
        if q:
            hay = " ".join(str(c.get(k) or "") for k in ("name", "company", "domain", "id", "contact_email",
                                                          "contact_name", "connected_by", "gstin")).lower()
            return q.lower() in hay
        return True

    def customer(tid: str) -> dict | None:
        return next((c for c in customers() if c["id"] == tid), None)

    def describe(field: str, value, plans: dict) -> str:
        if value in (None, ""):
            return {"seats": "plan's limit", "price_inr": "list price", "paid_until": "no end date"}.get(field, "—")
        if field == "plan":
            return plans.get(value, {}).get("name", value)
        if field == "price_inr":
            return f"₹{value:,.0f}/month"
        if field in ("status", "cycle"):
            return str(value).capitalize()
        return str(value)

    LABELS = {"plan": "Plan", "status": "Status", "paid_until": "Valid until", "seats": "Seats",
              "cycle": "Billing", "price_inr": "Price"}

    def change_summary(before: dict, after: dict) -> str:
        plans = plans_by_id()
        parts = [f"{LABELS[f]}: {describe(f, before.get(f), plans)} → {describe(f, after[f], plans)}"
                 for f in after if before.get(f) != after[f]]
        return "; ".join(parts)

    def licence_before(c: dict) -> dict:
        return {"plan": c["plan"], "status": c.get("licence_status") or "active", "paid_until": c.get("paid_until"),
                "seats": c.get("seats"), "cycle": c.get("cycle") or "monthly", "price_inr": c.get("price_inr")}

    def read_licence(plan: str, status: str, paid_until: str, seats: str, cycle: str, price: str) -> dict:
        if not store.get_plan(plan):
            raise FormError("Choose a plan.")
        if status not in ("active", "suspended"):
            raise FormError("Choose Active or Suspended.")
        if cycle not in licensing.CYCLES:
            raise FormError("Choose monthly or yearly billing.")
        return {"plan": plan, "status": status, "paid_until": licensing.day(paid_until, "Valid until"),
                "seats": licensing.whole(seats, "Seats", 1, 100_000), "cycle": cycle,
                "price_inr": licensing.money(price, "Price per month")}

    def read_details(company, domain, contact_name, contact_email, contact_phone, gstin, address, notes) -> dict:
        email = (contact_email or "").strip().lower()
        if email and not EMAIL.match(email):
            raise FormError("Contact email doesn't look right.")
        gst = (gstin or "").strip().upper()
        if gst and not GSTIN.match(gst):
            raise FormError("GSTIN should be 15 characters, like 29ABCDE1234F1Z5.")
        dom = (domain or "").strip()
        if dom:
            dom = normalize_domain(dom)
            if not dom:
                raise FormError("Domain doesn't look right. Try something like company.com.")
        return {"company": licensing.short(company, 120), "domain": dom or None,
                "contact_name": licensing.short(contact_name, 120), "contact_email": email or None,
                "contact_phone": licensing.short(contact_phone, 40), "gstin": gst or None,
                "address": (address or "").strip()[:500] or None, "notes": (notes or "").strip()[:2000] or None}

    # ------------------------------------------------------------ overview

    @app.get("/admin")
    def overview(request: Request):
        user, stop = team(request, "/admin")
        if stop:
            return stop
        rows = customers()
        keys = [c["state"]["key"] for c in rows]
        stats = {
            "customers": len(rows), "connected": sum(c["connected"] for c in rows),
            "paying": sum(1 for c in rows if c["mrr"] > 0), "trials": keys.count("trial"),
            "attention": sum(1 for c in rows if needs_attention(c)), "mrr": sum(c["mrr"] for c in rows),
        }
        by_plan = {}
        for c in rows:
            by_plan[c["plan_name"]] = by_plan.get(c["plan_name"], 0) + 1
        attention = [c for c in rows if needs_attention(c)][:12]
        return page(request, "overview.html", "overview", stats=stats, attention=attention,
                    by_plan=sorted(by_plan.items(), key=lambda kv: -kv[1]),
                    recent=rows[:6], errors=len(store.list_problems(limit=500)))

    # ------------------------------------------------------------ customers

    @app.get("/admin/customers")
    def customer_list(request: Request, show: str = "all", q: str = ""):
        user, stop = team(request, "/admin/customers")
        if stop:
            return stop
        show = show if show in FILTERS else "all"
        q = q.strip()[:80]
        rows = [c for c in customers() if matches(c, show, q)]
        return page(request, "customers.html", "customers", rows=rows, show=show, q=q, filters=FILTERS)

    @app.get("/admin/customers/new")
    def customer_new(request: Request):
        user, stop = team(request, "/admin/customers/new")
        if stop:
            return stop
        return page(request, "customer_new.html", "customers", plans=store.list_plans(), form={})

    @app.post("/admin/customers/new")
    def customer_create(request: Request, csrf: str = Form(""), who: str = Form(""), company: str = Form(""),
                        contact_name: str = Form(""), contact_email: str = Form(""), contact_phone: str = Form(""),
                        plan: str = Form("trial"), paid_until: str = Form(""), seats: str = Form(""),
                        cycle: str = Form("monthly"), price: str = Form("")):
        user = team_post(request, csrf)
        if not user:
            return RedirectResponse("/admin", status_code=303)
        form = dict(who=who, company=company, contact_name=contact_name, contact_email=contact_email,
                    contact_phone=contact_phone, plan=plan, paid_until=paid_until, seats=seats, cycle=cycle,
                    price=price)
        try:
            if not licensing.short(company):
                raise FormError("Enter the company name.")
            who = who.strip()
            if TENANT_ID.match(who):
                tid, domain = who.lower(), None
            else:
                domain = normalize_domain(who)
                if not domain:
                    raise FormError("Enter the company's email domain (like company.com) or its Microsoft tenant ID.")
                tid = tenant_lookup(domain)
                if not tid:
                    raise FormError(f"We couldn't find a Microsoft 365 tenant for {domain}. Check the spelling, "
                                    "or paste the tenant ID instead.")
                tid = tid.lower()
            details = read_details(company, domain, contact_name, contact_email, contact_phone, "", "", "")
            licence = read_licence(plan, "active", paid_until, seats, cycle, price)
        except FormError as exc:
            return page(request, "customer_new.html", "customers", plans=store.list_plans(), form=form,
                        error=str(exc))
        if store.get_account(tid) or store.get_tenant(tid):
            flash(request, "That company is already a customer. Here's its record.", "warn")
            return RedirectResponse(f"/admin/customers/{tid}", status_code=303)
        store.save_details(tid, details, user["email"])
        store.save_licence(tid, licence, user["email"],
                           "Customer added. " + change_summary({}, licence))
        audit(request, user["email"], "customer.add", f"{details['company']} ({tid})")
        flash(request, f"Added {details['company']}. The licence applies when they connect.", "good")
        return RedirectResponse(f"/admin/customers/{tid}", status_code=303)

    @app.get("/admin/customers/{tid}")
    def customer_detail(request: Request, tid: str):
        user, stop = team(request, f"/admin/customers/{tid}")
        if stop:
            return stop
        c = customer(tid) if TENANT_ID.match(tid) else None
        if not c:
            return message(request, "Customer not found", "There's no customer with that ID.", status_code=404,
                           actions=[("All customers", "/admin/customers")])
        tenant = store.get_tenant(tid)
        return page(request, "customer.html", "customers", c=c, plans=store.list_plans(),
                    events=store.licence_events(tid), members=store.list_members(tid) if tenant else [],
                    scans=store.list_scans(tid, limit=5) if tenant else [],
                    problems=problems.grouped(store.list_problems(limit=50, tenant_id=tid))[:5],
                    trial_days=store.trial_days())

    @app.post("/admin/customers/{tid}/licence")
    def customer_licence(request: Request, tid: str, csrf: str = Form(""), plan: str = Form(""),
                         status: str = Form("active"), paid_until: str = Form(""), seats: str = Form(""),
                         cycle: str = Form("monthly"), price: str = Form("")):
        user = team_post(request, csrf)
        c = customer(tid) if user and TENANT_ID.match(tid) else None
        if not c:
            return RedirectResponse("/admin/customers", status_code=303)
        try:
            licence = read_licence(plan, status, paid_until, seats, cycle, price)
        except FormError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(f"/admin/customers/{tid}#licence", status_code=303)
        summary = change_summary(licence_before(c), licence)
        if not summary:
            flash(request, "Nothing changed.")
        else:
            store.save_licence(tid, licence, user["email"], summary)
            audit(request, user["email"], "licence.change", f"{c['name'] or tid}: {summary}")
            flash(request, f"Licence saved. {summary}.", "good")
        return RedirectResponse(f"/admin/customers/{tid}#licence", status_code=303)

    @app.post("/admin/customers/{tid}/quick")
    def customer_quick(request: Request, tid: str, csrf: str = Form(""), action: str = Form("")):
        user = team_post(request, csrf)
        c = customer(tid) if user and TENANT_ID.match(tid) else None
        if not c or action not in ("month", "year", "suspend", "resume"):
            return RedirectResponse("/admin/customers", status_code=303)
        before = licence_before(c)
        if action in ("month", "year"):
            start = max(licensing.today(), c["state"]["ends"] or licensing.today())
            ends = licensing.add_months(start, 1 if action == "month" else 12)
            change = {"paid_until": ends.isoformat()}
            if before["status"] == "suspended":
                change["status"] = "active"
        else:
            change = {"status": "suspended" if action == "suspend" else "active"}
        summary = change_summary(before, change)
        if summary:
            store.save_licence(tid, change, user["email"], summary)
            audit(request, user["email"], "licence.change", f"{c['name'] or tid}: {summary}")
            flash(request, f"Licence saved. {summary}.", "good")
        return RedirectResponse(f"/admin/customers/{tid}#licence", status_code=303)

    @app.post("/admin/customers/{tid}/details")
    def customer_details(request: Request, tid: str, csrf: str = Form(""), company: str = Form(""),
                         domain: str = Form(""), contact_name: str = Form(""), contact_email: str = Form(""),
                         contact_phone: str = Form(""), gstin: str = Form(""), address: str = Form(""),
                         notes: str = Form("")):
        user = team_post(request, csrf)
        if not user or not TENANT_ID.match(tid) or not customer(tid):
            return RedirectResponse("/admin/customers", status_code=303)
        try:
            details = read_details(company, domain, contact_name, contact_email, contact_phone, gstin, address,
                                   notes)
        except FormError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(f"/admin/customers/{tid}#details", status_code=303)
        store.save_details(tid, details, user["email"])
        audit(request, user["email"], "customer.details", f"{details['company'] or tid} ({tid})")
        flash(request, "Customer details saved.", "good")
        return RedirectResponse(f"/admin/customers/{tid}#details", status_code=303)

    @app.get("/admin/customers.csv")
    def customers_csv(request: Request):
        user, stop = team(request, "/admin/customers.csv")
        if stop:
            return stop
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Tenant ID", "Company", "Domain", "Connected", "Plan", "Licence", "Valid until", "Seats",
                         "Users (last scan)", "Price per month (INR)", "Billing", "Contact", "Email", "Phone",
                         "GSTIN", "Address", "Customer since"])
        for c in customers():
            writer.writerow([_cell(v) for v in (
                c["id"], c["name"], c.get("domain"), "yes" if c["connected"] else "no", c["plan_name"],
                c["state"]["label"], c["state"]["ends"] or "", c["seats_limit"] or "", c.get("users") or "",
                f"{c['mrr']:.0f}" if c["mrr"] else "", c.get("cycle") or "", c.get("contact_name"),
                c.get("contact_email"), c.get("contact_phone"), c.get("gstin"), c.get("address"),
                (c.get("created_at") or "")[:10])])
        audit(request, user["email"], "data.export", "Downloaded the customer list (CSV)")
        name = f"tenvaultix-customers-{utcnow():%Y-%m-%d}.csv"
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------ pricing

    @app.get("/admin/pricing")
    def pricing(request: Request):
        user, stop = team(request, "/admin/pricing")
        if stop:
            return stop
        plans = store.list_plans()
        return page(request, "pricing.html", "pricing", plans=plans, units=UNITS, ctas=licensing.CTAS,
                    in_use={p["id"]: store.plan_in_use(p["id"]) for p in plans}, trial_days=store.trial_days())

    @app.post("/admin/pricing/new")
    def pricing_new(request: Request, csrf: str = Form(""), plan_id: str = Form(""), name: str = Form("")):
        user = team_post(request, csrf)
        if not user:
            return RedirectResponse("/admin", status_code=303)
        plan_id = plan_id.strip().lower()
        if not licensing.PLAN_ID.match(plan_id):
            flash(request, "Plan ID: 2 to 24 lower-case letters, numbers or dashes, starting with a letter.", "error")
        elif store.get_plan(plan_id):
            flash(request, f"There's already a plan called {plan_id}.", "error")
        elif not licensing.short(name, 40):
            flash(request, "Give the plan a name.", "error")
        else:
            last = max((p["sort"] for p in store.list_plans()), default=0)
            store.save_plan({"id": plan_id, "name": licensing.short(name, 40), "price_inr": None, "price_usd": None,
                             "unit": "month", "max_users": None, "scan_days": 7, "tagline": "", "features": [],
                             "public": False, "featured": False, "cta": "trial", "sort": last + 1}, user["email"])
            audit(request, user["email"], "pricing.add", f"Added plan {plan_id}")
            flash(request, "Plan added. It's hidden from the pricing page until you tick “Show on pricing page”.",
                  "good")
            return RedirectResponse(f"/admin/pricing#plan-{plan_id}", status_code=303)
        return RedirectResponse("/admin/pricing#new-plan", status_code=303)

    @app.post("/admin/pricing/trial")
    def pricing_trial(request: Request, csrf: str = Form(""), trial_days: str = Form("")):
        user = team_post(request, csrf)
        if not user:
            return RedirectResponse("/admin", status_code=303)
        try:
            days = licensing.whole(trial_days, "Trial length", 1, 90)
        except FormError as exc:
            flash(request, str(exc), "error")
        else:
            if days:
                store.set_option("trial_days", str(days))
                audit(request, user["email"], "pricing.trial", f"Free trial length set to {days} days")
                flash(request, f"New trials now last {days} days. Trials already running keep their end date.",
                      "good")
        return RedirectResponse("/admin/pricing#trial", status_code=303)

    @app.post("/admin/pricing/{plan_id}")
    def pricing_save(request: Request, plan_id: str, csrf: str = Form(""), name: str = Form(""),
                     price_inr: str = Form(""), price_usd: str = Form(""), unit: str = Form("month"),
                     max_users: str = Form(""), scan_days: str = Form("7"), tagline: str = Form(""),
                     features: str = Form(""), public: str = Form(""), featured: str = Form(""),
                     cta: str = Form("trial"), sort: str = Form("0")):
        user = team_post(request, csrf)
        plan = store.get_plan(plan_id) if user and licensing.PLAN_ID.match(plan_id) else None
        if not plan:
            return RedirectResponse("/admin/pricing", status_code=303)
        try:
            if not licensing.short(name, 40):
                raise FormError("Give the plan a name.")
            if unit not in UNITS:
                raise FormError("Choose what the price is per.")
            if cta not in licensing.CTAS:
                raise FormError("Choose the button.")
            updated = {
                "id": plan_id, "name": licensing.short(name, 40),
                "price_inr": licensing.money(price_inr, "Price in rupees"),
                "price_usd": licensing.money(price_usd, "Price in dollars"), "unit": unit,
                "max_users": licensing.whole(max_users, "User limit", 1, 1_000_000),
                "scan_days": licensing.whole(scan_days, "Scan every", 1, 31) or 7,
                "tagline": licensing.short(tagline, 80) or "", "features": licensing.features(features),
                "public": bool(public) and plan_id != "trial", "featured": bool(featured),
                "cta": cta, "sort": licensing.whole(sort, "Order", 0, 99) or 0,
            }
        except FormError as exc:
            flash(request, f"{plan['name']}: {exc}", "error")
            return RedirectResponse(f"/admin/pricing#plan-{plan_id}", status_code=303)
        store.save_plan(updated, user["email"])
        changed = [k for k in updated if k != "id" and plan.get(k) != updated[k]]
        audit(request, user["email"], "pricing.save",
              f"{updated['name']}: changed {', '.join(changed) if changed else 'nothing'}")
        shown = " It's on the pricing page now." if updated["public"] else ""
        flash(request, f"Saved {updated['name']}.{shown}", "good")
        return RedirectResponse(f"/admin/pricing#plan-{plan_id}", status_code=303)

    @app.post("/admin/pricing/{plan_id}/delete")
    def pricing_delete(request: Request, plan_id: str, csrf: str = Form("")):
        user = team_post(request, csrf)
        if user and licensing.PLAN_ID.match(plan_id):
            if store.delete_plan(plan_id):
                audit(request, user["email"], "pricing.delete", f"Deleted plan {plan_id}")
                flash(request, "Plan deleted.", "good")
            else:
                flash(request, "That plan can't be deleted while customers are on it (or it's the free trial).",
                      "warn")
        return RedirectResponse("/admin/pricing", status_code=303)

    # ------------------------------------------------------------ errors (was /ops)

    @app.get("/admin/errors")
    def errors(request: Request):
        user, stop = team(request, "/admin/errors")
        if stop:
            return stop
        names = {c["id"]: c["name"] for c in store.list_customers()}
        return page(request, "errors.html", "errors", recent=store.list_problems(limit=100), names=names,
                    hint=problems.hint)

    @app.get("/ops")
    def ops_moved(request: Request):
        return RedirectResponse("/admin/errors", status_code=303)

    @app.post("/admin/errors/test")
    def credential_test(request: Request, csrf: str = Form("")):
        user = team_post(request, csrf)
        if not user:
            return RedirectResponse("/admin/errors", status_code=303)
        try:
            credential_check(user["tid"])
            audit(request, user["email"], "security.credential-test", "The Microsoft credential works")
            flash(request, "The app's Microsoft credential works: Microsoft issued a token for your tenant.", "good")
        except Exception as exc:  # AuthError, or msal/network errors
            code = problems.code_for(str(exc), "credential")
            record("credential", code, str(exc))
            audit(request, user["email"], "security.credential-test", f"Failed: {code}")
            flash(request, f"The credential test failed: {problems.hint(code)['title']} ({code}).", "warn")
        return RedirectResponse("/admin/errors", status_code=303)

    # ------------------------------------------------------------ security: emergency stop, checklist, activity

    def checklist() -> list[tuple[str, str, str]]:
        """(good | warn | bad, what, why or what to do) for things the app can see about itself."""
        items = [
            ("good", "HTTPS only", "Browsers are told to use HTTPS for a year (HSTS).") if settings.https_only else
            ("bad", "Not on HTTPS", "TVX_BASE_URL should start with https:// on a live site."),
            ("good", "Microsoft credential: certificate", "Harder to leak than a secret.") if settings.has_certificate
            else ("warn", "Microsoft credential: client secret",
                  "Switch to a certificate (docs/AZURE_APP_REGISTRATION.md, option B) and keep the secret's expiry short."),
            ("good", "Session secret set", "Sign-in cookies can't be forged without it.")
            if settings.session_secret_from_env else
            ("bad", "No TVX_SESSION_SECRET", "A temporary key is in use; set a long random one."),
            ("good", "Automatic scans on", "Scans run on schedule.") if settings.scheduler else
            ("warn", "Automatic scans off", "Set TVX_SCHEDULER=on, or run scan-due hourly."),
        ]
        stop = safety.stop_state(settings, store)
        items.append(("bad", "Emergency stop is ON", f"Turned on by {stop['by']}.") if stop else
                     ("good", "Emergency stop is off", "Everything is running normally."))
        return items

    @app.get("/admin/security")
    def security(request: Request, show: str = "all"):
        user, stop = team(request, "/admin/security")
        if stop:
            return stop
        show = show if show in ACTIVITY else "all"
        return page(request, "security.html", "security", checks=checklist(), activity_filters=ACTIVITY, show=show,
                    activity=store.list_audit(limit=200, prefix=ACTIVITY[show][1]),
                    denied=store.list_audit(limit=5, prefix="console.denied"))

    @app.post("/admin/security/stop")
    def emergency_stop(request: Request, csrf: str = Form(""), confirm: str = Form("")):
        user = team_post(request, csrf)
        if not user:
            return RedirectResponse("/admin", status_code=303)
        if confirm.strip().upper() != "STOP":
            flash(request, "Type STOP to confirm.", "warn")
        elif not safety.stop_state(settings, store):
            safety.turn_on(store, user["email"])
            audit(request, user["email"], "security.stop-on", "Emergency stop turned on")
            flash(request, "Emergency stop turned on. Customers now see “paused for maintenance”. If the Microsoft "
                           "secret may have leaked, delete it in Entra too.", "warn")
        return RedirectResponse("/admin/security", status_code=303)

    @app.post("/admin/security/resume")
    def emergency_resume(request: Request, csrf: str = Form(""), confirm: str = Form("")):
        user = team_post(request, csrf)
        if not user:
            return RedirectResponse("/admin", status_code=303)
        state = safety.stop_state(settings, store)
        if confirm.strip().upper() != "RESUME":
            flash(request, "Type RESUME to confirm.", "warn")
        elif state and state["source"] == "server":
            flash(request, "The stop is set on the server (TVX_EMERGENCY_STOP). Remove it there and restart the app.",
                  "warn")
        elif state:
            safety.turn_off(store)
            audit(request, user["email"], "security.stop-off", "Emergency stop turned off")
            flash(request, "Everything is running again.", "good")
        return RedirectResponse("/admin/security", status_code=303)

    # ------------------------------------------------------------ backup and restore

    @app.get("/admin/backup")
    def backup(request: Request):
        user, stop = team(request, "/admin/backup")
        if stop:
            return stop
        data = store.export_admin()
        return page(request, "backup.html", "backup", counts={"plans": len(data["plans"]),
                                                              "accounts": len(data["accounts"]),
                                                              "events": len(data["licence_events"])})

    @app.get("/admin/backup.json")
    def backup_download(request: Request):
        user, stop = team(request, "/admin/backup")
        if stop:
            return stop
        audit(request, user["email"], "data.backup", "Downloaded a console backup")
        name = f"tenvaultix-console-{utcnow():%Y-%m-%d-%H%M}.json"
        return Response(json.dumps(store.export_admin(), indent=1, ensure_ascii=False),
                        media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/admin/restore")
    async def restore(request: Request, csrf: str = Form(""), confirm: str = Form(""),
                      backup_file: UploadFile = File(None)):
        user = team_post(request, csrf)
        if not user:
            return RedirectResponse("/admin", status_code=303)
        if confirm.strip().upper() != "RESTORE":
            flash(request, "Type RESTORE to confirm.", "warn")
            return RedirectResponse("/admin/backup", status_code=303)
        raw = await backup_file.read(MAX_BACKUP + 1) if backup_file else b""
        try:
            if not raw:
                raise ValueError("Choose the backup file first.")
            if len(raw) > MAX_BACKUP:
                raise ValueError("That file is too big to be a console backup.")
            counts = store.import_admin(json.loads(raw.decode("utf-8")), user["email"])
        except (ValueError, UnicodeDecodeError) as exc:
            flash(request, f"Nothing was restored: {exc}", "error")
            return RedirectResponse("/admin/backup", status_code=303)
        audit(request, user["email"], "data.restore",
              f"Restored {counts['plans']} plans and {counts['accounts']} customer records")
        flash(request, f"Restored {counts['plans']} plans and {counts['accounts']} customer records.", "good")
        return RedirectResponse("/admin", status_code=303)


def _cell(value) -> str:
    """CSV cell; values that a spreadsheet would run as a formula get a leading apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


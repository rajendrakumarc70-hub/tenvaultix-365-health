"""Web app: public pages, Microsoft sign-in, connecting a tenant, dashboard and reports.

create_app() takes its dependencies as arguments so tests can pass fakes; app/main.py builds the real one.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import blueprint, demo
from .auth import LoginError, MicrosoftAuth, consent_error_message, is_consent_admin
from .checks import results_for_domain, run_checks
from .collector import collect
from .config import GRAPH_PERMISSIONS, PLANS, Settings, load_settings
from .db import Store, utcnow
from .dnscheck import Resolver, SystemResolver, check_domain, lookup_m365_tenant, normalize_domain
from .graph import AuthError, GraphClient, GraphError, app_token_provider
from .licenses import prices_reviewed
from .scanner import can_scan, run_scan, trial_days_left
from .scoring import compare, sort_for_report, summarize

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
MANUAL_SCAN_GAP = timedelta(minutes=5)
MAX_MEMBERS = 25
EMAIL_RE = re.compile(r"^[^@\s]+@[a-z0-9.-]+\.[a-z]{2,}$", re.I)
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class RateLimiter:
    """Allow `limit` hits per `window` seconds per key (in memory; fine for a single instance)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def _fmt_date(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/app"


def create_app(settings: Settings | None = None, store: Store | None = None,
               graph_factory: Callable[[str], GraphClient] | None = None, resolver: Resolver | None = None,
               auth: MicrosoftAuth | None = None, tenant_lookup=lookup_m365_tenant,
               sleep: Callable[[float], None] = time.sleep) -> FastAPI:
    settings = settings or load_settings()
    store = store or Store(settings.database_path)
    resolver = resolver or SystemResolver()
    auth = auth or MicrosoftAuth(settings)
    if graph_factory is None:
        def graph_factory(tenant_id: str) -> GraphClient:
            return GraphClient(app_token_provider(settings, tenant_id))

    def collect_tenant(tenant_id: str) -> dict:
        return collect(graph_factory(tenant_id), resolver, tenant_id)

    app = FastAPI(title="Tenvaultix 365 Health Check", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="tvx_session",
                       max_age=8 * 3600, same_site="lax", https_only=settings.https_only)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=APP_DIR / "templates")
    templates.env.filters["date"] = _fmt_date
    templates.env.filters["money"] = lambda v: f"${v:,.0f}" if v else "$0"
    templates.env.tests["onmicrosoft"] = lambda d: str(d).endswith(".onmicrosoft.com")
    domain_limiter = RateLimiter(20, 3600)
    app.state.store = store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/app"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------ helpers

    def render(request: Request, name: str, status_code: int = 200, **context):
        session = request.session
        session.setdefault("csrf", secrets.token_urlsafe(24))
        base = {
            "settings": settings, "user": session.get("user"), "csrf": session["csrf"],
            "flash": session.pop("flash", []), "plans": PLANS, "year": utcnow().year,
        }
        return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)

    def message(request: Request, title: str, body: str, *, status_code: int = 200,
                actions: list[tuple[str, str]] | None = None, kind: str = "info"):
        return render(request, "message.html", status_code=status_code, title=title, body=body,
                      actions=actions or [("Back to home", "/")], kind=kind)

    def flash(request: Request, text: str, kind: str = "info") -> None:
        request.session.setdefault("flash", []).append({"kind": kind, "text": text})

    def csrf_ok(request: Request, token: str) -> bool:
        expected = request.session.get("csrf", "")
        return bool(token and expected and secrets.compare_digest(token, expected))

    def membership(request: Request):
        """(user, tenant, role) for the signed-in person; any of them may be None."""
        user = request.session.get("user")
        if not user:
            return None, None, None
        tenant = store.get_tenant(user["tid"])
        role = store.member_role(tenant["id"], user["email"]) if tenant else None
        return user, tenant, role

    def report_context(results: list[dict], *, tenant_name: str, scanned_at: str, meta: dict,
                       previous: list[dict] | None = None, is_demo: bool = False, scan: dict | None = None) -> dict:
        ordered = sort_for_report(results)
        return {
            "tenant_name": tenant_name, "scanned_at": scanned_at, "meta": meta, "scan": scan, "demo": is_demo,
            "summary": summarize(results),
            "problems": [r for r in ordered if r["status"] in ("fail", "warn")],
            "unavailable": [r for r in ordered if r["status"] in ("not_available", "error")],
            "info": [r for r in ordered if r["status"] == "info"],
            "passed": [r for r in ordered if r["status"] == "pass"],
            "changes": compare(previous, results) if previous is not None else None,
            "prices_reviewed": prices_reviewed(),
            "bp": blueprint.build(results, meta.get("facts")),
        }

    # ------------------------------------------------------------ public pages

    def demo_preview() -> dict:
        results = run_checks(demo.snapshot())
        summary = summarize(results)
        urgent = [r for r in sort_for_report(results) if r["status"] in ("fail", "warn")][:4]
        return {**summary, "top": urgent}

    @app.get("/")
    def home(request: Request):
        return render(request, "index.html", preview=demo_preview())

    @app.get("/check")
    def domain_check(request: Request, domain: str = ""):
        name = normalize_domain(domain)
        if not name:
            return render(request, "index.html", status_code=400, domain_value=domain, preview=demo_preview(),
                          domain_error="That doesn't look like a domain. Try something like yourcompany.com.")
        client = request.client.host if request.client else "unknown"
        if not domain_limiter.allow(client):
            return message(request, "Slow down a little",
                           "You've run a lot of checks in the last hour. Please try again later.", status_code=429)
        report = check_domain(name, resolver, tenant_lookup=tenant_lookup)
        results = results_for_domain(report)
        return render(request, "domain.html", report=report, results=sort_for_report(results),
                      summary=summarize(results))

    @app.get("/demo")
    def demo_report(request: Request):
        snap = demo.snapshot()
        results = run_checks(snap)
        meta = {"activity_source": snap["activity_source"], "mfa_source": snap["mfa_source"],
                "users": len(snap["users"]), "mailboxes_checked": snap["mailboxes_checked"],
                "domains": [d["name"] for d in snap["tenant"]["domains"]], "features": snap["features"],
                "facts": blueprint.facts(snap)}
        return render(request, "report.html", **report_context(
            results, tenant_name=snap["tenant"]["name"], scanned_at=snap["collected_at"], meta=meta, is_demo=True))

    @app.get("/privacy")
    def privacy(request: Request):
        return render(request, "privacy.html", permissions=GRAPH_PERMISSIONS)

    @app.get("/setup")
    def setup(request: Request):
        return render(request, "setup.html", permissions=GRAPH_PERMISSIONS)

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------ sign-in

    @app.get("/login")
    def login(request: Request, next: str = "/app"):
        if not settings.microsoft_configured:
            return message(request, "Sign-in isn't set up yet",
                           "Microsoft sign-in hasn't been configured on this server. The setup page lists what's needed.",
                           actions=[("Setup guide", "/setup"), ("See the sample report", "/demo")])
        request.session["next"] = _safe_next(next)
        return RedirectResponse(auth.start_login(request.session), status_code=303)

    @app.get("/auth/callback")
    def auth_callback(request: Request):
        params = dict(request.query_params)
        if "error" in params:
            return message(request, "Sign-in didn't finish", params.get("error_description", params["error"])[:300],
                           actions=[("Try again", "/login")], kind="error")
        try:
            user = auth.finish_login(request.session, params)
        except LoginError as exc:
            return message(request, "Sign-in didn't finish", str(exc), actions=[("Try again", "/login")], kind="error")
        request.session["user"] = user
        return RedirectResponse(_safe_next(request.session.pop("next", None)), status_code=303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------ connecting a tenant

    @app.get("/connect")
    def connect(request: Request):
        user, tenant, role = membership(request)
        if tenant and role:
            return RedirectResponse("/app", status_code=303)
        return render(request, "connect.html", permissions=GRAPH_PERMISSIONS, tenant=tenant)

    @app.get("/connect/start")
    def connect_start(request: Request):
        user = request.session.get("user")
        if not user:
            return RedirectResponse("/login?next=/connect", status_code=303)
        state = secrets.token_urlsafe(24)
        request.session["consent_state"] = state
        return RedirectResponse(auth.admin_consent_url(user["tid"], state), status_code=303)

    @app.get("/connect/callback")
    def connect_callback(request: Request, background: BackgroundTasks, state: str = "", tenant: str = "",
                         error: str = "", error_description: str = ""):
        user = request.session.get("user")
        expected = request.session.pop("consent_state", None)
        if not user:
            return message(request, "Please sign in again", "Your session ended during the approval.",
                           actions=[("Sign in", "/login?next=/connect")])
        if not expected or not secrets.compare_digest(state, expected):
            return message(request, "That approval link has expired", "Please start the connection again.",
                           actions=[("Connect Microsoft 365", "/connect")], kind="error")
        if error:
            return message(request, "Not connected", consent_error_message(error, error_description),
                           actions=[("Try again", "/connect"), ("Back to home", "/")], kind="error")
        if tenant.lower() != user["tid"].lower():
            return message(request, "Different organization",
                           "The approval was given for a different Microsoft 365 organization than the one you're "
                           "signed in to. Sign in with an account from the company you want to check.",
                           actions=[("Sign in again", "/logout")], kind="error")
        request.session["pending_tenant"] = user["tid"]
        return finish_connect(request, background)

    @app.get("/connect/verify")
    def connect_verify(request: Request, background: BackgroundTasks):
        if not request.session.get("user") or not request.session.get("pending_tenant"):
            return RedirectResponse("/connect", status_code=303)
        return finish_connect(request, background)

    def finish_connect(request: Request, background: BackgroundTasks):
        user = request.session["user"]
        tenant_id = request.session["pending_tenant"]
        if store.get_tenant(tenant_id):
            request.session.pop("pending_tenant", None)
            return RedirectResponse("/app", status_code=303)
        graph = graph_factory(tenant_id)
        is_admin = None
        for attempt in range(3):  # Microsoft can take a few seconds to apply a fresh approval
            try:
                is_admin = is_consent_admin(graph, user["oid"])
                break
            except (AuthError, GraphError) as exc:
                log.info("Consent not active yet for %s (attempt %s): %s", tenant_id, attempt + 1, exc)
                sleep(3)
        if is_admin is None:
            return message(request, "Almost there",
                           "Microsoft is still applying your approval. This usually takes less than a minute.",
                           actions=[("Try again", "/connect/verify")])
        if not is_admin:
            request.session.pop("pending_tenant", None)
            return message(request, "An admin needs to connect",
                           "Only a Global Administrator or Privileged Role Administrator can connect your company. "
                           "Ask them to sign in here and press Connect.", kind="error")
        request.session.pop("pending_tenant", None)
        store.create_tenant(tenant_id, None, user["email"])
        scan_id = store.start_scan(tenant_id)
        background.add_task(run_scan, store, tenant_id, collect_tenant, scan_id)
        flash(request, "Connected. Your first health check is running and usually takes a minute or two.", "good")
        return RedirectResponse("/app", status_code=303)

    # ------------------------------------------------------------ signed-in app

    @app.get("/app")
    def dashboard(request: Request):
        user, tenant, role = membership(request)
        if not user:
            return RedirectResponse("/login?next=/app", status_code=303)
        if not tenant:
            return RedirectResponse("/connect", status_code=303)
        if not role:
            return message(request, "Ask your admin for access",
                           f"Your company is connected, but {user['email']} hasn't been added to the report yet. "
                           f"Ask {tenant['connected_by']} to add you under Team access.",
                           actions=[("Sign out", "/logout")])
        latest = store.latest_scan(tenant["id"])
        running = store.running_scan(tenant["id"])
        summary = summarize(latest["results"]) if latest else None
        return render(request, "dashboard.html", tenant=tenant, role=role, latest=latest, summary=summary,
                      running=running, scans=store.list_scans(tenant["id"]), members=store.list_members(tenant["id"]),
                      days_left=trial_days_left(tenant), scanning_allowed=can_scan(tenant),
                      plan_name=PLANS.get(tenant["plan"], (tenant["plan"],))[0])

    @app.post("/app/scan")
    def start_scan(request: Request, background: BackgroundTasks, csrf: str = Form("")):
        user, tenant, role = membership(request)
        if not (tenant and role) or not csrf_ok(request, csrf):
            return RedirectResponse("/app", status_code=303)
        if not can_scan(tenant):
            flash(request, "Your free trial has ended. Choose a plan to keep scanning.", "warn")
        elif store.running_scan(tenant["id"]):
            flash(request, "A scan is already running.")
        else:
            last = store.list_scans(tenant["id"], limit=1)
            if last and utcnow() - datetime.fromisoformat(last[0]["started_at"]) < MANUAL_SCAN_GAP:
                flash(request, "Please wait a few minutes between scans.", "warn")
            else:
                scan_id = store.start_scan(tenant["id"])
                background.add_task(run_scan, store, tenant["id"], collect_tenant, scan_id)
                flash(request, "Scan started. This page refreshes when it's done.", "good")
        return RedirectResponse("/app", status_code=303)

    @app.get("/app/report")
    def latest_report(request: Request):
        user, tenant, role = membership(request)
        latest = store.latest_scan(tenant["id"]) if tenant and role else None
        return RedirectResponse(f"/app/report/{latest['id']}" if latest else "/app", status_code=303)

    @app.get("/app/report/{scan_id}")
    def report(request: Request, scan_id: int):
        user, tenant, role = membership(request)
        if not user:
            return RedirectResponse(f"/login?next=/app/report/{scan_id}", status_code=303)
        scan = store.get_scan(scan_id)
        if not (tenant and role and scan and scan["tenant_id"] == tenant["id"] and scan["status"] == "done"):
            return message(request, "Report not found", "This report doesn't exist or isn't yours to see.",
                           status_code=404, actions=[("Go to dashboard", "/app")])
        previous = store.previous_scan(tenant["id"], scan_id)
        return render(request, "report.html", **report_context(
            scan["results"], tenant_name=tenant["name"] or "Your company", scanned_at=scan["started_at"],
            meta=scan["meta"], previous=previous["results"] if previous else None, scan=scan))

    @app.post("/app/team")
    def add_member(request: Request, csrf: str = Form(""), email: str = Form("")):
        user, tenant, role = membership(request)
        if role != "owner" or not csrf_ok(request, csrf):
            return RedirectResponse("/app", status_code=303)
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            flash(request, "Enter a full work email address, such as name@yourcompany.com.", "warn")
        elif len(store.list_members(tenant["id"])) >= MAX_MEMBERS:
            flash(request, f"You can add up to {MAX_MEMBERS} people.", "warn")
        else:
            store.add_member(tenant["id"], email)
            flash(request, f"{email} can now see the reports after signing in with Microsoft.", "good")
        return RedirectResponse("/app#team", status_code=303)

    @app.post("/app/team/remove")
    def remove_member(request: Request, csrf: str = Form(""), email: str = Form("")):
        user, tenant, role = membership(request)
        if role == "owner" and csrf_ok(request, csrf):
            store.remove_member(tenant["id"], email)
            flash(request, f"Removed {email}.")
        return RedirectResponse("/app#team", status_code=303)

    @app.post("/app/disconnect")
    def disconnect(request: Request, csrf: str = Form(""), confirm: str = Form("")):
        user, tenant, role = membership(request)
        if role != "owner" or not csrf_ok(request, csrf):
            return RedirectResponse("/app", status_code=303)
        if confirm.strip().upper() != "DELETE":
            flash(request, "Type DELETE to confirm.", "warn")
            return RedirectResponse("/app#danger", status_code=303)
        store.delete_tenant(tenant["id"])
        return message(request, "Disconnected and deleted",
                       "We've deleted all reports and team access for your company. To finish, remove our app from "
                       "your tenant: Microsoft Entra admin center > Entra ID > Enterprise apps > "
                       "Tenvaultix 365 Health Check > Properties > Delete.",
                       actions=[("Back to home", "/")], kind="good")

    return app

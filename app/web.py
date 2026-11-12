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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import azure_costs, blueprint, demo, fixes, licensing, problems
from .auth import LoginError, MicrosoftAuth, consent_error_message, is_consent_admin
from .checks import results_for_domain, run_checks
from .collector import collect
from .config import GRAPH_PERMISSIONS, Settings, load_settings
from .db import Store, utcnow
from .dnscheck import Resolver, SystemResolver, check_domain, lookup_m365_tenant, normalize_domain
from .graph import AuthError, GraphClient, GraphError, app_token_provider, forget_tokens, token_roles
from .licenses import prices_reviewed
from .console import add_console
from .scanner import can_scan, run_scan, scan_interval
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
               sleep: Callable[[float], None] = time.sleep,
               delegated_graph: Callable[[str], GraphClient] | None = None,
               credential_check: Callable[[str], None] | None = None,
               arm_factory: Callable[[str], GraphClient] | None = None,
               app_roles: Callable[[str], list[str]] | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = store or Store(settings.database_path)
    resolver = resolver or SystemResolver()
    auth = auth or MicrosoftAuth(settings)
    if graph_factory is None:
        def graph_factory(tenant_id: str) -> GraphClient:
            return GraphClient(app_token_provider(settings, tenant_id))
    if delegated_graph is None:
        def delegated_graph(access_token: str) -> GraphClient:  # one-click fixes: the admin's own token
            return GraphClient(lambda: access_token, max_retries=1)

    if arm_factory is None:
        def arm_factory(tenant_id: str) -> GraphClient:  # Azure Resource Manager, for cost data
            return GraphClient(app_token_provider(settings, tenant_id, azure_costs.ARM_SCOPE),
                               root=azure_costs.ARM_ROOT, max_retries=1, timeout=20,
                               sleep=lambda seconds: time.sleep(min(seconds, 5)))  # someone is waiting on the page
    if app_roles is None:
        def app_roles(tenant_id: str) -> list[str]:  # what Microsoft actually granted the app in that tenant
            forget_tokens(tenant_id)
            return token_roles(app_token_provider(settings, tenant_id)())
    if credential_check is None:
        def credential_check(tenant_id: str) -> None:  # raises if Microsoft won't issue the app a token
            app_token_provider(settings, tenant_id)()

    def collect_tenant(tenant_id: str) -> dict:
        return collect(graph_factory(tenant_id), resolver, tenant_id)

    app = FastAPI(title="Tenvaultix 365 Health Check", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="tvx_session",
                       max_age=8 * 3600, same_site="lax", https_only=settings.https_only)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=APP_DIR / "templates")
    templates.env.filters["date"] = _fmt_date
    templates.env.filters["money"] = lambda v: f"${v:,.0f}" if v else "$0"
    templates.env.filters["cost"] = azure_costs.fmt_money
    templates.env.filters["price"] = licensing.price_text
    templates.env.filters["day"] = lambda d: d.strftime("%d %b %Y") if d else ""
    templates.env.filters["compact"] = azure_costs.compact
    templates.env.tests["onmicrosoft"] = lambda d: str(d).endswith(".onmicrosoft.com")
    domain_limiter = RateLimiter(20, 3600)
    sign_in_problem_limiter = RateLimiter(20, 3600)  # the sign-in return address is public; don't let it flood the log
    app.state.store = store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/app"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception):
        # Runs outside the session middleware, so the page is rendered without the signed-in user.
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        record("crash", "crash", f"{request.method} {request.url.path}: {type(exc).__name__}: {str(exc)[:200]}")
        response = templates.TemplateResponse(request, "message.html", {
            "settings": settings, "user": None, "csrf": "", "flash": [], "year": utcnow().year,
            "title": "Something went wrong on our side",
            "body": "The app hit an unexpected error. It's been logged. Please try again in a minute.",
            "actions": [("Back to home", "/")], "kind": "error"}, status_code=500)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    # ------------------------------------------------------------ helpers

    def render(request: Request, name: str, status_code: int = 200, **context):
        session = request.session
        session.setdefault("csrf", secrets.token_urlsafe(24))
        base = {
            "settings": settings, "user": session.get("user"), "csrf": session["csrf"],
            "flash": session.pop("flash", []), "year": utcnow().year,
        }
        return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)

    def message(request: Request, title: str, body: str, *, status_code: int = 200,
                actions: list[tuple[str, str]] | None = None, kind: str = "info"):
        return render(request, "message.html", status_code=status_code, title=title, body=body,
                      actions=actions or [("Back to home", "/")], kind=kind)

    def record(area: str, code: str, detail: str | None = None, tenant_id: str | None = None) -> None:
        """Keep a problem for /setup, /ops and the company's dashboard. Never lets logging break a page."""
        try:
            store.add_problem(area, code, detail, tenant_id)
        except Exception:
            log.exception("Couldn't record problem %s", code)

    def is_ops(user: dict | None) -> bool:
        # Email alone isn't enough to trust (Microsoft advises against authorizing on it), so the person must also
        # sign in from the Tenvaultix tenant.
        return bool(user and settings.ops_emails and settings.ops_tenant_id
                    and user["tid"].lower() == settings.ops_tenant_id
                    and user["email"].lower() in settings.ops_emails)

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
        return render(request, "index.html", preview=demo_preview(), pricing=store.list_plans(public_only=True),
                      trial_days=store.trial_days())

    @app.get("/check")
    def domain_check(request: Request, domain: str = ""):
        name = normalize_domain(domain)
        if not name:
            return render(request, "index.html", status_code=400, domain_value=domain, preview=demo_preview(),
                          pricing=store.list_plans(public_only=True), trial_days=store.trial_days(),
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

    @app.get("/demo/azure")
    def demo_azure(request: Request):
        costs = demo.azure_costs()
        return render(request, "azure.html", costs=costs, chart=azure_costs.chart(costs["days"]),
                      tenant_name="Acme Diagnostics (sample)", demo=True)

    @app.get("/privacy")
    def privacy(request: Request):
        return render(request, "privacy.html", permissions=GRAPH_PERMISSIONS)

    @app.get("/setup")
    def setup(request: Request):
        recent = problems.grouped(store.list_problems(limit=50, server_only=True))
        return render(request, "setup.html", permissions=GRAPH_PERMISSIONS, fix_permissions=fixes.FIX_PERMISSIONS,
                      recent_problems=recent[:6], console=console_check(request.session.get("user")))

    def console_check(user: dict | None) -> dict:
        """Whether the console settings are in place and, for the person signed in, whether they match.
        Shows only the signed-in person's own email and tenant, never the configured values."""
        check = {"emails": len(settings.ops_emails), "tenant_set": bool(settings.ops_tenant_id),
                 "tenant_looks_right": bool(re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                                                         settings.ops_tenant_id))}
        if user:
            check["me"] = {"email": user["email"], "tid": user["tid"],
                           "email_listed": user["email"].lower() in settings.ops_emails,
                           "tenant_matches": user["tid"].lower() == settings.ops_tenant_id,
                           "allowed": is_ops(user)}
        return check

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
        fix_flow = request.session.get("fix_flow")
        if fix_flow and params.get("state") and secrets.compare_digest(params["state"], fix_flow.get("state", "")):
            return finish_fix(request, params)
        started_here = "auth_flow" in request.session  # only a sign-in we started is worth recording
        client = request.client.host if request.client else "unknown"
        if "error" in params:
            request.session.pop("auth_flow", None)
            detail = params.get("error_description", params["error"])
            code = problems.code_for(detail, problems.oauth_code(params["error"]))
            if started_here and sign_in_problem_limiter.allow(client):
                record("sign-in", code, detail)
            return sign_in_failed(request, code, detail)
        try:
            user = auth.finish_login(request.session, params)
        except LoginError as exc:
            code = problems.code_for(str(exc), "sign-in")
            if started_here and sign_in_problem_limiter.allow(client):
                record("sign-in", code, str(exc))
            return sign_in_failed(request, code, str(exc))
        request.session["user"] = user
        return RedirectResponse(_safe_next(request.session.pop("next", None)), status_code=303)

    def sign_in_failed(request: Request, code: str, detail: str):
        known = code in problems.HINTS
        hint = problems.hint(code)
        body = f"{hint['title']}. {hint['advice']} (Microsoft code {code})" if known else detail[:300]
        return message(request, "Sign-in didn't finish", body,
                       actions=[("Try again", "/login"), ("Setup guide", "/setup")], kind="error")

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
            record("connect", problems.code_for(error_description, error[:40]), error_description or error,
                   user["tid"])
            return message(request, "Not connected", consent_error_message(error, error_description),
                           actions=[("Try again", "/connect"), ("Back to home", "/")], kind="error")
        if tenant.lower() != user["tid"].lower():
            return message(request, "Different organization",
                           "The approval was given for a different Microsoft 365 organization than the one you're "
                           "signed in to. Sign in with an account from the company you want to check.",
                           actions=[("Sign in again", "/logout")], kind="error")
        request.session["pending_tenant"] = user["tid"]
        request.session["connect_tries"] = 0
        forget_tokens(user["tid"])  # tokens from before this approval don't carry its permissions
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
        is_admin, last_error = None, None
        for attempt in range(3):  # Microsoft can take a few seconds to apply a fresh approval
            forget_tokens(tenant_id)  # ask for a new token each time; a cached one may predate the approval
            try:
                is_admin = is_consent_admin(graph_factory(tenant_id), user["oid"])
                break
            except (AuthError, GraphError) as exc:
                log.info("Consent not active yet for %s (attempt %s): %s", tenant_id, attempt + 1, exc)
                last_error = exc
                sleep(3)
        if is_admin is None:
            fallback = problems.graph_code(last_error.status) if isinstance(last_error, GraphError) else "graph-401"
            code = problems.code_for(str(last_error), fallback)
            record("connect", code, str(last_error), tenant_id)
            tries = request.session.get("connect_tries", 0) + 1
            request.session["connect_tries"] = tries
            if tries < 2:
                return message(request, "Almost there",
                               "Microsoft is still applying your approval. This usually takes less than a minute. "
                               "If it happens again, we'll show you what Microsoft says.",
                               actions=[("Try again", "/connect/verify")])
            return connect_blocked(request, tenant_id, code, last_error)
        if not is_admin:
            request.session.pop("pending_tenant", None)
            return message(request, "An admin needs to connect",
                           "Only a Global Administrator or Privileged Role Administrator can connect your company. "
                           "Ask them to sign in here and press Connect.", kind="error")
        request.session.pop("pending_tenant", None)
        request.session.pop("connect_tries", None)
        store.create_tenant(tenant_id, None, user["email"])
        scan_id = store.start_scan(tenant_id)
        background.add_task(run_scan, store, tenant_id, collect_tenant, scan_id)
        flash(request, "Connected. Your first health check is running and usually takes a minute or two.", "good")
        return RedirectResponse("/app", status_code=303)

    def connect_blocked(request: Request, tenant_id: str, code: str, error: Exception | None):
        """The approval still isn't working after a few tries: say what Microsoft is telling us, and what to do."""
        actions = [("Try again", "/connect/verify"), ("Approve again", "/connect/start"), ("Setup guide", "/setup")]
        if isinstance(error, GraphError) and error.forbidden:
            try:
                roles = app_roles(tenant_id)
            except Exception as exc:  # the token itself failed; fall back to the general advice
                log.info("Couldn't read the app's permissions for %s: %s", tenant_id, exc)
                roles = None
            if roles == []:
                body = ("Microsoft accepted the approval but gave the app no application permissions in your "
                        "company. This usually means the permissions in the app registration were added as "
                        "Delegated instead of Application, or admin consent wasn't granted for them. In Microsoft "
                        "Entra admin center open App registrations > Tenvaultix 365 Health Check > API "
                        "permissions. Every Microsoft Graph permission except User.Read should say Type: "
                        "Application and Status: Granted. Fix any that don't, press \"Grant admin consent\", "
                        "then press Try again here.")
            elif roles:
                needed = {"Directory.Read.All", "RoleManagement.Read.Directory"}
                missing = [name for name, _ in GRAPH_PERMISSIONS if name not in roles]
                if needed & set(roles):
                    body = ("Microsoft has granted the permissions but is still switching them on. Wait two or "
                            "three minutes, then press Try again.")
                else:
                    body = (f"The app has {len(roles)} of the {len(GRAPH_PERMISSIONS)} application permissions it "
                            f"needs in your company ({', '.join(roles)}). Still missing: {', '.join(missing)}. "
                            "In App registrations > API permissions, add each missing one as type Application, "
                            "press \"Grant admin consent\", wait a minute, then press Try again.")
            else:
                body = ("Microsoft refused the app's request to read your directory. Check the app registration's "
                        "API permissions are type Application and show Granted, then press Try again.")
        else:
            hint = problems.hint(code)
            body = f"{hint['title']}. {hint['advice']}"
        return message(request, "Not connected yet", f"{body} (Microsoft code: {code})", actions=actions,
                       kind="error")

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
        scans = store.list_scans(tenant["id"])
        return render(request, "dashboard.html", tenant=tenant, role=role, latest=latest, summary=summary,
                      health=connection_health(tenant, latest, scans), costs=store.get_costs(tenant["id"]),
                      running=running, scans=scans, members=store.list_members(tenant["id"]),
                      changes=store.list_fixes(tenant["id"]), fix_catalog=fixes.FIXES,
                      licence=licence_view(tenant, latest), scanning_allowed=can_scan(tenant))

    def licence_view(tenant: dict, latest: dict | None) -> dict:
        """The company's plan and licence, as its dashboard shows them."""
        plan = store.get_plan(tenant["plan"]) or {}
        users = licensing.staff_count(latest.get("meta")) if latest else None
        seats = licensing.seat_limit(tenant, plan)
        return {**licensing.state(tenant), "plan_name": plan.get("name", tenant["plan"]), "users": users,
                "seats": seats, "over_seats": bool(users and seats and users > seats)}

    def connection_health(tenant: dict, latest: dict | None, scans: list[dict]) -> dict:
        """What's stopping this company's checks from working, in plain English, for the dashboard."""
        last = next((s for s in scans if s["status"] != "running"), None)
        blocked = (latest or {}).get("meta", {}).get("no_permission", []) if latest else []
        interval = scan_interval(store, tenant)
        late = bool(latest and can_scan(tenant)
                    and utcnow() - datetime.fromisoformat(latest["started_at"]) > interval + timedelta(days=1))
        recent = problems.grouped(store.list_problems(limit=30, tenant_id=tenant["id"]))[:5]
        items = []
        if last and last["status"] == "failed":
            items.append(("bad", "The last scan failed", last["error"] or ""))
        elif last:
            items.append(("good", "Microsoft access is working", f"Last scan finished {_fmt_date(last['finished_at'])}."))
        if blocked:
            names = ", ".join(problems.SECTIONS.get(b, b) for b in blocked)
            items.append(("warn", f"{len(blocked)} part{'s' if len(blocked) != 1 else ''} of your tenant couldn't be read",
                          f"Missing permission for: {names}. An admin can approve the latest permissions."))
        if late:
            items.append(("warn", "Automatic scans are late",
                          f"Your plan scans every {interval.days} day{'s' if interval.days != 1 else ''}, but the "
                          f"last one was {_fmt_date(latest['started_at'])}. The server may have been asleep."))
        worst = "bad" if any(i[0] == "bad" for i in items) else ("warn" if any(i[0] == "warn" for i in items)
                                                                   else "good")
        return {"items": items, "problems": recent, "worst": worst, "reapprove": bool(blocked) or (
            last is not None and last["status"] == "failed")}

    @app.post("/app/scan")
    def start_scan(request: Request, background: BackgroundTasks, csrf: str = Form("")):
        user, tenant, role = membership(request)
        if not (tenant and role) or not csrf_ok(request, csrf):
            return RedirectResponse("/app", status_code=303)
        if not can_scan(tenant):
            ended = {"suspended": "Your account is paused.", "expired": "Your licence has ended."}
            flash(request, ended.get(licensing.state(tenant)["key"], "Your free trial has ended.")
                  + " Contact Tenvaultix to keep scanning.", "warn")
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
        return render(request, "report.html", fixable=list(fixes.FIXES) if role == "owner" else [], **report_context(
            scan["results"], tenant_name=tenant["name"] or "Your company", scanned_at=scan["started_at"],
            meta=scan["meta"], previous=previous["results"] if previous else None, scan=scan))

    # ------------------------------------------------------------ one-click fixes

    def owner_context(request: Request):
        """(user, tenant) when the signed-in person owns the connected company, else (user, None)."""
        user, tenant, role = membership(request)
        return (user, tenant) if role == "owner" else (user, None)

    @app.get("/app/fix/{check_id}")
    def fix_preview(request: Request, check_id: str):
        user, tenant = owner_context(request)
        if not user:
            return RedirectResponse(f"/login?next=/app/fix/{check_id}", status_code=303)
        fix = fixes.FIXES.get(check_id)
        if not fix:
            return message(request, "No one-click fix for this", "Follow the step-by-step guide in the report instead.",
                           status_code=404, actions=[("Go to dashboard", "/app")])
        if not tenant:
            return message(request, "Only the owner can make changes",
                           "One-click fixes are available to the person who connected your company.",
                           status_code=403, actions=[("Go to dashboard", "/app")])
        latest = store.latest_scan(tenant["id"])
        result = next((r for r in (latest or {}).get("results", []) if r["id"] == check_id), None)
        return render(request, "fix.html", fix=fix, result=result, latest=latest)

    @app.post("/app/fix/{check_id}")
    def fix_start(request: Request, check_id: str, csrf: str = Form("")):
        user, tenant = owner_context(request)
        fix = fixes.FIXES.get(check_id)
        if not (fix and tenant and csrf_ok(request, csrf)):
            return RedirectResponse("/app", status_code=303)
        request.session["fix_pending"] = {"check_id": check_id}
        return RedirectResponse(auth.start_fix(request.session, fix.scope, user["email"]), status_code=303)

    @app.post("/app/fixes/{fix_id}/undo")
    def fix_undo(request: Request, fix_id: int, csrf: str = Form("")):
        user, tenant = owner_context(request)
        record = store.get_fix(fix_id)
        if not (tenant and record and record["tenant_id"] == tenant["id"] and csrf_ok(request, csrf)
                and record["action"] == "apply" and record["status"] != "failed" and not record["undone_by"]):
            return RedirectResponse("/app", status_code=303)
        fix = fixes.FIXES[record["check_id"]]
        request.session["fix_pending"] = {"check_id": fix.check_id, "undo_of": fix_id}
        return RedirectResponse(auth.start_fix(request.session, fix.scope, user["email"]), status_code=303)

    def finish_fix(request: Request, params: dict):
        pending = request.session.pop("fix_pending", None) or {}
        back = [("Back to dashboard", "/app")]
        if "error" in params:
            request.session.pop("fix_flow", None)
            return message(request, "Nothing was changed",
                           "The approval on Microsoft's page wasn't completed, so we didn't change anything.",
                           actions=back)
        try:
            grant = auth.finish_fix(request.session, params)
        except LoginError as exc:
            user = request.session.get("user") or {}
            record("fix", problems.code_for(str(exc), "fix-approval"), str(exc), user.get("tid"))
            return message(request, "Nothing was changed", str(exc), actions=back, kind="error")
        user, tenant = owner_context(request)
        fix = fixes.FIXES.get(pending.get("check_id", ""))
        if not (fix and tenant):
            return message(request, "Nothing was changed", "This change isn't available to your account.",
                           actions=back, kind="error")
        if grant["tid"] != tenant["id"] or grant["oid"] != user["oid"]:
            return message(request, "Nothing was changed",
                           "Microsoft approved the change with a different account or company. Approve it with the "
                           "same account you're signed in with.", actions=back, kind="error")
        undo_of = store.get_fix(pending["undo_of"]) if pending.get("undo_of") else None
        if undo_of and (undo_of["tenant_id"] != tenant["id"] or undo_of["undone_by"]):
            return message(request, "Nothing was changed", "That change has already been undone.", actions=back)
        action = "undo" if undo_of else "apply"
        try:
            outcome = fixes.apply(fix, delegated_graph(grant["access_token"]),
                                  value=undo_of["before"] if undo_of else None)
        except GraphError as exc:
            log.warning("Fix %s for %s failed: %s", fix.check_id, tenant["id"], exc)
            record("fix", problems.graph_code(exc.status), f"{fix.check_id}: {exc}", tenant["id"])
            store.add_fix(tenant["id"], fix.check_id, action, status="failed", by_email=user["email"],
                          error=f"{exc.status} {exc.code}", undo_of=undo_of["id"] if undo_of else None)
            text = (f"Your Microsoft account isn't allowed to make this change. It needs the {fix.roles} role."
                    if exc.forbidden else f"Microsoft returned an error ({exc.status}). Please try again later.")
            return message(request, "Nothing was changed", text, actions=back, kind="error")
        fix_id = store.add_fix(tenant["id"], fix.check_id, action, before=outcome["before"], after=outcome["after"],
                               status="done" if outcome["verified"] else "unverified", by_email=user["email"],
                               undo_of=undo_of["id"] if undo_of else None)
        log.info("Fix %s %s for %s by %s", fix.check_id, action, tenant["id"], user["email"])
        return RedirectResponse(f"/app/fixes/{fix_id}", status_code=303)

    @app.get("/app/fixes/{fix_id}")
    def fix_result(request: Request, fix_id: int):
        user, tenant, role = membership(request)
        if not user:
            return RedirectResponse(f"/login?next=/app/fixes/{fix_id}", status_code=303)
        record = store.get_fix(fix_id)
        if not (tenant and role and record and record["tenant_id"] == tenant["id"]):
            return message(request, "Change not found", "This change doesn't exist or isn't yours to see.",
                           status_code=404, actions=[("Go to dashboard", "/app")])
        fix = fixes.FIXES[record["check_id"]]
        can_undo = (role == "owner" and record["action"] == "apply" and record["status"] != "failed"
                    and not record["undone_by"])
        return render(request, "fix_result.html", fix=fix, record=record, can_undo=can_undo,
                      before_text=fix.describe(record["before"]), after_text=fix.describe(record["after"]))

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

    # ------------------------------------------------------------ Azure spend

    def azure_view(tenant: dict, refresh: bool = False) -> dict:
        """This month's Azure costs: from the cache if under an hour old, else fresh from Microsoft."""
        cached = store.get_costs(tenant["id"])
        if not refresh and azure_costs.is_fresh(cached):
            return cached
        try:
            data = azure_costs.fetch(arm_factory(tenant["id"]))
        except Exception as exc:  # a bad answer must not break the page
            log.exception("Azure costs for %s crashed", tenant["id"])
            data = {"state": "error", "error": "Reading Azure costs hit an unexpected error.",
                    "code_text": f"{type(exc).__name__}: {str(exc)[:200]}"}
        if data["state"] == "error":
            record("azure", problems.code_for(data.get("code_text"), "azure-error"),
                   data.get("code_text") or data.get("error"), tenant["id"])
            if cached and cached.get("state") == "ok":  # keep showing the last good figures, marked as old
                data = {**{k: v for k, v in cached.items() if k != "fetched_at"},
                        "stale_error": data.get("error"), "good_at": cached.get("good_at") or cached["fetched_at"]}
        store.save_costs(tenant["id"], data)
        return store.get_costs(tenant["id"])

    @app.get("/app/azure")
    def azure(request: Request):
        user, tenant, role = membership(request)
        if not user:
            return RedirectResponse("/login?next=/app/azure", status_code=303)
        if not (tenant and role):
            return RedirectResponse("/app", status_code=303)
        costs = azure_view(tenant)
        chart = azure_costs.chart(costs["days"]) if costs.get("state") == "ok" else None
        return render(request, "azure.html", costs=costs, chart=chart, tenant_name=tenant["name"] or "Your company",
                      role=role, demo=False)

    @app.post("/app/azure/refresh")
    def azure_refresh(request: Request, csrf: str = Form("")):
        user, tenant, role = membership(request)
        if not (tenant and role) or not csrf_ok(request, csrf):
            return RedirectResponse("/app/azure", status_code=303)
        cached = store.get_costs(tenant["id"])
        if cached and utcnow() - datetime.fromisoformat(cached["fetched_at"]) < timedelta(minutes=5):
            flash(request, "These figures were updated a moment ago. Azure itself updates costs every few hours.")
        else:
            azure_view(tenant, refresh=True)
        return RedirectResponse("/app/azure", status_code=303)

    # ------------------------------------------------------------ Tenvaultix console (/admin)

    add_console(app, store=store, render=render, message=message, flash=flash, csrf_ok=csrf_ok, is_ops=is_ops,
                record=record, credential_check=credential_check, tenant_lookup=tenant_lookup)

    return app

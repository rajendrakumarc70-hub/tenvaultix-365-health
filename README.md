# Tenvaultix 365 Health Check

**Dev site:** https://tenvaultix-365-health-1.onrender.com/demo (Render free plan, redeployed on every push to `main`; the first visit after 15 idle minutes takes about a minute)

**Guides:** [Going live: approvals checklist](docs/APPROVALS_CHECKLIST.md) · [Register the app in Microsoft Entra](docs/AZURE_APP_REGISTRATION.md)

A web app that checks a small business's Microsoft 365 tenant for security gaps and wasted licenses, and
explains each problem in plain English with step-by-step fixes. It's sold as a subscription: one price per
tenant, a scan every week (or every day on higher plans).

```
 Customer                      This app                                  Microsoft
 ────────                      ────────                                  ─────────
 1. "Check my domain"   ──▶   DNS lookups (SPF, DMARC, DKIM)  ──▶      public DNS          (free, no sign-in)
 2. Sign in             ──▶   Microsoft sign-in (work accounts)  ──▶   Entra ID
 3. Admin approves      ──▶   admin-consent link  ──▶                  Microsoft's consent screen
 4. Scan                ──▶   app-only token for *their* tenant  ──▶   Microsoft Graph (read-only)
                              collector → 20 checks → score → findings stored
 5. Report              ◀──   dashboard, full report, Save as PDF, history, team access
```

## What's in it

| Part | File | What it does |
|---|---|---|
| Free domain check | `app/dnscheck.py` | SPF, DMARC and DKIM for any domain, no sign-in. The lead magnet on the home page. |
| Collector | `app/collector.py` | Reads a read-only snapshot of a tenant from Microsoft Graph. Each section fails on its own, so a missing licence or permission becomes "not checked, here's why" rather than a broken scan. |
| Check library | `app/checks/catalog.yaml` + `app/checks/rules.py` | 20 checks. Wording, severity and fix steps live in YAML; the pass/warn/fail logic lives in Python under the same id. |
| Scoring | `app/scoring.py` | 0–100 score weighted by severity, grade A–F, score per area, money wasted, "what changed since last scan". |
| License prices | `app/data/license_prices.yaml` | Microsoft list prices (after the July 2026 increase) used for the money-wasted estimate. Review when Microsoft changes prices. |
| One-click fixes | `app/fixes.py`, `app/templates/fix.html` | Optional fixes for four safe settings (app approvals, app registration, guest invitations, "Anyone" links). Preview → the admin approves one delegated permission on Microsoft's screen → the app changes the setting with the admin's own rights, reads it back, records who/what/when and offers Undo. The admin's token is used once and not kept. History on the dashboard under "Changes made". |
| Azure spend | `app/azure_costs.py`, `app/templates/azure.html` | This month's Azure costs from Azure Cost Management: spent so far, month-end estimate, a daily bar chart, top services and each subscription. Read-only: the customer gives the app the **Cost Management Reader** role (no extra Graph permission). Cached for an hour; Microsoft itself updates costs a few times a day. Rupees use Indian digit grouping. Sample at `/demo/azure`. |
| Problems and health | `app/problems.py`, `/setup`, `/admin/errors`, dashboard | Every failure (sign-in, connect, scan, fix, Azure read, crash) is recorded with its Microsoft code (AADSTS…) and a plain-English fix. `/setup` shows recent server-level problems (codes and advice only); each company's dashboard has a **Connection health** card (last scan, parts blocked by a missing permission, late scans, its own errors); the console's **Errors** page shows everything to the Tenvaultix team, with a one-button credential test. |
| Tenvaultix console | `app/console.py`, `app/licensing.py`, `app/templates/console/` | `/admin`, for the Tenvaultix team only (`TVX_OPS_EMAILS` signed in from `TVX_OPS_TENANT_ID`), with its own sidebar layout. **Overview**: monthly revenue, customers, trials, and a *needs attention* list (licences ending, ended or suspended, trials ending, more staff than seats, failed scans). **Customers**: search and filters, CSV export, and a record per company with its licence (plan, valid until, seats, agreed price, monthly/yearly, active/suspended, quick *extend 1 month / 1 year*), contact and billing details (GSTIN, address, notes) and a licence history of who changed what. Customers can be added from their domain **before** they connect; the licence then applies instead of a free trial. **Pricing**: edit every plan's rupee and dollar price, user limit, scan frequency, features, order and visibility, add or delete plans and set the trial length; the public pricing section reads from here. **Backup**: download and restore plans, customers and licences as JSON (needed while the host's disk is temporary). An ended or suspended licence pauses new scans; reports stay. |
| Office blueprint | `app/blueprint.py` + `app/templates/_blueprint.html` | The top of every report: the tenant drawn as an office floor plan an inspector has marked up. Front door = sign-in and MFA, master keys = admins, mailroom = email seals, file room = sharing, one desk per paid license, reception = guests, equipment room = devices. Numbered red marks link to the fix. Fonts are self-hosted under the SIL Open Font License (`app/static/fonts/`). |
| Web app | `app/web.py`, `app/templates/` | Home page with pricing, free check, sample report, sign-in, connect flow, dashboard, report, team access, disconnect. |
| Storage | `app/db.py` | SQLite: tenants, team members, scan findings. Disconnecting deletes everything for that tenant. |
| Scheduler CLI | `app/cli.py` | `scan-due` (run hourly), `scan`, `list`, `set-plan` (recorded in the licence history). |
| Sample tenant | `app/demo.py` | "Acme Diagnostics", a made-up company that runs through the real checks for the public sample report. |

### The checks

| Area | Checks |
|---|---|
| Sign-in & MFA | MFA enforcement (security defaults or Conditional Access) · staff without MFA · old sign-in methods blocked |
| Admin & apps | Admins without MFA · number of Global Administrators · staff approving outside apps (consent phishing) · who can register apps |
| Accounts & guests | Inactive accounts · who can invite guests · old guest accounts |
| Email security | SPF · DMARC · DKIM · inbox rules forwarding email outside the company |
| Sharing | "Anyone" links in SharePoint/OneDrive |
| Licenses & cost | Unassigned paid licenses · licenses on blocked accounts · licenses on inactive people (all with $/month) |
| Devices | Intune device compliance |
| Overview | Microsoft Secure Score (shown, not scored) |

Business Basic/Standard tenants don't have Entra ID P1, so the app falls back automatically: MFA status
from each person's registered sign-in methods, and activity from Microsoft 365 usage reports. Conditional
Access, guest sign-ins and Intune show as "not checked: needs Business Premium", which doubles as an
upgrade conversation.

## Try it locally (no Microsoft setup needed)

```bash
cd m365-health
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
TVX_SESSION_SECRET=dev uvicorn app.main:app --reload
```

Open http://localhost:8000. The home page, free domain check, sample report (`/demo`), pricing and privacy
pages all work. Sign-in shows a "not set up yet" page until you complete the steps below.

On **Windows** (PowerShell), from the `m365-health` folder:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:TVX_SESSION_SECRET="dev"
.venv\Scripts\python -m uvicorn app.main:app
```

On an **Android tablet** with Termux, `bash scripts/termux-host.sh` runs the app **and shares it on the internet**
through a Cloudflare Quick Tunnel (a temporary `https://….trycloudflare.com` link, no Cloudflare account needed).
Add email addresses to let only those people in: `bash scripts/termux-host.sh you@company.com`. Install the
packages first (the first install compiles two packages and can take 10–30 minutes):

```bash
pkg install -y git python rust binutils python-cryptography
export ANDROID_API_LEVEL=$(getprop ro.build.version.sdk)
pip install fastapi uvicorn jinja2 itsdangerous python-multipart msal requests dnspython pyyaml
export TVX_SESSION_SECRET=dev
python -m uvicorn app.main:app
```

Run the tests (81, all offline; Microsoft Graph, sign-in and DNS are faked):

```bash
python -m pytest -q
```

## Connect it to Microsoft (one-time, about 20 minutes)

1. **Register the app** in the Tenvaultix tenant: Microsoft Entra admin center > App registrations > New
   registration.
   - Name: `Tenvaultix 365 Health Check`
   - Supported account types: **Accounts in any organizational directory (multitenant)**
   - Redirect URIs (Web): `https://<your-domain>/auth/callback` and `https://<your-domain>/connect/callback`
2. **Credentials:** Certificates & secrets > upload a certificate (recommended for production) or create a
   client secret.
3. **API permissions:** delegated `User.Read`, plus these Microsoft Graph **application** permissions (all
   read-only). They're also listed, with the reasons customers see, in `app/config.py`.

   `Directory.Read.All`, `AuditLog.Read.All`, `UserAuthenticationMethod.Read.All`, `Policy.Read.All`,
   `RoleManagement.Read.Directory`, `Reports.Read.All`, `SecurityEvents.Read.All`, `MailboxSettings.Read`,
   `SharePointTenantSettings.Read.All`, `DeviceManagementManagedDevices.Read.All`
4. **Publisher verification** (Branding & properties): link your Microsoft partner (Partner Center) account
   so customers' consent screen shows a verified badge instead of "Unverified". Do this before selling.
5. **Configure** the environment variables in `.env.example` (`TVX_CLIENT_ID`, the certificate or secret,
   `TVX_BASE_URL`, `TVX_SESSION_SECRET`, and `TVX_CONTACT_EMAIL` / `TVX_BOOKING_URL` for the "fix it for me"
   buttons). `/setup` on a running server shows the exact redirect URIs to register.

Test it on the Tenvaultix tenant first, then on a friendly client.

### How connecting works

1. The customer signs in with their work account (`/login`, MSAL auth-code flow with PKCE).
2. `/connect/start` sends them to Microsoft's admin-consent screen for *their* tenant, with a one-time `state`.
3. On `/connect/callback` the app checks the `state`, checks that the tenant matches the signed-in user, then
   uses its new app-only access to confirm that the user really is a Global Administrator or Privileged Role
   Administrator. Only then is the tenant created, with that person as owner. A forged callback from a
   non-admin is refused.
4. The first scan runs in the background; the dashboard refreshes until it's done.

The owner can add colleagues by email (they sign in with Microsoft and see the reports) and can disconnect,
which deletes all of that tenant's data immediately.

## Deploy to Render (free, the current dev site)

Render runs the app straight from the `Dockerfile`, no other files needed:

1. **render.com** → sign in with GitHub → **New → Web Service** → pick this repository.
2. Render detects the Dockerfile. Choose instance type **Free** and create the service.
3. **Environment**: add `TVX_BASE_URL` = `https://tenvaultix-365-health-1.onrender.com` and `TVX_SESSION_SECRET` (tap **Generate**). Add the Microsoft
   values (`TVX_CLIENT_ID`, then `TVX_CLIENT_SECRET` or the certificate pair) once the app is registered
   ([guide](docs/AZURE_APP_REGISTRATION.md), step 6).
   Optionally add `TVX_OPS_EMAILS` (your work email) and `TVX_OPS_TENANT_ID` (the Directory (tenant) ID
   on the app registration's Overview page) to open the Tenvaultix console at `/admin`.
4. With **Auto-Deploy** on, every push to `main` redeploys.

Free instances sleep after 15 minutes without visitors (the next visit takes about a minute) and their disk is
temporary, so connected tenants and reports are lost on restart: fine for demos, not for customers (see
"Before real customers" below). A paid instance stays awake.

## Deploy to Cloudflare (needs Workers Paid)

The app runs on **Cloudflare Containers**: a small Worker (`src/worker.ts`) receives every request and
forwards it to one container built from `Dockerfile`. `wrangler.jsonc` holds the configuration. Containers
need the **Workers Paid** plan ($5/month).

**Easiest: Cloudflare builds from GitHub (no tokens).** Cloudflare dashboard > **Workers & Pages > Create
application > Import a repository**, authorize Cloudflare's GitHub app, pick this repository and keep the
default deploy command (`npx wrangler deploy`). Every push to `main` then builds the container on Cloudflare
and deploys it. GitHub Actions (`.github/workflows/deploy.yml`) still runs the tests on every push.

**Alternative: deploy from GitHub Actions.** Create a Cloudflare API token (template **Edit Cloudflare
Workers**, plus **Account > Containers > Edit**) and add it with your Account ID as the repository secrets
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. The workflow's deploy job only runs when that secret
exists, so don't use both options at once.

The site is then at `https://tenvaultix-365-health.<your-subdomain>.workers.dev`. To deploy by hand instead:
`npm ci && npx wrangler deploy` (needs Docker running).

**If the build fails with `A request to the Cloudflare API (/accounts/…/containers/me) failed`** (and the site
says *"There is no container application assigned to this Durable Object namespace"*), Cloudflare refused to set
up the container. Check two things: the account is on **Workers Paid** (Containers aren't available on the Free
plan), and the build's API token has **Account > Containers > Edit** (**My Profile > API Tokens**, edit the token
the Worker's **Settings > Build** uses). Then select **Retry build**.

Settings: public values go in `vars` in `wrangler.jsonc` (`TVX_BASE_URL`, `TVX_CONTACT_EMAIL`,
`TVX_BOOKING_URL`); secrets go in with `npx wrangler secret put` or the dashboard (`TVX_SESSION_SECRET`, `TVX_CLIENT_ID`, and
`TVX_CLIENT_SECRET` or `TVX_CERT_THUMBPRINT` + `TVX_CERT_PRIVATE_KEY` once the Microsoft app is registered). The Worker passes them to the container.

**Before real customers:** the container's disk is temporary. It's reset when the container sleeps after 30
idle minutes or is redeployed, which is fine for a demo but would lose connected tenants and reports. Move the
database to persistent storage (for example Cloudflare D1, or PostgreSQL) and set a fixed
`TVX_SESSION_SECRET` before going live. Scheduled scans (`python -m app.cli scan-due`) also need a trigger,
for example a Cron Trigger on the Worker.

## Deploy to your own server (Azure VM or Hyper-V)

The production setup: one Ubuntu server running Docker, with HTTPS from Caddy (or a Cloudflare Tunnel for an
office server behind a router), automatic scans inside the app (`TVX_SCHEDULER=on`) and nightly database
backups. Everything is in [`deploy/`](deploy/) and the step-by-step guide is
[docs/DEPLOY_SERVER.md](docs/DEPLOY_SERVER.md):

```bash
curl -fsSL https://raw.githubusercontent.com/rajendrakumarc70-hub/tenvaultix-365-health/main/deploy/install.sh -o install.sh
bash install.sh                                  # Docker, the app in /opt/tenvaultix, deploy/.env, nightly backup
sudo nano /opt/tenvaultix/deploy/.env            # domain, Microsoft app values, console access
sudo bash /opt/tenvaultix/deploy/update.sh       # build and start; run again to update
```

**Azure App Service / Container Apps:** not yet. Their storage is a network share, which Microsoft says can't
hold a SQLite database; the app needs PostgreSQL support first. Use an Azure VM until then.

**Scheduled scans:** `TVX_SCHEDULER=on` runs them inside the web app (the server setup turns it on). Without
it, run `python -m app.cli scan-due` every hour from cron. Either way, each company is scanned when its plan's
interval has passed (weekly on the trial and Essentials, daily on Business and Partner, as set on the console's
Pricing page); ended or suspended licences are skipped.

**Database backup:** `python -m app.cli backup <file>` makes a safe copy while the app runs
(`deploy/backup.sh` uses it). Plans and licences are changed in the console (`/admin`); from the command line:

```bash
python -m app.cli list
python -m app.cli set-plan <tenant-id> business
```

## Adding a check

1. Add an entry to `app/checks/catalog.yaml` (title, category, severity, why, fix steps, Microsoft link).
2. Add a function with the same id in `app/checks/rules.py`, decorated with `@rule("ID", "<sections it needs>")`.
3. If it needs new data, add a section to `app/collector.py`, and to `tests/fakes.py` and `app/demo.py`.
4. If it needs a new Graph permission, add it to `GRAPH_PERMISSIONS` in `app/config.py` and the app
   registration. Existing customers must re-approve before that check can run; until then it shows as
   "not checked: permission missing".

`tests/test_checks.py` fails if a catalog entry has no rule or the other way round.

## Security notes

- Scanning uses read-only Graph application permissions and can't change anything in a customer tenant.
- One-click fixes are separate and optional: delegated permissions an admin approves when they use a fix, used
  for that one change with the admin's own rights, from a throwaway token cache. Every change is recorded and can
  be undone. Fixes that could lock people out (MFA policies, removing admins) are deliberately not offered.
- No customer passwords are ever seen. Our own credential is a certificate or secret in our tenant, so
  protect it (Key Vault, rotation). Anyone holding it can read every connected tenant.
- Only findings are stored (including names and emails of the accounts a finding is about); raw directory
  data is discarded after each scan.
- Azure spend uses an app-only token for Azure Resource Manager and only works where the customer has assigned
  the read-only **Cost Management Reader** role; it can't see or change resources.
- Error records keep Microsoft's message for the team; public pages (`/setup`) show only codes and advice, and
  never anything from a customer tenant. A crash shows a friendly page and never the stack trace.
- The console logs every team sign-in, change, export and restore, plus attempts by people who aren't on the
  team (console → Security). An **emergency stop** there (or `TVX_EMERGENCY_STOP=on`) pauses all use of
  Microsoft access. Plan for incidents: [docs/INCIDENT_RESPONSE.md](docs/INCIDENT_RESPONSE.md); how to report a
  vulnerability: [SECURITY.md](SECURITY.md).
- HTTPS sites send HSTS, so browsers refuse plain HTTP for a year.
- Sessions are signed cookies (8 hours, `SameSite=Lax`, `Secure` on HTTPS). Every form has a CSRF token,
  responses carry a strict Content-Security-Policy, and the free domain check is rate-limited per IP.
- Have the privacy page (`app/templates/privacy.html`) reviewed for DPDP (India), GDPR (UK/EU) and the
  other markets you sell in before launch.

## Not built yet

- Payments (Razorpay for India, Stripe elsewhere) and self-service plan changes
- Instant email alerts for new critical findings, and the weekly summary email
- Partner view: all customer tenants on one screen, with the partner's logo on reports
- Optional "fix it for me" with a separate, write-enabled app that customers opt into
- Automatically giving access to any Global Administrator of a connected tenant (today the owner adds people)
- Microsoft Marketplace listing (transactable SaaS offer; Microsoft takes 3%)

# Tenvaultix 365 Health Check

**Dev site:** https://tenvaultix-365-health-dashboard.rajendrakumarc70.workers.dev (deployed by Cloudflare on every push to `main`)

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
| Web app | `app/web.py`, `app/templates/` | Home page with pricing, free check, sample report, sign-in, connect flow, dashboard, report, team access, disconnect. |
| Storage | `app/db.py` | SQLite: tenants, team members, scan findings. Disconnecting deletes everything for that tenant. |
| Scheduler CLI | `app/cli.py` | `scan-due` (run hourly), `scan`, `list`, `set-plan`. |
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

## Deploy to Cloudflare (dev link)

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

The site is then at `https://tenvaultix-365-health-dashboard.<your-subdomain>.workers.dev`. To deploy by hand instead:
`npm ci && npx wrangler deploy` (needs Docker running).

Settings: public values go in `vars` in `wrangler.jsonc` (`TVX_BASE_URL`, `TVX_CONTACT_EMAIL`,
`TVX_BOOKING_URL`); secrets go in with `npx wrangler secret put` or the dashboard (`TVX_SESSION_SECRET`, `TVX_CLIENT_ID`, and
`TVX_CLIENT_SECRET` or `TVX_CERT_THUMBPRINT` + `TVX_CERT_PRIVATE_KEY` once the Microsoft app is registered). The Worker passes them to the container.

**Before real customers:** the container's disk is temporary. It's reset when the container sleeps after 30
idle minutes or is redeployed, which is fine for a demo but would lose connected tenants and reports. Move the
database to persistent storage (for example Cloudflare D1, or PostgreSQL) and set a fixed
`TVX_SESSION_SECRET` before going live. Scheduled scans (`python -m app.cli scan-due`) also need a trigger,
for example a Cron Trigger on the Worker.

## Deploy elsewhere

The same container also runs on **Azure Container Apps** or **App Service for Containers**:

- Set the environment variables from `.env.example` in the app's configuration (store the certificate or
  secret in Azure Key Vault and reference it).
- Give `/srv/data` persistent storage (Azure Files) so the SQLite database survives restarts. SQLite is fine
  for a single instance and the first few hundred tenants; move to PostgreSQL before running more than one
  instance.
- Health probe: `GET /healthz`.
- **Scheduled scans:** run `python -m app.cli scan-due` every hour (a Container Apps scheduled job, App
  Service WebJob or cron) with the same environment and storage. It scans each tenant whose interval has
  passed: weekly on trial and Essentials, daily on Business and Partner. Trials that have ended keep their
  reports but stop scanning.

Plans aren't wired to a payment provider yet. For now, change a customer's plan after they pay:

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

- Read-only Graph permissions; the app can't change anything in a customer tenant.
- No customer passwords are ever seen. Our own credential is a certificate or secret in our tenant, so
  protect it (Key Vault, rotation). Anyone holding it can read every connected tenant.
- Only findings are stored (including names and emails of the accounts a finding is about); raw directory
  data is discarded after each scan.
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

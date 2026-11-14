# Security

## Reporting a problem

If you find a security problem in Tenvaultix 365 Health Check, please tell us privately. Don't open a public
issue.

- On GitHub: the repository's **Security** tab → **Report a vulnerability**.
- Include what you found, how to reproduce it, and what an attacker could do with it.
- Please don't access other people's data, run scans against tenants you don't own, or test denial of service.

We aim to reply within 3 working days and to fix serious problems quickly. We're happy to credit you once it's
fixed.

## How the app protects customers

- **Read-only scanning.** The app reads tenant settings with read-only Microsoft Graph application permissions.
  It never reads email content, files, chats or calendars, and a scan can't change anything.
- **Changes need the customer's admin every time.** One-click fixes use a permission the customer's admin approves
  on Microsoft's own page for that one change; the app doesn't keep it. Every change is recorded and can be undone.
- **No passwords.** Everyone signs in with Microsoft, so their own MFA and Conditional Access apply.
- **Only findings are stored**, never raw directory data. A company can delete everything with Disconnect.
- **Web protections:** HTTPS only (HSTS), strict Content-Security-Policy, CSRF tokens on every form, signed
  session cookies (8 hours, Secure, SameSite=Lax), rate limits on public endpoints, no stack traces shown.
- **The Tenvaultix console** (`/admin`) answers "not found" to anyone who isn't on the team, logs every sign-in,
  change, export and restore, and records attempts by people who aren't on the team.
- **Emergency stop:** one switch (console → Security, or `TVX_EMERGENCY_STOP=on` on the server) stops all use of
  Microsoft access while an incident is handled.

## The most valuable secret

The app's Microsoft credential (`TVX_CLIENT_SECRET` or the certificate) can read every connected tenant until it's
deleted. It lives only in the server's `deploy/.env` (readable by root only) and never in this repository. If it
may have leaked, delete it in Microsoft Entra immediately (App registrations → Certificates & secrets): that cuts
the app off from every customer at once.

## If something happens

Follow [docs/INCIDENT_RESPONSE.md](docs/INCIDENT_RESPONSE.md).

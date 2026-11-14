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
- **Changes need the customer's admin.** One-click fixes use a write permission the app asks for only at that moment
  (it is never in the app registration, so connecting doesn't grant it). The admin approves it on Microsoft's own
  page; the app uses it for that one change and keeps no token, and never asks Microsoft for a refresh token. The
  result page shows the admin how to remove the approval afterwards. Every change is recorded and can be undone.
- **No passwords.** Everyone signs in with Microsoft, so their own MFA and Conditional Access apply.
- **Only findings are stored**, never raw directory data. A company can delete everything with Disconnect.
- **Web protections:** HTTPS only (HSTS), strict Content-Security-Policy, CSRF tokens on every form (Sign out
  included), rate limits on public endpoints keyed on the visitor's real address (forwarded addresses are trusted
  only from the proxy in front), requests over 5 MB refused before they're read, no stack traces shown.
- **Sessions end on the server.** Each sign-in is a row in the database (only a hash of the cookie value is kept),
  valid for 8 hours. Sign out deletes it, so a copied cookie stops working; "Sign out on every device" ends them
  all, and `python -m app.cli sign-out-everyone` ends everyone's.
- **The Tenvaultix console** (`/admin`) answers "not found" to anyone who isn't on the team, asks for a fresh
  Microsoft sign-in every hour, can check the team's account IDs instead of emails (`TVX_OPS_OBJECT_IDS`), logs
  every sign-in, change, export and restore, and records attempts by people who aren't on the team. Once it is
  set up, the server's setup page (`/setup`) is shown to the team only.
- **Emergency stop:** one switch (console → Security, or `TVX_EMERGENCY_STOP=on` on the server) stops all use of
  Microsoft access while an incident is handled.

## The most valuable secret

The app's Microsoft credential (`TVX_CLIENT_SECRET` or the certificate) can read every connected tenant until it's
deleted. It lives only in the server's `deploy/.env` (readable by root only) and never in this repository. If it
may have leaked, delete it in Microsoft Entra immediately (App registrations → Certificates & secrets): that cuts
the app off from every customer at once.

## If something happens

Follow [docs/INCIDENT_RESPONSE.md](docs/INCIDENT_RESPONSE.md).

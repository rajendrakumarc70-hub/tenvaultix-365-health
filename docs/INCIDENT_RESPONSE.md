# If there's a security incident

A plan for the first hours, so nobody has to think it up under pressure. Print it, and fill in the contacts table
now, while things are calm.

## Signs that something is wrong

- The console's **Security** page shows sign-ins, changes, exports or restores nobody on the team made, or
  repeated attempts by people who aren't on the team.
- In the Tenvaultix Entra tenant: a new secret or certificate on the app registration, a new owner on it, or
  sign-ins to your admin accounts from places you don't recognise.
- New commits on GitHub that nobody on the team pushed.
- A customer tells you about something they didn't expect (a change, an email, a sign-in from the app).
- Azure (Defender for Cloud) or Cloudflare alerts, or the server suddenly busy for no reason.
- Someone reports a vulnerability (see SECURITY.md).

Write down the time you first noticed. Some deadlines below count from that moment.

## The first hour

1. **Stop the app using Microsoft access.**
   - Console → **Security** → type `STOP` → **Emergency stop**.
   - If the console itself may be compromised, on the server: add `TVX_EMERGENCY_STOP=on` to
     `/opt/tenvaultix/deploy/.env` and run `sudo bash /opt/tenvaultix/deploy/update.sh`.
   - If the server itself may be compromised: `cd /opt/tenvaultix/deploy && sudo docker compose stop`, or stop
     the VM in the Azure portal.
2. **If the app's Microsoft secret or certificate may have leaked, delete it now.** Entra admin center →
   App registrations → your app → **Certificates & secrets** → delete it. This cuts the app off from every
   customer tenant at once. Tokens already issued can keep working for up to about an hour.
3. **Keep the evidence before you clean anything up.**
   - Azure: VM → Disks → the OS disk → **Create snapshot**.
   - Logs: `cd /opt/tenvaultix/deploy && sudo docker compose logs --no-color > ~/incident-$(date +%F).log`
   - Console → Security: save the page (it lists the activity log), and download a backup from Backup.
   - Entra: export the audit and sign-in logs for the app and your admin accounts.
4. **Write a timeline**: what you noticed, when, and everything you've done since.

## Within 6 hours: report to CERT-In

Companies in India must report listed cyber incidents to CERT-In within 6 hours of noticing them, with whatever
is known so far; details can follow. Use the reporting form or email shown on **cert-in.org.in**. Keep a copy of
what you sent and when.

## Contain and clean up

Change everything the attacker could have seen:

- [ ] The app's Microsoft credential: create a new certificate (preferred) or secret, put it in `deploy/.env`.
- [ ] `TVX_SESSION_SECRET`: a new random value signs everyone out, including the attacker.
- [ ] Passwords and MFA for the Microsoft admin, GitHub and Azure accounts; remove sessions and app passwords
      you don't recognise; check who owns the app registration.
- [ ] GitHub: check recent commits and repository settings; revoke personal access tokens and deploy keys.
- [ ] Server access: replace SSH keys; Cloudflare: rotate the tunnel token.
- [ ] Find how they got in and close it before going live again.

If the server itself was compromised, don't try to clean it: build a new one (docs/DEPLOY_SERVER.md, Part 2),
restore the **last backup from before the incident**, set the new secrets, and delete the old VM once the evidence
is kept.

## Tell the people affected

- **Customers whose data may be involved**: what happened, what data (findings about their tenant: account names
  and emails, settings; never passwords, email content or files), what you've done, and what they can do. Any
  customer can cut the app off from their own tenant at once: Entra → Enterprise applications → the app → Delete.
- **India's DPDP Act**: once its breach rule applies (phased in; currently expected from May 2027), personal data
  breaches must be reported to the Data Protection Board and the people affected without delay, with a detailed
  report within 72 hours. Check the current position with your lawyer.
- **Customers in the EU or UK**: GDPR asks for notice to the regulator within 72 hours; they'll need your help.
- Your **cyber insurance**, if you have it, usually needs to hear quickly too.

A short customer message to adapt:

> On [date] we found [what happened] affecting Tenvaultix 365 Health Check. [What data was involved, or that we
> found no sign customer data was accessed.] We stopped all access to customer tenants at [time], replaced our
> credentials, and [what else]. You don't need to do anything, but if you want to cut our access completely you
> can remove the "Tenvaultix 365 Health Check" app in Entra → Enterprise applications. We'll update you by
> [date]. Questions: [contact].

## Back to normal

1. Check `/setup` (every row "set") and the console's **Security** checks.
2. Console → Errors → **Test the app's Microsoft credential**.
3. Console → Security → type `RESUME` → turn everything back on (or remove `TVX_EMERGENCY_STOP` and run update.sh).
4. Watch the Security activity log and Errors page closely for a few days.
5. Write down what happened, what worked, and what you'll change. Update this plan.

## Contacts (fill in now)

| Who | How to reach them |
|---|---|
| CERT-In | cert-in.org.in (reporting form and email) |
| Your lawyer | |
| Cyber insurance | |
| Azure support | Azure portal → Help + support |
| Microsoft Partner Center support | |
| Cloudflare | dash.cloudflare.com → Support |
| Tenvaultix team member 2 | |

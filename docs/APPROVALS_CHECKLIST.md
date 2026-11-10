# Going live: approvals checklist

Everything Tenvaultix needs before charging real customers for the 365 Health Check, in the order to do it.
"Required" means you can't launch properly without it. "Recommended" builds trust and helps sales.

| # | Approval | Who approves | Needed? | Cost | Typical time |
|---|---|---|---|---|---|
| 1 | Microsoft AI Cloud Partner Program account, company verified | Microsoft (Partner Center) | Required | Free | Usually a few business days |
| 2 | Your company domain verified in the Tenvaultix Microsoft tenant | You (DNS record) | Required | Free | Minutes to a few hours |
| 3 | App registered in Microsoft Entra | You | Required | Free | 20 minutes ([guide](AZURE_APP_REGISTRATION.md)) |
| 4 | Publisher verification (blue "verified" badge) | Microsoft, automatic once 1–3 are done | Required in practice | Free | Minutes |
| 5 | Each customer's admin approves read-only access | The customer's Global Administrator | Required, per customer | Free | 2 minutes, built into the app |
| 6 | Hosting with permanent storage and your own web address | You (Render, Cloudflare or Azure) | Required | Render free for demos; a paid always-on plan for customers (e.g. Render paid instance or Cloudflare Workers Paid $5/month + usage) | 1–2 days of work |
| 7 | Privacy policy, Terms of Service and Data Processing Agreement | Your lawyer | Required | Lawyer's fee | 1–2 weeks |
| 8 | Payment gateway account (Razorpay in India, Stripe abroad) | Razorpay / Stripe (business KYC) | Required to take card payments | Per-transaction fees | A few days |
| 9 | GST registration and invoicing set-up | Your chartered accountant | Required (ask your CA) | CA's fee | Varies |
| 10 | Microsoft 365 Publisher Attestation, then Microsoft 365 Certification | Microsoft | Recommended | Attestation free; certification involves an external assessment | Weeks |
| 11 | Microsoft Marketplace listing (customers pay on their Microsoft bill) | Microsoft (offer review) | Optional sales channel | 3% of sales | Weeks |
| 12 | Security certification such as ISO 27001, plus a penetration test | Certification body / testers | Recommended as you grow | Varies | Months |

Nothing else needs Microsoft's approval. Microsoft Graph doesn't charge for the read-only calls this app makes,
and there is no app store review because it's a website.

---

## 1. Microsoft AI Cloud Partner Program (Partner Center)

This proves to Microsoft that Tenvaultix is a real company. Many Microsoft services companies are already members:
check before signing up.

1. Go to **partner.microsoft.com** and choose to join the **Microsoft AI Cloud Partner Program**. Sign in with a
   work account in the Tenvaultix Microsoft tenant, not a personal Microsoft account.
2. Enter the company's legal details exactly as they appear on registration documents.
3. Use a contact email on **your company domain** (for example `name@yourcompany.com`). This domain must match the
   one you verify in step 2 below.
4. Microsoft checks the company and the email. When it's done, Partner Center shows a **Partner One ID** for your
   **partner global account (PGA)**. Write it down: you need it in step 4.
   Use the global account's ID, not a location ID; location IDs don't work for publisher verification.

## 2. Verify your company domain in the Tenvaultix tenant

If your company email already runs on Microsoft 365, this is probably done: check **Microsoft 365 admin center →
Settings → Domains** for your domain with status *Healthy*. Otherwise:

1. **entra.microsoft.com → Entra ID → Domain names** (or **Custom domain names**) → **Add custom domain**.
2. Enter your company domain and add the TXT record Microsoft shows to your DNS provider.
3. Back in Entra, select **Verify**. It can take a few minutes to a few hours for DNS to update.

## 3. Register the app

Follow [AZURE_APP_REGISTRATION.md](AZURE_APP_REGISTRATION.md).

## 4. Publisher verification

Without it, every customer's admin sees **"Unverified"** on Microsoft's approval screen, and many organizations
block unverified apps. Microsoft's requirements:

- Partner One ID from step 1, for a **verified** partner global account.
- The app is registered with a work account in the tenant linked to that partner account.
- The app's **publisher domain** is set, and it, or a DNS-verified custom domain in the tenant, matches the email
  domain used for partner verification. It can't be `onmicrosoft.com`.
- The person doing it signs in with MFA and has **Application Administrator** or **Cloud Application Administrator**
  in Entra **and** **Partner Admin** or **Accounts Admin** in Partner Center.

Then: **App registrations → Tenvaultix 365 Health Check → Branding & properties → Add Partner ID to verify
publisher** → enter the Partner One ID → **Verify and save**. A blue verified badge appears next to the publisher
name.

## 5. Customer approval (built in)

Each customer connects from the app's **Connect** page. Their Global Administrator (or Privileged Role
Administrator) approves the read-only permissions on Microsoft's own screen. The app checks that the person is
really an admin before saving anything. Customers can remove access any time in Entra → Enterprise applications.

## 6. Production hosting

Before real customers, change these from the dev set-up (details in the README):

- **Permanent storage.** Today the database lives on the container's temporary disk and is wiped when the
  container sleeps or is redeployed. Move it to Cloudflare D1 or PostgreSQL.
- **Your own address**, for example `health.yourcompany.com`, added as a Custom Domain on the Worker. Then add its
  two redirect addresses to the app registration.
- **Fixed session secret:** add `TVX_SESSION_SECRET` as a Cloudflare secret.
- **Certificate instead of client secret** for the Microsoft credential (see the registration guide).
- **Scheduled scans:** add a Cron Trigger that runs the weekly and daily scans.
- **Backups and monitoring:** daily database backups and an uptime alert on `/healthz`.

## 7. Legal documents

Have a lawyer prepare or review:

- **Privacy policy:** the app's `/privacy` page is a plain-English starting point. Cover India's Digital Personal
  Data Protection Act 2023 and its Rules, and the UK and EU GDPR if you sell there.
- **Terms of Service:** subscription terms, trial, cancellation, limits of liability. Add a `/terms` page and put
  its address in the app registration's branding.
- **Data Processing Agreement (DPA):** business customers will ask for one, because the reports contain staff
  names and email addresses.

## 8–9. Payments and tax

- **Razorpay** (India) and/or **Stripe** (international) need business verification (KYC) before you can take
  payments. Subscriptions (₹1,999 / ₹4,999, $39 / $79) map to their subscription plans.
- Ask your chartered accountant about **GST registration**, invoice format, and treatment of foreign customers.

## 10–12. Trust programmes (after launch)

- **Microsoft 365 Publisher Attestation:** a free self-assessment questionnaire, the step before Microsoft 365
  Certification. It shows customers how the app handles their data.
- **Microsoft Marketplace:** list a SaaS offer so customers can buy on their existing Microsoft bill.
- **ISO 27001 or SOC 2** and a penetration test once larger customers ask. Your app can read many customers'
  tenants, so keep its credential in a key vault and rotate it.

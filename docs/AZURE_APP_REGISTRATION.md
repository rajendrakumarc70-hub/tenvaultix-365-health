# Register the app in Microsoft Entra (Azure)

This connects the 365 Health Check to Microsoft. Do it once, in **the Tenvaultix Microsoft tenant** (your own
company's Microsoft 365), not in a customer's. It takes about 20 minutes.

**You need:** a work account in the Tenvaultix tenant with at least the **Application Developer** role (a Global
Administrator also works), and the site address. For the dev site that's
`https://tenvaultix-365-health-1.onrender.com`. Later you'll add your own domain the same
way.

---

## Step 1: Create the registration

1. Go to **entra.microsoft.com** and sign in.
2. If you belong to more than one tenant, use the **Settings** (gear) icon to switch to the Tenvaultix tenant.
3. Go to **Entra ID → App registrations → New registration**.
4. **Name:** `Tenvaultix 365 Health Check`. Customers see this name on Microsoft's approval screen.
5. **Supported account types:** choose **Multiple Entra ID tenants**. Older screens call it *"Accounts in any
   organizational directory (Any Microsoft Entra ID tenant - Multitenant)"*. Don't choose the option that also
   includes personal Microsoft accounts.
6. **Redirect URI:** platform **Web**, address:
   `https://tenvaultix-365-health-1.onrender.com/auth/callback`
7. Select **Register**.
8. On the **Overview** page, copy the **Application (client) ID**. You need it in step 6.

## Step 2: Add the second redirect address

1. In the app, open **Authentication** (on some screens **Authentication → Redirect URI configuration**).
2. Under **Web → Redirect URIs**, select **Add URI** and enter:
   `https://tenvaultix-365-health-1.onrender.com/connect/callback`
3. Leave **Access tokens** and **ID tokens** (implicit grant) **unticked**. The app doesn't use them.
4. Select **Save**.

You should now have exactly these two Web redirect URIs:

| Redirect URI | Used for |
|---|---|
| `…onrender.com/auth/callback` | Signing in with Microsoft |
| `…onrender.com/connect/callback` | A customer's admin approving access |

The address must match exactly: `https`, no trailing slash. A mismatch gives error **AADSTS50011**.

## Step 3: Add the API permissions

1. Open **API permissions**. **Microsoft Graph → User.Read (Delegated)** is already there; keep it.
2. Select **Add a permission → Microsoft Graph → Application permissions**.
3. Search for and tick each of these ten. All are read-only:

   | Permission | Why the app needs it |
   |---|---|
   | `Directory.Read.All` | Users, admin roles, licenses and company domains |
   | `AuditLog.Read.All` | Last sign-in dates and MFA registration (Business Premium tenants) |
   | `UserAuthenticationMethod.Read.All` | Which sign-in methods (Authenticator, phone, passkey) each person has |
   | `Policy.Read.All` | Security defaults, Conditional Access, app-consent and guest-invite settings |
   | `RoleManagement.Read.Directory` | Who holds admin roles |
   | `Reports.Read.All` | Usage reports, to find unused licenses |
   | `SecurityEvents.Read.All` | Microsoft Secure Score |
   | `MailboxSettings.Read` | Inbox rules that forward email outside (never reads email content) |
   | `SharePointTenantSettings.Read.All` | SharePoint and OneDrive sharing settings |
   | `DeviceManagementManagedDevices.Read.All` | Intune device compliance |

4. Select **Add permissions**.
5. **Don't** select "Grant admin consent" here just to set things up: each customer grants consent for their own
   company from the app's Connect page. If you want to scan the Tenvaultix tenant itself, simply connect it
   through the app like a customer would.

### Optional: permissions for one-click fixes

One-click fixes let a customer's admin make a few safe changes from the report (stop staff approving outside
apps, only admins register apps, stop guests inviting guests, turn off "Anyone" links). They use **delegated**
permissions: the admin approves one permission on Microsoft's screen when they press **Fix it for me**, and the
change is made with their own admin rights. The app doesn't keep their sign-in afterwards.

1. **Add a permission → Microsoft Graph → Delegated permissions**, and tick:

   | Permission | Used by |
   |---|---|
   | `Policy.ReadWrite.Authorization` | App approvals, app registration, guest invitations |
   | `SharePointTenantSettings.ReadWrite.All` | Turning off "Anyone" links |

2. **Don't** grant admin consent for these. Each admin approves them only when they use a fix.

No extra redirect address is needed: fixes come back to `/auth/callback`.

## Step 4: Create the app's credential

The app proves who it is to Microsoft with either a **client secret** (quick, fine for testing) or a
**certificate** (what Microsoft recommends for production). Start with a secret, then switch to a certificate
before real customers.

### Option A: client secret (testing)

1. **Certificates & secrets → Client secrets → New client secret**.
2. Description `dev`, expiry **180 days**. Microsoft recommends less than 12 months; the maximum
   is 24.
3. Select **Add**, then copy the **Value** column straight away. It's shown only once. Don't copy the
   *Secret ID*: using it gives error **AADSTS7000215**.
4. Put a reminder in your calendar to replace it before it expires.

### Option B: certificate (production)

On a computer with OpenSSL (Termux works too: `pkg install openssl-tool`):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout health-check.key -out health-check.crt -subj "/CN=Tenvaultix 365 Health Check"
openssl x509 -in health-check.crt -noout -fingerprint -sha1
```

1. **Certificates & secrets → Certificates → Upload certificate** and upload **`health-check.crt`** (the public
   part). Entra shows its **thumbprint**, which matches the fingerprint printed above (without colons).
2. Keep **`health-check.key`** private. It goes into the app's hosting settings as a secret in step 6 and nowhere else. Don't
   email it or commit it to GitHub.
3. For a long-term set-up, create and renew the certificate in **Azure Key Vault** instead. Microsoft recommends
   a certificate from a trusted certificate authority for production.

## Step 5: Branding and publisher

1. Open **Branding & properties**.
2. **Logo:** upload `entra-logo-215.png` from the package (a 215 × 215 Tenvaultix shield).
3. **Home page URL:** the site address.
4. **Privacy statement URL:** `https://…onrender.com/privacy` (later your own domain).
5. **Terms of service URL:** add once your lawyer has written the Terms page.
6. **Publisher domain:** choose your company domain, which must already be verified in the tenant
   ([approvals checklist](APPROVALS_CHECKLIST.md), step 2). It can't be `onmicrosoft.com`.
7. **Add Partner ID to verify publisher:** enter your Partner One ID (approvals checklist, step 1) → **Verify and
   save**. You need MFA, **Application Administrator** or **Cloud Application Administrator** in Entra, and
   **Partner Admin** or **Accounts Admin** in Partner Center. A blue *verified* badge appears when it works.
   You can do this later; until then customers see "Unverified".
8. **Owners:** add a second Tenvaultix admin as an owner, so you're not locked out if one person leaves.

## Step 6: Give the values to the app (Render)

In **Render → tenvaultix-365-health → Environment → Add Environment Variable**, add each of these. Render keeps
them private and passes them to the app. (On Cloudflare instead: **Workers & Pages → tenvaultix-365-health →
Settings → Variables and Secrets**, type **Secret**.)

| Name | Value |
|---|---|
| `TVX_CLIENT_ID` | Application (client) ID from step 1 |
| `TVX_CLIENT_SECRET` | Secret **value** from step 4A (skip if you use a certificate) |
| `TVX_CERT_THUMBPRINT` | Certificate thumbprint from step 4B (certificate only) |
| `TVX_CERT_PRIVATE_KEY` | Entire contents of `health-check.key`, including the BEGIN/END lines (certificate only) |
| `TVX_SESSION_SECRET` | A long random string. On Render, tap **Generate** |
| `TVX_BASE_URL` | `https://tenvaultix-365-health-1.onrender.com` (no slash at the end) |

Saving environment variables on Render restarts the app with the new values. (On Cloudflare, `TVX_BASE_URL` is
set in `wrangler.jsonc`, and a fresh deploy picks up new secrets.)

## Step 7: Test it

1. Open the site and select **Start free trial → Sign in with Microsoft**. Sign in with your Tenvaultix account.
2. Select **Approve read-only access**. Microsoft's screen lists the ten permissions. As a Global Administrator,
   tick consent for your organization and **Accept**.
3. You return to the dashboard and the first scan starts. Refresh after a minute or two to see the report.
4. Check that **/setup** on the site says Microsoft sign-in is **configured**.

## When something goes wrong

| Message | Meaning | Fix |
|---|---|---|
| AADSTS50011 (redirect URI mismatch) | The address isn't registered exactly | Add the exact URI from step 2 |
| AADSTS700016 (application not found) | Wrong client ID, or the registration isn't multitenant | Check `TVX_CLIENT_ID` and step 1.5 |
| AADSTS7000215 (invalid client secret) | Secret ID used instead of value, or expired | Create a new secret, copy the **Value** |
| AADSTS90094 / "Need admin approval" | The person isn't an admin | A Global Administrator must connect |
| "Unverified" on the approval screen | Publisher verification not done yet | Step 5.7 |
| Sign-in page says "isn't set up yet" | The app can't see the values from step 6 | Step 6, then check **/setup** |
| "Almost there" keeps coming back, then "Not connected yet" | The app's token has no application permissions | Step 3: every Graph permission except User.Read must be type **Application** with status **Granted**. Press **Grant admin consent**, then **Try again** |

Tip: every one of these errors is also recorded. Open **/setup** on the site to see recent ones with the fix, or
set `TVX_OPS_EMAILS` to your work email and `TVX_OPS_TENANT_ID` to the **Directory (tenant) ID**
(on the app registration's Overview page), then open **/ops** for the full list and a **Test the app's Microsoft
credential** button.

## Optional: Azure spend

Nothing to add in the app registration. Each customer who wants to see Azure costs gives the app read-only
access to costs in **their** Azure:

1. Azure portal → **Subscriptions** → pick one (or **Management groups → Tenant Root Group** for all of them).
2. **Access control (IAM) → Add → Add role assignment**.
3. Role **Cost Management Reader** → **Next** → **Select members** → search *Tenvaultix 365 Health Check* →
   **Select** → **Review + assign**.
4. Wait a few minutes (up to an hour), then open **Azure spend** on the dashboard and press **Refresh**.

The page shows what Microsoft has: costs usually appear 8–24 hours after use (up to 72 hours on
pay-as-you-go). Companies that buy Azure through a CSP partner see costs only if the partner allows it.
Test it on your own tenant first: assign the role on one of your subscriptions, connect, and open Azure spend.

## Later: your own domain

When the site moves to, for example, `https://health.yourcompany.com`:

1. Add the domain in Render (**Settings → Custom Domains → Add**) and create the DNS record Render shows.
2. Add `https://health.yourcompany.com/auth/callback` and `/connect/callback` to step 2's redirect URIs.
3. Change the `TVX_BASE_URL` environment variable to the new address.
4. Update the home page and privacy URLs in step 5.

# Run Tenvaultix 365 Health Check on your own server (Azure VM or Hyper-V)

One small Linux server runs everything: the website, the console, automatic weekly and daily scans, HTTPS and
nightly database backups. The steps are the same on an **Azure virtual machine** and a **Hyper-V virtual
machine** in your office; only Part 1 differs.

| | Azure VM | Hyper-V VM (office server) |
|---|---|---|
| Good for | Real customers | Testing, or if you already run a reliable server |
| Uptime, power, internet | Microsoft's data centre | Yours |
| Public address | Fixed public IP from Azure | Cloudflare Tunnel (no ports opened) or router port forwarding |
| Backups off the server | Azure Backup (a few clicks) | Copy `deploy/backups` elsewhere yourself |
| Cost | A small monthly VM charge (see the Azure pricing calculator; check your partner benefits for Azure credits) | Free if you have the hardware |

> Why not Azure App Service? Its storage is a network share, and Microsoft says file databases like SQLite
> can't be used on it. App Service needs the app to move to PostgreSQL first. A VM with its own disk runs the
> app exactly as it is today.

You need: the domain name customers will use (for example `health.tenvaultix.com`), access to that domain's
DNS, and the values you set on Render (client ID, client secret, console emails and tenant ID).

---

## Part 1A: Create the Azure VM

1. Sign in to **portal.azure.com** → **Create a resource** → **Virtual machine** → **Create**.
2. **Basics** tab:
   - Resource group: **Create new** → `tenvaultix-prod`
   - Virtual machine name: `tvx-app-01`
   - Region: **(Asia Pacific) Central India** (customer data stays in India)
   - Image: **Ubuntu Server 24.04 LTS - x64 Gen2**
   - Size: **B1ms** (1 vCPU, 2 GB memory). On a new Azure free account, **B1s** may be free for 12 months and also works.
   - Authentication type: **SSH public key**, username `azureuser`, **Generate new key pair**, key pair name `tvx-app-01_key`
   - Public inbound ports: **Allow selected ports** → tick **HTTP (80)**, **HTTPS (443)**, **SSH (22)**
3. **Disks** tab: OS disk type **Standard SSD**.
4. **Review + create** → **Create** → **Download private key and create resource**. Keep the `.pem` file safe:
   it's the only way in.
5. When it finishes, open the VM and copy its **Public IP address**. Azure keeps this address fixed.
6. Recommended: VM → **Backup** → enable it (creates a Recovery Services vault, daily backup policy).
7. **DNS**: wherever your domain's DNS is managed, add an **A record**: name `health`, value = the VM's public
   IP. If the domain is on Cloudflare, set it to **DNS only** (grey cloud) so the server can get its own
   HTTPS certificate.
8. **Connect from Termux** (Android) or any terminal:
   ```bash
   termux-setup-storage                      # Termux only, once: lets it read Downloads
   cp /sdcard/Download/tvx-app-01_key.pem ~/ && chmod 600 ~/tvx-app-01_key.pem
   ssh -i ~/tvx-app-01_key.pem azureuser@<public-ip>
   ```

Then go to **Part 2**.

## Part 1B: Create the Hyper-V VM

1. Download **Ubuntu Server 24.04 LTS** (ISO) from ubuntu.com.
2. **Hyper-V Manager** → **New** → **Virtual Machine**:
   - Generation **2**, memory **2048 MB** (untick dynamic memory or set its minimum to 2048)
   - Network: an **External** virtual switch (so the VM is on your office network)
   - New virtual hard disk: **40 GB**; install from the Ubuntu ISO
3. Before starting it: VM **Settings** → **Security** → Secure Boot template **Microsoft UEFI Certificate
   Authority** (Ubuntu won't boot with the default Windows template).
4. Start the VM and install Ubuntu. Tick **Install OpenSSH server** when asked.
5. On your router, give the VM a **fixed IP** (DHCP reservation), then connect: `ssh <user>@<vm-ip>`.
6. Choose how the internet reaches it:
   - **Cloudflare Tunnel (recommended for an office server)**: needs your domain's DNS on Cloudflare. In
     Cloudflare **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel** → **Cloudflared** → name it
     `tenvaultix` → copy the **token** shown in the install command (the long text after `--token`). Then
     **Public hostname**: subdomain `health`, domain `tenvaultix.com`, service **HTTP**, URL `app:8000`.
     Nothing needs opening on your router. In Part 2 set `COMPOSE_PROFILES=tunnel` and paste the token.
   - **Port forwarding**: needs a fixed public IP from your internet provider. Forward ports 80 and 443 on the
     router to the VM, and add the DNS A record `health` → your public IP. In Part 2 keep `COMPOSE_PROFILES=caddy`.

---

## Part 2: Install the app (same on Azure and Hyper-V)

On the server:

```bash
curl -fsSL https://raw.githubusercontent.com/rajendrakumarc70-hub/tenvaultix-365-health/main/deploy/install.sh -o install.sh
bash install.sh
```

This installs Docker and git, downloads the app to `/opt/tenvaultix`, creates `/opt/tenvaultix/deploy/.env`
with a random session secret, and schedules a nightly backup.

Fill in the settings:

```bash
sudo nano /opt/tenvaultix/deploy/.env
```

| Setting | Value |
|---|---|
| `COMPOSE_PROFILES` | `caddy` (Azure VM, or port forwarding) or `tunnel` (Cloudflare Tunnel) |
| `TVX_DOMAIN` | `health.tenvaultix.com` (no `https://`) |
| `TUNNEL_TOKEN` | Only for `tunnel`: the token from Cloudflare |
| `TVX_CLIENT_ID` | Application (client) ID of the app registration |
| `TVX_CLIENT_SECRET` | The secret's **Value** (not the Secret ID) |
| `TVX_OPS_EMAILS` | `rajendra@tenvaultix.com` |
| `TVX_OPS_TENANT_ID` | Directory (tenant) ID |
| `TVX_CONTACT_EMAIL` / `TVX_BOOKING_URL` | Where customers contact you |

Save (Ctrl+O, Enter, Ctrl+X), then start it:

```bash
sudo bash /opt/tenvaultix/deploy/update.sh
```

The first start takes a few minutes. Automatic scans are switched on; you don't need a cron job for them.

## Part 3: Tell Microsoft about the new address

Microsoft Entra admin center → **App registrations** → your app → **Authentication** → under **Web** redirect
URIs, **add** (keep the Render ones until you switch over):

- `https://health.tenvaultix.com/auth/callback`
- `https://health.tenvaultix.com/connect/callback`

Save.

## Part 4: Check it

Open `https://health.tenvaultix.com/setup`. Every row should say **set**, `TVX_SCHEDULER` should say **on**,
and **Running version** should match the latest commit on GitHub. Then sign in, open `/admin`, and connect your
own tenant from `/connect`.

**Moving your console data from Render:** on the Render site open **/admin/backup** → **Download backup**; on
the new server open **/admin/backup** → choose the file → type `RESTORE` → **Restore from file**. Companies
connected on Render need to connect again on the new address. Render's free disk loses them anyway.

When everything works, point customers at the new address, remove the Render redirect URIs from the app
registration, and suspend the Render service.

---

## Everyday tasks

**Update to the latest version** (after changes are pushed to GitHub):
```bash
sudo bash /opt/tenvaultix/deploy/update.sh
```
Customers, licences, prices and reports are kept: they live in a Docker volume, not in the app's files.

**Back up now** (also runs every night at 02:30, keeps 14 days):
```bash
sudo bash /opt/tenvaultix/deploy/backup.sh
```
Backups are in `/opt/tenvaultix/deploy/backups`. On Azure, VM backup copies them off the server; on Hyper-V,
copy that folder to another machine or include the VM in your backups.

**Restore a backup** (replaces everything in the current database):
```bash
cd /opt/tenvaultix/deploy
sudo docker compose exec -T app python -m app.cli restore - --yes < backups/health-YYYY-MM-DD-HHMM.db
sudo docker compose restart app
```

**See what's happening:**
```bash
cd /opt/tenvaultix/deploy
sudo docker compose ps               # app should be "healthy"
sudo docker compose logs -f app      # live log (Ctrl+C to stop)
```
The console's **Errors** page (`/admin/errors`) shows failures in plain English.

**Change a setting:** `sudo nano /opt/tenvaultix/deploy/.env`, then `sudo bash /opt/tenvaultix/deploy/update.sh`.

## Keeping it safe

- Ubuntu installs security updates automatically. Reboot now and then: `sudo reboot` (the app starts by itself).
- On Azure, limit SSH to your own IP: VM → **Networking** → the SSH rule → **Source**: *My IP address*.
- `deploy/.env` holds the app's Microsoft secret: it's readable only by root and never goes to GitHub.
- Backups contain customer details: keep copies somewhere private.
- Prefer a certificate over a client secret for production (see `AZURE_APP_REGISTRATION.md`).

## When something goes wrong

| What you see | What to do |
|---|---|
| Browser can't reach the site | Azure: check ports 80/443 are allowed on the VM's network settings, and the DNS A record points at the public IP (`nslookup health.tenvaultix.com`). Tunnel: check the tunnel shows **Healthy** in Cloudflare. |
| Certificate warning | Caddy needs ports 80 and 443 reachable and DNS pointing at the server. On Cloudflare DNS, use **DNS only**. Look at `sudo docker compose logs caddy`. |
| `update.sh` says to fill in settings | Edit `deploy/.env`: `TVX_DOMAIN` and `TVX_CLIENT_ID` can't be empty. |
| Sign-in fails with AADSTS50011 | Part 3: the redirect URIs for the new address are missing. |
| Anything else | `/setup` and `/admin/errors` explain most problems; `sudo docker compose logs app` shows the rest. |

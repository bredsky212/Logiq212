# Windows Server VPS Deployment Guide (Secure)

This guide is for server administrators deploying **Logiq212** on a **Windows Server VPS** (2019/2022/2025), with either **MongoDB Atlas** (recommended) or a **self-hosted MongoDB** on Windows Server. It also covers Discord Developer Portal setup, secure runtime configuration, and the **Phase 3 security bootstrap** (`/perms security-bootstrap`).

---

## 0) Security Checklist (Do This First)

* Run the bot as a **dedicated, non-admin Windows user** (e.g. `logiq`).
* Keep secrets **out of git**: never commit `DISCORD_BOT_TOKEN` or `MONGODB_URI`.
* Prefer **service-scoped environment variables** (via NSSM) or a **locked-down secret file** (ACL-restricted) over anything in the repo.
* If using MongoDB Atlas: restrict access with **IP allow-list** (or private networking) and a least-privilege DB user.
* If self-hosting MongoDB: bind to **localhost**, enable **authorization**, and do not expose `27017` publicly.
* Use Windows Defender Firewall; keep the OS patched.

---

## 1) Prepare the Windows Server

### 1.1 Patch the OS

* Run **Windows Update** and reboot until fully up to date.

### 1.2 Install prerequisites (Python + Git)

You can install Python from **python.org**. ([Python.org][1])
Or use **WinGet** if available on your Server version. ([Microsoft Learn][2])

**Option A — Python.org installer (simple & predictable)**

1. Download and install Python (64-bit).
2. During install, check:

   * **Add python.exe to PATH**
   * **pip**

**Option B — WinGet**

```powershell
winget --version
winget install --id Git.Git -e
# Pick a Python version you standardize on (example):
winget install --id Python.Python.3.11 -e
```

(WinGet overview and usage) ([Microsoft Learn][2])

### 1.3 Create a dedicated service account (local user)

Open **PowerShell as Administrator**:

```powershell
# Create local user
$pw = Read-Host "Enter a strong password for logiq" -AsSecureString
New-LocalUser -Name "logiq" -Password $pw -PasswordNeverExpires:$true -UserMayNotChangePassword:$true
# Optional: add a description
Set-LocalUser -Name "logiq" -Description "Logiq212 service account (non-admin)"
```

### 1.4 Create the install directory + lock permissions

We’ll use `C:\opt\logiq\app`.

```powershell
New-Item -ItemType Directory -Force -Path C:\opt\logiq\app | Out-Null
New-Item -ItemType Directory -Force -Path C:\opt\logiq\logs | Out-Null

# Give ownership/rights to the service user (and keep Administrators with full control)
icacls C:\opt\logiq /inheritance:r
icacls C:\opt\logiq /grant "Administrators:(OI)(CI)F"
icacls C:\opt\logiq /grant "logiq:(OI)(CI)M"
```

### 1.5 Allow the account “Log on as a service”

You must grant **Log on as a service** to the `logiq` user (Local Security Policy). Microsoft’s steps: ([Microsoft Learn][3])

* Run: `secpol.msc`
* Local Policies → User Rights Assignment → **Log on as a service**
* Add `.\logiq`

### 1.6 Firewall (Windows Defender Firewall)

The bot typically needs **outbound** access only. You **don’t need** to open inbound ports unless you later enable a dashboard/reverse proxy.

If you later deploy a web dashboard behind TLS, create inbound rules with PowerShell (`New-NetFirewallRule`). ([Microsoft Learn][4])

Example (only if needed later):

```powershell
New-NetFirewallRule -DisplayName "Allow HTTP 80"  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80
New-NetFirewallRule -DisplayName "Allow HTTPS 443" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 443
```

---

## 2) MongoDB (Choose One)

### Option A: MongoDB Atlas (Recommended)

Same idea as your Debian guide:

1. Create a cluster in MongoDB Atlas.
2. Create a DB user with least privilege (e.g., `readWrite` on DB `Logiq`).
3. **Network Access**: add your VPS public IP to the allow-list (avoid `0.0.0.0/0`).
4. Store the SRV connection string in an environment variable:

   ```text
   mongodb+srv://<user>:<password>@<cluster-host>/Logiq?retryWrites=true&w=majority
   ```

### Option B: Self-host MongoDB on Windows Server (Local Only)

MongoDB has an official Windows install guide (MSI + unattended installs). ([MongoDB][5])

#### 2.1 Install MongoDB Community Edition (MSI)

* Install MongoDB Community Edition for Windows using the MSI wizard (typical choice).
* Ensure MongoDB is installed **as a Windows service** (the MSI offers this).

#### 2.2 Create users (before enabling authorization)

Open a terminal as Administrator:

```powershell
mongosh
```

In the Mongo shell:

```javascript
use admin
db.createUser({
  user: "admin",
  pwd: "<STRONG_PASSWORD>",
  roles: [ { role: "userAdminAnyDatabase", db: "admin" } ]
})

use Logiq
db.createUser({
  user: "logiq",
  pwd: "<STRONG_PASSWORD>",
  roles: [ { role: "readWrite", db: "Logiq" } ]
})
```

#### 2.3 Enable authorization + keep MongoDB bound to localhost

MongoDB supports configuration via `mongod.cfg` / YAML config file. ([MongoDB][6])

Locate your config file (MSI commonly provides one; paths vary by version/install). Then ensure:

```yaml
net:
  bindIp: 127.0.0.1
security:
  authorization: enabled
```

Restart the MongoDB service:

* `services.msc` → MongoDB Server → Restart
  (or use PowerShell `Restart-Service` if service name is known)

Your `MONGODB_URI` becomes:

```text
mongodb://logiq:<password>@127.0.0.1:27017/Logiq?authSource=Logiq
```

---

## 3) Install the Bot on the VPS

### 3.1 Clone the repo as the service user

Either log in as `logiq`, or run a shell “as user”.

Example (from an elevated PowerShell, you can just do it normally and keep ACLs correct):

```powershell
cd C:\opt\logiq\app
git clone <YOUR_REPO_URL> .
```

### 3.2 Create a virtual environment + install dependencies

```powershell
cd C:\opt\logiq\app

# Create venv
python -m venv venv

# Upgrade pip
.\venv\Scripts\python.exe -m pip install --upgrade pip

# Install requirements
.\venv\Scripts\pip.exe install -r requirements.txt
```

### 3.3 Use the minimal (recommended) configuration

```powershell
Copy-Item .\config.minimal.example.yaml .\config.yaml -Force
```

Keep `web.enabled: false` unless you deploy a web dashboard behind TLS.

---

## 4) Discord Developer Portal Setup

1. Create an application + bot and copy the **Bot Token**.
2. Enable privileged intents (required by this codebase):

   * **Guild Members (Server Members Intent)**
   * **Message Content Intent**
   * **Guild Presences (Presence Intent)**
     Discord’s privileged intents docs + FAQ: ([Support Dev Discord][7])
     (discord.py also explains how privileged intents work from the library perspective) ([discord.py][8])
3. Invite with scopes: `bot`, `applications.commands`, and the required permissions.

---

## 5) Secure Runtime Secrets (Windows approach)

### Recommended: service-scoped environment variables (via NSSM)

NSSM supports setting environment variables specifically for a service (including `AppEnvironmentExtra`). ([NSSM][9])

You’ll set:

* `DISCORD_BOT_TOKEN`
* `MONGODB_URI`

(Details in the service section below.)

---

## 6) Run as a Windows Service (Recommended: NSSM)

### 6.1 Install NSSM

Download and extract NSSM from the official site. ([NSSM][10])

Example layout:

* `C:\tools\nssm\nssm.exe`

### 6.2 Create the service

Open **PowerShell as Administrator**:

```powershell
C:\tools\nssm\nssm.exe install logiq212 C:\opt\logiq\app\venv\Scripts\python.exe C:\opt\logiq\app\main.py
```

Then configure the service (NSSM GUI will open, or you can set values by command):

**Set the working directory**

```powershell
C:\tools\nssm\nssm.exe set logiq212 AppDirectory C:\opt\logiq\app
```

**Set service environment variables (secrets)**

```powershell
C:\tools\nssm\nssm.exe set logiq212 AppEnvironmentExtra `
  DISCORD_BOT_TOKEN=YOUR_DISCORD_TOKEN_HERE `
  MONGODB_URI=YOUR_MONGODB_URI_HERE
```

(NSSM environment variable behavior) ([NSSM][9])

**Run service as the dedicated user**
In NSSM GUI:

* “Log on” tab → This account: `.\logiq` + password
  (Ensure you granted “Log on as a service” earlier.) ([Microsoft Learn][3])

**Optional: log redirection**
You can configure I/O redirection in NSSM to write stdout/stderr to:

* `C:\opt\logiq\logs\logiq212.out.log`
* `C:\opt\logiq\logs\logiq212.err.log`

### 6.3 Start + verify

```powershell
Start-Service logiq212
Get-Service logiq212
```

Check logs:

* If using NSSM I/O redirection → open the log files in `C:\opt\logiq\logs\`
* Or use Windows Event Viewer (System/Application) depending on your configuration

---

## 7) First-Time Bootstrap in Discord (Important)

### 7.1 Sync slash commands

* `/sync` (admin-only)

### 7.2 Configure logging channels

* `/setlogchannel`
* `/setlogchannel-advanced` for:

  * `reports`, `moderation`, `vcmod`, `tickets`, `feature_permissions`, `default`

### 7.3 Run security bootstrap (Phase 3)

Sensitive features are locked by default until an admin acknowledges protected roles:

1. `/perms security-bootstrap`
2. `/perms security-protected-list`
3. Adjust if needed:

   * `/perms security-protected-add role:@Role`
   * `/perms security-protected-remove role:@Role` (keep at least one protected role)

### 7.4 Configure feature permissions

* `/perms feature-list`
* `/perms feature-allow feature:<key> role:@Role`
* `/perms feature-deny feature:<key> role:@Role`
* `/perms feature-clear feature:<key> role:@Role`
* `/perms feature-reset feature:<key>`

---

## 8) Operations (Updates & Safety)

### Update the bot

```powershell
Stop-Service logiq212

cd C:\opt\logiq\app
git pull
.\venv\Scripts\pip.exe install -r requirements.txt

Start-Service logiq212
```

### Rotate secrets

* Rotate `DISCORD_BOT_TOKEN` if it ever leaks.
* Rotate MongoDB credentials periodically.

### Backups (MongoDB)

* Atlas: use Atlas backups/snapshots.
* Self-hosted: schedule backups (e.g., `mongodump`) and store archives off-host.

---

## Appendix: Alternative to NSSM (Not recommended, but works)

You *can* use **Task Scheduler** to run at startup, but services are usually more reliable for long-running bots. (General comparison discussion) ([serverfault.com][11])

[1]: https://www.python.org/downloads/?utm_source=chatgpt.com "Download Python"
[2]: https://learn.microsoft.com/en-us/windows/package-manager/winget/?utm_source=chatgpt.com "Use WinGet to install and manage applications"
[3]: https://learn.microsoft.com/en-us/system-center/scom/enable-service-logon?view=sc-om-2025&utm_source=chatgpt.com "Enable Service Log on for run as accounts"
[4]: https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule?view=windowsserver2025-ps&utm_source=chatgpt.com "New-NetFirewallRule (NetSecurity)"
[5]: https://www.mongodb.com/docs/v7.0/tutorial/install-mongodb-on-windows/?utm_source=chatgpt.com "Install MongoDB Community Edition on Windows"
[6]: https://www.mongodb.com/docs/manual/reference/configuration-options/?utm_source=chatgpt.com "Self-Managed Configuration File Options - Database Manual"
[7]: https://support-dev.discord.com/hc/en-us/articles/6207308062871-What-are-Privileged-Intents?utm_source=chatgpt.com "What are Privileged Intents?"
[8]: https://discordpy.readthedocs.io/en/latest/intents.html?utm_source=chatgpt.com "A Primer to Gateway Intents - Discord.py - Read the Docs"
[9]: https://nssm.cc/usage?utm_source=chatgpt.com "NSSM - the Non-Sucking Service Manager"
[10]: https://nssm.cc/?utm_source=chatgpt.com "NSSM - the Non-Sucking Service Manager"
[11]: https://serverfault.com/questions/901388/windows-service-vs-task-scheduler-with-startup-trigger?utm_source=chatgpt.com "Windows Service vs. Task Scheduler with startup trigger"

# Debian 12 VPS Deployment Guide (Secure)

This guide is for server administrators deploying **Logiq212** on a Debian 12 VPS, with either **MongoDB Atlas** (recommended) or a **self-hosted MongoDB** on Debian 12. It also covers Discord Developer Portal setup, secure runtime configuration, and the **Phase 3 security bootstrap** (`/perms security-bootstrap`).

## 0) Security Checklist (Do This First)

- Run the bot as a **dedicated non-root user** (e.g. `logiq`).
- Keep secrets **out of git**: never commit `DISCORD_BOT_TOKEN` or `MONGODB_URI`.
- Prefer a root-owned `EnvironmentFile` for systemd (chmod `600`) over `.env` in the repo.
- If using MongoDB Atlas: restrict access with **IP allow-list** (or private networking) and a least-privilege DB user.
- If self-hosting MongoDB: bind to **localhost**, enable **authorization**, and do not expose `27017` publicly.
- Use a firewall (UFW/nftables) and keep the OS patched.

## 1) Prepare the VPS

### 1.1 Update packages

```bash
sudo apt update
sudo apt -y upgrade
sudo apt -y install ca-certificates curl git
```

### 1.2 Create a dedicated service user

```bash
sudo adduser --system --group --home /opt/logiq --shell /usr/sbin/nologin logiq
sudo mkdir -p /opt/logiq/app
sudo chown -R logiq:logiq /opt/logiq
```

### 1.3 Firewall (UFW example)

```bash
sudo apt -y install ufw
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

If you later enable the web dashboard, also allow `80`/`443` (and terminate TLS via a reverse proxy).

### 1.4 Install Python + build dependencies

Debian 12 ships Python 3.11. Install venv + common build deps:

```bash
sudo apt -y install python3 python3-venv python3-pip build-essential python3-dev libffi-dev libsodium-dev
```

## 2) MongoDB (Choose One)

### Option A: MongoDB Atlas (Recommended)

1. Create a cluster in MongoDB Atlas.
2. **Database Access**: create a DB user with least privilege:
   - Role: `readWrite` on your bot DB (default in this repo: `Logiq`).
3. **Network Access**:
   - Prefer Private Endpoint/VPC peering when available.
   - Otherwise, add your VPS public IP to the allow-list (avoid `0.0.0.0/0`).
4. Copy the connection string (SRV):
   - Example (store in an env var, not in git):
     ```text
     mongodb+srv://<user>:<password>@<cluster-host>/Logiq?retryWrites=true&w=majority
     ```

### Option B: Self-host MongoDB on Debian 12 (Local Only)

If you don’t want Atlas, self-host MongoDB **bound to localhost** on the VPS.

> MongoDB packaging and versions can change; prefer MongoDB’s official documentation for Debian 12 (“bookworm”). The steps below follow the standard MongoDB Community pattern.

1. Install MongoDB Community (example: 7.0):

```bash
sudo apt -y install gnupg
curl -fsSL https://pgp.mongodb.com/server-7.0.asc | sudo gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg
echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update
sudo apt -y install mongodb-org
sudo systemctl enable --now mongod
```

2. Create users (before enabling authorization):

```bash
mongosh
```

In the shell:

```javascript
use admin
db.createUser({ user: "admin", pwd: "<STRONG_PASSWORD>", roles: [ { role: "userAdminAnyDatabase", db: "admin" } ] })

use Logiq
db.createUser({ user: "logiq", pwd: "<STRONG_PASSWORD>", roles: [ { role: "readWrite", db: "Logiq" } ] })
```

3. Enable authorization and keep MongoDB local-only:

Edit `/etc/mongod.conf` and ensure:

```yaml
net:
  bindIp: 127.0.0.1
security:
  authorization: enabled
```

Restart:

```bash
sudo systemctl restart mongod
```

4. Your `MONGODB_URI` becomes:

```text
mongodb://logiq:<password>@127.0.0.1:27017/Logiq?authSource=Logiq
```

## 3) Install the Bot on the VPS

### 3.1 Clone the repo and install dependencies

```bash
sudo -u logiq git clone <YOUR_REPO_URL> /opt/logiq/app
cd /opt/logiq/app

sudo -u logiq python3 -m venv /opt/logiq/app/venv
sudo -u logiq /opt/logiq/app/venv/bin/pip install --upgrade pip
sudo -u logiq /opt/logiq/app/venv/bin/pip install -r requirements.txt
```

### 3.2 Use the minimal (recommended) configuration

This repo supports config-driven cog loading (`modules.<cog>.enabled`). For a secure baseline, start from:

- `config.minimal.example.yaml`

Deploy it as your server config:

```bash
sudo -u logiq cp /opt/logiq/app/config.minimal.example.yaml /opt/logiq/app/config.yaml
sudo -u logiq mkdir -p /opt/logiq/app/logs
```

Keep `web.enabled: false` unless you are deploying the web dashboard behind TLS.

Note: the `/report` command is implemented in `cogs/moderation.py` (there is no separate `cogs/report.py`), so `modules.moderation.enabled: true` is what actually controls report availability.

## 4) Discord Developer Portal Setup

1. Go to https://discord.com/developers/applications and create an application.
2. Create a **Bot** inside the application and copy the **Bot Token** (keep it secret).
3. Enable privileged intents (required by this codebase):
   - **Server Members Intent**
   - **Message Content Intent**
   - **Presence Intent**
4. OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Permissions (minimum for the “minimal” config features):
     - Read Messages/View Channels, Send Messages, Embed Links, Read Message History
     - Manage Channels (tickets, lock/unlock), Manage Messages (clear)
     - Moderate Members (timeout/VC suspend), Kick Members, Ban Members
     - Manage Nicknames (nickname moderation)

After inviting, ensure the **bot role is positioned high enough** to moderate regular members, but consider keeping it **below admin/protected roles** as a defense-in-depth measure.

## 5) Secure Runtime Secrets (systemd EnvironmentFile)

Create a root-owned env file:

```bash
sudo mkdir -p /etc/logiq
sudo nano /etc/logiq/logiq.env
sudo chmod 600 /etc/logiq/logiq.env
```

Example `/etc/logiq/logiq.env`:

```bash
DISCORD_BOT_TOKEN=YOUR_DISCORD_TOKEN_HERE
MONGODB_URI=YOUR_MONGODB_URI_HERE
```

## 6) Run as a systemd service

Create `/etc/systemd/system/logiq.service`:

```ini
[Unit]
Description=Logiq Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=logiq
Group=logiq
WorkingDirectory=/opt/logiq/app
EnvironmentFile=/etc/logiq/logiq.env
ExecStart=/opt/logiq/app/venv/bin/python main.py
Restart=on-failure
RestartSec=5

# Optional hardening (enable after verifying the bot runs fine):
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now logiq
sudo systemctl status logiq --no-pager
```

Logs:

```bash
sudo journalctl -u logiq -f
```

## 7) First-Time Bootstrap in Discord (Important)

### 7.1 Sync slash commands

The bot attempts a global sync on startup. If you need to force-refresh in a guild, use:

- `/sync` (admin-only)

### 7.2 Configure logging channels

- `/setlogchannel` to set a default log channel.
- `/setlogchannel-advanced` to route specific logs to dedicated channels:
  - `reports`, `moderation`, `vcmod`, `tickets`, `feature_permissions`, `default`

### 7.3 Run security bootstrap (Phase 3)

Sensitive features are **locked by default** until an admin acknowledges protected roles.

1. Run:
   - `/perms security-bootstrap`
2. Review protected roles:
   - `/perms security-protected-list`
3. Adjust if needed:
   - `/perms security-protected-add role:@Role`
   - `/perms security-protected-remove role:@Role` (must keep at least one protected role)

This creates a per-guild `guild_security` config that:

- Auto-protects roles with `administrator` or `manage_guild`
- Prevents destructive actions on the guild owner and protected-role members

### 7.4 Configure feature permissions

- List current overrides:
  - `/perms feature-list` (filtered to enabled modules from `config.yaml`)
  - `/perms feature-list show_all:true` (shows all feature keys)
- Allow/deny roles per feature:
  - `/perms feature-allow feature:<key> role:@Role`
  - `/perms feature-deny feature:<key> role:@Role`
  - `/perms feature-clear feature:<key> role:@Role`
  - `/perms feature-reset feature:<key>`

## 8) Operations (Updates & Safety)

### Update the bot

```bash
sudo systemctl stop logiq
cd /opt/logiq/app
sudo -u logiq git pull
sudo -u logiq /opt/logiq/app/venv/bin/pip install -r requirements.txt
sudo systemctl start logiq
```

### Rotate secrets

- Rotate `DISCORD_BOT_TOKEN` in the Developer Portal if it ever leaks.
- Rotate MongoDB credentials (Atlas or local user) periodically.

### Backups (MongoDB)

- Atlas: use Atlas backups/snapshots.
- Self-hosted: schedule `mongodump` and store archives off-host.

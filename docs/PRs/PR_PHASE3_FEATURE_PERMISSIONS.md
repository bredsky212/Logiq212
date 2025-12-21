# PR: Phase 3 Feature Permissions, Security, and Config Controls

## Summary
- Implemented guild security bootstrap with protected roles and sensitive feature gating.
- Added per-category logging channels and improved log routing.
- Introduced config-driven cog loading with a minimal config example.
- Hardened moderation/VC/roles behaviors, bot info/stat commands, and ticket logging.

## Changes
- Security
  - Added `GuildSecurityConfig` model/collection with auto-bootstrap of admin/manage_guild roles.
  - Added `/perms security-bootstrap`, `security-protected-{add,remove,list}` to manage protected roles.
  - Sensitive features (ban/kick/timeout/lock/slowmode/VC suspend, tickets admin, staffapp template manage) denied until security is initialized; protected members cannot be targeted.
  - Role assignment flows (role menus, forced add/remove, verification) filter out protected roles.
- Logging
  - Per-type log channels configurable via `/setlogchannel-advanced` (`reports`, `moderation`, `vcmod`, `tickets`, `feature_permissions`, `default`).
  - Unified log resolution helper; ticket open/close now log to tickets log channel.
- Config
  - Config-driven cog loading (`modules.<cog>.enabled`), defaults to load if missing; minimal example config added for moderation-only deployments.
- Commands UX
  - `/ticket-panel` posts publicly again (non-ephemeral).
  - Info commands (`/botinfo`, `/serverstats`) are admin-only, ephemeral by default with optional public flag.
  - `/timeout` now reports invalid user conversion errors ephemerally instead of silent failures.
  - `/perms feature-list` filters by `config.yaml` `modules` enables by default; use `show_all: true` to include disabled modules.
- Tests
  - Added `tests/test_security.py` covering protected member detection and sensitive feature gating (pytest required).

## How to Use
- Run `/perms security-bootstrap` after deploy to unlock sensitive features and confirm protected roles.
- Set log channels with `/setlogchannel-advanced` (per type) or `/setlogchannel` (default).
- Toggle cogs via `config.yaml` `modules` block; see `config.minimal.example.yaml` for a moderation-only template.
- Keep giveaways disabled by setting `modules.giveaways.enabled: false`.

## Risk & Notes
- Sensitive commands are locked until security bootstrap; ensure admins run `/perms security-bootstrap`.
- Protected roles and guild owner are immune to destructive actions and bot role assignments.
- Per-log-channel routing requires configuring channels; otherwise falls back to default when present.

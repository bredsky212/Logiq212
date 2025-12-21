# TODO: Phase 3 Feature Permission Integration

Branch: eature/phase3-feature-permissions

## Current State
- Branch exists; no Phase 3 wiring applied yet (clean cogs).
- database/models.py already includes new FeatureKey values for all domains:
  - verify.config
  - games.panel.manage
  - roles.menu.manage, roles.force.assign
  - economy.admin.adjust
  - leveling.admin.set, leveling.admin.reset
  - giveaway.create, giveaway.manage
  - music.dj.basic, music.dj.volume
  - alerts.manage, alerts.view
  - tempvoice.setup, tempvoice.owner.power
  - utility.poll
  - analytics.view

## Next Steps (per cog)
1) **Verification**
   - Add perms/denials (VERIFY_CONFIG) to setup-verification, set-welcome-message, send-verification.
   - Base check: manage_guild/admin/owner.
   - Log denies to mod-log (throttled).

2) **Games**
   - Gate /setup-game-panel with GAMES_PANEL_MANAGE; base: manage_guild or manage_channels.
   - Add denials/logging and defer.

3) **Roles**
   - Gate /create-role-menu with ROLES_MENU_MANAGE; base: manage_roles/admin/owner.
   - Gate /addrole /removerole with ROLES_FORCE_ASSIGN; enforce hierarchy/top-role checks.
   - Deny → ephemeral + throttled mod-log.

4) **Economy**
   - Gate /addbalance with ECONOMY_ADMIN_ADJUST; base: manage_guild/admin/owner.
   - Deny log (Tier 3); keep other commands public.

5) **Leveling**
   - Gate /setlevel (LEVELING_ADMIN_SET) and /resetlevels (LEVELING_ADMIN_RESET) with base manage_guild/admin/owner.
   - Defer + log denies; log resets to mod-log.

6) **Giveaways**
   - Gate /giveaway with GIVEAWAY_CREATE; /gend /greroll with GIVEAWAY_MANAGE; base: manage_guild or manage_channels.
   - Add denies/throttling and defers/followups.

7) **Music**
   - Add perms manager/denials in music.py.
   - MUSIC_DJ_VOLUME: gate /volume by default (base: manage_channels or DJ-role check if present).
   - MUSIC_DJ_BASIC: optional; only restrict if configured; default open.

8) **Social Alerts**
   - Gate /alert-add /alert-remove /alert-test with ALERTS_MANAGE; base: manage_guild or manage_channels.
   - /alert-list optional gating via ALERTS_VIEW (can default open).
   - Deny logging for manage actions.

9) **Temp Voice**
   - Gate /setup-tempvoice with TEMPVOICE_SETUP; base: manage_channels/manage_guild.
   - Owner commands (lock/unlock/limit/rename/claim): keep current owner checks; apply TEMPVOICE_OWNER_POWER only to *further restrict* (do not broaden).
   - Log denies (Tier 2 for owner_power, Tier 3 for setup).

10) **Utility**
    - Gate /poll with UTILITY_POLL (base: send_messages/embed_links as appropriate); default open if no override.

11) **Analytics**
    - Gate /analytics /activity with ANALYTICS_VIEW; base: manage_guild or view_audit_log.
    - Deny logging (Tier 2).

12) **Admin**
    - No feature keys for reload/sync/config/modules/setlogchannel.

## General Patterns to Follow
- Use ot.perms (FeaturePermissionManager) and DenialLogger.
- Base checks are hard floor: feature overrides cannot bypass missing Discord perms/hierarchy.
- Denies: ephemeral error + throttled mod-log for Tier 2/3.
- Defer before DB/IO; use followups.
- Bind grouped commands from cog instances (already set up in other cogs; ensure consistent).
- Use member.timeout(timedelta, reason=...) for VC timeouts (if touched).

## Docs
- Update README Permissions & Roles section with new feature keys and defaults.

## Commit Plan
- Group commits by domain (e.g., verification/games, roles, economy, leveling, giveaways, music, alerts, tempvoice, utility+analytics, README).

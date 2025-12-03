# PR: Phase 2 – Feature Permissions Integration

## Summary
- integrate `FeaturePermissionManager` across staff applications, tickets, and moderation commands/buttons
- gate staffapp config/template commands with `staffapp.template.manage`; queue/status/review buttons with `staffapp.review`, with denial logging
- gate ticket admin commands with `tickets.admin`; staff closes with `tickets.close`; log denials with throttling
- gate moderation commands (warn, warnings, timeout, kick, ban/unban, clear, slowmode, lock/unlock, nickname) with corresponding `mod.*` keys plus Discord perms/hierarchy checks; denials logged

## Branch
- feature/permissions-vc-suspension

## Notes
- `/report` remains open unless a feature override is configured
- responses unchanged when feature permissions are empty; admins/owner always bypass feature gating (but still require Discord perms)
- tests not run here; run `pytest` with Mongo available

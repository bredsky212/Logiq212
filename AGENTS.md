# Agent Instructions (Logiq212)

This file summarizes how to work on this repo and points to the canonical docs.

## Source of Truth
- Developer setup + Windows/PowerShell gotchas: `docs/ai_agent/DEV_SETUP_AND_BEST_PRACTICES.md`
- Contribution rules and coding standards: `docs/ai_agent/ai_agent_contribution_guidelines.md`
- AI chat feature usage and admin commands: `docs/guides/ai_chat_guide.md`
- Raisehand speaking queue guide (non-dev): `docs/guides/raisehand_guide.md`
- Feature permissions, security bootstrap, and config controls: `docs/PRs/PR_PHASE3_FEATURE_PERMISSIONS.md`
- Feature permissions integration (tickets, moderation, staffapps): `docs/PRs/PR_PHASE2_PERMISSIONS.md`
- VC moderation and perms quick guide (non-dev): `docs/guides/permissions_vcmod_guide.md`
- Staff applications PR notes: `docs/PRs/PR_STAFF_APPLICATIONS.md`
- Staff applications guide (non-dev): `docs/guides/staff_applications_guide.md`
- VC suspension PR notes: `docs/PRs/PR_PERMISSIONS_VC_SUSPENSION.md`

## Workflow Expectations
- Create a feature branch before changes; keep commits focused.
- Use the existing cog/DB/util patterns and FeaturePermissionManager checks.
- Defer interactions for any DB/IO work and always respond once.
- Keep secrets out of logs; follow encryption rules for AI keys.
- Prefer small, scoped edits; avoid unrelated formatting changes.

## Feature Permission and Security Notes
- Sensitive features are locked until `/perms security-bootstrap` runs; protected roles and owner cannot be targeted.
- `/perms feature-list` filters by enabled modules unless `show_all: true` is provided.
- Admin/owner bypass feature gating but still require Discord permissions and hierarchy checks.

## Logging and Audit Routing
- Use per-purpose log channels via `/setlogchannel-advanced` and resolve via `utils.logs.resolve_log_channel`.
- Raisehand uses the `raisehand` log purpose.
- Feature permission changes and denials are logged; keep denial logging throttled.

## VC Moderation Expectations
- VC suspend/unsuspend uses Discord timeouts; always check role hierarchy and permissions.
- Responses are ephemeral; actions are logged to the mod log channel.

## Staff Applications Expectations
- Templates and panels are configured via `/staffapp`; views and buttons persist across restarts.
- Creator/reviewer roles gate template management and reviews; DMs are sent on status changes.

## Config Conventions
- Enable/disable cogs via `modules.<cog>.enabled`.
- Use `config.minimal.example.yaml` as the baseline for a limited deployment.

If any instructions conflict, the linked documents above are authoritative.

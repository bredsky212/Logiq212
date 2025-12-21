# PR: Fine-Grained Permissions & VC Suspension

## Summary
- add feature-level permission enum/models with allow/deny docs, audit logging, and checks
- new `/perms` commands for admins to allow/deny/clear/reset feature access with mod-log entries
- VC moderation commands (`/vcmod suspend|unsuspend|status`) with hierarchy checks, feature gating, logging, and Mongo suspension records

## Branch
- feature/permissions-vc-suspension

## Notes
- responses for VC actions are ephemeral; details logged to mod-log
- tests not run here; run `pytest` with Mongo available

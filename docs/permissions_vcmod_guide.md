# Fine-Grained Permissions & VC Moderation – Quick Guide (Non-Developers)

This guide shows how to configure feature-level permissions and use the VC moderation tools with the new `/perms` and `/vcmod` commands.

## What you need
- You must be an admin or have Manage Guild to change permissions with `/perms`.
- Bot online and synced in your server.
- A mod log channel set via `/setlogchannel` (for logging changes and suspensions).

## Part 1: Feature Permissions
Feature permissions let you allow/deny specific Logiq features per role, without bypassing Discord’s own permissions. Admins/owner are always allowed.

### List current overrides
```
/perms feature-list
```
Shows allow/deny roles for each feature (defaults to base behavior if none set).

### Allow a role for a feature
```
/perms feature-allow feature:mod.vc_suspend role:@SeniorMod
```
Adds the role to the allowed list (still requires the underlying Discord permission like Moderate Members).

### Deny a role for a feature
```
/perms feature-deny feature:mod.vc_suspend role:@TrialMod
```
Denials win over allows (admins/owner still bypass).

### Clear a role from allow/deny
```
/perms feature-clear feature:mod.vc_suspend role:@TrialMod
```

### Reset a feature to default
```
/perms feature-reset feature:mod.vc_suspend
```
Removes overrides; falls back to base checks.

All changes are logged to the mod log channel and stored in an audit collection.

## Part 2: VC Moderation (Timeout/Suspension)
VC suspension uses Discord’s timeout (communication disable) and offers fixed durations: 2h, 4h, 12h. Responses to moderators are ephemeral; full details go to the mod log.

### Suspend a user
```
/vcmod suspend user:@User duration:2h reason:"Disruptive in VC"
```
Rules:
- You must have Moderate Members and be allowed by feature permissions.
- You cannot act on owner/admin or higher/equal roles.

### Unsuspend a user early
```
/vcmod unsuspend user:@User reason:"Issue resolved"
```
Removes the timeout and marks the suspension inactive in Mongo.

### Check status/history
```
/vcmod status user:@User
```
Shows current timeout (if any) and last suspensions.

## Tips
- If a command seems missing, run `/sync` as admin (guild sync) to refresh.
- Keep the bot’s role to only needed Discord perms (e.g., Moderate Members) rather than full Administrator.
- Mod log channel set via `/setlogchannel` is reused for all new actions. 

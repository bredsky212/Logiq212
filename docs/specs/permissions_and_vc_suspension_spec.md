# Fine-Grained Role Permissions & VC Suspension – Design Spec

## 0. Goals & Non-Goals

### Goals

1. **Fine-grained, safe feature permissions**
   - Allow server admins to say:
     - “This role can use *these* Logiq features/commands”
     - “…and **cannot** use *those* features”
   - Without ever letting non-admins escalate power beyond what Discord itself allows.

2. **VC moderation suspension tools**
   - Add commands for **Voice Chat Moderators (VCmods)** to temporarily suspend users:
     - Exactly **2 hours**, **4 hours**, or **12 hours**.
   - Suspension = Discord **timeout** (aka communication disable):
     - User cannot send messages or join voice channels server-wide.   

3. **Abuse resistance**
   - No way for a malicious moderator to:
     - Give themselves “admin-like” powers via the bot,
     - Kick/ban/time out people they shouldn’t be able to (e.g. higher roles, admins),
     - Reconfigure permissions to lock out the actual server owner/admins.

### Non-Goals

- No web admin UI for permissions (all is via slash commands).
- No full “policy language” – just **feature keys + allow/deny per role**.
- No auto-unban or scheduled tasks beyond **Discord’s own timeout expiry** (timeouts expire automatically on Discord’s side).

---

## 1. Security Model & Invariants

These rules are **non-negotiable** and must be enforced in code:

1. **Admin supremacy**
   - The following **can never be blocked** by bot-level permissions:
     - Server owner.
     - Members with the **Administrator** permission.   
   - Any check should early-return “allowed” if `member.guild_permissions.administrator` is true or `member == guild.owner`.

2. **No privilege escalation beyond Discord**
   - If a feature logically requires a Discord permission (e.g. `Moderate Members` for timeouts, `Ban Members` for bans), the bot must **still check** that underlying permission.
   - Role-based feature overrides can:
     - **Restrict** access more than Discord.
     - **Delegate** access among roles that *already* meet the underlying condition.
   - They must **never** allow:
     - A role without `Moderate Members` to perform timeouts, or
     - A role without `Ban Members` to perform bans, etc.   

3. **Config commands are owner/admin only**
   - Any command that mutates permission config (`/perms …`) must require:
     - Server owner **or**
     - `Administrator` **or**
     - `Manage Guild` (if we want a “config admin” tier).
   - No permission override can change this.

4. **Role hierarchy respect**
   - Any moderation action (including VC suspensions) must **refuse** if:
     - Target is the server owner, or
     - Target has `Administrator`, or
     - Target’s top role ≥ moderator’s top role (standard Discord moderation rule).   

5. **Auditability**
   - All config changes and VC suspension actions must:
     - Be stored in MongoDB.
     - Be logged to the existing moderation log channel (if configured) using consistent embed style.

---

## 2. Part A – Fine-Grained Role Permissions

### 2.1 Concept

Introduce a **feature-level permissions layer** on top of:

- Discord’s built-in permissions (roles, timeouts, bans, etc.), and
- Existing Logiq internal role configs (e.g. `staffapp` creator/reviewer).

This layer controls **which roles can use which bot features**, but **never overrides Discord’s security**.

Examples:

- Allow a `Senior Mod` role to use `/vcmod suspend`, but not `/ban`.
- Explicitly deny `/tickets close` for a `Helper` role, even if they have a generic `moderator` role.

### 2.2 Data Model

#### 2.2.1 Feature keys

Define a centralized enum/list of **feature keys**, e.g.:

```python
class FeatureKey(str, Enum):
    # Moderation
    MOD_VC_SUSPEND = "mod.vc_suspend"
    MOD_VC_UNSUSPEND = "mod.vc_unsuspend"
    MOD_WARN = "mod.warn"
    MOD_TIMEOUT = "mod.timeout"
    MOD_BAN = "mod.ban"

    # Tickets
    TICKETS_CREATE = "tickets.create"
    TICKETS_CLOSE = "tickets.close"
    TICKETS_ADMIN = "tickets.admin"

    # Staff applications
    STAFFAPP_TEMPLATE_MANAGE = "staffapp.template.manage"
    STAFFAPP_REVIEW = "staffapp.review"

    # Reports
    REPORT_VIEW = "report.view"
    REPORT_MANAGE = "report.manage"

    # etc.
```

Each **command / button / modal handler** that should be permission-gated declares which feature keys it uses.

#### 2.2.2 Mongo collection: `feature_permissions`

New collection: `feature_permissions`.

Document shape:

```json
{
  "guild_id": 123456789012345678,
  "feature_key": "mod.vc_suspend",
  "allowed_roles": [ 111111111111111111, 222222222222222222 ],
  "denied_roles": [ 333333333333333333 ],
  "updated_by": 444444444444444444,
  "updated_at": "2025-12-03T16:00:00Z"
}
```

Rules:

* One document per `(guild_id, feature_key)` pair.
* If `allowed_roles` is empty:

  * The bot falls back to **built-in logic** (Discord perms + local static checks).
* If `denied_roles` contains *any* of a member’s roles:

  * The feature is denied, even if base logic would allow (except for admin/owner).
* If `allowed_roles` is non-empty:

  * Non-admins must have **at least one** allowed role to use the feature (and still satisfy base Discord permission requirements).

#### 2.2.3 Mongo collection: `feature_permissions_audit`

For **audit**, new collection `feature_permissions_audit`:

```json
{
  "guild_id": 123456789012345678,
  "feature_key": "mod.vc_suspend",
  "changed_by": 444444444444444444,
  "change_type": "allow" | "deny" | "clear" | "reset",
  "role_id": 111111111111111111,
  "old_doc": { ... },
  "new_doc": { ... },
  "at": "2025-12-03T16:00:00Z"
}
```

This allows investigation after a compromise.

### 2.3 Permission Manager Service

Create `utils/feature_permissions.py`:

```python
class FeaturePermissionManager:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def check(
        self,
        member: discord.Member,
        feature_key: FeatureKey,
        base_check: Callable[[discord.Member], bool],
    ) -> bool:
        ...
```

`check()` logic:

1. **Admins/owner bypass**:

   * Return True immediately if member is server owner or has `Administrator`.

2. **Base check**:

   * If `base_check(member)` is False → return False.

     * `base_check` contains:

       * Discord perms check (e.g. `Moderate Members`, `Ban Members`)
       * Any internal tier check (e.g. “staffapp reviewer”).

3. **Load guild feature doc** from `feature_permissions`:

   ```python
   doc = await db.get_feature_permissions(guild_id, feature_key)
   if not doc:
       return True  # no override → base logic only
   ```

4. **Denied roles check**:

   * If any `member_role.id` is in `doc["denied_roles"]` → return False.

5. **Allowed roles check**:

   * If `doc["allowed_roles"]` is empty → return True (no additional restriction).
   * Else:

     * If member has at least one role in `allowed_roles` → True
     * Otherwise → False.

This makes the override system **only ever stricter** than base logic, not looser.

### 2.4 Integration in commands

For each command:

```python
@app_commands.command(name="suspend", description="Temporarily suspend a user from voice & chat")
async def vc_suspend(...):
    if not await self.perms.check(
        interaction.user,
        FeatureKey.MOD_VC_SUSPEND,
        base_check=self._base_check_vc_suspend,
    ):
        return await interaction.response.send_message(
            "❌ You do not have permission to use this command.",
            ephemeral=True,
        )
    ...
```

`_base_check_vc_suspend(member)` MUST verify:

* `Moderate Members` permission.
* Not targeting higher/equal roles.

Existing cogs (e.g. `staff_applications`, `moderation`, `tickets`) can be gradually migrated to use this system.

---

## 3. Part A Commands – `/perms` Management

Create a new command group:

```python
perms = app_commands.Group(
    name="perms",
    description="Configure Logiq feature permissions",
    guild_only=True,
)
```

### 3.1 `/perms feature list`

```text
/perms feature list
```

* **Permissions**: only owner / Administrator / Manage Guild.
* Behaviour:

  * Lists either:

    * All features with a non-default config, or
    * All known features for this guild (with indication of which are default).
  * For each:

    * `Feature: mod.vc_suspend`
    * Allowed roles: @VC Mod, @Senior Mod
    * Denied roles: @Trial Mod
  * Output: ephemeral embed (supports pagination if needed).

### 3.2 `/perms feature allow`

```text
/perms feature allow
    feature: <choice from FeatureKey>
    role: @Role
```

* **Permissions**: owner / Administrator / Manage Guild.
* Behaviour:

  * Ensures a `feature_permissions` document exists for `(guild_id, feature_key)`.
  * Adds `role.id` to `allowed_roles` (set semantics).
  * Writes an entry to `feature_permissions_audit`.
  * Logs a config change embed to the log channel (if configured).
  * Replies ephemeral: success + current allow/deny for that feature.

### 3.3 `/perms feature deny`

```text
/perms feature deny
    feature: <FeatureKey>
    role: @Role
```

* Same permissions.
* Adds `role.id` to `denied_roles`.

### 3.4 `/perms feature clear`

```text
/perms feature clear
    feature: <FeatureKey>
    role: @Role
```

* Removes `role.id` from both `allowed_roles` & `denied_roles`.

### 3.5 `/perms feature reset`

```text
/perms feature reset
    feature: <FeatureKey>
```

* Resets config to default (doc deleted or arrays emptied).
* Default behaviour = fallback to base logic.

---

## 4. Part B – VC Suspension (Timeout) System

### 4.1 Concept & semantics

VCmods need a safe tool to **temporarily suspend** users who misbehave in voice channels.

We map “suspension” to Discord’s built-in **timeout** feature (“communication disabled”), which:

* Prevents sending messages and joining voice channels for a given duration.
* Requires the `Moderate Members` permission.

We expose **exactly three durations**:

* **2 hours**,
* **4 hours**,
* **12 hours**.

### 4.2 Data Model – `suspensions` collection

New Mongo collection: `suspensions`.

Document:

```json
{
  "guild_id": 123456789012345678,
  "user_id": 222222222222222222,
  "moderator_id": 333333333333333333,
  "reason": "Spamming loud noises in VC",
  "duration_seconds": 7200,
  "started_at": "2025-12-03T16:00:00Z",
  "ends_at": "2025-12-03T18:00:00Z",
  "type": "timeout",
  "active": true,
  "resolved_at": null,
  "resolved_by": null
}
```

Indexes:

* `(guild_id, user_id, active)`
* `(guild_id, ends_at)` (for future scheduled checks if needed).

When a new suspension is applied:

* Insert a `suspensions` doc with `active = true`.
* If an existing `active` suspension exists for `(guild_id, user_id)`:

  * Option: either block new suspension, or override it; for now:

    * Override: mark previous as `active=false, resolved_at=now, resolved_by=moderator_id`, then insert the new one.

When unsuspending manually:

* Mark `active=false`, set `resolved_at`, `resolved_by`.

Note: When Discord’s timeout expires naturally, we do **not** need to automatically update `active`; we can treat `ends_at <= now` as “effectively inactive” in queries. If needed later we can add a cleanup job.

### 4.3 VC moderation command group

Create new cog or extend `moderation` cog with a group:

```python
vcmod = app_commands.Group(
    name="vcmod",
    description="Voice chat moderation tools",
    guild_only=True,
)
```

#### 4.3.1 Permission check helper

```python
def _can_use_vcmod(self, member: discord.Member) -> bool:
    # 1) Admin/owner always allowed
    if member.guild_permissions.administrator or member == member.guild.owner:
        return True

    # 2) Must have Moderate Members
    if not member.guild_permissions.moderate_members:
        return False

    # 3) Feature override
    return self.perms.check(
        member,
        FeatureKey.MOD_VC_SUSPEND,  # or appropriate key per command
        base_check=lambda m: m.guild_permissions.moderate_members,
    )
```

This ensures:

* Only users with `Moderate Members` **and** allowed by the feature system can use VC suspension tools.

#### 4.3.2 `/vcmod suspend`

```text
/vcmod suspend
    user: @User
    duration: [2h, 4h, 12h]
    reason: "Being extremely disruptive in voice"
```

Behaviour:

1. **Permission checks:**

   * `_can_use_vcmod(interaction.user)` must be True.
   * Validate target:

     * Not server owner.
     * Target doesn’t have Administrator.
     * Caller’s top role > target’s top role.

2. **Compute `ends_at`**:

   * `2h` → `now + 2 hours`
   * `4h` → `now + 4 hours`
   * `12h` → `now + 12 hours`

3. **Apply Discord timeout:**

   ```python
   await target_member.edit(communication_disabled_until=ends_at)
   ```

4. **Record in `suspensions`:**

   * Insert doc as above with `type="timeout"` and `active=true`.
   * Close any previous active suspensions for the same user/guild.

5. **Respond:**

   * Public acknowledgement in the channel (or ephemeral, depending on moderation style), e.g.:

     > `✅ @User has been suspended from voice & chat for 2 hours.`

6. **Log to mod-log channel:**

   * Embed with:

     * Moderator
     * Target
     * Duration
     * Reason
     * Time range
   * Include a link to the invocation message and the suspension ID from DB.

7. **DM the target (best-effort):**

   * DM message:

     > “You have been temporarily suspended from **<ServerName>** for **2 hours** by **<Moderator>**. Reason: `<reason>`.”
   * If DM fails (`Forbidden`), ignore after logging.

#### 4.3.3 `/vcmod unsuspend`

```text
/vcmod unsuspend
    user: @User
    reason: "Issue resolved, lifting suspension early"
```

Behaviour:

1. Permission check same as `/vcmod suspend`.

2. Validate target:

   * Same hierarchy checks; if target has `Administrator` or higher role, unsuspending is safe but not required; we can allow.

3. Remove Discord timeout:

   ```python
   await target_member.edit(communication_disabled_until=None)
   ```

4. In Mongo:

   * Find active suspension for `(guild_id, user_id, type="timeout")`.
   * Set `active=false`, fill `resolved_at` and `resolved_by`.

5. Respond + log.

#### 4.3.4 `/vcmod status`

```text
/vcmod status
    user: @User
```

* Shows:

  * Whether the user is currently timed out (check `member.communication_disabled_until`).
  * Active suspension doc if any.
  * Last 3 suspensions (history) from `suspensions` collection.

Response: ephemeral embed.

---

## 5. Abuse Scenarios & Mitigations

### 5.1 Malicious mod granting themselves full power

* **Risk:** A mod uses `/perms` to allow their role to use highly sensitive features (e.g. ban, config).
* **Mitigations:**

  * `/perms` commands require owner/Admin/Manage Guild only.
  * Sensitive features (like `perms.*` themselves, `mod.ban`, `staffapp.template.manage`) should **only** be configurable by owner/Admin, not by roles configured via overrides.
  * For these features, `base_check` must explicitly require `Administrator`, ignoring `FeaturePermissions`.

### 5.2 Mod timing out admins / higher roles

* **Risk:** VCmods use timeout commands against staff above them.
* **Mitigations:**

  * Always check role hierarchy:

    * Deny if target top role ≥ moderator top role, or target has `Administrator`, or is guild owner.

### 5.3 Misconfigured bot role (too many Discord perms)

* **Risk:** Bot has dangerous Discord permissions it doesn’t really need.
* **Mitigations:**

  * Document that Logiq’s bot role should be restricted to:

    * `Moderate Members`, `Manage Messages` (if needed), etc., not `Administrator` by default.
  * Encourage server owners to follow Discord security guidelines for roles and bot roles.

### 5.4 Hidden/unlogged punishments

* **Risk:** Mods punish users without leaving traces.
* **Mitigations:**

  * All suspension actions log:

    * To `suspensions` collection
    * To the moderation log channel
  * Optionally, `/modlog` or `/history` commands can show actions for a user.

---

## 6. Acceptance Criteria

Feature is considered complete when:

1. **Feature permissions**

   * `/perms feature list/allow/deny/clear/reset` exist and work.
   * Only owner/Admin/Manage Guild can change permissions.
   * A feature like `mod.vc_suspend` can be:

     * Restricted to a specific role.
     * Denied to another role.
   * Admins/owner can always use the feature regardless of overrides.

2. **VC suspension**

   * `/vcmod suspend` suspends users via Discord timeout for exactly 2h/4h/12h.
   * Hierarchy rules prevent suspending higher/equal roles, admins, or owner.
   * Suspensions are recorded in Mongo and logged to the mod-log channel.
   * `/vcmod unsuspend` removes timeout and marks the suspension inactive.
   * `/vcmod status` shows current timeout and last suspensions.
   * Feature gates respect:

     * Discord `Moderate Members` permission.
     * `FeatureKey.MOD_VC_SUSPEND` and `MOD_VC_UNSUSPEND` via the permission manager.

3. **No regressions**

   * Existing `/report`, `tickets`, `staffapp` and other moderation flows keep working.
   * Initial integration can limit the feature-permission system to:

     * VC suspension commands,
     * Selected moderation commands,
     * (Optionally later) staffapp and tickets.

4. **Security**

   * Manual tests confirm:

     * A non-admin role without `Moderate Members` cannot use `/vcmod suspend` even if added to `allowed_roles`.
     * A lower-role VCmod cannot suspend someone above them.
     * Resetting feature permissions restores default behaviour.

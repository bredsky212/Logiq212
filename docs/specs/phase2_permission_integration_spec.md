
# Phase 2 – Integrate Feature Permissions into Existing Cogs

Target version: `feature/permissions-vc-suspension` (after Phase 1 is merged)  
Scope: **staff_applications**, **tickets**, **moderation** cogs

---

## 0. Objectives

1. **Unify access control**  
   Integrate the `FeaturePermissionManager` introduced in Phase 1 into:
   - Staff applications commands & review UI (`cogs/staff_applications.py`)
   - Ticketing setup & management (`cogs/tickets.py`)
   - Core moderation commands (`cogs/moderation.py`)

2. **Preserve Discord’s security model**  
   For any high-impact action (ban, timeout, lock channels, etc.), the new system:
   - **Must not** bypass Discord’s elevated permissions or role hierarchy.
   - **May only restrict** access further (per feature + per role).

3. **Per-command risk classification**  
   Classify commands into **risk tiers** and wire feature keys accordingly so that:
   - High-risk commands (ban, channel lock, template/config mgmt) are tightly guarded.
   - Medium-risk (warn, close tickets, staff review) are still controlled but more flexible.
   - Low-risk/user commands (`/report`, open ticket) remain broadly available unless explicitly restricted.

4. **No breaking changes by default**  
   With an **empty feature-permissions config**:
   - All commands must behave exactly as before (relying on existing decorators and permission checks).

---

## 1. Risk Taxonomy & General Rules

### 1.1 Risk tiers

We classify **existing commands** into three tiers:

- **Tier 3 – High risk (admin / critical moderation)**
  - Can materially damage a server or community if misused.
  - Examples:
    - `/ban`, `/unban`
    - `/kick`
    - `/timeout` (long durations)
    - `/clear` (mass delete messages)
    - `/lock`, `/unlock`
    - `/ticket-setup`, `/ticket-panel`, `/tickets` (server-wide ticket config)
    - `/staffapp config *`
    - `/staffapp template create/enable/disable` (shapes staff intake pipeline)

- **Tier 2 – Medium risk (operational staff actions)**
  - Impacts individual users or limited channels but not entire server configuration.
  - Examples:
    - `/warn`, `/warnings`
    - `/close-ticket` (closing another user’s ticket as staff)
    - `/staffapp queue`, `/staffapp set-status`
    - Staff buttons in staffapp review views
    - Ticket control buttons used by staff

- **Tier 1 – Low risk (user-facing, self-service)**
  - Designed for general members; abuse is limited by existing safeguards.
  - Examples:
    - `/report`
    - Ticket **Create Ticket** button
    - Closing **own** ticket via button
    - Staff application “Apply” button + modal

### 1.2 Integration rules

1. **Base checks stay authoritative**  
   For each command, we define a `base_check(member, target?, context?)` that contains:
   - Existing decorators / PermissionChecker logic (is_moderator, is_admin, role hierarchy).
   - Underlying Discord permission requirements (kick, ban, timeout, manage channels, etc.).
   - Any cog-specific checks (ticket owner, staffapp creator/reviewer, etc.).

   `FeaturePermissionManager.check(...)` is **always called on top** of that. If `base_check` is false, the feature permission **must not** override it.

2. **Admins & owner bypass feature restrictions**  
   - Guild owner and members with `Administrator` are **always allowed** to run Tier 2/3 commands, regardless of feature config.
   - Feature permissions cannot be used to lock admin/owner out of bot features, although they may still lose power at Discord level (e.g. removed `KickMembers` permission).

3. **Feature permissions can only tighten**  
   - For non-admins:
     - If `feature_permissions` has **no entry** for a feature → behavior unchanged (base check only).
     - If `denied_roles` contains any of their roles → feature denied.
     - If `allowed_roles` is non-empty → user must have at least one allowed role **in addition** to passing base_check.

4. **Views/buttons must respect same checks**  
   - Any `discord.ui.View` button (staff review, ticket controls) that causes a privileged action must perform the **same permission check** as the equivalent slash command:
     - Same `FeatureKey`.
     - Same `base_check`.

---

## 2. Staff Applications Cog (`cogs/staff_applications.py`)

### 2.1 Existing commands and flows

Commands (from code):

- Group: `/staffapp`
  - `/staffapp config set-creator-role`
  - `/staffapp config add-reviewer-role`
  - `/staffapp config remove-reviewer-role`
  - `/staffapp config show`
  - `/staffapp template create`
  - `/staffapp template list`
  - `/staffapp template enable`
  - `/staffapp template disable`
  - `/staffapp queue`
  - `/staffapp set-status`

Views / modals:

- `StaffApplyView` – “Apply” button → `StaffApplicationModal`.
- `StaffApplicationModal` – collects answers and creates a `StaffApplication`.
- `StaffApplicationReviewView` – `Interview`, `Accept`, `Reject` buttons.

Internal permission helpers:

- `_get_config(guild_id)`
- `_is_creator(member, config)`
- `_is_reviewer(member, config)`

### 2.2 Feature keys

Reuse / extend Phase 1 keys:

- `STAFFAPP_TEMPLATE_MANAGE = "staffapp.template.manage"`
  - For:
    - `/staffapp config *`
    - `/staffapp template create/list/enable/disable`

- `STAFFAPP_REVIEW = "staffapp.review"`
  - For:
    - `/staffapp queue`
    - `/staffapp set-status`
    - `StaffApplicationReviewView` buttons

- **(Future)** `STAFFAPP_APPLY = "staffapp.apply"` (optional)
  - Could gate which roles are allowed to submit staff applications; initially not enforced (Tier 1).

### 2.3 Base checks

Define in the cog:

```python
def _base_check_staffapp_template(member: discord.Member, config: dict) -> bool:
    # Only admins or creator roles
    if member.guild_permissions.administrator or member == member.guild.owner:
        return True
    return self._is_creator(member, config)

def _base_check_staffapp_review(member: discord.Member, config: dict) -> bool:
    # Creator or reviewer (and admin/owner implicitly)
    if member.guild_permissions.administrator or member == member.guild.owner:
        return True
    return self._is_reviewer(member, config)
```

Notes:

* No additional Discord elevated permission needed beyond these logical roles.
* We intentionally **do not** let arbitrary feature permission grants override `_is_creator` / `_is_reviewer`; those remain part of `base_check`.

### 2.4 Wiring feature permissions

#### 2.4.1 Config & template commands (Tier 3)

* Commands:

  * `/staffapp config set-creator-role`
  * `/staffapp config add-reviewer-role`
  * `/staffapp config remove-reviewer-role`
  * `/staffapp template create`
  * `/staffapp template list`
  * `/staffapp template enable`
  * `/staffapp template disable`

Integration:

1. Keep current admin check for `set-creator-role` (admin-only).
2. For all of the above:

   * Fetch config via `_get_config`.

   * Run:

     ```python
     allowed = await perms.check(
         member=interaction.user,
         feature_key=FeatureKey.STAFFAPP_TEMPLATE_MANAGE,
         base_check=lambda m: _base_check_staffapp_template(m, config),
     )
     ```

   * If `allowed` is False:

     * Reply with standard “No Permission” embed (ephemeral).
     * Return early.

Rationale:

* Tier 3: only admins and explicitly trusted roles should manipulate staffapp config & templates.
* For empty feature-permissions: behavior identical to current implementation.

#### 2.4.2 Queue & status commands + review view (Tier 2)

* Commands:

  * `/staffapp queue`
  * `/staffapp set-status`

* Buttons:

  * `StaffApplicationReviewView` → statuses: `pending`, `interview`, `accepted`, `rejected`.

Integration:

* Shared check:

  ```python
  allowed = await perms.check(
      member=interaction.user,
      feature_key=FeatureKey.STAFFAPP_REVIEW,
      base_check=lambda m: _base_check_staffapp_review(m, config),
  )
  ```

* For:

  * Slash commands: `queue`, `set-status`.
  * Button callbacks: `_handle_status_button`, `_handle_status_update`.

If `allowed` is False:

* For slash commands: send ephemeral “No Permission” error.
* For button interactions: respond ephemeral with “You are not allowed to review staff applications.”

#### 2.4.3 Apply button & modal (Tier 1 – optional gating)

* Keep behavior as is for now:

  * Any member can apply unless we later introduce `FeatureKey.STAFFAPP_APPLY` as an opt-in restriction.
* If/when we add `STAFFAPP_APPLY`:

  * `base_check` = “member is not a bot and is in a guild”.
  * `perms.check()` can then be used to prevent certain roles from applying (e.g. blacklisted roles, cooldown roles).

---

## 3. Tickets Cog (`cogs/tickets.py`)

### 3.1 Existing commands and flows

Commands:

* `/ticket-setup` – configure category, support role(s), log channel, etc. (Tier 3).
* `/ticket-panel` – send panel with “Create Ticket” button (Tier 3).
* `/close-ticket` – close ticket, delete or archive channels (Tier 2 when used by staff).
* `/tickets` – list active tickets (Tier 3).

Views:

* `TicketCreateView` – “Create Ticket” button (Tier 1).
* `TicketControlView` – likely contains “Close Ticket” button and maybe more.
* Helper methods:

  * `create_ticket_for_user`
  * `close_ticket_for_user`

Existing permission checks:

* Admin decorator:

  * `@is_admin()` on `/ticket-setup`, `/ticket-panel`, `/tickets`.
* For `close_ticket_for_user`:

  * Checks whether:

    * Channel name starts with `ticket-` (guard).
    * User is ticket owner OR is admin OR has support role.

### 3.2 Feature keys

Reuse / refine from Phase 1 list:

* `TICKETS_ADMIN = "tickets.admin"`

  * `/ticket-setup`
  * `/ticket-panel`
  * `/tickets`

* `TICKETS_CLOSE = "tickets.close"`

  * `/close-ticket`
  * Staff-side “Close Ticket” button in `TicketControlView`.

* `TICKETS_CREATE = "tickets.create"` (optional)

  * User-side “Create Ticket” button from `TicketCreateView`.
  * By default, any member should be allowed.

### 3.3 Base checks

Define in Tickets cog:

```python
def _base_check_tickets_admin(member: discord.Member) -> bool:
    return (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )

def _base_check_tickets_close(member: discord.Member, interaction: discord.Interaction) -> bool:
    # Use existing logic:
    #   - must be in a ticket channel
    #   - either ticket owner, admin, or support role
    # This logic may already live in `close_ticket_for_user`.
    ...
```

Notes:

* `_base_check_tickets_admin` should be stricter than current `@is_admin()` if we decide to allow Manage Guild.
* `_base_check_tickets_close` delegates to existing ticket-owner & support-role checks:

  * The feature-permission layer then controls **which staff roles** are allowed to perform staff closes.

### 3.4 Wiring feature permissions

#### 3.4.1 Admin-level ticket commands (Tier 3)

* `/ticket-setup`, `/ticket-panel`, `/tickets`.

Integration:

* Keep `@is_admin()` decorator OR replace with explicit admin/guild-manage check.

* Inside each handler:

  ```python
  allowed = await perms.check(
      member=interaction.user,
      feature_key=FeatureKey.TICKETS_ADMIN,
      base_check=_base_check_tickets_admin,
  )
  ```

* Deny with ephemeral error if `allowed` is False.

#### 3.4.2 Staff close command & button (Tier 2)

* `/close-ticket` command.
* `TicketControlView.close_ticket_button`.

Integration:

* In `/close-ticket`:

  * After verifying context (ticket channel), call:

    ```python
    allowed = await perms.check(
        member=interaction.user,
        feature_key=FeatureKey.TICKETS_CLOSE,
        base_check=lambda m: _base_check_tickets_close(m, interaction),
    )
    ```

* In `TicketControlView.close_ticket_button`:

  * Do the same before calling `close_ticket_for_user`.

Behavior:

* Ticket owners (non-staff) closing **their own** tickets:

  * Should still be allowed by `_base_check_tickets_close` and be unaffected by feature permissions **if** we decide that `TICKETS_CLOSE` only applies to staff actions.
  * Option: refine `base_check` to:

    * Return True for ticket owners (bypassing feature permissions for them).
    * For non-owners, fall back to staff logic + `perms.check`.

#### 3.4.3 Create Ticket button (Tier 1)

* `TicketCreateView.create_ticket`.

Default behavior:

* Keep open to all members with access to the channel.
* No feature permission check by default.

Optional extension:

* Introduce `TICKETS_CREATE` gating for servers that only want certain roles to open tickets.
* If added:

  * `base_check = lambda m: True` for all guild members.
  * `perms.check` then restricts to allowed roles.

---

## 4. Moderation Cog (`cogs/moderation.py`)

### 4.1 Existing commands and features

Commands (from file):

* `/report` – structured user report (Tier 1 for callers; Tier 2 for staff review side).
* `/warn`
* `/warnings`
* `/timeout`
* `/kick`
* `/ban`
* `/unban`
* `/clear`
* `/slowmode`
* `/lock`
* `/unlock`
* `/nickname`

Other behaviors:

* Auto-mod: spam, excessive mentions, filters in `on_message`.
* Uses `is_moderator()` decorator and `PermissionChecker.can_moderate` (hierarchy-aware) for many commands.

### 4.2 Feature keys (extended)

Extend Phase 1’s moderation keys:

* `MOD_WARN = "mod.warn"` – for `/warn`.
* `MOD_WARNINGS = "mod.warnings"` – for `/warnings`.
* `MOD_TIMEOUT = "mod.timeout"` – for `/timeout`.
* `MOD_KICK = "mod.kick"` – for `/kick`.
* `MOD_BAN = "mod.ban"` – for `/ban` and `/unban`.
* `MOD_CLEAR = "mod.clear"` – for `/clear`.
* `MOD_SLOWMODE = "mod.slowmode"` – for `/slowmode`.
* `MOD_LOCK = "mod.lock"` – for `/lock` and `/unlock`.
* `MOD_NICKNAME = "mod.nickname"` – for `/nickname`.
* `REPORT_CREATE = "report.create"` – for `/report` usage (low risk; default open).
* `REPORT_MANAGE = "report.manage"` – for future staff-side commands to list / resolve reports.

### 4.3 Base checks per command

Use existing helpers as much as possible.

#### 4.3.1 Moderation actor checks

* Common pattern:

```python
def _base_check_moderation(
    moderator: discord.Member,
    target: Optional[discord.Member] = None,
    required_permissions: Optional[list[str]] = None,
) -> bool:
    # 1. Owner/admin shortcut
    if moderator.guild_permissions.administrator or moderator == moderator.guild.owner:
        return True

    # 2. PermissionChecker-based permissions + hierarchy
    if required_permissions:
        missing = PermissionChecker.get_missing_permissions(moderator, required_permissions)
        if missing:
            return False

    if target is not None:
        can_moderate, _error = PermissionChecker.can_moderate(moderator, target)
        if not can_moderate:
            return False

    return True
```

* Each command then chooses appropriate `required_permissions`, e.g.:

  * `/warn` → `["manage_messages"]` or a softer requirement (can stay `is_moderator`).
  * `/timeout` → `["moderate_members"]`.
  * `/kick` → `["kick_members"]`.
  * `/ban` / `/unban` → `["ban_members"]`.
  * `/clear` → `["manage_messages"]`.
  * `/slowmode`, `/lock`, `/unlock`, `/nickname` → `["manage_channels"]` or `["manage_channels", "manage_nicknames"]` depending on the action.

#### 4.3.2 Command-specific base checks

* `/report`:

  * Base check: guild presence, not reporting self, reason length, cooldown.
  * No staff permission required to submit.

* `/warn`:

  * `@is_moderator()` already ensures a baseline staff role.
  * For `base_check`, reuse `PermissionChecker.can_moderate` to enforce hierarchy.

* `/warnings`:

  * Usually same requirement as `/warn`, but read-only; can share `MOD_WARNINGS` key.

* `/timeout`:

  * `required_permissions = ["moderate_members"]`.
  * Enforce max 28 days in code (already done).

* `/kick`:

  * `required_permissions = ["kick_members"]`.

* `/ban`, `/unban`:

  * `required_permissions = ["ban_members"]`.

* `/clear`:

  * `required_permissions = ["manage_messages"]`.

* `/slowmode`, `/lock`, `/unlock`:

  * `required_permissions = ["manage_channels"]`.

* `/nickname`:

  * `required_permissions = ["manage_nicknames"]`.

All target-based commands must **always** run `PermissionChecker.can_moderate` to ensure the moderator is higher in the role hierarchy than the target.

### 4.4 Wiring feature permissions

For each command:

```python
allowed = await perms.check(
    member=interaction.user,
    feature_key=FeatureKey.MOD_<SOMETHING>,
    base_check=lambda m: _base_check_moderation(m, target, required_permissions),
)
if not allowed:
    return await interaction.response.send_message(
        embed=EmbedFactory.error("No Permission", "You are not allowed to use this command."),
        ephemeral=True,
    )
```

Mapping:

* `/warn` → `FeatureKey.MOD_WARN`, target & hierarchy applied.
* `/warnings` → `FeatureKey.MOD_WARNINGS`.
* `/timeout` → `FeatureKey.MOD_TIMEOUT`, with `["moderate_members"]`.
* `/kick` → `FeatureKey.MOD_KICK`, with `["kick_members"]`.
* `/ban`, `/unban` → `FeatureKey.MOD_BAN`, with `["ban_members"]`.
* `/clear` → `FeatureKey.MOD_CLEAR`, with `["manage_messages"]`.
* `/slowmode` → `FeatureKey.MOD_SLOWMODE`, with `["manage_channels"]`.
* `/lock`, `/unlock` → `FeatureKey.MOD_LOCK`, with `["manage_channels"]`.
* `/nickname` → `FeatureKey.MOD_NICKNAME`, with `["manage_nicknames"]`.

For `/report`:

* Keep open to all members (Tier 1):

  * Do **not** require passing through `FeaturePermissionManager` by default.
  * Optionally:

    * Add a server-config flag or `REPORT_CREATE` feature key to enable rate/gating in the future.

---

## 5. Shared Security Considerations

### 5.1 Denial vs. escalation

* The feature permission system must **never** grant new powers to roles that lack:

  * The required Discord permission, or
  * Sufficient position in role hierarchy, or
  * Existing “logical” staff roles (`is_moderator`, staffapp creator/reviewer, ticket support roles).
* It can only:

  * Further restrict who, among already-eligible accounts, can use each feature.

### 5.2 Views and modals

* For any action triggered from a `discord.ui.View` that corresponds to a slash command:

  * Must call `FeaturePermissionManager.check()` with:

    * The same `FeatureKey`.
    * A `base_check` with equivalent semantics.
* This prevents bypassing feature restrictions via buttons even if slash commands are blocked.

### 5.3 Logging and auditability

* All **denied** attempts on Tier 3 and Tier 2 commands **may** be logged to moderation log in a low-noise way, e.g.:

  * Throttle repeated denials from the same user.
  * Include:

    * User, guild, command, feature key, base_check result vs. feature permission result.

---

## 6. Implementation Checklist

1. **Refactor Phase 1 permission manager into a reusable service**

   * Make sure `FeaturePermissionManager` is constructible once and injected into cogs that need it (e.g. via `bot.perms` or similar).

2. **Add/extend `FeatureKey` enum**

   * Add all keys referenced in this spec (staffapp, tickets, moderation).
   * Ensure they’re stable strings; changing them later will break configs.

3. **Staff Applications integration**

   * Implement `_base_check_staffapp_template` and `_base_check_staffapp_review`.
   * Wrap:

     * `/staffapp config *`, `/staffapp template *` with `STAFFAPP_TEMPLATE_MANAGE`.
     * `/staffapp queue`, `/staffapp set-status`, review buttons with `STAFFAPP_REVIEW`.

4. **Tickets integration**

   * Implement `_base_check_tickets_admin` and `_base_check_tickets_close`.
   * Wrap:

     * `/ticket-setup`, `/ticket-panel`, `/tickets` with `TICKETS_ADMIN`.
     * `/close-ticket` command and `TicketControlView.close_ticket_button` with `TICKETS_CLOSE`.

5. **Moderation integration**

   * Implement `_base_check_moderation`.
   * For each moderation command:

     * Identify underlying Discord permissions & target logic.
     * Wire to appropriate `FeatureKey.MOD_*`.
   * Keep `/report` as user-level; only optionally wire `REPORT_CREATE` if an opt-in server config is added.

6. **Regression testing**

   * With **empty feature-permissions config**:

     * All commands behave as they did before (phase 2 must be backward compatible).
   * With a restricted config:

     * Verify:

       * Non-admin staff can be allowed/denied specific features.
       * Admins/owner are never blocked by feature permissions (only by Discord perms).

7. **Documentation**

   * Update `README` and/or `docs/`:

     * Add a “Permissions & Roles” section explaining:

       * How `/perms` works (Phase 1).
       * How specific cogs (staffapp, tickets, moderation) are controlled via feature keys.
       * Examples: “Allow Senior Mods to close tickets but not ban users”.


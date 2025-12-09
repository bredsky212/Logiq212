# AI Contribution Guidelines for Logiq212 (Logiq Fork)

_This document is for the coding agent working on the `Logiq212` fork of
[programmify/Logiq](https://github.com/programmify/Logiq). It defines how new
features must be designed and implemented so they stay stable, secure, and
maintainable._

---

## 1. Project Overview & Constraints

- **Stack**: Python 3.11+, `discord.py` 2.x (slash commands via `discord.app_commands`), MongoDB (Atlas recommended).   
- **Structure**:
  - `main.py` – bot bootstrap, DB init, cog loading, command sync.
  - `cogs/` – feature modules (tickets, moderation, music, staff applications, feature permissions, vcmod, etc.).
  - `database/` – `DatabaseManager` plus models and helpers.
  - `utils/` – shared utilities (`embeds`, permissions, etc.).
  - `web/` – any web endpoints used by the bot.

**Key constraints for all new code:**

1. Do not break existing commands or DB schemas unless explicitly requested.
2. All new behavior must respect Discord’s permission model and role hierarchy.
3. All new commands must respond to interactions within Discord’s timeout
   (3 seconds) either with a direct response or a deferred response. 
4. Prefer extending existing patterns (cogs, embeds, DB helpers) over inventing new ones.

---

## 2. General Coding Style

- Use **PEP 8** conventions for names and formatting.
- Use **type hints** for all function signatures in new code.
- Prefer **dataclasses** (or simple `@dataclass`-style models) for internal
  data structures where suitable.
- Imports: standard lib → third-party → local, with blank lines between groups.
- Use **f-strings** for interpolation.
- Avoid magic numbers and strings – promote them to constants (e.g.,
  default durations, collection names, feature keys).

---

## 3. Cogs & Slash Commands

### 3.1 Cog structure

- Every feature lives in a dedicated cog:

  ```python
  class Tickets(commands.Cog):
      def __init__(self, bot: commands.Bot, db: DatabaseManager):
          self.bot = bot
          self.db = db
          self.perms = bot.perms  # shared FeaturePermissionManager
  ```

* Cogs must subclass `commands.Cog`.
* Cogs should keep **minimal state**; long-lived data belongs in MongoDB.

### 3.2 Slash commands in cogs

* Use `discord.app_commands` decorators on methods inside cogs.

  ```python
  from discord import app_commands

  @app_commands.command(name="ping", description="Test latency")
  async def ping(self, interaction: discord.Interaction):
      await interaction.response.send_message("Pong!")
  ```

* For grouped commands (like `/vcmod suspend`, `/perms feature-allow`), use
  `app_commands.Group` defined as a **class attribute**:

  ```python
  class VCMod(commands.Cog):
      vcmod = app_commands.Group(
          name="vcmod",
          description="Voice chat moderation tools",
          guild_only=True,
      )
  ```

* **Registration best practice**: register the **bound instance** in `setup` so
  callbacks stay wired to the cog:

  ```python
  async def setup(bot: commands.Bot):
      cog = VCMod(bot, bot.db)
      await bot.add_cog(cog)
      existing = bot.tree.get_command("vcmod")
      if existing:
          bot.tree.remove_command("vcmod", type=discord.AppCommandType.chat_input)
      bot.tree.add_command(cog.vcmod)  # bind the instance, not the class
  ```

  Adding the class attribute (`VCMod.vcmod`) directly can trigger
  `CommandSignatureMismatch` and “application didn’t respond”.

### 3.3 Interaction responses

* If command work may exceed ~1s, **defer** first:

  ```python
  await interaction.response.defer(ephemeral=True, thinking=True)
  # ...do work...
  await interaction.followup.send(embed=..., ephemeral=True)
  ```

* For light commands, you may respond directly:

  ```python
  await interaction.response.send_message(embed=..., ephemeral=True)
  ```

* Each command **must guarantee exactly one response path**, even when an
  exception occurs (see error handling).

---

## 4. Permissions & Security Model

There are **three layers** of permissions to respect:

1. **Discord-native** permissions (`administrator`, `manage_guild`,
   `moderate_members`, channel-specific permissions).
2. **Role hierarchy rules** (you cannot act on users with equal/higher top role;
   never act on the server owner; be careful with administrators).
3. **Feature-level overrides** using `FeaturePermissionManager` and `FeatureKey`.

### 4.1 Base permission checks

Each cog that controls sensitive actions must define a lightweight base check:

```python
def _base_moderation_check(self, member: discord.Member) -> bool:
    return (
        member.guild_permissions.moderate_members
        or member.guild_permissions.ban_members
        or member.guild_permissions.kick_members
    )
```

Use this together with the feature permission manager:

```python
async def _can_use(self, member: discord.Member, feature: FeatureKey) -> bool:
    return await self.perms.check(member, feature, self._base_moderation_check)
```

### 4.2 Role hierarchy / safety

Whenever an action targets a user (warn, timeout, kick, ban, VC suspension, etc.):

```python
def _hierarchy_block(self, moderator: discord.Member, target: discord.Member) -> Optional[str]:
    if target == moderator.guild.owner:
        return "You cannot act on the server owner."
    if target.guild_permissions.administrator:
        return "You cannot act on an administrator."
    if target.top_role >= moderator.top_role:
        return "You cannot act on someone with an equal or higher role."
    return None
```

* Always call this before executing the action.
* If it returns a message, send it as an **ephemeral error embed** and abort.

### 4.3 FeaturePermissionManager usage

* All **new privileged commands** must be associated with a `FeatureKey`
  (e.g. `MOD_VC_SUSPEND`, `TICKETS_ADMIN`, `STAFFAPP_REVIEW`, etc.).

* Before executing a command:

  ```python
  if not await self._can_use(interaction.user, FeatureKey.MOD_VC_SUSPEND):
      await interaction.followup.send(
          embed=EmbedFactory.error("No Permission", "You do not have permission to use this command."),
          ephemeral=True,
      )
      return
  ```

* For **Tier 1** actions (low-risk, high-utility, like `/report`):

  * Keep them open by default.
  * Respect overrides **if** the guild configured them, but do not require any
    special permissions unless explicitly requested.

### 4.4 Feature permissions commands

* The `perms` cog is the **only** place where roles can be allowed/denied for
  features.
* Only allow **Admin / Manage Guild / Owner** to use `/perms` commands.
* Any new feature key must be:

  * Added to `FeatureKey` enum.
  * Documented in the permissions README section.
  * Checked in its owning cog.

- Sensitive features remain locked until `/perms security-bootstrap` runs. Protected roles (admin/manage_guild and any explicitly added) and the guild owner must never be targeted, assigned, or removed by the bot.
- Resolve log channels via `utils.logs.resolve_log_channel(db, guild, purpose)` instead of hardcoding `log_channel`; `/setlogchannel-advanced` sets per-purpose channels (`reports`, `moderation`, `vcmod`, `tickets`, `feature_permissions`, `default`).

---

## 5. Database Access & Schemas

### 5.1 DatabaseManager usage

* Always use existing `DatabaseManager` async methods.
* If you need new collections or queries:

  * Add explicit helper methods (`get_feature_permission`, `upsert_feature_permission`, `create_suspension`, etc.).
  * Keep all raw Mongo operations inside `DatabaseManager`, **not** in cogs.

### 5.2 Schema evolution

* When adding new collections (e.g. `feature_permissions`, `feature_permissions_audit`, `suspensions`):

  * Define a model-like helper in `database.models` (e.g. `Suspension`).
  * Keep field names stable and documented.
  * Use `datetime` in UTC (`timezone.utc`).

### 5.3 Atomic updates & safety

* Prefer atomic updates (`$set`, `$push`, `$addToSet`) via helper methods to avoid race conditions.
* When marking something “closed” (tickets, suspensions, applications):

  * Set `active=False`.
  * Add `resolved_at` and `resolved_by` for traceability.

---

## 6. Embeds, UX & Ephemeral Responses

* Use `EmbedFactory` and `EmbedColor` for all user-visible messages to preserve
  the visual style of the bot.
* Use **ephemeral responses** for:

  * Permission errors.
  * Configuration commands (perms, staffapp templates, etc.).
  * Moderation commands that shouldn’t spam public channels.
* Use **public messages** only when:

  * It’s part of the expected UX (e.g. ticket panel, game panels).
  * Server policy explicitly wants visible moderation actions.

---

## 7. Logging & Error Handling

### 7.1 Logging

* Each file must define a module-level logger:

  ```python
  import logging
  logger = logging.getLogger(__name__)
  ```

* Log levels:

  * `INFO`: high-level events (cog loaded, command executed, key DB change).
  * `WARNING`: suspicious but recoverable situations (missing log channel, no permissions to send logs).
  * `ERROR` or `logger.exception(...)`: unexpected exceptions.

* For feature permission denials or security-sensitive failures, use the
  **throttled denial logging** helper in the permission manager (if available).

### 7.2 Error handling in commands

* Wrap complex command bodies in a `try/except`:

  ```python
  try:
      # main logic
  except discord.Forbidden:
      await interaction.followup.send(
          embed=EmbedFactory.error("Error", "I don't have permission to do that."),
          ephemeral=True
      )
  except discord.HTTPException as e:
      await interaction.followup.send(
          embed=EmbedFactory.error("Error", f"Discord API error: {e}"),
          ephemeral=True
      )
  except Exception as e:
      logger.exception("Error in /some-command: %s", e)
      await interaction.followup.send(
          embed=EmbedFactory.error("Error", "Something went wrong."),
          ephemeral=True
      )
  ```

* Never leave an interaction without a final response in the exception path.

### 7.3 Command sync & binding hygiene
- Keep `/sync` global (`tree.sync()`) unless a guild-only sync is explicitly required.
- After changing command structure/choices, restart and run `/sync` to avoid stale definitions.
- When registering grouped commands, add the bound cog instance to the tree (see §3.2) to avoid `CommandSignatureMismatch` and timeouts.

---

## 8. VC Moderation & Time-Limited Actions

For features like VC suspension:

* **Durations** should be centralized (mapping like `{"2h": 7200, ...}`).
* Always store:

  * `guild_id`, `user_id`, `moderator_id`, `reason`,
    `duration_seconds`, `started_at`, `ends_at`, `active`.
* On new suspension:

  * Close any previously active suspensions for that user in that guild.
* When lifting a suspension:

  * Update DB record (set `active=False`, `resolved_at`, `resolved_by`).
  * Apply/clear timeouts with the discord.py API: `member.timeout(timedelta, reason=...)` and `member.timeout(None, reason=...)`. Do **not** pass the raw `communication_disabled_until` field to `Member.edit` on discord.py 2.x.

---

## 9. Testing & Manual Verification

* Keep tests light but meaningful:

  * Unit tests for new DB helpers and permission logic.
  * Where possible, tests for `_base_check` + feature overrides.
* For features that must be manually tested:

  * Document a short “manual test checklist” in the PR description
    (e.g. “Steps: `/perms feature-allow`, then `/vcmod suspend` user X…”).

---

## 10. Git & PR Practices

* Branch naming: `feature/<name>`, `fix/<name>`, `refactor/<name>`.
* Each PR should:

  * Be scoped to one logical feature or fix.
  * Include a brief summary, affected cogs, and any DB changes.
  * Note any **breaking changes** to command signatures or behaviour.
* Do not reformat or mass-edit files that are unrelated to your change.

---

## 11. When in Doubt

When you’re unsure about how to integrate a new feature:

1. Look at similar existing cogs (tickets, moderation, staff applications) and
   mimic their patterns.
2. Default to:

   * Strong Discord permission checks.
   * FeaturePermissionManager integration.
   * Ephemeral responses for configuration / moderation UX.
3. Err on the side of **more logging and more safety**, not less.

If a change might affect security (permissions, moderation powers, feature
overrides), **treat it as Tier 2/3** and require explicit confirmation or
configuration from the human maintainer before enabling for general use.


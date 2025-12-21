Based on the current Logiq repo structure (cogs, MongoDB via `db_manager`, log channel via `/setlogchannel`, tickets via `/ticket-setup` etc.). ([github.com][1])

````markdown
# `/report` Command – User Report System for Logiq

## 1. Goal & Scope

We want to add a **new user-facing slash command**:

> `/report` – let any member report a user + optional message to the moderation team.

**MVP behavior:**

- Collect:
  - **Reported user**
  - **Report category** (spam, harassment, NSFW, scam, other…)
  - **Free-text description**
  - Optional **message link** (Discord jump URL)
- **Store** each report in MongoDB (`reports` collection).
- **Notify moderators** via the existing **log channel** configured in Logiq (set via `/setlogchannel`).
- Provide an **ephemeral confirmation** to the reporter.
- No LLM, no auto-moderation yet. This is just **structured reporting + logging**.

This command should fit naturally into the existing **moderation/tickets/logging** architecture of Logiq.   


## 2. Placement in Codebase

- **Cog:** Implement `/report` inside the **moderation layer** so it feels like a core moderation tool.

  **Option A (preferred):**
  - Extend `cogs/moderation.py` and add the `/report` command on the existing moderation cog class.

  **Option B (if moderation cog is already too large):**
  - Create a new cog `cogs/reporting.py` with its own `ReportingCog`.
  - Loader already auto-loads all `.py` files in `cogs/` (see startup logs), so just adding the file should be enough.

- **Database:** Use the existing MongoDB infrastructure (`database/db_manager.py`, `database/models.py`) to:
  - Introduce a new **Report** data model, and
  - Persist documents in a `reports` collection.

- **Config:**
  - No new `.env` entries required.
  - No new `config.yaml` keys required for MVP.
  - Reuse the **log channel** already used by moderation/admin (`/setlogchannel`).   


## 3. Command Design

### 3.1 Slash Command Signature

Create a **slash command**:

```text
/report <user> <category> <reason> [message_link]
````

Parameters:

* `user` – `discord.Member` (required)

  * User being reported.

* `category` – `str` (required, as an enum/choice)
  Suggested choices (exact labels are up to you but keep them short):

  * `spam`
  * `harassment`
  * `hate`
  * `nsfw`
  * `scam`
  * `other`

* `reason` – `str` (required)

  * Free text describing what happened.
  * Validate length (e.g. 10–512 characters). If too short or too long, return an ephemeral error.

* `message_link` – `str | None` (optional)

  * A Discord message URL like
    `https://discord.com/channels/<guild_id>/<channel_id>/<message_id>`.
  * If provided:

    * Parse the IDs from the URL.
    * Try to fetch the message (if bot has access).
    * If fetch fails (unknown channel, missing perms, invalid URL), handle gracefully:

      * Still store the plain `message_link` string in DB,
      * Note in the mod embed that the message could not be fetched.

### 3.2 Permissions & Cooldown

* **Who can use `/report`?**

  * Any regular member; **no admin/mod permission** required.

* **Checks:**

  * Command must be used **in a guild**, not in DMs. If `interaction.guild is None`, respond with ephemeral error:
    “This command can only be used inside a server.”

  * Optional: prevent silly use-cases:

    * If `user == interaction.user`, optionally reject or allow; for MVP we can **allow self-report**, but include that fact clearly in logs.

* **Cooldown:**

  * Add **per-user cooldown** to avoid spam.
  * Example: **1 report per 60 seconds per user**.
  * On cooldown hit, reply ephemerally with a friendly message.

Use the same cooldown mechanism already used elsewhere in the cogs (check other commands for pattern).

## 4. Data Model & MongoDB Collection

### 4.1 New Model: `Report`

Add a new model in `database/models.py` (or wherever models live) for reports.

**Fields (MVP):**

* `_id` – `ObjectId` (Mongo default)
* `guild_id` – `int`
* `reporter_id` – `int`
* `reported_user_id` – `int`
* `category` – `str`
* `reason` – `str`
* `message_link` – `str | None`
* `message_id` – `int | None` (parsed from link if available)
* `channel_id` – `int | None` (parsed from link if available or from context)
* `status` – `str` (default `"open"`)
* `created_at` – ISO datetime (`datetime.utcnow()`)
* `resolved_at` – ISO datetime | `None` (for future moderation tools)
* `resolved_by_id` – `int | None` (for future tools)
* `moderation_action` – `str | None` (for future tools; e.g. `"warn"`, `"timeout"`, `"ban"`)

### 4.2 Access via DB Manager

Use the existing DB abstraction for other collections (tickets, warnings, etc.) as reference:

* Add a `reports` property / accessor to `DBManager` (or equivalent) so cogs can call something like:

  ```python
  reports_collection = self.bot.db.reports
  await reports_collection.insert_one(report_dict)
  ```

* Add **indexes** if other cogs will query by `guild_id`, `status`, `reported_user_id` later. For MVP, this is optional but recommended:

  * Index on `(guild_id, status)`
  * Index on `(reported_user_id, guild_id)`

## 5. Runtime Behavior

### 5.1 High-Level Flow

When a user runs `/report`:

1. **Validate** input:

   * Check guild.
   * Apply cooldown.
   * Validate `reason` length.
   * If `message_link` set, try to parse & fetch message.

2. **Persist** in MongoDB:

   * Build a dict matching the `Report` model described above.
   * Insert into `reports` collection.

3. **Send moderator notification** to the configured log channel:

   * Use same log channel as other moderation actions (`/setlogchannel` in Logiq).
   * If no log channel is configured, still store the report in DB and reply ephemerally that “No mod log channel is configured; please ask an admin to run `/setlogchannel`.”

4. **Reply to the reporter**:

   * Ephemeral embed or message like:

     > ✅ Your report has been sent to the moderation team.
     > Thank you for helping keep the server safe.

### 5.2 Mod Log Embed Layout

Use the existing `utils/embeds` helper if available for consistent embed styling.

**Embed suggestion:**

* **Title:** `New User Report`
* **Color:** Same palette used for moderation embeds.
* **Fields:**

  * `Reporter` → mention + ID
  * `Reported User` → mention + ID
  * `Guild` → name + ID
  * `Channel` → channel mention (if known)
  * `Category` → string
  * `Reason` → shortened (truncate if > 1024 chars)
  * `Message` →

    * If fetched successfully:

      * Content preview (cut to 400–500 chars max)
      * "Jump to message" hyperlink using the raw `message_link`
    * If not fetched:

      * Show raw `message_link` or a note “Message could not be fetched; link might be invalid or permission-restricted.”
  * `Report ID` → string-version of `_id` from Mongo (for future reference)

The embed should be posted **once** per report to the log channel. No reply in the channel where the report was created (only ephemeral response to user).

### 5.3 Error Cases

* **No log channel configured:**

  * Still **insert into DB**.
  * Ephemeral answer to user like:

    * “Your report has been recorded, but no moderation log channel is configured. Please ping a server admin.”

* **MongoDB error on insert:**

  * Catch exceptions around `insert_one`.
  * Log the exception using `utils.logger`.
  * Reply ephemerally:

    * “Something went wrong while recording your report. Please try again later or contact a moderator directly.”

* **Bad/invalid `message_link`:**

  * Store the raw string.
  * Indicate in embed that message fetch failed.

## 6. Implementation Steps for the Agent

### Step 1 – Inspect Existing Patterns

1. Open `cogs/moderation.py` to see how:

   * Existing mod commands are defined (likely `app_commands.command`).
   * Log channel is retrieved for warnings/bans/etc.
2. Open `database/models.py` and `database/db_manager.py`:

   * See how models are structured.
   * See how collections are exposed to cogs.
3. Open `utils/embeds.py` and `utils/logger.py`:

   * Reuse embed styles and logging.

### Step 2 – Add `Report` Model and Collection

1. Add a `Report` model (dataclass or simple schema) to `database/models.py`.
2. In `db_manager.py`, add support for:

   * A `reports` collection.
   * Optional creation of indexes on `guild_id`, `status`, `reported_user_id`.
3. Ensure the bot’s startup (`main.py`) still creates the DB manager and that `self.bot.db` is accessible from cogs (same pattern as existing code).

### Step 3 – Implement `/report` in the Moderation Layer

1. **Option A:** in `cogs/moderation.py`:

   * Add a new method on the moderation cog:

     ```python
     @app_commands.command(
         name="report",
         description="Report a user and optionally a message to the moderation team"
     )
     @app_commands.describe(
         user="The user you want to report",
         category="Type of issue (spam, harassment, etc.)",
         reason="Explain what happened",
         message_link="Optional link to the offending message"
     )
     async def report(
         self,
         interaction: discord.Interaction,
         user: discord.Member,
         category: str,
         reason: str,
         message_link: Optional[str] = None,
     ):
         ...
     ```

   * Add `@app_commands.choices` for `category` if the existing code uses that pattern.

   * Add cooldown decorator consistent with other commands.

2. Inside the command:

   * Validate:

     * `interaction.guild` not None.
     * `len(reason)` within expected bounds.
   * Parse & optionally fetch the message from `message_link`.
   * Build the `report_data` dictionary.
   * Insert into MongoDB via `self.bot.db.reports.insert_one(report_data)`.
   * Send embed to the mod-log channel.
   * Reply ephemerally to the user.

### Step 4 – Unit Tests

Create tests in `tests/` (name consistent with existing tests, e.g. `test_report_command.py`):

* Test that, given valid input and mock DB:

  * `insert_one` is called with expected payload.
  * An embed is sent to the log channel.
  * The user receives an ephemeral confirmation.

* Test that, if no log channel is configured:

  * DB insert still occurs.
  * A different ephemeral message is sent.

* Test invalid `message_link` parsing path:

  * DB insert uses `None` for `message_id` / `channel_id`.
  * Mod embed indicates fetch failure.

Use existing test patterns and helpers; do not introduce a new testing framework.

## 7. Future Extensions (Not in MVP)

**Important:** The coding agent should NOT implement these yet, but the design should not block them.

Future ideas:

1. **Moderator follow-up commands:**

   * `/reports [status]` – list reports for the guild.
   * `/resolve-report <report_id> <action>` – mark as resolved, log taken action.

2. **Integration with the ticket system:**

   * Optionally auto-open a ticket per report in the same category used by `/ticket-setup`.
   * This could create a private channel where moderators discuss the specific report.

3. **LLM-powered triage:**

   * A background worker or on-demand command that:

     * Summarizes the report + referenced message(s),
     * Suggests a severity level and recommended moderation action,
     * Populates `moderation_action` field in the `reports` collection.

The current design (storing structured report data in MongoDB + clear schema + `status`/`moderation_action` fields) is meant to make these future features easy to add.

## 8. Acceptance Criteria

The `/report` feature is considered done when:

1. `/report` appears in Discord and is usable by regular members.

2. A user can successfully run:

   ```text
   /report @user harassment "They sent insults in #general" https://discord.com/channels/...
   ```

3. After the command:

   * A new document exists in the `reports` collection in MongoDB with the expected fields.
   * An embed appears in the configured log channel containing:

     * Reporter, reported user, category, reason, and message link/preview.

4. The reporter receives an ephemeral confirmation.

5. If MongoDB is unavailable, the command fails gracefully with a clear ephemeral error and logs the exception.

6. The rest of the bot behavior (tickets, moderation, games, etc.) is **unchanged**.

```
```

[1]: https://github.com/programmify/Logiq "GitHub - programmify/Logiq:  Logiq - Open-source Discord bot and MEE6 alternative with verification, tickets, role menus, leveling, economy, moderation, games, music, and more. Fully free and self-hostable. Built with discord.py and MongoDB."

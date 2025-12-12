# Staff Applications System – Design Spec

## 1. Goal & Scope

We want a **staff application system** for Logiq212 using Discord’s
**buttons + modals** so users can apply for staff teams (Design, R&D, etc.)
without leaving Discord.

Discord modals are form-like dialogs that let us collect multiple text
fields in a single interaction, with up to **5 text inputs per modal**,
each as an action row.   

### High-level user experience

Per team (Design, R&D, …):

1. Admin / staff-mod creates an **application template** and a public
   **“Apply” panel** in an `apply` channel for that team.
2. Users click the **Apply** button → a **modal** pops up asking them a few
   questions (motivation, experience, availability, etc.).   
3. On submit, the bot:
   - Saves the application in **MongoDB**.
   - Sends an **embed** to a **team review channel** with action buttons:
     `Interview`, `Accept`, `Reject`.
   - Sends the applicant an **ephemeral confirmation** (and optionally DM).
4. Staff selectors review the application:
   - Use buttons or slash commands to mark as **interview / accepted /
     rejected**.
   - Applicant receives a DM and status is updated in DB.

No web dashboard required; everything is inside Discord (similar to bots
like ApplicationForms / Staff Applications that use modals for staff apps).   

---

## 2. Roles & Permissions

We introduce three levels of control:

- **Admins** – users with Discord `Administrator` or guild owner.
- **Staff creators** – role(s) allowed to **configure** templates & panels
  (e.g. `@staff-mod`).
- **Staff reviewers** – role(s) allowed to **process** applications
  (e.g. `@staff-select`, `@staff-mod`).

### 2.1 Config document

Per guild, store a `staff_app_config` document:

```json
{
  "guild_id": 123456789012345678,
  "creator_roles": [111111111111111111],     // e.g. staff-mod
  "reviewer_roles": [111111111111111111, 222222222222222222] // staff-mod + staff-select
}
```

### 2.2 Permission helpers

In the staff applications cog:

```python
def _is_creator(self, member: discord.Member, config: StaffAppConfig) -> bool:
    return member.guild_permissions.administrator or any(
        role.id in config.creator_roles for role in member.roles
    )

def _is_reviewer(self, member: discord.Member, config: StaffAppConfig) -> bool:
    return _is_creator(member, config) or any(
        role.id in config.reviewer_roles for role in member.roles
    )
```

These helpers will be used in both **slash commands** and **button / modal**
callbacks.

---

## 3. Data Model & MongoDB Collections

All data uses the existing async MongoDB layer (`DBManager`) similarly
to `Report` / `reports`.

### 3.1 Templates: `StaffApplicationTemplate`

File: `database/models.py`

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

@dataclass
class StaffApplicationField:
    key: str              # stable key used in DB ("experience", "motivation", ...)
    label: str            # visible label in modal
    style: str            # "short" | "paragraph"
    required: bool = True
    max_length: int = 1000
    placeholder: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "style": self.style,
            "required": self.required,
            "max_length": self.max_length,
            "placeholder": self.placeholder,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplicationField":
        return cls(
            key=data["key"],
            label=data["label"],
            style=data.get("style", "paragraph"),
            required=data.get("required", True),
            max_length=data.get("max_length", 1000),
            placeholder=data.get("placeholder"),
        )


@dataclass
class StaffApplicationTemplate:
    guild_id: int
    template_id: str             # string id (ObjectId or slug)
    name: str                    # "Design Team"
    description: str             # description shown in the panel embed
    team_role_id: Optional[int]  # role for that team (optional)
    apply_channel_id: int        # channel where Apply panel lives
    review_channel_id: int       # channel where applications are posted
    fields: List[StaffApplicationField]
    created_by_id: int
    created_at: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "team_role_id": self.team_role_id,
            "apply_channel_id": self.apply_channel_id,
            "review_channel_id": self.review_channel_id,
            "fields": [f.to_dict() for f in self.fields],
            "created_by_id": self.created_by_id,
            "created_at": self.created_at,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplicationTemplate":
        return cls(
            guild_id=data["guild_id"],
            template_id=str(data["template_id"]),
            name=data["name"],
            description=data.get("description", ""),
            team_role_id=data.get("team_role_id"),
            apply_channel_id=data["apply_channel_id"],
            review_channel_id=data["review_channel_id"],
            fields=[StaffApplicationField.from_dict(f) for f in data.get("fields", [])],
            created_by_id=data["created_by_id"],
            created_at=data.get("created_at", datetime.utcnow()),
            is_active=data.get("is_active", True),
        )
```

> Note: each modal is limited to 5 text inputs by the Discord API, so
> `len(fields)` must not exceed 5. Validate this on creation.

### 3.2 Applications: `StaffApplication`

```python
@dataclass
class StaffApplicationAnswer:
    key: str
    label: str
    value: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplicationAnswer":
        return cls(
            key=data["key"],
            label=data["label"],
            value=data["value"],
        )


@dataclass
class StaffApplication:
    guild_id: int
    template_id: str
    application_id: str          # ObjectId string
    applicant_id: int
    team_role_id: Optional[int]
    answers: List[StaffApplicationAnswer]
    status: str = "pending"      # pending | interview | accepted | rejected
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    reviewed_by_id: Optional[int] = None
    review_notes: Optional[str] = None
    review_channel_id: int = 0
    review_message_id: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "template_id": self.template_id,
            "application_id": self.application_id,
            "applicant_id": self.applicant_id,
            "team_role_id": self.team_role_id,
            "answers": [a.to_dict() for a in self.answers],
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewed_by_id": self.reviewed_by_id,
            "review_notes": self.review_notes,
            "review_channel_id": self.review_channel_id,
            "review_message_id": self.review_message_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StaffApplication":
        return cls(
            guild_id=data["guild_id"],
            template_id=str(data["template_id"]),
            application_id=str(data["application_id"]),
            applicant_id=data["applicant_id"],
            team_role_id=data.get("team_role_id"),
            answers=[StaffApplicationAnswer.from_dict(a) for a in data.get("answers", [])],
            status=data.get("status", "pending"),
            created_at=data.get("created_at", datetime.utcnow()),
            updated_at=data.get("updated_at", datetime.utcnow()),
            reviewed_by_id=data.get("reviewed_by_id"),
            review_notes=data.get("review_notes"),
            review_channel_id=data.get("review_channel_id", 0),
            review_message_id=data.get("review_message_id", 0),
        )
```

### 3.3 DB manager integration

In `database/db_manager.py`:

* Add collection accessors:

```python
@property
def staff_application_templates(self):
    return self.db.staff_application_templates if self.db else None

@property
def staff_applications(self):
    return self.db.staff_applications if self.db else None
```

* In `_ensure_indexes`, create indexes:

```python
await self.db.staff_application_templates.create_index(
    [("guild_id", 1), ("template_id", 1)], unique=True
)
await self.db.staff_applications.create_index(
    [("guild_id", 1), ("status", 1)]
)
await self.db.staff_applications.create_index(
    [("guild_id", 1), ("template_id", 1), ("status", 1)]
)
await self.db.staff_applications.create_index(
    [("applicant_id", 1), ("guild_id", 1)]
)
```

* Helper methods (async):

```python
async def create_staff_template(self, data: Dict[str, Any]) -> str: ...
async def get_staff_template(self, guild_id: int, template_id: str) -> Optional[Dict]: ...
async def list_staff_templates(self, guild_id: int) -> List[Dict]: ...
async def set_staff_template_active(self, guild_id: int, template_id: str, is_active: bool): ...

async def create_staff_application(self, data: Dict[str, Any]) -> str: ...
async def update_staff_application(self, guild_id: int, application_id: str, update: Dict[str, Any]): ...
async def get_staff_application(self, guild_id: int, application_id: str) -> Optional[Dict]: ...
async def query_staff_applications(self, guild_id: int, **filters) -> List[Dict]: ...
```

---

## 4. Staff Applications Cog

Create `cogs/staff_applications.py` with a `StaffApplications` cog and an
`app_commands.Group` named `staffapp`.

### 4.1 Group & registration

```python
class StaffApplications(commands.Cog):
    def __init__(self, bot: LogiqBot):
        self.bot = bot

    staffapp = app_commands.Group(
        name="staffapp",
        description="Configure and review staff applications",
        guild_only=True,
    )
```

Register this cog in the bot’s setup as with other cogs.

---

## 5. Configuration Commands

### 5.1 `/staffapp config set-creator-role`

```text
/staffapp config set-creator-role role:@staff-mod
```

* Only admins can run this.
* Stores / updates `creator_roles` in `staff_app_config`.

### 5.2 `/staffapp config add-reviewer-role`

```text
/staffapp config add-reviewer-role role:@staff-select
```

* Only admins / creators.
* Adds role ID to `reviewer_roles` array.

### 5.3 `/staffapp config remove-reviewer-role`

```text
/staffapp config remove-reviewer-role role:@staff-select
```

### 5.4 `/staffapp config show`

Shows current creator/reviewer role assignments.

All config commands reply ephemerally.

---

## 6. Template & Panel Commands

### 6.1 `/staffapp template create`

**Signature:**

```text
/staffapp template create
    name: "Design Team"
    team_role: @Design
    apply_channel: #apply-design
    review_channel: #design-apps
```

* Check `_is_creator`.

* Ensure `fields` for this template are within modal limits (≤5 inputs).

* Construct a `StaffApplicationTemplate` with **default fields**, e.g.:

  1. Motivation (paragraph)
  2. Experience (paragraph)
  3. Availability / timezone (short)
  4. Age or basic info (short) – optional

* Insert template to `staff_application_templates`.

* In `apply_channel`, send an **embed with Apply button**:

  * Title: `<name> Applications`
  * Description: `template.description` or default clarifying text.
  * Footer: “Click Apply to submit your application.”

  Use a `discord.ui.View` with a green button labeled `Apply`:

  ```python
  class StaffApplyView(discord.ui.View):
      def __init__(self, template_id: str):
          super().__init__(timeout=None)
          self.template_id = template_id

      @discord.ui.button(
          label="Apply",
          style=discord.ButtonStyle.success,
          custom_id="staffapp_apply:{template_id}",
      )
      async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
          ...
  ```

* Respond ephemerally: “Created application panel in #apply-design for Design Team.”

### 6.2 `/staffapp template list`

Lists templates for the guild:

* For each: ID, name, team role, apply channel, review channel, active/disabled.
* Ephemeral, only creators & admins.

### 6.3 `/staffapp template enable` / `disable`

```text
/staffapp template disable template_id:<id>
```

Toggles `is_active`. If disabled, the Apply button should respond with an
ephemeral “Applications for this team are currently closed.”

---

## 7. Apply Button → Modal

### 7.1 Button callback

When a user clicks `Apply`:

1. Parse `template_id` from `interaction.data["custom_id"]` (split on `:`).
2. Fetch template from DB; if missing or `is_active=False`, ephemeral error.
3. Build a `discord.ui.Modal` with up to 5 `TextInput` components based on
   `template.fields`.

Discord’s components reference documents the constraints for text inputs
(label length, placeholder length, etc.); enforce `max_length` accordingly.

### 7.2 Modal construction

```python
class StaffApplicationModal(discord.ui.Modal):
    def __init__(self, template: StaffApplicationTemplate):
        super().__init__(title=f"{template.name} Application")
        self.template = template
        self.inputs: Dict[str, discord.ui.TextInput] = {}

        for f in template.fields:
            style = (
                discord.TextStyle.short
                if f.style == "short"
                else discord.TextStyle.paragraph
            )
            input = discord.ui.TextInput(
                label=f.label,
                placeholder=f.placeholder or discord.utils.MISSING,
                required=f.required,
                max_length=f.max_length,
                style=style,
                custom_id=f.key,
            )
            self.inputs[f.key] = input
            self.add_item(input)
```

Show the modal:

```python
await interaction.response.send_modal(StaffApplicationModal(template))
```

Modal behaviour with interactions is documented in Discord’s official
components & modal docs and in libs like Pycord / discord.js guides.

### 7.3 Modal `on_submit`

In `StaffApplicationModal.on_submit`:

1. Build `answers` list from `self.inputs`:

   ```python
   answers = [
       StaffApplicationAnswer(
           key=field.key,
           label=field.label,
           value=self.inputs[field.key].value,
       )
       for field in template.fields
   ]
   ```

2. Insert application into `staff_applications`:

   * `status = "pending"`
   * `applicant_id = interaction.user.id`
   * `team_role_id = template.team_role_id`
   * `review_channel_id = template.review_channel_id`
   * `review_message_id` temporarily 0 (will set after posting review embed).

3. Post review embed to `review_channel`.

4. Edit DB document to set `review_message_id` to the created message ID.

5. Respond to the user:

   ```python
   await interaction.response.send_message(
       "✅ Your application has been submitted. Staff will review it soon.",
       ephemeral=True,
   )
   ```

Optionally DM the applicant with a copy of their answers.

---

## 8. Review Embed & Action Buttons

### 8.1 Review embed format

In `review_channel`, send an embed for each new application:

* Title: `New Application – <template.name>`
* Description: short summary.
* Fields:

  * `Applicant`: mention + ID
  * `Team`: role mention or name
  * For each answer:

    * Field name = label
    * Value = truncated answer (≤1024 chars)
  * `Status`: `Pending`
  * `Application ID`: DB ID (string)

Attach a `discord.ui.View` with three buttons:

* `Interview` – primary, `custom_id="staffapp_status:interview:<application_id>"`
* `Accept` – success, `custom_id="staffapp_status:accepted:<application_id>"`
* `Reject` – danger, `custom_id="staffapp_status:rejected:<application_id>"`

### 8.2 Status button callbacks

For each button:

1. Ensure `_is_reviewer(interaction.user, config)` is true.
2. Optionally show a small modal to collect **notes** (especially for
   rejection reasons).
3. Update application in DB:

   * `status`
   * `reviewed_by_id`
   * `review_notes`
   * `updated_at`
4. Edit review embed:

   * Update `Status` field to new status.
   * Add/Update a `Reviewed by` field with user mention + timestamp.
5. Notify applicant via DM:

   * `interview`: “You’ve been moved to interview for <team>; staff will contact you.”
   * `accepted`: “You’ve been accepted for <team>!” (optionally add `team_role`)
   * `rejected`: “We’re not proceeding with your application this time. …”

If DM fails (`Forbidden`), ignore gracefully.

---

## 9. Queue & Management Commands

### 9.1 `/staffapp queue`

List applications in the guild:

```text
/staffapp queue [team_role:@Design] [status:pending|interview|accepted|rejected]
```

* Only reviewers.
* Queries `staff_applications` by:

  * `guild_id`
  * optional `team_role_id`
  * optional `status` (default: `pending`)
* Returns a paginated embed with lines like:

  * `ID: 64f... – <@applicant> – Design – pending – created <date>`

Include “jump to review message” links using:

```text
https://discord.com/channels/<guild_id>/<review_channel_id>/<review_message_id>
```

### 9.2 `/staffapp set-status`

Backend override for status changes (when buttons are not enough):

```text
/staffapp set-status application_id:<id> status:<pending|interview|accepted|rejected> [notes]
```

* Only reviewers.
* Performs the same DB update & embed edit logic as the buttons.
* Optionally DM the applicant with the new status.

---

## 10. Persistent Views

Because review messages and apply panels must keep working across restarts,
register their views as **persistent views** in the bot’s startup (`setup_hook`):

```python
async def setup_hook(self):
    # existing setup hooks...
    self.add_view(StaffApplyViewPersistent())
    self.add_view(StaffApplicationReviewViewPersistent())
```

These versions of the views should be initialised with no state except the
`custom_id` patterns (template ID / application ID are encoded there).

Persistent views for buttons are the recommended pattern when you need
long-lived interactive messages.

---

## 11. Acceptance Criteria

Feature is considered complete when:

1. Admin / staff-mod can configure staff application roles using:

   * `/staffapp config set-creator-role`
   * `/staffapp config add-reviewer-role`
   * `/staffapp config show`

2. Creator can create at least one template:

   ```text
   /staffapp template create
       name:"Design Team"
       team_role:@Design
       apply_channel:#apply-design
       review_channel:#design-apps
   ```

   and a public **Apply** panel appears in `#apply-design`.

3. A regular member can click **Apply**, fill a modal form, and:

   * See an ephemeral confirmation.
   * Cause an embed to appear in `#design-apps` with their answers,
     status `Pending`, and `Interview/Accept/Reject` buttons.

4. Staff reviewers can:

   * Change status via buttons or `/staffapp set-status`.
   * See embed status updated and “Reviewed by …” info.
   * Confirm DB record reflects the new status and notes.

5. Applicant receives appropriate DMs on status changes (unless DMs are
   blocked, in which case it fails gracefully).

6. `/staffapp queue` lists pending applications correctly, including
   working “jump to message” links.

7. Restarts do **not** break old panels or review messages (persistent
   views). Clicking existing buttons after a restart still works.

8. No regression to existing Logiq212 features (tickets, moderation,
   `/report`, etc.), and all new code follows the project’s style
   (logging, error handling, type hints).


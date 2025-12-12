# Staff Applications – How to Use (Non-Developers)

This guide shows how to set up and run staff applications fully inside Discord using the `/staffapp` commands. No coding required.

## What you need
- Admin or creator role permission (configured below).
- Bot online with the staff applications feature deployed.
- A channel for users to click **Apply** (e.g., `#apply-design`).
- A channel for reviewers to see submissions (e.g., `#design-apps`).

## Step 1: Set permissions
Creators configure templates; reviewers process applications.

1) Set creator role (admins only):
```
/staffapp config set-creator-role role:@StaffMod
```
2) Add reviewer roles (admins or creators):
```
/staffapp config add-reviewer-role role:@StaffSelect
/staffapp config add-reviewer-role role:@StaffMod
```
3) Optional: set a default apply channel (used if a template omits apply_channel):
```
/staffapp config set-apply-channel channel:#apply-here
```
3) Check current config:
```
/staffapp config show
```

## Step 2: Create an application template
Creators or admins can create one template per team. The bot will post an **Apply** panel in the apply channel.

Example:
```
/staffapp template create
    name: "Design Team"
    team_role: @Design           # optional
    apply_channel: #apply-design # optional if default is set
    review_channel: #design-apps
    description: "Tell us about your design experience.\nList tools you use."
```
- Descriptions accept `\n` for new lines.
- Default fields: Motivation, Experience, Availability/Timezone, Age/Basic info (max 5 fields enforced).
- The bot posts an embed with an **Apply** button in `#apply-design` (or the default apply channel).
- Users click **Apply**, fill the modal, and get an ephemeral confirmation. Their application goes to `#design-apps`.

## Step 3: Process applications
Reviewers use the buttons on the review embed or a command.

- Buttons on the review message: **Interview**, **Accept**, **Reject**.
- Command override:
```
/staffapp set-status application_id:<id> status:<pending|interview|accepted|rejected> [notes]
```
The embed updates with the new status and “Reviewed by”. Applicants are DM’d (DM failures are ignored).

## Step 4: See the queue
Reviewers can list applications:
```
/staffapp queue [team_role:@Design] [status:pending|interview|accepted|rejected]
```
Each entry includes a jump link to the review message.

## Step 5: Open/close applications
Creators can enable/disable a template:
```
/staffapp template disable template_id:<id>
/staffapp template enable template_id:<id>
```
If disabled, clicking **Apply** returns an ephemeral “applications closed” message.

## Tips
- Make sure `/staffapp` commands are synced in your server (run `/sync` as admin if needed).
- Panels and review buttons are persistent across restarts.
- If you change reviewer/creator roles, re-run `/staffapp config show` to confirm.

# PR: Add /report Command and Persistence

## Summary
- add `/report` slash command with validation, cooldown, and user-facing confirmation
- persist reports in MongoDB via new `Report` model and `reports` collection accessors
- log reports to the configured moderation log channel with rich embeds and message-link handling
- extend database tests to cover report creation

## Branch
- feature/report-command

## Notes
- tests not run here; run `pytest` with Mongo available to validate

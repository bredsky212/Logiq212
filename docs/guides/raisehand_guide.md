# Raisehand Speaking Queue Guide

This guide explains how to run a timed speaking queue inside a voice channel text chat.

## Overview
- Starts a session per voice channel (one session per VC).
- Posts a queue panel in the VC text chat; members join by reacting or sending the emoji alone.
- Server-mutes everyone except the moderator and the current speaker.
- Automatically rotates speakers after a fixed turn duration (minutes).

## Requirements
- Module enabled: `modules.raisehand.enabled: true`.
- Bot permissions in the guild/channel:
  - Mute Members
  - Send Messages
  - Add Reactions
  - Read Message History
- Security bootstrap completed: `/perms security-bootstrap`.
- Feature permission allowed for moderators:
  - `/perms feature-allow feature:raisehand.manage role:@Moderator`

## Configuration
Example config block:
```yaml
raisehand:
  default_turn_minutes: 3
  emoji: "\U0001F44B"
  max_queue_display: 15
  panel_debounce_ms: 700
```

Notes:
- `default_turn_minutes` sets the turn length when `/raisehand start` is used without a value.
- `emoji` can be any single emoji string; use a unicode escape if your editor has encoding issues.
- `panel_debounce_ms` reduces API spam when the queue changes rapidly.

## Admin Setup (Quick)
1) Enable the module in your config (or use the minimal config example):
   - `modules.raisehand.enabled: true`
2) Restart the bot and run `/sync` if commands are missing.
3) Run `/perms security-bootstrap` (required for sensitive features).
4) Allow roles to manage raisehand:
   - `/perms feature-allow feature:raisehand.manage role:@Moderator`
5) (Optional) Set a log channel:
   - `/setlogchannel-advanced purpose:raisehand channel:#mod-log`

## Commands (Moderator)
Start a session (from the VC text chat):
```
/raisehand start turn_minutes:3
```

Stop the session and restore original mute states:
```
/raisehand stop
```

Skip the current speaker:
```
/raisehand skip
```

Extend the current turn (choices: 2, 3, 5 minutes):
```
/raisehand extend extra_minutes:3
```

Swap the current speaker with someone in the queue:
```
/raisehand swap user:@Member
```

Remove a user from the queue (or current speaker):
```
/raisehand remove user:@Member
```

Show the current queue status:
```
/raisehand status public:true full:true
```

## Behavior Notes
- Commands must be run inside the voice channel text chat for the target VC.
- The moderator must be connected to the same VC.
- Joining the queue requires being in the VC and reacting with the configured emoji (or sending only the emoji as a message).
- Users can appear more than once in the queue, but not in consecutive positions.
- On stop, the bot restores the original server mute state for each member it touched.
- Sessions restore after restarts when possible (if the moderator is still in the VC).

## Troubleshooting
- "Missing permissions": ensure the bot role has **Mute Members** and is above the target roles.
- "Invalid channel": run the command from the voice channel text chat.
- "No permission": allow `raisehand.manage` via `/perms`.

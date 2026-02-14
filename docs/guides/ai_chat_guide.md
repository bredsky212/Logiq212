# AI Chat Guide (Users and Admins)

This guide explains how to use and configure the AI chat feature powered by OpenRouter.

## Overview
- Commands live under `/ai ...`
- AI is deny-by-default: you must explicitly allow roles via `/perms`
- AI replies only work in allowlisted channels
- Keys are stored encrypted at rest (BYOK)
- Sessions are short-lived (TTL 24h, max 12 user turns)

## Quick Setup (Admins)
1) Enable the module in `config.yaml`:
```
modules:
  ai_chat:
    enabled: true
    max_tokens: 500
    max_tokens_cap: 500
```
2) Set encryption secret in your environment (required):
```
LOGIQ_AI_KEY_ENC_SECRET=your_random_secret
```
3) Enable AI in the guild:
```
/ai admin enable
```
4) Add at least one OpenRouter key:
```
/ai admin keys-add name:openrouter1 key:sk-or-... rpm:20 rpd:50
```
5) Allow roles to use AI:
```
/perms feature-allow feature:ai.use role:@Member
/perms feature-allow feature:ai.admin role:@Admin
```
6) Allow a channel:
```
/ai admin channel-allow-add channel:#ai-chat
```
7) (Optional) Set model and limits:
```
/ai admin model-set model_id:z-ai/glm-4.5-air:free
/ai admin limits-set user_cooldown_seconds:15 channel_cooldown_seconds:5 max_concurrent:3 max_tokens:500
```
8) If commands look missing after deploy:
```
/sync
```

## User Commands
Ask a question (default private response):
```
/ai ask prompt:... mode:fast private:true
```

Start a chat session (creates a thread if possible):
```
/ai chat-start private:true
```
If `private:true`, the bot creates a private thread (only you + admins). If `private:false`, it creates a public thread.
AI replies in threads are sent as plain messages (not embeds), split by paragraph, with mentions escaped and Markdown preserved.

Reset your session in the current channel:
```
/ai chat-reset
```

Stop your active session (keeps history unless delete is true):
```
/ai chat-stop delete:false
/ai chat-stop delete:true
```

Show the current model and channel status:
```
/ai model
```

Deprecated legacy commands (still available):
- `/ask` (use `/ai ask`)
- `/summarize` (use `/ai ask` with a summary prompt)
- `/clear-conversation` (use `/ai chat-reset`)

## Admin Commands
Enable AI for the guild:
```
/ai admin enable
```

Allowlist channels:
```
/ai admin channel-allow-add channel:#ai-chat
/ai admin channel-allow-remove channel:#ai-chat
/ai admin channel-allow-list
```

Manage keys (stored encrypted, never echoed back):
```
/ai admin keys-add name:openrouter1 key:sk-or-... rpm:20 rpd:50
/ai admin keys-list
/ai admin keys-list live:true
/ai admin keys-probe name:openrouter1
/ai admin keys-disable name:openrouter1
/ai admin keys-enable name:openrouter1
/ai admin keys-remove name:openrouter1
```

Adjust cooldowns and concurrency:
```
/ai admin limits-set user_cooldown_seconds:15 channel_cooldown_seconds:5 max_concurrent:3 max_tokens:500
```

Set a model (non-free models require confirmation):
```
/ai admin model-set model_id:z-ai/glm-4.5-air:free
/ai admin model-set model_id:provider/model-id confirm_paid:true
/ai admin model-set model_id:z-ai/glm-4.5-air:free max_tokens:500
```

List OpenRouter models:
```
/ai admin models-list
/ai admin models-list filter:glm
```

## Provider Routing (Advanced)
OpenRouter can route to specific providers. If you do nothing, OpenRouter auto-selects.

Allow or deny providers:
```
/ai admin provider-allow-add provider:AtlasCloud
/ai admin provider-allow-remove provider:AtlasCloud
/ai admin provider-deny-add provider:ModelRun
/ai admin provider-deny-remove provider:ModelRun
```

Set a preferred order (comma-separated):
```
/ai admin provider-order-set providers:ProviderA,ProviderB
/ai admin provider-order-clear
```

View current provider routing:
```
/ai admin provider-config
```

## Notes and Limits
- One active session per user per guild.
- Prompts with @everyone/@here are blocked.
- AI replies use `allowed_mentions=none` to avoid pings.
- All AI actions require `ai.use` permission and an allowlisted channel.
- Max tokens can be set per guild and per model, and is capped by `config.yaml` (`max_tokens_cap`).
- In AI threads, the bot replies to normal messages (no slash command required).

## Troubleshooting
- "Invalid model ID" (400): use `/ai admin models-list` and set a valid model ID.
- "Provider returned error" (400/503): provider outage or routing issue. Try a different model or deny a provider.
- 429 (rate limit): reduce usage or add another key; check `/ai admin keys list live:true`.
- 401/402: key invalid or out of credits; the key will be disabled automatically.

Log files include OpenRouter status, model, mode, provider, and request metadata, but never keys or prompts.

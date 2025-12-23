"""
AI Chat Cog for Logiq
OpenRouter-backed AI chat with key pooling and admin controls.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from database.models import (
    AI_DEFAULT_CHANNEL_COOLDOWN_SECONDS,
    AI_DEFAULT_MAX_CONCURRENT,
    AI_DEFAULT_MODEL_ALLOWLIST,
    AI_DEFAULT_MODEL_ID,
    AI_DEFAULT_MODE,
    AI_DEFAULT_RPD_LIMIT,
    AI_DEFAULT_RPM_LIMIT,
    AI_DEFAULT_SESSION_MAX_TURNS,
    AI_DEFAULT_USER_COOLDOWN_SECONDS,
    FeatureKey,
    AIGuildSettings,
)
from utils.ai_keys import decrypt_api_key, encrypt_api_key, fingerprint_api_key
from utils.denials import DenialLogger
from utils.embeds import EmbedColor, EmbedFactory
from utils.feature_permissions import FeaturePermissionManager
from utils.logs import resolve_log_channel
from utils.openrouter import request_json

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a helpful assistant in a Discord server. Be concise. "
    "Do not mention system messages."
)
MAX_PROMPT_CHARS = 2000
MAX_COMPLETION_TOKENS = 500
TEMPERATURE = 0.7
MAX_KEY_ATTEMPTS = 3
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 30
DEFAULT_SERVER_ERROR_COOLDOWN_SECONDS = 20
EMBED_CHUNK_SIZE = 3800
MAX_STATUS_TEXT = 600


class AIChat(commands.Cog):
    """AI chat and moderation cog."""

    ai = app_commands.Group(
        name="ai",
        description="AI chat commands",
        guild_only=True,
    )
    ai_admin = app_commands.Group(
        name="admin",
        description="AI admin commands",
        guild_only=True,
        parent=ai,
    )

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get("modules", {}).get("ai_chat", {})
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()
        if hasattr(self.perms, "denials"):
            self.perms.denials = self.denials

        self.user_cooldowns: Dict[Tuple[int, int], float] = {}
        self.channel_cooldowns: Dict[Tuple[int, int], float] = {}
        self.guild_inflight: Dict[int, int] = {}
        self.guild_locks: Dict[int, asyncio.Lock] = {}

        self.openai_api_key = config.get("api_keys", {}).get("openai", "")

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self.guild_locks[guild_id] = lock
        return lock

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _ensure_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _is_timed_out(self, member: discord.Member) -> bool:
        if hasattr(member, "is_timed_out"):
            return member.is_timed_out()
        until = getattr(member, "communication_disabled_until", None)
        if until is None:
            return False
        until = self._ensure_utc(until)
        return until is not None and until > self._now()

    def _base_ai_use_check(self, member: discord.Member) -> bool:
        return not self._is_timed_out(member)

    def _base_ai_admin_check(self, member: discord.Member) -> bool:
        return (
            not self._is_timed_out(member)
            and (member.guild_permissions.administrator or member.guild_permissions.manage_guild)
        )

    async def _log_denial(self, interaction: discord.Interaction, feature: FeatureKey, reason: str) -> None:
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, "ai", feature.value):
            return
        logger.warning(
            "AI permission denied for feature=%s user=%s guild=%s reason=%s",
            feature.value,
            interaction.user.id,
            interaction.guild.id,
            reason,
        )

    async def _get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        defaults = AIGuildSettings(guild_id=guild_id).to_dict()
        doc = await self.db.get_ai_guild_settings(guild_id)
        if not doc:
            return await self.db.upsert_ai_guild_settings(guild_id, defaults)

        update: Dict[str, Any] = {}
        for key in (
            "enabled",
            "allowed_channel_ids",
            "default_model_id",
            "default_mode",
            "user_cooldown_seconds",
            "channel_cooldown_seconds",
            "max_concurrent",
            "session_max_turns",
            "session_ttl_seconds",
            "model_allowlist",
        ):
            if key not in doc:
                update[key] = defaults[key]
                doc[key] = defaults[key]

        if not doc.get("model_allowlist"):
            update["model_allowlist"] = list(AI_DEFAULT_MODEL_ALLOWLIST)
            doc["model_allowlist"] = list(AI_DEFAULT_MODEL_ALLOWLIST)

        if doc.get("default_model_id") and doc["default_model_id"] not in doc["model_allowlist"]:
            allowlist = list(doc["model_allowlist"])
            allowlist.append(doc["default_model_id"])
            update["model_allowlist"] = allowlist
            doc["model_allowlist"] = allowlist

        if update:
            doc = await self.db.upsert_ai_guild_settings(guild_id, update)
        return doc

    def _is_channel_allowed(self, channel: discord.abc.GuildChannel, allowed_ids: List[int]) -> bool:
        if isinstance(channel, discord.Thread):
            parent_id = channel.parent_id
            return parent_id in allowed_ids if parent_id else False
        return channel.id in allowed_ids

    def _cooldown_remaining(self, last_ts: Optional[float], cooldown_seconds: int) -> float:
        if last_ts is None:
            return 0.0
        remaining = cooldown_seconds - (time.monotonic() - last_ts)
        return max(0.0, remaining)

    def _check_cooldowns(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        user_cooldown: int,
        channel_cooldown: int,
    ) -> Optional[str]:
        user_key = (guild_id, user_id)
        channel_key = (guild_id, channel_id)

        user_remaining = self._cooldown_remaining(self.user_cooldowns.get(user_key), user_cooldown)
        if user_remaining > 0:
            return f"Cooldown active. Try again in {int(user_remaining)}s."

        channel_remaining = self._cooldown_remaining(self.channel_cooldowns.get(channel_key), channel_cooldown)
        if channel_remaining > 0:
            return f"Channel cooldown active. Try again in {int(channel_remaining)}s."

        return None
    def _set_cooldowns(self, guild_id: int, user_id: int, channel_id: int) -> None:
        now = time.monotonic()
        self.user_cooldowns[(guild_id, user_id)] = now
        self.channel_cooldowns[(guild_id, channel_id)] = now

    async def _acquire_guild_slot(self, guild_id: int, max_concurrent: int) -> bool:
        lock = self._get_lock(guild_id)
        async with lock:
            inflight = self.guild_inflight.get(guild_id, 0)
            if inflight >= max_concurrent:
                return False
            self.guild_inflight[guild_id] = inflight + 1
            return True

    async def _release_guild_slot(self, guild_id: int) -> None:
        lock = self._get_lock(guild_id)
        async with lock:
            inflight = max(0, self.guild_inflight.get(guild_id, 0) - 1)
            if inflight == 0:
                self.guild_inflight.pop(guild_id, None)
            else:
                self.guild_inflight[guild_id] = inflight

    def _contains_mass_mentions(self, text: str) -> bool:
        return "@everyone" in text or "@here" in text

    def _window_state(
        self,
        start: Optional[datetime],
        count: Optional[int],
        window_seconds: int,
        now: datetime,
    ) -> Tuple[datetime, int]:
        start = self._ensure_utc(start)
        count = int(count or 0)
        if not start or (now - start).total_seconds() >= window_seconds:
            return now, 0
        return start, count

    def _build_key_candidate(self, doc: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        if not doc.get("enabled", True):
            return None

        cooldown_until = self._ensure_utc(doc.get("cooldown_until"))
        if cooldown_until and now < cooldown_until:
            return None

        rpm_limit = int(doc.get("rpm_limit") or 0)
        rpd_limit = int(doc.get("rpd_limit") or 0)
        if rpm_limit <= 0 or rpd_limit <= 0:
            return None

        minute_start, minute_count = self._window_state(
            doc.get("minute_window_started_at"),
            doc.get("minute_window_count"),
            60,
            now,
        )
        day_start, day_count = self._window_state(
            doc.get("day_started_at"),
            doc.get("day_count"),
            86400,
            now,
        )

        if minute_count >= rpm_limit or day_count >= rpd_limit:
            return None

        remaining_rpm = (rpm_limit - minute_count) / rpm_limit
        remaining_rpd = (rpd_limit - day_count) / rpd_limit
        score = (remaining_rpm * 0.4) + (remaining_rpd * 0.6)

        return {
            "doc": doc,
            "score": score,
            "minute_start": minute_start,
            "minute_count": minute_count,
            "day_start": day_start,
            "day_count": day_count,
            "last_used_at": self._ensure_utc(doc.get("last_used_at")),
        }

    async def _select_key_candidates(self, guild_id: int) -> List[Dict[str, Any]]:
        keys = await self.db.list_ai_api_keys(guild_id)
        now = self._now()
        candidates = []
        for doc in keys:
            candidate = self._build_key_candidate(doc, now)
            if candidate:
                candidates.append(candidate)
        candidates.sort(
            key=lambda item: (
                -item["score"],
                item["last_used_at"] or datetime.min.replace(tzinfo=timezone.utc),
            )
        )
        return candidates

    async def _update_key_usage(self, doc: Dict[str, Any], candidate: Dict[str, Any]) -> None:
        now = self._now()
        await self.db.update_ai_api_key(
            doc["guild_id"],
            doc["name"],
            {
                "minute_window_started_at": candidate["minute_start"],
                "minute_window_count": candidate["minute_count"] + 1,
                "day_started_at": candidate["day_start"],
                "day_count": candidate["day_count"] + 1,
                "last_used_at": now,
                "last_error": None,
                "last_error_code": None,
                "last_error_at": None,
                "updated_at": now,
            },
        )

    async def _set_key_error(
        self,
        doc: Dict[str, Any],
        code: int,
        message: str,
        cooldown_seconds: Optional[int] = None,
        disable: bool = False,
    ) -> None:
        now = self._now()
        update: Dict[str, Any] = {
            "last_error_code": code,
            "last_error": message[:MAX_STATUS_TEXT],
            "last_error_at": now,
            "updated_at": now,
        }
        if cooldown_seconds:
            update["cooldown_until"] = now + timedelta(seconds=cooldown_seconds)
        if disable:
            update["enabled"] = False
        await self.db.update_ai_api_key(doc["guild_id"], doc["name"], update)

    async def _log_to_ai_channel(self, guild: discord.Guild, embed: discord.Embed) -> None:
        channel = await resolve_log_channel(self.db, guild, "ai")
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send AI log to channel %s in guild %s", channel, guild.id)

    async def _call_openrouter(self, guild_id: int, payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        candidates = await self._select_key_candidates(guild_id)
        if not candidates:
            return None, "No available AI keys. Ask an admin to add or enable keys."

        last_error = "OpenRouter request failed."
        for candidate in candidates[:MAX_KEY_ATTEMPTS]:
            doc = candidate["doc"]

            try:
                api_key = decrypt_api_key(doc["encrypted_api_key"])
            except ValueError as exc:
                return None, str(exc)
            except Exception as exc:
                logger.warning("Failed to decrypt AI key %s: %s", doc.get("name"), exc)
                await self._set_key_error(doc, 0, "Key decryption failed", disable=True)
                last_error = "AI key decryption failed."
                continue

            await self._update_key_usage(doc, candidate)

            try:
                status, data, text, headers = await request_json(
                    "POST",
                    "/chat/completions",
                    api_key,
                    payload,
                )
            except Exception as exc:
                logger.warning("OpenRouter request failed for key %s: %s", doc.get("name"), exc)
                await self._set_key_error(
                    doc,
                    0,
                    "OpenRouter request failed",
                    cooldown_seconds=DEFAULT_SERVER_ERROR_COOLDOWN_SECONDS,
                )
                last_error = "OpenRouter request failed. Please try again."
                continue

            if status == 200 and data:
                try:
                    return data["choices"][0]["message"]["content"], None
                except (KeyError, IndexError, TypeError):
                    last_error = "OpenRouter response missing completion content."
                    await self._set_key_error(doc, status, last_error, cooldown_seconds=DEFAULT_SERVER_ERROR_COOLDOWN_SECONDS)
                    continue

            error_message = None
            if isinstance(data, dict):
                error_info = data.get("error") or {}
                if isinstance(error_info, dict):
                    error_message = error_info.get("message")
                elif isinstance(error_info, str):
                    error_message = error_info
            if not error_message:
                error_message = text[:MAX_STATUS_TEXT] if text else "OpenRouter error"

            if status == 429:
                retry_after = headers.get("Retry-After")
                cooldown = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                if retry_after:
                    try:
                        cooldown = int(float(retry_after))
                    except ValueError:
                        cooldown = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                await self._set_key_error(doc, status, error_message, cooldown_seconds=cooldown)
                last_error = "AI rate limit reached. Retrying with another key."
                continue

            if status in (401, 402):
                await self._set_key_error(doc, status, error_message, disable=True)
                last_error = "AI key disabled due to authorization or credit error."
                if guild_id:
                    embed = EmbedFactory.warning(
                        "AI Key Disabled",
                        f"Key `{doc['name']}` disabled due to OpenRouter status {status}.",
                    )
                    guild = self.bot.get_guild(guild_id)
                    if guild:
                        await self._log_to_ai_channel(guild, embed)
                continue

            if status >= 500:
                await self._set_key_error(doc, status, error_message, cooldown_seconds=DEFAULT_SERVER_ERROR_COOLDOWN_SECONDS)
                last_error = "OpenRouter server error. Retrying with another key."
                continue

            await self._set_key_error(doc, status, error_message, cooldown_seconds=DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)
            last_error = error_message
            break

        return None, last_error
    def _trim_messages(self, messages: List[Dict[str, Any]], max_turns: int) -> List[Dict[str, Any]]:
        if max_turns <= 0:
            return []
        trimmed: List[Dict[str, Any]] = []
        user_turns = 0
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_turns += 1
            trimmed.append(msg)
            if user_turns >= max_turns:
                break
        return list(reversed(trimmed))

    def _chunk_text(self, text: str, limit: int) -> List[str]:
        if len(text) <= limit:
            return [text]
        chunks = []
        start = 0
        while start < len(text):
            chunks.append(text[start:start + limit])
            start += limit
        return chunks

    async def _send_ai_response(
        self,
        interaction: discord.Interaction,
        response_text: str,
        model: str,
        private: bool,
    ) -> None:
        allowed_mentions = discord.AllowedMentions.none()
        chunks = self._chunk_text(response_text, EMBED_CHUNK_SIZE)
        if len(chunks) == 1:
            embed = EmbedFactory.ai_response(chunks[0], model)
            await interaction.followup.send(embed=embed, ephemeral=private, allowed_mentions=allowed_mentions)
            return

        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            embed = EmbedFactory.create(
                title=f"AI Response ({idx}/{total})",
                description=chunk,
                color=EmbedColor.AI,
                footer=f"Powered by {model}",
            )
            await interaction.followup.send(embed=embed, ephemeral=private, allowed_mentions=allowed_mentions)

    async def _get_session(self, guild_id: int, user_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
        return await self.db.get_ai_session(guild_id, user_id, channel_id)

    async def _update_session(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        messages: List[Dict[str, Any]],
        active: bool,
        private_default: bool,
    ) -> None:
        await self.db.upsert_ai_session(
            guild_id,
            user_id,
            channel_id,
            {
                "messages": messages,
                "active": active,
                "private_default": private_default,
                "updated_at": self._now(),
            },
        )

    async def _reset_session(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        return await self.db.delete_ai_session(guild_id, user_id, channel_id)

    async def _validate_ai_use(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.AI_USE,
            self._base_ai_use_check,
            allow_admin=False,
            require_allowlist=True,
        )
        if not allowed:
            await self._log_denial(interaction, FeatureKey.AI_USE, "ai.use")
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to use AI commands."),
                ephemeral=True,
            )
        return allowed

    async def _validate_ai_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.AI_ADMIN,
            self._base_ai_admin_check,
            allow_admin=True,
            require_allowlist=True,
        )
        if not allowed:
            await self._log_denial(interaction, FeatureKey.AI_ADMIN, "ai.admin")
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage AI settings."),
                ephemeral=True,
            )
        return allowed

    async def _build_prompt(
        self,
        settings: Dict[str, Any],
        session_messages: List[Dict[str, Any]],
        user_prompt: str,
    ) -> List[Dict[str, str]]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(session_messages)
        messages.append({"role": "user", "content": user_prompt})
        return messages

    async def _run_ai_request(
        self,
        interaction: discord.Interaction,
        prompt: str,
        mode: Optional[str],
        private: bool,
        use_session: bool = True,
        session_active: bool = False,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "AI commands are only available in servers."),
                ephemeral=True,
            )
            return

        if not self.module_config.get("enabled", True):
            await interaction.followup.send(
                embed=EmbedFactory.error("Module Disabled", "AI chat is currently disabled."),
                ephemeral=True,
            )
            return

        settings = await self._get_guild_settings(guild.id)
        if not settings.get("enabled", False):
            await interaction.followup.send(
                embed=EmbedFactory.error("AI Disabled", "AI is not enabled for this server."),
                ephemeral=True,
            )
            return

        if not await self._validate_ai_use(interaction):
            return

        allowed_channels = settings.get("allowed_channel_ids", [])
        if not self._is_channel_allowed(interaction.channel, allowed_channels):
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Allowed", "AI is disabled here; ask an admin to allow this channel."),
                ephemeral=True,
            )
            return

        if not prompt.strip():
            await interaction.followup.send(
                embed=EmbedFactory.error("Empty Prompt", "Please provide a prompt."),
                ephemeral=True,
            )
            return

        if len(prompt) > MAX_PROMPT_CHARS:
            await interaction.followup.send(
                embed=EmbedFactory.error("Prompt Too Long", f"Prompt must be under {MAX_PROMPT_CHARS} characters."),
                ephemeral=True,
            )
            return

        if self._contains_mass_mentions(prompt):
            await interaction.followup.send(
                embed=EmbedFactory.error("Mentions Blocked", "Prompts cannot include @everyone or @here."),
                ephemeral=True,
            )
            return

        user_cooldown = int(settings.get("user_cooldown_seconds", AI_DEFAULT_USER_COOLDOWN_SECONDS))
        channel_cooldown = int(settings.get("channel_cooldown_seconds", AI_DEFAULT_CHANNEL_COOLDOWN_SECONDS))
        cooldown_error = self._check_cooldowns(
            guild.id,
            interaction.user.id,
            interaction.channel.id,
            user_cooldown,
            channel_cooldown,
        )
        if cooldown_error:
            await interaction.followup.send(
                embed=EmbedFactory.warning("Cooldown", cooldown_error),
                ephemeral=True,
            )
            return

        max_concurrent = int(settings.get("max_concurrent", AI_DEFAULT_MAX_CONCURRENT))
        if not await self._acquire_guild_slot(guild.id, max_concurrent):
            await interaction.followup.send(
                embed=EmbedFactory.warning("Busy", "AI is busy in this server. Please try again soon."),
                ephemeral=True,
            )
            return

        self._set_cooldowns(guild.id, interaction.user.id, interaction.channel.id)

        try:
            session_messages: List[Dict[str, Any]] = []
            private_default = private
            session = None
            if use_session:
                session = await self._get_session(guild.id, interaction.user.id, interaction.channel.id)
            if session:
                private_default = session.get("private_default", private_default)
                session_messages = session.get("messages", [])
                session_active = session.get("active", session_active)

            max_turns = int(settings.get("session_max_turns", AI_DEFAULT_SESSION_MAX_TURNS))
            session_messages = self._trim_messages(session_messages, max_turns)

            model_id = settings.get("default_model_id", AI_DEFAULT_MODEL_ID)
            model_allowlist = settings.get("model_allowlist", list(AI_DEFAULT_MODEL_ALLOWLIST))
            if model_id not in model_allowlist:
                model_id = AI_DEFAULT_MODEL_ID

            request_mode = mode
            if not request_mode and session:
                request_mode = session.get("mode")
            if request_mode not in ("fast", "think"):
                request_mode = settings.get("default_mode", AI_DEFAULT_MODE)

            max_tokens = max(1, int(self.module_config.get("max_tokens", MAX_COMPLETION_TOKENS)))
            temperature = float(self.module_config.get("temperature", TEMPERATURE))
            temperature = min(max(temperature, 0.0), 2.0)
            messages = await self._build_prompt(settings, session_messages, prompt)
            payload = {
                "model": model_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if request_mode == "think":
                payload["reasoning"] = {"effort": "high"}

            response_text, error = await self._call_openrouter(guild.id, payload)
            if error or not response_text:
                await interaction.followup.send(
                    embed=EmbedFactory.error("AI Error", error or "Failed to get AI response."),
                    ephemeral=True,
                )
                return

            updated_messages = session_messages + [
                {"role": "user", "content": prompt, "ts": self._now()},
                {"role": "assistant", "content": response_text, "ts": self._now()},
            ]
            updated_messages = self._trim_messages(updated_messages, max_turns)
            if use_session:
                await self._update_session(
                    guild.id,
                    interaction.user.id,
                    interaction.channel.id,
                    updated_messages,
                    active=session_active,
                    private_default=private_default,
                )

            await self._send_ai_response(interaction, response_text, model_id, private_default)
        finally:
            await self._release_guild_slot(guild.id)

    def _resolve_auto_archive_duration(self, guild: discord.Guild, target_minutes: int) -> int:
        supported = {60, 1440}
        if "THREE_DAY_THREAD_ARCHIVE" in guild.features:
            supported.add(4320)
        if "SEVEN_DAY_THREAD_ARCHIVE" in guild.features:
            supported.add(10080)
        return min(supported, key=lambda value: abs(value - target_minutes))

    async def _moderate_content(self, text: str) -> Dict[str, Any]:
        if not self.openai_api_key:
            return {"flagged": False}

        url = "https://api.openai.com/v1/moderations"
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }
        data = {"input": text}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result.get("results", [{}])[0]
                    return {"flagged": False}
        except Exception as exc:
            logger.error("Error moderating content: %s", exc, exc_info=True)
            return {"flagged": False}
    @ai.command(name="ask", description="Ask the AI a question.")
    @app_commands.describe(
        prompt="Your question for the AI",
        mode="fast or think",
        private="Send response privately (default: true)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="fast", value="fast"),
            app_commands.Choice(name="think", value="think"),
        ]
    )
    async def ai_ask(
        self,
        interaction: discord.Interaction,
        prompt: str,
        mode: Optional[app_commands.Choice[str]] = None,
        private: Optional[bool] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._run_ai_request(
            interaction,
            prompt=prompt,
            mode=mode.value if mode else None,
            private=True if private is None else private,
            use_session=True,
            session_active=False,
        )

    @ai.command(name="chat-start", description="Start an AI chat in a thread.")
    @app_commands.describe(private="Send responses privately in this session")
    async def ai_chat_start(self, interaction: discord.Interaction, private: bool = True) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "AI commands are only available in servers."),
                ephemeral=True,
            )
            return

        settings = await self._get_guild_settings(interaction.guild.id)
        if not settings.get("enabled", False):
            await interaction.followup.send(
                embed=EmbedFactory.error("AI Disabled", "AI is not enabled for this server."),
                ephemeral=True,
            )
            return

        if not await self._validate_ai_use(interaction):
            return

        allowed_channels = settings.get("allowed_channel_ids", [])
        if not self._is_channel_allowed(interaction.channel, allowed_channels):
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Allowed", "AI is disabled here; ask an admin to allow this channel."),
                ephemeral=True,
            )
            return

        existing = await self.db.get_active_ai_session(interaction.guild.id, interaction.user.id)
        if existing:
            channel_id = existing.get("channel_id")
            await interaction.followup.send(
                embed=EmbedFactory.warning(
                    "Session Active",
                    f"You already have an active AI session in <#{channel_id}>.",
                ),
                ephemeral=True,
            )
            return

        channel = interaction.channel
        thread = None
        if isinstance(channel, discord.Thread):
            thread = channel
        elif isinstance(channel, (discord.TextChannel, discord.NewsChannel)):
            try:
                auto_archive = self._resolve_auto_archive_duration(interaction.guild, 1440)
                thread = await channel.create_thread(
                    name=f"{interaction.user.display_name} AI Chat",
                    auto_archive_duration=auto_archive,
                    reason="AI chat session",
                )
            except discord.Forbidden:
                thread = None
            except discord.HTTPException as exc:
                logger.warning("Failed to create AI thread: %s", exc)
                thread = None

        target_channel = thread or channel
        await self._update_session(
            interaction.guild.id,
            interaction.user.id,
            target_channel.id,
            [],
            active=True,
            private_default=private,
        )

        message = f"AI chat session started in <#{target_channel.id}>."
        if thread is None and not isinstance(channel, discord.Thread):
            message = "AI chat session started in this channel (thread creation not available)."

        await interaction.followup.send(
            embed=EmbedFactory.success("Session Started", message),
            ephemeral=True,
        )

    @ai.command(name="chat-reset", description="Reset your AI chat session.")
    async def ai_chat_reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "AI commands are only available in servers."),
                ephemeral=True,
            )
            return

        if not await self._validate_ai_use(interaction):
            return

        reset = await self._reset_session(interaction.guild.id, interaction.user.id, interaction.channel.id)
        if reset:
            embed = EmbedFactory.success("Session Reset", "Your AI session has been cleared.")
        else:
            embed = EmbedFactory.info("No Session", "No active AI session found for this channel.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ai.command(name="model", description="Show the current AI model and status.")
    async def ai_model(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "AI commands are only available in servers."),
                ephemeral=True,
            )
            return

        settings = await self._get_guild_settings(interaction.guild.id)
        enabled = settings.get("enabled", False)
        allowed_channels = settings.get("allowed_channel_ids", [])
        channel_allowed = self._is_channel_allowed(interaction.channel, allowed_channels)
        model_id = settings.get("default_model_id", AI_DEFAULT_MODEL_ID)
        mode = settings.get("default_mode", AI_DEFAULT_MODE)

        description = (
            f"Model: `{model_id}`\n"
            f"Mode: `{mode}`\n"
            f"AI enabled: `{enabled}`\n"
            f"Channel allowed: `{channel_allowed}`"
        )
        embed = EmbedFactory.create(title="AI Model", description=description, color=EmbedColor.INFO)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ai_admin.command(name="enable", description="Enable AI features for this guild.")
    async def ai_admin_enable(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        settings = await self._get_guild_settings(interaction.guild.id)
        if settings.get("enabled"):
            await interaction.followup.send(
                embed=EmbedFactory.info("Already Enabled", "AI is already enabled for this server."),
                ephemeral=True,
            )
            return
        await self.db.upsert_ai_guild_settings(
            interaction.guild.id,
            {"enabled": True, "updated_at": self._now()},
        )
        await interaction.followup.send(
            embed=EmbedFactory.success("AI Enabled", "AI has been enabled for this server."),
            ephemeral=True,
        )

    @ai_admin.command(name="channel-allow-add", description="Allow AI responses in a channel.")
    async def ai_admin_channel_allow_add(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        if not isinstance(channel, (discord.TextChannel, discord.NewsChannel, discord.ForumChannel)):
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Channel", "Only text or forum channels can be allowlisted."),
                ephemeral=True,
            )
            return
        settings = await self._get_guild_settings(interaction.guild.id)
        allowed = set(settings.get("allowed_channel_ids", []))
        allowed.add(channel.id)
        updated = await self.db.upsert_ai_guild_settings(
            interaction.guild.id,
            {"allowed_channel_ids": list(allowed), "updated_at": self._now()},
        )
        channel_mentions = ", ".join(f"<#{cid}>" for cid in updated.get("allowed_channel_ids", [])) or "None"
        await interaction.followup.send(
            embed=EmbedFactory.success("Channel Allowed", f"Allowed channels: {channel_mentions}"),
            ephemeral=True,
        )

    @ai_admin.command(name="channel-allow-remove", description="Remove a channel from the AI allowlist.")
    async def ai_admin_channel_allow_remove(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        settings = await self._get_guild_settings(interaction.guild.id)
        allowed = set(settings.get("allowed_channel_ids", []))
        allowed.discard(channel.id)
        updated = await self.db.upsert_ai_guild_settings(
            interaction.guild.id,
            {"allowed_channel_ids": list(allowed), "updated_at": self._now()},
        )
        channel_mentions = ", ".join(f"<#{cid}>" for cid in updated.get("allowed_channel_ids", [])) or "None"
        await interaction.followup.send(
            embed=EmbedFactory.success("Channel Removed", f"Allowed channels: {channel_mentions}"),
            ephemeral=True,
        )

    @ai_admin.command(name="channel-allow-list", description="List AI-allowed channels.")
    async def ai_admin_channel_allow_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        settings = await self._get_guild_settings(interaction.guild.id)
        allowed = settings.get("allowed_channel_ids", [])
        channel_mentions = ", ".join(f"<#{cid}>" for cid in allowed) or "None"
        await interaction.followup.send(
            embed=EmbedFactory.info("Allowed Channels", channel_mentions),
            ephemeral=True,
        )
    @ai_admin.command(name="keys-add", description="Add an OpenRouter API key.")
    @app_commands.describe(
        name="Key name/label",
        key="OpenRouter API key",
        rpm="Requests per minute limit",
        rpd="Requests per day limit",
        notes="Optional notes",
    )
    async def ai_admin_keys_add(
        self,
        interaction: discord.Interaction,
        name: str,
        key: str,
        rpm: Optional[int] = None,
        rpd: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return

        name = name.strip()
        if not name:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Name", "Key name cannot be empty."),
                ephemeral=True,
            )
            return

        existing = await self.db.get_ai_api_key(interaction.guild.id, name)
        if existing:
            await interaction.followup.send(
                embed=EmbedFactory.error("Duplicate Name", "A key with that name already exists."),
                ephemeral=True,
            )
            return

        try:
            encrypted = encrypt_api_key(key)
        except ValueError as exc:
            await interaction.followup.send(
                embed=EmbedFactory.error("Missing Secret", str(exc)),
                ephemeral=True,
            )
            return

        try:
            status, data, text, _headers = await request_json("GET", "/key", key)
        except Exception as exc:
            await interaction.followup.send(
                embed=EmbedFactory.error("Validation Failed", f"OpenRouter request failed: {exc}"),
                ephemeral=True,
            )
            return
        if status != 200:
            message = "Invalid key or failed to validate with OpenRouter."
            if text:
                message = text[:MAX_STATUS_TEXT]
            await interaction.followup.send(
                embed=EmbedFactory.error("Validation Failed", message),
                ephemeral=True,
            )
            return

        now = self._now()
        rpm_limit = int(rpm or AI_DEFAULT_RPM_LIMIT)
        rpd_limit = int(rpd or AI_DEFAULT_RPD_LIMIT)
        if rpm_limit < 1 or rpd_limit < 1:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Limits", "RPM and RPD limits must be at least 1."),
                ephemeral=True,
            )
            return
        record = {
            "guild_id": interaction.guild.id,
            "name": name,
            "encrypted_api_key": encrypted,
            "key_fingerprint": fingerprint_api_key(key),
            "rpm_limit": rpm_limit,
            "rpd_limit": rpd_limit,
            "enabled": True,
            "notes": notes,
            "minute_window_count": 0,
            "minute_window_started_at": now,
            "day_count": 0,
            "day_started_at": now,
            "openrouter_info": data,
            "created_at": now,
            "updated_at": now,
        }
        await self.db.create_ai_api_key(record)
        logger.info("AI key added for guild %s: %s", interaction.guild.id, name)

        await interaction.followup.send(
            embed=EmbedFactory.success("Key Added", f"Stored key `{name}` ({record['key_fingerprint']})."),
            ephemeral=True,
        )

    @ai_admin.command(name="keys-list", description="List OpenRouter API keys.")
    @app_commands.describe(live="Fetch live status from OpenRouter")
    async def ai_admin_keys_list(self, interaction: discord.Interaction, live: bool = False) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return

        keys = await self.db.list_ai_api_keys(interaction.guild.id)
        if not keys:
            await interaction.followup.send(
                embed=EmbedFactory.info("No Keys", "No OpenRouter keys configured."),
                ephemeral=True,
            )
            return

        lines = []
        for key_doc in keys:
            line = [
                f"**{key_doc['name']}** ({key_doc.get('key_fingerprint', 'n/a')})",
                f"Enabled: {key_doc.get('enabled', False)}",
                f"Limits: rpm={key_doc.get('rpm_limit')}, rpd={key_doc.get('rpd_limit')}",
            ]
            cooldown_until = key_doc.get("cooldown_until")
            if cooldown_until:
                cooldown_until = self._ensure_utc(cooldown_until)
                line.append(f"Cooldown: {cooldown_until.isoformat()}")
            last_used = key_doc.get("last_used_at")
            if last_used:
                last_used = self._ensure_utc(last_used)
                line.append(f"Last used: {last_used.isoformat()}")
            last_error_code = key_doc.get("last_error_code")
            last_error_text = key_doc.get("last_error")
            if last_error_code or last_error_text:
                suffix = f" {last_error_text}" if last_error_text else ""
                line.append(f"Last error: {last_error_code or 'n/a'}{suffix}")
            notes = key_doc.get("notes")
            if notes:
                line.append(f"Notes: {notes}")

            if live:
                try:
                    api_key = decrypt_api_key(key_doc["encrypted_api_key"])
                    status, data, _text, _headers = await request_json("GET", "/key", api_key)
                    line.append(f"Live status: {status}")
                    if data:
                        raw = json.dumps(data, ensure_ascii=True)
                        if len(raw) > 300:
                            raw = raw[:297] + "..."
                        line.append(f"Live info: {raw}")
                except Exception as exc:
                    line.append(f"Live status: error ({exc})")

            lines.append("\n".join(line))

        description = "\n\n".join(lines)
        embed = EmbedFactory.create(
            title="AI Keys",
            description=description,
            color=EmbedColor.INFO,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ai_admin.command(name="keys-disable", description="Disable an OpenRouter API key.")
    async def ai_admin_keys_disable(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        updated = await self.db.update_ai_api_key(interaction.guild.id, name, {"enabled": False, "updated_at": self._now()})
        if not updated:
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Found", "No key found with that name."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=EmbedFactory.success("Key Disabled", f"Key `{name}` disabled."),
            ephemeral=True,
        )

    @ai_admin.command(name="keys-enable", description="Enable an OpenRouter API key.")
    async def ai_admin_keys_enable(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        updated = await self.db.update_ai_api_key(interaction.guild.id, name, {"enabled": True, "updated_at": self._now()})
        if not updated:
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Found", "No key found with that name."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=EmbedFactory.success("Key Enabled", f"Key `{name}` enabled."),
            ephemeral=True,
        )

    @ai_admin.command(name="keys-remove", description="Remove an OpenRouter API key.")
    async def ai_admin_keys_remove(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        removed = await self.db.delete_ai_api_key(interaction.guild.id, name)
        if not removed:
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Found", "No key found with that name."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=EmbedFactory.success("Key Removed", f"Key `{name}` removed."),
            ephemeral=True,
        )

    @ai_admin.command(name="keys-probe", description="Probe an OpenRouter API key.")
    async def ai_admin_keys_probe(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return
        key_doc = await self.db.get_ai_api_key(interaction.guild.id, name)
        if not key_doc:
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Found", "No key found with that name."),
                ephemeral=True,
            )
            return

        try:
            api_key = decrypt_api_key(key_doc["encrypted_api_key"])
        except Exception as exc:
            await interaction.followup.send(
                embed=EmbedFactory.error("Decrypt Failed", str(exc)),
                ephemeral=True,
            )
            return

        try:
            status, data, text, _headers = await request_json("GET", "/key", api_key)
        except Exception as exc:
            await interaction.followup.send(
                embed=EmbedFactory.error("Probe Failed", f"OpenRouter request failed: {exc}"),
                ephemeral=True,
            )
            return
        if status != 200:
            message = text[:MAX_STATUS_TEXT] if text else "Probe failed."
            await interaction.followup.send(
                embed=EmbedFactory.error("Probe Failed", message),
                ephemeral=True,
            )
            return

        await self.db.update_ai_api_key(
            interaction.guild.id,
            name,
            {"openrouter_info": data, "updated_at": self._now()},
        )

        details = json.dumps(data, ensure_ascii=True)
        if len(details) > 1800:
            details = details[:1797] + "..."
        embed = EmbedFactory.create(
            title="Key Probe",
            description=f"```json\n{details}\n```",
            color=EmbedColor.INFO,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    @ai_admin.command(name="limits-set", description="Set AI cooldowns and concurrency limits.")
    @app_commands.describe(
        user_cooldown_seconds="Per-user cooldown in seconds",
        channel_cooldown_seconds="Per-channel cooldown in seconds",
        max_concurrent="Max in-flight AI requests per guild",
    )
    async def ai_admin_limits_set(
        self,
        interaction: discord.Interaction,
        user_cooldown_seconds: int,
        channel_cooldown_seconds: int,
        max_concurrent: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return

        if user_cooldown_seconds < 1 or channel_cooldown_seconds < 1 or max_concurrent < 1:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Limits", "All limits must be at least 1."),
                ephemeral=True,
            )
            return

        await self.db.upsert_ai_guild_settings(
            interaction.guild.id,
            {
                "user_cooldown_seconds": user_cooldown_seconds,
                "channel_cooldown_seconds": channel_cooldown_seconds,
                "max_concurrent": max_concurrent,
                "updated_at": self._now(),
            },
        )
        await interaction.followup.send(
            embed=EmbedFactory.success(
                "Limits Updated",
                f"User cooldown: {user_cooldown_seconds}s\n"
                f"Channel cooldown: {channel_cooldown_seconds}s\n"
                f"Max concurrent: {max_concurrent}",
            ),
            ephemeral=True,
        )

    @ai_admin.command(name="model-set", description="Set the default AI model.")
    @app_commands.describe(
        model_id="OpenRouter model ID",
        confirm_paid="Confirm if this model may cost credits",
    )
    async def ai_admin_model_set(
        self,
        interaction: discord.Interaction,
        model_id: str,
        confirm_paid: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._validate_ai_admin(interaction):
            return

        model_id = model_id.strip()
        if not model_id:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Model", "Model ID cannot be empty."),
                ephemeral=True,
            )
            return

        is_free = model_id.endswith(":free")
        if not is_free and not confirm_paid:
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Confirmation Required",
                    "This model may cost credits. Re-run with confirm_paid=true to allow it.",
                ),
                ephemeral=True,
            )
            return

        settings = await self._get_guild_settings(interaction.guild.id)
        allowlist = list(settings.get("model_allowlist", list(AI_DEFAULT_MODEL_ALLOWLIST)))
        if model_id not in allowlist:
            allowlist.append(model_id)

        await self.db.upsert_ai_guild_settings(
            interaction.guild.id,
            {
                "default_model_id": model_id,
                "model_allowlist": allowlist,
                "updated_at": self._now(),
            },
        )

        await interaction.followup.send(
            embed=EmbedFactory.success("Model Updated", f"Default model set to `{model_id}`."),
            ephemeral=True,
        )

    @app_commands.command(name="ask", description="Deprecated: use /ai ask")
    @app_commands.describe(question="Your question for the AI")
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._run_ai_request(
            interaction,
            prompt=question,
            mode=None,
            private=True,
            use_session=True,
            session_active=False,
        )

    @app_commands.command(name="clear-conversation", description="Deprecated: use /ai chat-reset")
    async def clear_conversation(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "AI commands are only available in servers."),
                ephemeral=True,
            )
            return
        if not await self._validate_ai_use(interaction):
            return
        reset = await self._reset_session(interaction.guild.id, interaction.user.id, interaction.channel.id)
        if reset:
            embed = EmbedFactory.success("Conversation Cleared", "Your AI conversation history has been reset.")
        else:
            embed = EmbedFactory.info("No Conversation", "No AI history found for this channel.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="summarize", description="Deprecated: use /ai ask with summarize prompt")
    @app_commands.describe(count="Number of messages to summarize (max 100)")
    async def summarize(self, interaction: discord.Interaction, count: int = 50) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if count < 1 or count > 100:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Count", "Count must be between 1 and 100."),
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "AI commands are only available in servers."),
                ephemeral=True,
            )
            return

        settings = await self._get_guild_settings(interaction.guild.id)
        if not settings.get("enabled", False):
            await interaction.followup.send(
                embed=EmbedFactory.error("AI Disabled", "AI is not enabled for this server."),
                ephemeral=True,
            )
            return

        if not await self._validate_ai_use(interaction):
            return

        allowed_channels = settings.get("allowed_channel_ids", [])
        if not self._is_channel_allowed(interaction.channel, allowed_channels):
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Allowed", "AI is disabled here; ask an admin to allow this channel."),
                ephemeral=True,
            )
            return

        messages = []
        try:
            async for message in interaction.channel.history(limit=count):
                if not message.author.bot and message.content:
                    messages.append(f"{message.author.display_name}: {message.content}")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "I don't have permission to read message history."),
                ephemeral=True,
            )
            return

        if not messages:
            await interaction.followup.send(
                embed=EmbedFactory.info("No Messages", "No messages to summarize."),
                ephemeral=True,
            )
            return

        messages.reverse()
        conversation_text = "\n".join(messages)
        if self._contains_mass_mentions(conversation_text):
            await interaction.followup.send(
                embed=EmbedFactory.error("Mentions Blocked", "Summary input contains @everyone or @here."),
                ephemeral=True,
            )
            return

        prompt = f"Summarize this Discord conversation concisely:\n\n{conversation_text}"
        if len(prompt) > MAX_PROMPT_CHARS:
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Too Long",
                    f"Summary input too long. Try a smaller count (max {MAX_PROMPT_CHARS} chars).",
                ),
                ephemeral=True,
            )
            return
        await self._run_ai_request(
            interaction,
            prompt=prompt,
            mode=None,
            private=True,
            use_session=False,
            session_active=False,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.module_config.get("enabled", True):
            return
        if message.author.bot or not message.guild:
            return

        module_config = self.config.get("modules", {}).get("moderation", {})
        if not module_config.get("auto_mod", {}).get("toxicity_filter", False):
            return

        moderation_result = await self._moderate_content(message.content)
        if moderation_result.get("flagged", False):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention} Your message was removed for violating community guidelines.",
                    delete_after=10,
                )
                logger.info("Auto-moderated message from %s in %s", message.author, message.guild.id)
            except discord.Forbidden:
                pass


async def setup(bot: commands.Bot) -> None:
    """Setup function for cog loading."""
    cog = AIChat(bot, bot.db, bot.config)
    await bot.add_cog(cog)

    existing = bot.tree.get_command("ai")
    if existing:
        bot.tree.remove_command("ai", type=discord.AppCommandType.chat_input)
    bot.tree.add_command(cog.ai)

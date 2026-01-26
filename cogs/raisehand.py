"""
Raise hand / speaking queue for voice channel text chat.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from database.models import FeatureKey
from utils.embeds import EmbedFactory, EmbedColor
from utils.feature_permissions import FeaturePermissionManager, SENSITIVE_FEATURES
from utils.logs import resolve_log_channel

logger = logging.getLogger(__name__)

DEFAULT_TURN_MINUTES = 3
DEFAULT_TURN_SECONDS = DEFAULT_TURN_MINUTES * 60
DEFAULT_EMOJI = "\U0001F44B"
DEFAULT_ALT_EMOJI = "\U0001F590\uFE0F"
DEFAULT_MAX_QUEUE_DISPLAY = 15
DEFAULT_DEBOUNCE_MS = 700


@dataclass
class RaiseHandSession:
    guild_id: int
    vc_id: int
    text_channel_id: int
    moderator_id: int
    turn_seconds: int
    panel_message_id: int
    emoji: str
    max_queue_display: int
    debounce_ms: int
    queue: List[int] = field(default_factory=list)
    current_speaker_id: Optional[int] = None
    current_ends_at: Optional[datetime] = None
    original_mute: Dict[int, bool] = field(default_factory=dict)
    running: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer_task: Optional[asyncio.Task] = None
    panel_update_task: Optional[asyncio.Task] = None
    panel_dirty: bool = False


class RaiseHand(commands.Cog):
    """Raise-hand speaking queue for voice channels."""

    raisehand = app_commands.Group(
        name="raisehand",
        description="Manage a voice channel speaking queue",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot, db: DatabaseManager):
        self.bot = bot
        self.db = db
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        if hasattr(self.perms, "denials"):
            self.denials = self.perms.denials
        else:
            self.denials = None
        self.sessions: Dict[Tuple[int, int], RaiseHandSession] = {}
        self.config = getattr(bot, "config", {}) or {}
        self._restore_task: Optional[asyncio.Task] = None

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _config_int(self, key: str, default: int) -> int:
        value = (self.config.get("raisehand", {}) or {}).get(key, default)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        return max(1, value)

    def _config_str(self, key: str, default: str) -> str:
        value = (self.config.get("raisehand", {}) or {}).get(key, default)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default

    def _config_turn_minutes(self) -> int:
        cfg = self.config.get("raisehand", {}) or {}
        if "default_turn_minutes" in cfg:
            return self._config_int("default_turn_minutes", DEFAULT_TURN_MINUTES)
        seconds = cfg.get("default_turn_seconds")
        if seconds is not None:
            try:
                seconds_value = int(seconds)
            except (TypeError, ValueError):
                seconds_value = DEFAULT_TURN_SECONDS
            return max(1, (seconds_value + 59) // 60)
        return DEFAULT_TURN_MINUTES

    def _ensure_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _serialize_original_mute(self, original_mute: Dict[int, bool]) -> Dict[str, bool]:
        return {str(user_id): muted for user_id, muted in original_mute.items()}

    def _deserialize_original_mute(self, stored: Dict[str, bool]) -> Dict[int, bool]:
        cleaned: Dict[int, bool] = {}
        for key, value in (stored or {}).items():
            try:
                cleaned[int(key)] = bool(value)
            except (TypeError, ValueError):
                continue
        return cleaned

    async def _persist_session(self, session: RaiseHandSession) -> None:
        try:
            await self.db.upsert_raisehand_session(
                session.guild_id,
                session.vc_id,
                {
                    "text_channel_id": session.text_channel_id,
                    "moderator_id": session.moderator_id,
                    "turn_seconds": session.turn_seconds,
                    "panel_message_id": session.panel_message_id,
                    "emoji": session.emoji,
                    "max_queue_display": session.max_queue_display,
                    "debounce_ms": session.debounce_ms,
                    "queue": list(session.queue),
                    "current_speaker_id": session.current_speaker_id,
                    "current_ends_at": session.current_ends_at,
                    "original_mute": self._serialize_original_mute(session.original_mute),
                    "running": session.running,
                    "updated_at": self._now(),
                },
            )
        except Exception:
            logger.exception(
                "Failed to persist raisehand session guild=%s vc=%s",
                session.guild_id,
                session.vc_id,
            )

    async def _delete_persisted_session(self, session: RaiseHandSession) -> None:
        try:
            await self.db.delete_raisehand_session(session.guild_id, session.vc_id)
        except Exception:
            logger.exception(
                "Failed to delete raisehand session guild=%s vc=%s",
                session.guild_id,
                session.vc_id,
            )

    async def cog_load(self) -> None:
        self._restore_task = asyncio.create_task(self._restore_sessions())

    def cog_unload(self) -> None:
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()

    def _session_key(self, guild_id: int, vc_id: int) -> Tuple[int, int]:
        return guild_id, vc_id

    def _get_session(self, guild_id: int, vc_id: int) -> Optional[RaiseHandSession]:
        return self.sessions.get(self._session_key(guild_id, vc_id))

    async def _restore_sessions(self) -> None:
        await self.bot.wait_until_ready()
        try:
            records = await self.db.list_raisehand_sessions()
        except Exception:
            logger.exception("Failed to load raisehand sessions from database")
            return

        for record in records:
            guild_id = record.get("guild_id")
            vc_id = record.get("vc_id")
            if not guild_id or not vc_id:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await self.db.delete_raisehand_session(guild_id, vc_id)
                continue
            channel = guild.get_channel(vc_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(vc_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    await self.db.delete_raisehand_session(guild_id, vc_id)
                    continue
            if not isinstance(channel, discord.VoiceChannel):
                await self.db.delete_raisehand_session(guild_id, vc_id)
                continue

            panel_message_id = record.get("panel_message_id")
            if not panel_message_id:
                await self.db.delete_raisehand_session(guild_id, vc_id)
                continue

            moderator_id = record.get("moderator_id")
            moderator = guild.get_member(moderator_id) if moderator_id else None
            if not moderator or not moderator.voice or not moderator.voice.channel or moderator.voice.channel.id != vc_id:
                await self.db.delete_raisehand_session(guild_id, vc_id)
                continue

            session = RaiseHandSession(
                guild_id=guild_id,
                vc_id=vc_id,
                text_channel_id=record.get("text_channel_id", vc_id),
                moderator_id=moderator_id,
                turn_seconds=int(record.get("turn_seconds") or DEFAULT_TURN_SECONDS),
                panel_message_id=panel_message_id,
                emoji=record.get("emoji") or DEFAULT_EMOJI,
                max_queue_display=int(record.get("max_queue_display") or DEFAULT_MAX_QUEUE_DISPLAY),
                debounce_ms=int(record.get("debounce_ms") or DEFAULT_DEBOUNCE_MS),
                queue=list(record.get("queue") or []),
            )
            session.current_speaker_id = record.get("current_speaker_id")
            session.current_ends_at = self._ensure_utc(record.get("current_ends_at"))
            session.original_mute = self._deserialize_original_mute(record.get("original_mute", {}))
            session.running = bool(record.get("running", True))

            self.sessions[self._session_key(guild_id, vc_id)] = session

            for member in channel.members:
                session.original_mute.setdefault(member.id, bool(member.voice and member.voice.mute))

            if session.current_speaker_id:
                speaker = guild.get_member(session.current_speaker_id)
                if not speaker or not speaker.voice or not speaker.voice.channel or speaker.voice.channel.id != vc_id:
                    session.current_speaker_id = None
                    session.current_ends_at = None

            for member in channel.members:
                if member.id == session.moderator_id:
                    continue
                if session.current_speaker_id and member.id == session.current_speaker_id:
                    try:
                        await member.edit(mute=False, reason="Raisehand restore")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.warning("Failed to unmute restored speaker %s", member.id)
                else:
                    try:
                        await member.edit(mute=True, reason="Raisehand restore")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.warning("Failed to mute restored member %s", member.id)

            if session.current_speaker_id and session.current_ends_at:
                if session.current_ends_at <= self._now():
                    await self._advance(session, reason="restore")
                else:
                    self._start_timer(session)
            elif session.queue:
                await self._advance(session, reason="restore")

            await self._update_panel(session, note="Session restored after restart.")
            await self._persist_session(session)

    async def _security_locked(self, interaction: discord.Interaction, feature: FeatureKey) -> bool:
        if feature not in SENSITIVE_FEATURES:
            return False
        ready = await self.perms.security_ready(interaction.guild)
        if ready:
            return False
        embed = EmbedFactory.error(
            "Security Setup Required",
            "Sensitive commands are locked until an admin runs `/perms security-bootstrap`.",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        if self.denials and self.denials.should_log(interaction.guild.id, interaction.user.id, "raisehand", feature.value):
            logger.warning(
                "Sensitive feature %s blocked due to uninitialized security in guild %s",
                feature.value,
                interaction.guild.id,
            )
        return True

    def _base_raisehand_check(self, member: discord.Member) -> bool:
        return member.guild_permissions.mute_members

    async def _can_manage(self, member: discord.Member) -> bool:
        return await self.perms.check(member, FeatureKey.RAISEHAND_MANAGE, self._base_raisehand_check)

    async def _log_to_mod(self, guild: discord.Guild, embed: discord.Embed) -> None:
        channel = await resolve_log_channel(self.db, guild, "raisehand")
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send raisehand log to %s in %s", channel, guild.id)

    def _required_app_perms(self, interaction: discord.Interaction) -> List[str]:
        perms = interaction.app_permissions
        required = ["mute_members", "send_messages", "add_reactions", "read_message_history"]
        missing = [perm for perm in required if not getattr(perms, perm, False)]
        return missing

    def _valid_channel(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        if isinstance(interaction.channel, discord.VoiceChannel):
            return interaction.channel
        return None

    def _ensure_same_vc(self, interaction: discord.Interaction, vc: discord.VoiceChannel) -> bool:
        voice = interaction.user.voice
        return bool(voice and voice.channel and voice.channel.id == vc.id)

    def _panel_embed(
        self,
        session: RaiseHandSession,
        note: Optional[str] = None,
        queue_override: Optional[str] = None,
    ) -> discord.Embed:
        current = "None"
        remaining = None
        if session.current_speaker_id:
            current = f"<@{session.current_speaker_id}>"
            if session.current_ends_at:
                remaining = max(0, int((session.current_ends_at - self._now()).total_seconds()))

        turn_minutes = max(1, (session.turn_seconds + 59) // 60)
        queue_lines, extra = self._build_queue_lines(session, session.max_queue_display)
        if extra > 0:
            queue_lines.append(f"+{extra} more...")

        fields = [
            {
                "name": "Current Speaker",
                "value": f"{current}" + (f" (`{self._format_duration(remaining)}` left)" if remaining is not None else ""),
                "inline": False,
            },
            {
                "name": "Queue",
                "value": queue_override or ("\n".join(queue_lines) if queue_lines else "Waiting for hands..."),
                "inline": False,
            },
        ]
        description = (
            f"React with {session.emoji} to join. Remove the reaction to leave.\n"
            f"Turn duration: `{turn_minutes} min`"
        )
        if note:
            description = f"{description}\n\n{note}"
        return EmbedFactory.create(
            title=f"{session.emoji} Speaking Queue",
            description=description,
            color=EmbedColor.INFO,
            fields=fields,
        )

    async def _fetch_panel_message(self, session: RaiseHandSession) -> Optional[discord.Message]:
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return None
        channel = guild.get_channel(session.text_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(session.text_channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None
        try:
            return await channel.fetch_message(session.panel_message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _format_duration(self, seconds: Optional[int]) -> str:
        if seconds is None:
            return "0s"
        seconds = max(0, int(seconds))
        minutes, secs = divmod(seconds, 60)
        if minutes <= 0:
            return f"{secs}s"
        if secs == 0:
            return f"{minutes}m"
        return f"{minutes}m {secs}s"

    def _build_queue_lines(
        self, session: RaiseHandSession, limit: Optional[int] = None
    ) -> Tuple[List[str], int]:
        if limit is None:
            queue_ids = session.queue
        else:
            queue_ids = session.queue[:limit]
        lines = [f"{idx}. <@{user_id}>" for idx, user_id in enumerate(queue_ids, start=1)]
        extra = len(session.queue) - len(queue_ids)
        return lines, extra

    def _chunk_lines(self, lines: List[str], max_len: int = 1800) -> List[str]:
        chunks: List[str] = []
        current: List[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line)
            if current and current_len + line_len + 1 > max_len:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                if current:
                    current_len += 1
                current.append(line)
                current_len += line_len
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _queue_tail_id(self, session: RaiseHandSession) -> Optional[int]:
        if session.queue:
            return session.queue[-1]
        return session.current_speaker_id

    def _queue_last_position(self, session: RaiseHandSession, user_id: int) -> Optional[int]:
        for idx in range(len(session.queue) - 1, -1, -1):
            if session.queue[idx] == user_id:
                return idx + 1
        return None

    def _estimate_wait_seconds(self, session: RaiseHandSession, user_id: int) -> Optional[int]:
        if session.current_speaker_id == user_id:
            return 0
        position = self._queue_last_position(session, user_id)
        if position is None:
            return None
        remaining = 0
        if session.current_speaker_id and session.current_ends_at:
            remaining = max(0, int((session.current_ends_at - self._now()).total_seconds()))
        return remaining + (position - 1) * session.turn_seconds

    def _accepted_emojis(self, session: RaiseHandSession) -> set[str]:
        emojis = {session.emoji}
        if session.emoji in {DEFAULT_EMOJI, DEFAULT_ALT_EMOJI}:
            emojis.update({DEFAULT_EMOJI, DEFAULT_ALT_EMOJI})
        return emojis

    async def _update_panel(self, session: RaiseHandSession, note: Optional[str] = None) -> None:
        message = await self._fetch_panel_message(session)
        if not message:
            return
        async with session.lock:
            embed = self._panel_embed(session, note=note)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            logger.warning("Failed to update raisehand panel for guild=%s vc=%s", session.guild_id, session.vc_id)

    async def _fetch_text_channel(self, session: RaiseHandSession) -> Optional[discord.abc.Messageable]:
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return None
        channel = guild.get_channel(session.text_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(session.text_channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None
        return channel

    async def _repost_panel(self, session: RaiseHandSession, note: Optional[str] = None) -> bool:
        channel = await self._fetch_text_channel(session)
        if channel is None:
            return False
        old_message = await self._fetch_panel_message(session)
        if old_message:
            try:
                await old_message.delete()
            except discord.HTTPException:
                logger.warning(
                    "Failed to delete raisehand panel for guild=%s vc=%s",
                    session.guild_id,
                    session.vc_id,
                )
        async with session.lock:
            embed = self._panel_embed(session, note=note)
        try:
            new_message = await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Failed to repost raisehand panel for guild=%s vc=%s", session.guild_id, session.vc_id)
            return False
        try:
            await new_message.add_reaction(session.emoji)
        except discord.HTTPException:
            logger.warning("Failed to add reaction to raisehand panel for guild=%s", session.guild_id)
        async with session.lock:
            session.panel_message_id = new_message.id
            session.panel_dirty = False
        if session.panel_update_task and not session.panel_update_task.done():
            session.panel_update_task.cancel()
            session.panel_update_task = None
        await self._persist_session(session)
        return True

    async def _enqueue_member(self, session: RaiseHandSession, user_id: int) -> Tuple[bool, bool, Optional[str]]:
        async with session.lock:
            last_in_line = self._queue_tail_id(session)
            if last_in_line == user_id:
                return False, False, "You are already next in line. Wait for another speaker before rejoining."
            session.queue.append(user_id)
            should_advance = session.current_speaker_id is None
        return True, should_advance, None

    def _schedule_panel_update(self, session: RaiseHandSession) -> None:
        session.panel_dirty = True
        if session.panel_update_task and not session.panel_update_task.done():
            return
        session.panel_update_task = asyncio.create_task(self._panel_update_worker(session))

    async def _panel_update_worker(self, session: RaiseHandSession) -> None:
        await asyncio.sleep(session.debounce_ms / 1000)
        if not session.running:
            return
        if not session.panel_dirty:
            return
        session.panel_dirty = False
        await self._update_panel(session)

    def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()

    def _start_timer(self, session: RaiseHandSession) -> None:
        self._cancel_task(session.timer_task)
        if session.current_ends_at is None:
            session.timer_task = None
            return
        session.timer_task = asyncio.create_task(self._timer_worker(session))

    async def _timer_worker(self, session: RaiseHandSession) -> None:
        try:
            remaining = (session.current_ends_at - self._now()).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
            if session.running:
                await self._advance(session, reason="timer")
        except asyncio.CancelledError:
            return

    async def _advance(self, session: RaiseHandSession, reason: str, force_repost: bool = False) -> None:
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return

        had_speaker = False
        async with session.lock:
            if not session.running:
                return

            had_speaker = session.current_speaker_id is not None or force_repost
            if session.current_speaker_id:
                current = guild.get_member(session.current_speaker_id)
                if current and current.voice and current.voice.channel and current.voice.channel.id == session.vc_id:
                    try:
                        await current.edit(mute=True, reason=f"Raisehand advance ({reason})")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.warning("Failed to mute previous speaker %s in raisehand", session.current_speaker_id)

            next_member = None
            next_id = None
            while session.queue:
                candidate_id = session.queue.pop(0)
                candidate = guild.get_member(candidate_id)
                if not candidate or not candidate.voice or not candidate.voice.channel:
                    continue
                if candidate.voice.channel.id != session.vc_id:
                    continue
                try:
                    await candidate.edit(mute=False, reason="Raisehand turn")
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("Failed to unmute next speaker %s in raisehand", candidate_id)
                    continue
                next_member = candidate
                next_id = candidate_id
                break

            if not next_id:
                session.current_speaker_id = None
                session.current_ends_at = None
                self._cancel_task(session.timer_task)
                session.timer_task = None
            else:
                session.current_speaker_id = next_id
                session.current_ends_at = self._now() + timedelta(seconds=session.turn_seconds)
                self._start_timer(session)

        if had_speaker:
            reposted = await self._repost_panel(session)
            if not reposted:
                self._schedule_panel_update(session)
                await self._persist_session(session)
        else:
            self._schedule_panel_update(session)
            await self._persist_session(session)
        if next_member is None:
            logger.info("Raisehand advance ended (no speakers) for guild=%s vc=%s", session.guild_id, session.vc_id)

    async def _stop_session(self, session: RaiseHandSession, reason: str, note: Optional[str] = None) -> None:
        async with session.lock:
            session.running = False
            self._cancel_task(session.timer_task)
            self._cancel_task(session.panel_update_task)
            session.timer_task = None
            session.panel_update_task = None

        guild = self.bot.get_guild(session.guild_id)
        if guild:
            channel = guild.get_channel(session.vc_id)
            if channel and isinstance(channel, discord.VoiceChannel):
                for member in channel.members:
                    if member.id in session.original_mute:
                        try:
                            await member.edit(
                                mute=session.original_mute[member.id],
                                reason=f"Raisehand stop ({reason})",
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            logger.warning("Failed to restore mute for %s in raisehand", member.id)

        await self._update_panel(session, note=note or "Session ended.")
        self.sessions.pop(self._session_key(session.guild_id, session.vc_id), None)
        await self._delete_persisted_session(session)

    async def _ensure_session(
        self,
        interaction: discord.Interaction,
    ) -> Optional[Tuple[RaiseHandSession, discord.VoiceChannel]]:
        vc = self._valid_channel(interaction)
        if vc is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Channel", "Run this inside the target voice channel chat."),
                ephemeral=True,
            )
            return None
        if not self._ensure_same_vc(interaction, vc):
            await interaction.followup.send(
                embed=EmbedFactory.error("Not in VC", "You must be connected to this voice channel."),
                ephemeral=True,
            )
            return None
        session = self._get_session(interaction.guild.id, vc.id)
        if not session:
            await interaction.followup.send(
                embed=EmbedFactory.info("No Session", "No active raisehand session for this channel."),
                ephemeral=True,
            )
            return None
        return session, vc

    @raisehand.command(name="start", description="Start a raisehand speaking queue")
    @app_commands.describe(turn_minutes="Minutes per speaker turn")
    async def raisehand_start(self, interaction: discord.Interaction, turn_minutes: Optional[int] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "This command is only available in servers."),
                ephemeral=True,
            )
            return

        if await self._security_locked(interaction, FeatureKey.RAISEHAND_MANAGE):
            return

        if not await self._can_manage(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage raisehand."),
                ephemeral=True,
            )
            return

        vc = self._valid_channel(interaction)
        if vc is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Channel", "Run this inside the target voice channel chat."),
                ephemeral=True,
            )
            return
        if not self._ensure_same_vc(interaction, vc):
            await interaction.followup.send(
                embed=EmbedFactory.error("Not in VC", "You must be connected to this voice channel."),
                ephemeral=True,
            )
            return

        if self._get_session(interaction.guild.id, vc.id):
            await interaction.followup.send(
                embed=EmbedFactory.warning("Already Running", "A raisehand session is already active here."),
                ephemeral=True,
            )
            return

        missing = self._required_app_perms(interaction)
        if missing:
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Missing Permissions",
                    f"Bot lacks: {', '.join(missing)}",
                ),
                ephemeral=True,
            )
            return

        turn_minutes = turn_minutes or self._config_turn_minutes()
        if turn_minutes < 1:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Duration", "Turn minutes must be at least 1."),
                ephemeral=True,
            )
            return
        turn_seconds = turn_minutes * 60

        emoji = self._config_str("emoji", DEFAULT_EMOJI)
        max_queue_display = self._config_int("max_queue_display", DEFAULT_MAX_QUEUE_DISPLAY)
        debounce_ms = self._config_int("panel_debounce_ms", DEFAULT_DEBOUNCE_MS)

        panel_message = await interaction.channel.send(
            embed=EmbedFactory.create(
                title=f"{emoji} Speaking Queue",
                description=f"React with {emoji} to join.\nTurn duration: `{turn_minutes} min`",
                color=EmbedColor.INFO,
            )
        )
        try:
            await panel_message.add_reaction(emoji)
        except discord.HTTPException:
            logger.warning("Failed to add reaction to raisehand panel in guild=%s", interaction.guild.id)

        failures = []
        original_mute: Dict[int, bool] = {}
        for member in vc.members:
            original_mute[member.id] = bool(member.voice and member.voice.mute)
            if member.id == interaction.user.id:
                continue
            try:
                await member.edit(mute=True, reason="Raisehand session start")
            except (discord.Forbidden, discord.HTTPException):
                failures.append(member.mention)

        session = RaiseHandSession(
            guild_id=interaction.guild.id,
            vc_id=vc.id,
            text_channel_id=vc.id,
            moderator_id=interaction.user.id,
            turn_seconds=turn_seconds,
            panel_message_id=panel_message.id,
            emoji=emoji,
            max_queue_display=max_queue_display,
            debounce_ms=debounce_ms,
            original_mute=original_mute,
        )
        self.sessions[self._session_key(interaction.guild.id, vc.id)] = session
        await self._persist_session(session)

        embed = EmbedFactory.success(
            "Raisehand Started",
            f"Session started in {vc.mention}.\nTurn duration: `{turn_minutes} min`",
        )
        if failures:
            embed.add_field(name="Mute Failures", value=", ".join(failures), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Raisehand Started",
                description=f"Started by {interaction.user.mention} in {vc.mention}",
                color=EmbedColor.INFO,
            ),
        )

    @raisehand.command(name="stop", description="Stop the raisehand session")
    async def raisehand_stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "This command is only available in servers."),
                ephemeral=True,
            )
            return

        if await self._security_locked(interaction, FeatureKey.RAISEHAND_MANAGE):
            return

        if not await self._can_manage(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage raisehand."),
                ephemeral=True,
            )
            return

        result = await self._ensure_session(interaction)
        if not result:
            return
        session, _vc = result
        await self._stop_session(session, reason="command", note="Session ended.")

        await interaction.followup.send(
            embed=EmbedFactory.success("Raisehand Stopped", "Session stopped and mutes restored."),
            ephemeral=True,
        )
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Raisehand Stopped",
                description=f"Stopped by {interaction.user.mention} in <#{session.vc_id}>",
                color=EmbedColor.WARNING,
            ),
        )

    @raisehand.command(name="skip", description="Skip the current speaker")
    async def raisehand_skip(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "This command is only available in servers."),
                ephemeral=True,
            )
            return

        if await self._security_locked(interaction, FeatureKey.RAISEHAND_MANAGE):
            return
        if not await self._can_manage(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage raisehand."),
                ephemeral=True,
            )
            return

        result = await self._ensure_session(interaction)
        if not result:
            return
        session, _vc = result

        await self._advance(session, reason="skip")
        await interaction.followup.send(
            embed=EmbedFactory.success("Speaker Skipped", "Moved to the next speaker."),
            ephemeral=True,
        )
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Raisehand Skip",
                description=f"Skipped by {interaction.user.mention} in <#{session.vc_id}>",
                color=EmbedColor.WARNING,
            ),
        )

    @raisehand.command(name="extend", description="Extend the current speaker's turn")
    @app_commands.describe(extra_minutes="Extra minutes to add to the current turn")
    @app_commands.choices(
        extra_minutes=[
            app_commands.Choice(name="2 minutes", value=2),
            app_commands.Choice(name="3 minutes", value=3),
            app_commands.Choice(name="5 minutes", value=5),
        ]
    )
    async def raisehand_extend(
        self,
        interaction: discord.Interaction,
        extra_minutes: app_commands.Choice[int],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "This command is only available in servers."),
                ephemeral=True,
            )
            return

        if await self._security_locked(interaction, FeatureKey.RAISEHAND_MANAGE):
            return
        if not await self._can_manage(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage raisehand."),
                ephemeral=True,
            )
            return

        extra_seconds = extra_minutes.value * 60

        result = await self._ensure_session(interaction)
        if not result:
            return
        session, _vc = result

        async with session.lock:
            if not session.current_ends_at:
                await interaction.followup.send(
                    embed=EmbedFactory.info("No Speaker", "No current speaker to extend."),
                    ephemeral=True,
                )
                return
            session.current_ends_at += timedelta(seconds=extra_seconds)
            self._start_timer(session)
        self._schedule_panel_update(session)
        await self._persist_session(session)

        await interaction.followup.send(
            embed=EmbedFactory.success(
                "Speaker Extended",
                f"Added {extra_minutes.value} minutes to the current turn.",
            ),
            ephemeral=True,
        )

    @raisehand.command(name="swap", description="Swap the current speaker with a queued user")
    @app_commands.describe(user="User to promote from the queue")
    async def raisehand_swap(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "This command is only available in servers."),
                ephemeral=True,
            )
            return

        if await self._security_locked(interaction, FeatureKey.RAISEHAND_MANAGE):
            return
        if not await self._can_manage(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage raisehand."),
                ephemeral=True,
            )
            return

        result = await self._ensure_session(interaction)
        if not result:
            return
        session, _vc = result

        should_advance = False
        force_repost = False
        async with session.lock:
            if not session.current_speaker_id:
                await interaction.followup.send(
                    embed=EmbedFactory.info("No Speaker", "No current speaker to swap."),
                    ephemeral=True,
                )
                return
            if user.id not in session.queue:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Not Queued", "That user is not in the queue."),
                    ephemeral=True,
                )
                return
            if not user.voice or not user.voice.channel or user.voice.channel.id != session.vc_id:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Not in VC", "That user is not in this voice channel."),
                    ephemeral=True,
                )
                return

            current_id = session.current_speaker_id
            current_member = interaction.guild.get_member(current_id)
            if current_member:
                try:
                    await current_member.edit(mute=True, reason="Raisehand swap")
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("Failed to mute current speaker during swap")

            session.queue.remove(user.id)
            session.queue.insert(0, current_id)
            try:
                await user.edit(mute=False, reason="Raisehand swap")
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Failed to unmute swapped speaker; advancing instead")
                session.current_speaker_id = None
                session.current_ends_at = None
                should_advance = True
                force_repost = True
            else:
                session.current_speaker_id = user.id
                session.current_ends_at = self._now() + timedelta(seconds=session.turn_seconds)
                self._start_timer(session)

        if should_advance:
            await self._advance(session, reason="swap", force_repost=force_repost)
        else:
            self._schedule_panel_update(session)
            await self._persist_session(session)

        await interaction.followup.send(
            embed=EmbedFactory.success("Speaker Swapped", f"{user.mention} is now speaking."),
            ephemeral=True,
        )
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Raisehand Swap",
                description=f"Swapped by {interaction.user.mention} in <#{session.vc_id}>",
                color=EmbedColor.INFO,
            ),
        )

    @raisehand.command(name="remove", description="Remove a user from the queue or current turn")
    @app_commands.describe(user="User to remove from the queue")
    async def raisehand_remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "This command is only available in servers."),
                ephemeral=True,
            )
            return

        if await self._security_locked(interaction, FeatureKey.RAISEHAND_MANAGE):
            return
        if not await self._can_manage(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage raisehand."),
                ephemeral=True,
            )
            return

        result = await self._ensure_session(interaction)
        if not result:
            return
        session, _vc = result

        should_advance = False
        force_repost = False
        async with session.lock:
            if user.id == session.current_speaker_id:
                current_member = interaction.guild.get_member(user.id)
                if current_member:
                    try:
                        await current_member.edit(mute=True, reason="Raisehand remove")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.warning("Failed to mute current speaker during remove")
                session.current_speaker_id = None
                session.current_ends_at = None
                should_advance = True
                force_repost = True
            elif user.id in session.queue:
                session.queue = [user_id for user_id in session.queue if user_id != user.id]
            else:
                await interaction.followup.send(
                    embed=EmbedFactory.info("Not Found", "That user is not in the queue or speaking."),
                    ephemeral=True,
                )
                return

        if should_advance:
            await self._advance(session, reason="remove", force_repost=force_repost)
        else:
            self._schedule_panel_update(session)
            await self._persist_session(session)

        await interaction.followup.send(
            embed=EmbedFactory.success("User Removed", f"Removed {user.mention} from the session."),
            ephemeral=True,
        )
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Raisehand Remove",
                description=f"Removed {user.mention} by {interaction.user.mention} in <#{session.vc_id}>",
                color=EmbedColor.WARNING,
            ),
        )

    @raisehand.command(name="status", description="Show the current raisehand status")
    @app_commands.describe(public="Post the status publicly in the channel", full="Show the full queue list")
    async def raisehand_status(
        self,
        interaction: discord.Interaction,
        public: Optional[bool] = False,
        full: Optional[bool] = False,
    ) -> None:
        ephemeral = not bool(public)
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Unavailable", "This command is only available in servers."),
                ephemeral=ephemeral,
            )
            return

        if await self._security_locked(interaction, FeatureKey.RAISEHAND_MANAGE):
            return
        if not await self._can_manage(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "You do not have permission to manage raisehand."),
                ephemeral=ephemeral,
            )
            return

        result = await self._ensure_session(interaction)
        if not result:
            return
        session, _vc = result

        queue_override = None
        queue_chunks: List[str] = []
        if full:
            lines, _extra = self._build_queue_lines(session, limit=None)
            if lines:
                queue_chunks = self._chunk_lines(lines)
                queue_override = "Full queue posted below."
        embed = self._panel_embed(session, queue_override=queue_override)
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        if queue_chunks:
            for index, chunk in enumerate(queue_chunks, start=1):
                label = f"Queue (part {index}/{len(queue_chunks)})"
                await interaction.followup.send(
                    f"{label}\n{chunk}",
                    ephemeral=ephemeral,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.channel, discord.VoiceChannel):
            return
        session = self._get_session(message.guild.id, message.channel.id)
        if not session or not session.running:
            return
        content = message.content.strip()
        if content not in self._accepted_emojis(session):
            return

        member = message.author if isinstance(message.author, discord.Member) else None
        if member is None:
            return
        if not member.voice or not member.voice.channel or member.voice.channel.id != session.vc_id:
            await message.reply(
                "Join the voice channel to enter the queue.",
                mention_author=False,
            )
            return

        added, should_advance, reason = await self._enqueue_member(session, member.id)
        if not added:
            await message.reply(reason or "Unable to add you to the queue.", mention_author=False)
            return
        if should_advance:
            await self._advance(session, reason="queue")
        else:
            self._schedule_panel_update(session)
            await self._persist_session(session)

        if session.current_speaker_id == member.id:
            response = "You are up now."
        else:
            estimate = self._estimate_wait_seconds(session, member.id)
            if estimate is None:
                response = "Unable to confirm your spot in the queue. Please try again."
            elif estimate <= 0:
                response = "You are next in line."
            else:
                response = f"You are in the queue. Estimated wait: {self._format_duration(estimate)}."
        await message.reply(response, mention_author=False)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        session = self._get_session(payload.guild_id, payload.channel_id)
        if not session or not session.running:
            return
        if payload.message_id != session.panel_message_id:
            return
        emoji = str(payload.emoji)
        if emoji not in self._accepted_emojis(session):
            return
        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        if not member.voice or not member.voice.channel or member.voice.channel.id != session.vc_id:
            return

        added, should_advance, _reason = await self._enqueue_member(session, payload.user_id)
        if not added:
            return
        if should_advance:
            await self._advance(session, reason="queue")
        else:
            self._schedule_panel_update(session)
            await self._persist_session(session)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        session = self._get_session(payload.guild_id, payload.channel_id)
        if not session or not session.running:
            return
        if payload.message_id != session.panel_message_id:
            return
        emoji = str(payload.emoji)
        if emoji not in self._accepted_emojis(session):
            return
        if payload.user_id == self.bot.user.id:
            return

        changed = False
        async with session.lock:
            original_len = len(session.queue)
            if original_len:
                session.queue = [user_id for user_id in session.queue if user_id != payload.user_id]
                changed = len(session.queue) != original_len
        if not changed:
            return
        self._schedule_panel_update(session)
        await self._persist_session(session)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        before_channel = before.channel
        after_channel = after.channel

        if before_channel and before_channel.id != (after_channel.id if after_channel else None):
            session = self._get_session(member.guild.id, before_channel.id)
            if session:
                should_advance = False
                force_repost = False
                async with session.lock:
                    if session.queue:
                        session.queue = [user_id for user_id in session.queue if user_id != member.id]
                    if member.id == session.current_speaker_id:
                        session.current_speaker_id = None
                        session.current_ends_at = None
                        should_advance = True
                        force_repost = True
                    if member.id == session.moderator_id:
                        should_advance = False
                if member.id == session.moderator_id:
                    await self._stop_session(session, reason="moderator_left", note="Session ended (moderator left).")
                    return
                if should_advance:
                    await self._advance(session, reason="speaker_left", force_repost=force_repost)
                else:
                    self._schedule_panel_update(session)
                    await self._persist_session(session)

        if after_channel and after_channel.id != (before_channel.id if before_channel else None):
            session = self._get_session(member.guild.id, after_channel.id)
            if session and session.running:
                async with session.lock:
                    if member.id not in session.original_mute:
                        session.original_mute[member.id] = bool(member.voice and member.voice.mute)
                if member.id != session.moderator_id and member.id != session.current_speaker_id:
                    try:
                        await member.edit(mute=True, reason="Raisehand join")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.warning("Failed to mute joining member %s in raisehand", member.id)
                self._schedule_panel_update(session)
                await self._persist_session(session)


async def setup(bot: commands.Bot) -> None:
    """Setup function for cog loading."""
    cog = RaiseHand(bot, bot.db)
    await bot.add_cog(cog)

    existing = bot.tree.get_command("raisehand")
    if existing:
        bot.tree.remove_command("raisehand", type=discord.AppCommandType.chat_input)
    bot.tree.add_command(cog.raisehand)

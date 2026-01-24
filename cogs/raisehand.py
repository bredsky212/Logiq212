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

DEFAULT_TURN_SECONDS = 60
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

    def _session_key(self, guild_id: int, vc_id: int) -> Tuple[int, int]:
        return guild_id, vc_id

    def _get_session(self, guild_id: int, vc_id: int) -> Optional[RaiseHandSession]:
        return self.sessions.get(self._session_key(guild_id, vc_id))

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

    def _panel_embed(self, session: RaiseHandSession, note: Optional[str] = None) -> discord.Embed:
        current = "None"
        remaining = None
        if session.current_speaker_id:
            current = f"<@{session.current_speaker_id}>"
            if session.current_ends_at:
                remaining = max(0, int((session.current_ends_at - self._now()).total_seconds()))

        queue_lines = []
        for idx, user_id in enumerate(session.queue[: session.max_queue_display], start=1):
            queue_lines.append(f"{idx}. <@{user_id}>")
        extra = len(session.queue) - session.max_queue_display
        if extra > 0:
            queue_lines.append(f"+{extra} more...")

        fields = [
            {
                "name": "Current Speaker",
                "value": f"{current}" + (f" (`{remaining}s` left)" if remaining is not None else ""),
                "inline": False,
            },
            {
                "name": "Queue",
                "value": "\n".join(queue_lines) if queue_lines else "Waiting for hands...",
                "inline": False,
            },
        ]
        description = (
            f"React with {session.emoji} to join. Remove the reaction to leave.\n"
            f"Turn duration: `{session.turn_seconds}s`"
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

    async def _advance(self, session: RaiseHandSession, reason: str) -> None:
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return

        async with session.lock:
            if not session.running:
                return

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

        self._schedule_panel_update(session)
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
    @app_commands.describe(turn_seconds="Seconds per speaker turn")
    async def raisehand_start(self, interaction: discord.Interaction, turn_seconds: Optional[int] = None) -> None:
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

        turn_seconds = turn_seconds or self._config_int("default_turn_seconds", DEFAULT_TURN_SECONDS)
        if turn_seconds < 1:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Duration", "Turn seconds must be at least 1."),
                ephemeral=True,
            )
            return

        emoji = self._config_str("emoji", DEFAULT_EMOJI)
        max_queue_display = self._config_int("max_queue_display", DEFAULT_MAX_QUEUE_DISPLAY)
        debounce_ms = self._config_int("panel_debounce_ms", DEFAULT_DEBOUNCE_MS)

        panel_message = await interaction.channel.send(
            embed=EmbedFactory.create(
                title=f"{emoji} Speaking Queue",
                description=f"React with {emoji} to join.\nTurn duration: `{turn_seconds}s`",
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

        embed = EmbedFactory.success(
            "Raisehand Started",
            f"Session started in {vc.mention}.\nTurn duration: `{turn_seconds}s`",
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
    @app_commands.describe(extra_seconds="Extra seconds to add to the current turn")
    async def raisehand_extend(self, interaction: discord.Interaction, extra_seconds: int) -> None:
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

        if extra_seconds < 1:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Duration", "Extra seconds must be at least 1."),
                ephemeral=True,
            )
            return

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

        await interaction.followup.send(
            embed=EmbedFactory.success("Speaker Extended", f"Added {extra_seconds}s to the current turn."),
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
            else:
                session.current_speaker_id = user.id
                session.current_ends_at = self._now() + timedelta(seconds=session.turn_seconds)
                self._start_timer(session)

        if should_advance:
            await self._advance(session, reason="swap")
        else:
            self._schedule_panel_update(session)

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
        if emoji not in {session.emoji, DEFAULT_EMOJI, DEFAULT_ALT_EMOJI}:
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

        async with session.lock:
            if payload.user_id == session.current_speaker_id:
                return
            if payload.user_id in session.queue:
                return
            session.queue.append(payload.user_id)
        self._schedule_panel_update(session)

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
        if emoji not in {session.emoji, DEFAULT_EMOJI, DEFAULT_ALT_EMOJI}:
            return
        if payload.user_id == self.bot.user.id:
            return

        async with session.lock:
            if payload.user_id in session.queue:
                session.queue.remove(payload.user_id)
            else:
                return
        self._schedule_panel_update(session)

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
                async with session.lock:
                    if member.id in session.queue:
                        session.queue.remove(member.id)
                    if member.id == session.current_speaker_id:
                        session.current_speaker_id = None
                        session.current_ends_at = None
                        should_advance = True
                    if member.id == session.moderator_id:
                        should_advance = False
                if member.id == session.moderator_id:
                    await self._stop_session(session, reason="moderator_left", note="Session ended (moderator left).")
                    return
                if should_advance:
                    await self._advance(session, reason="speaker_left")
                else:
                    self._schedule_panel_update(session)

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


async def setup(bot: commands.Bot) -> None:
    """Setup function for cog loading."""
    cog = RaiseHand(bot, bot.db)
    await bot.add_cog(cog)

    existing = bot.tree.get_command("raisehand")
    if existing:
        bot.tree.remove_command("raisehand", type=discord.AppCommandType.chat_input)
    bot.tree.add_command(cog.raisehand)

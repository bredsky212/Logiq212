"""
Voice chat moderation (suspension) commands
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from database.models import FeatureKey, Suspension
from utils.embeds import EmbedFactory, EmbedColor
from utils.feature_permissions import FeaturePermissionManager

logger = logging.getLogger(__name__)


class VCMod(commands.Cog):
    """VC moderation tools"""

    vcmod = app_commands.Group(
        name="vcmod",
        description="Voice chat moderation tools",
        guild_only=True
    )

    def __init__(self, bot: commands.Bot, db: DatabaseManager):
        self.bot = bot
        self.db = db
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)

    async def _log_to_mod(self, guild: discord.Guild, embed: discord.Embed):
        guild_config = await self.db.get_guild(guild.id)
        if not guild_config:
            return
        log_channel_id = guild_config.get("log_channel")
        if not log_channel_id:
            return
        channel = guild.get_channel(log_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(log_channel_id)
            except discord.HTTPException:
                return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning(f"Cannot send VC mod log to {channel} in {guild.id}")

    def _base_vcmod_check(self, member: discord.Member) -> bool:
        return member.guild_permissions.moderate_members

    async def _can_use(self, member: discord.Member, feature: FeatureKey) -> bool:
        return await self.perms.check(member, feature, self._base_vcmod_check)

    def _hierarchy_block(self, moderator: discord.Member, target: discord.Member) -> Optional[str]:
        if target == moderator.guild.owner:
            return "You cannot act on the server owner."
        if target.guild_permissions.administrator:
            return "You cannot act on an administrator."
        if target.top_role >= moderator.top_role:
            return "You cannot act on someone with an equal or higher role."
        return None

    def _duration_seconds(self, choice: str) -> int:
        mapping = {"2h": 7200, "4h": 14400, "12h": 43200}
        return mapping.get(choice, 7200)

    @vcmod.command(name="suspend", description="Temporarily suspend a user from voice & chat (timeout)")
    @app_commands.describe(
        user="User to suspend",
        duration="Duration (2h, 4h, 12h)",
        reason="Reason for suspension"
    )
    @app_commands.choices(
        duration=[
            app_commands.Choice(name="2h", value="2h"),
            app_commands.Choice(name="4h", value="4h"),
            app_commands.Choice(name="12h", value="12h"),
        ]
    )
    async def suspend(self, interaction: discord.Interaction, user: discord.Member, duration: app_commands.Choice[str], reason: str = "No reason provided"):
        logger.info(
            "vcmod suspend invoked by user=%s (%s) target=%s (%s) guild=%s duration=%s",
            interaction.user,
            interaction.user.id,
            user,
            user.id,
            interaction.guild_id,
            duration.value,
        )
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            logger.exception("Failed to defer vcmod suspend")
            return

        try:
            if not await self._can_use(interaction.user, FeatureKey.MOD_VC_SUSPEND):
                await interaction.followup.send(
                    embed=EmbedFactory.error("No Permission", "You do not have permission to use this command."),
                    ephemeral=True
                )
                return

            block = self._hierarchy_block(interaction.user, user)
            if block:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Cannot Suspend", block),
                    ephemeral=True
                )
                return

            seconds = self._duration_seconds(duration.value)
            ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)

            try:
                await user.edit(communication_disabled_until=ends_at, reason=reason)
            except discord.Forbidden:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Error", "I don't have permission to timeout that user."),
                    ephemeral=True
                )
                return
            except discord.HTTPException as e:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Error", f"Failed to apply timeout: {e}"),
                    ephemeral=True
                )
                return

            await self.db.close_active_suspensions(interaction.guild.id, user.id, interaction.user.id)

            suspension = Suspension(
                guild_id=interaction.guild.id,
                user_id=user.id,
                moderator_id=interaction.user.id,
                reason=reason,
                duration_seconds=seconds,
                started_at=datetime.now(timezone.utc),
                ends_at=ends_at
            )
            await self.db.create_suspension(suspension.to_dict())

            await interaction.followup.send(
                embed=EmbedFactory.success(
                    "Suspended",
                    f"{user.mention} has been suspended for {duration.value}."
                ),
                ephemeral=True
            )

            log_embed = EmbedFactory.create(
                title="VC Suspension",
                description=(
                    f"**Moderator:** {interaction.user.mention}\n"
                    f"**User:** {user.mention}\n"
                    f"**Duration:** {duration.value}\n"
                    f"**Reason:** {reason}\n"
                    f"**Ends at:** {ends_at.isoformat()}"
                ),
                color=EmbedColor.WARNING
            )
            await self._log_to_mod(interaction.guild, log_embed)

            try:
                await user.send(
                    f"You have been suspended from **{interaction.guild.name}** for {duration.value}.\nReason: {reason}"
                )
            except discord.Forbidden:
                logger.debug(f"Could not DM suspended user {user.id}")
        except Exception as e:
            logger.exception(f"Error in vcmod suspend: {e}")
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "Something went wrong applying the suspension."),
                ephemeral=True
            )

    @vcmod.command(name="unsuspend", description="Remove a suspension early")
    @app_commands.describe(
        user="User to unsuspend",
        reason="Reason for unsuspending"
    )
    async def unsuspend(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        logger.info(
            "vcmod unsuspend invoked by user=%s (%s) target=%s (%s) guild=%s",
            interaction.user,
            interaction.user.id,
            user,
            user.id,
            interaction.guild_id,
        )
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            logger.exception("Failed to defer vcmod unsuspend")
            return

        try:
            if not await self._can_use(interaction.user, FeatureKey.MOD_VC_UNSUSPEND):
                await interaction.followup.send(
                    embed=EmbedFactory.error("No Permission", "You do not have permission to use this command."),
                    ephemeral=True
                )
                return

            block = self._hierarchy_block(interaction.user, user)
            if block:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Cannot Unsuspend", block),
                    ephemeral=True
                )
                return

            try:
                await user.edit(communication_disabled_until=None, reason=reason)
            except discord.Forbidden:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Error", "I don't have permission to modify that user."),
                    ephemeral=True
                )
                return
            except discord.HTTPException as e:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Error", f"Failed to remove timeout: {e}"),
                    ephemeral=True
                )
                return

            await self.db.update_suspension(
                interaction.guild.id,
                user.id,
                {"active": False, "resolved_at": datetime.now(timezone.utc), "resolved_by": interaction.user.id, "reason": reason}
            )

            await interaction.followup.send(
                embed=EmbedFactory.success("Unsuspended", f"{user.mention} has been unsuspended."),
                ephemeral=True
            )

            log_embed = EmbedFactory.create(
                title="VC Unsuspension",
                description=(
                    f"**Moderator:** {interaction.user.mention}\n"
                    f"**User:** {user.mention}\n"
                    f"**Reason:** {reason}"
                ),
                color=EmbedColor.INFO
            )
            await self._log_to_mod(interaction.guild, log_embed)

            try:
                await user.send(
                    f"Your suspension in **{interaction.guild.name}** has been lifted. Reason: {reason}"
                )
            except discord.Forbidden:
                logger.debug(f"Could not DM unsuspended user {user.id}")
        except Exception as e:
            logger.exception(f"Error in vcmod unsuspend: {e}")
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "Something went wrong removing the suspension."),
                ephemeral=True
            )

    @vcmod.command(name="status", description="Check a user's suspension status")
    @app_commands.describe(user="User to check")
    async def status(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._can_use(interaction.user, FeatureKey.MOD_VC_SUSPEND):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You do not have permission to view suspension status."),
                ephemeral=True
            )
            return

        active_doc = await self.db.get_active_suspension(interaction.guild.id, user.id)
        history = await self.db.get_suspension_history(interaction.guild.id, user.id, limit=3)

        status_lines = []
        if active_doc:
            status_lines.append(
                f"Active: {active_doc.get('duration_seconds', 0)}s until {active_doc.get('ends_at')} (reason: {active_doc.get('reason')})"
            )
        else:
            status_lines.append("Active: None")

        for idx, entry in enumerate(history, 1):
            status_lines.append(
                f"{idx}. {entry.get('duration_seconds', 0)}s by <@{entry.get('moderator_id')}> on {entry.get('started_at')} (active={entry.get('active')})"
            )

        embed = EmbedFactory.create(
            title=f"Suspension Status - {user.display_name}",
            description="\n".join(status_lines),
            color=EmbedColor.INFO
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(VCMod(bot, bot.db))

    existing = bot.tree.get_command("vcmod")
    if existing:
        bot.tree.remove_command("vcmod", type=discord.AppCommandType.chat_input)
    bot.tree.add_command(VCMod.vcmod)

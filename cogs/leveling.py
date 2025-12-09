"""
Leveling Cog for Logiq
XP and leveling system with rank cards
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from typing import Optional
import logging
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

from utils.embeds import EmbedFactory, EmbedColor
from utils.constants import calculate_level_xp
from utils.feature_permissions import FeaturePermissionManager
from utils.denials import DenialLogger
from database.db_manager import DatabaseManager
from database.models import FeatureKey

logger = logging.getLogger(__name__)


class Leveling(commands.Cog):
    """Leveling system cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('leveling', {})
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()
        self.xp_cooldown = {}

    def _base_level_admin_check(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.manage_guild or perms.administrator or member == member.guild.owner

    async def _can_use(self, member: discord.Member, feature: FeatureKey) -> bool:
        return await self.perms.check(member, feature, self._base_level_admin_check)

    async def _log_denial(self, interaction: discord.Interaction, feature: FeatureKey, reason: str):
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, "leveling", feature.value):
            return
        embed = EmbedFactory.warning(
            "Permission Denied",
            f"{interaction.user.mention} denied `{feature.value}` in {interaction.guild.name}.\nReason: {reason}"
        )
        await self._log_to_mod(interaction.guild, embed)

    async def _log_to_mod(self, guild: discord.Guild, embed: discord.Embed):
        guild_config = await self.db.get_guild(guild.id)
        if not guild_config:
            return
        log_channel_id = guild_config.get("log_channel")
        if not log_channel_id:
            return
        channel = guild.get_channel(log_channel_id)
        if not channel:
            try:
                channel = await guild.fetch_channel(log_channel_id)
            except discord.HTTPException:
                return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning(f"Cannot send leveling log to channel {channel} in {guild}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Award XP for messages"""
        if not self.module_config.get('enabled', True):
            return

        if message.author.bot or not message.guild:
            return

        # Check cooldown
        user_key = f"{message.guild.id}_{message.author.id}"
        current_time = datetime.utcnow().timestamp()

        if user_key in self.xp_cooldown:
            if current_time - self.xp_cooldown[user_key] < self.module_config.get('xp_cooldown', 60):
                return

        self.xp_cooldown[user_key] = current_time

        # Get or create user
        user_data = await self.db.get_user(message.author.id, message.guild.id)
        if not user_data:
            user_data = await self.db.create_user(message.author.id, message.guild.id)

        # Calculate XP
        xp_gain = self.module_config.get('xp_per_message', 10)
        new_xp = user_data.get('xp', 0) + xp_gain
        current_level = user_data.get('level', 0)

        # Check for level up
        next_level_xp = calculate_level_xp(current_level + 1)

        if new_xp >= next_level_xp:
            new_level = current_level + 1
            await self.db.update_user(message.author.id, message.guild.id, {
                'xp': new_xp,
                'level': new_level
            })

            # Send level up message
            embed = EmbedFactory.level_up(message.author, new_level, new_xp)
            await message.channel.send(embed=embed)
            logger.info(f"{message.author} leveled up to {new_level} in {message.guild}")
        else:
            await self.db.update_user(message.author.id, message.guild.id, {'xp': new_xp})

    # NOTE: /rank and /leaderboard commands have been moved to games.py as PUBLIC commands

    @app_commands.command(name="setlevel", description="Set user's level (Admin)")
    @app_commands.describe(
        user="User to modify",
        level="New level"
    )
    async def set_level(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        level: int
    ):
        """Set user level"""
        if not await self._can_use(interaction.user, FeatureKey.LEVELING_ADMIN_SET):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to set levels."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.LEVELING_ADMIN_SET, "setlevel")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if level < 0:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Level", "Level must be 0 or greater"),
                ephemeral=True
            )
            return

        xp = sum(calculate_level_xp(i) for i in range(1, level + 1))

        await self.db.update_user(user.id, interaction.guild.id, {
            'level': level,
            'xp': xp
        })

        embed = EmbedFactory.success(
            "Level Set",
            f"Set {user.mention}'s level to **{level}**"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"{interaction.user} set {user}'s level to {level}")

    @app_commands.command(name="resetlevels", description="Reset all levels (Admin)")
    async def reset_levels(self, interaction: discord.Interaction):
        """Reset all levels in guild"""
        if not await self._can_use(interaction.user, FeatureKey.LEVELING_ADMIN_RESET):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to reset levels."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.LEVELING_ADMIN_RESET, "resetlevels")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # This would require a bulk update - implementing basic version
        warning_embed = EmbedFactory.warning(
            "Reset Levels",
            "This feature will reset all user levels. This is a destructive action.\n\n"
            "To implement: Use database bulk operations to reset all users in this guild."
        )
        await interaction.followup.send(embed=warning_embed, ephemeral=True)
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.info(
                "Level Reset Requested",
                f"{interaction.user.mention} invoked /resetlevels in {interaction.guild.name}."
            )
        )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Leveling(bot, bot.db, bot.config))

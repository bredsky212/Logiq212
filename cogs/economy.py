"""
Economy Cog for Logiq
Virtual currency system with shop and gambling
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
from typing import Optional
import logging
import random

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin
from utils.feature_permissions import FeaturePermissionManager
from utils.denials import DenialLogger
from database.db_manager import DatabaseManager
from database.models import FeatureKey

logger = logging.getLogger(__name__)


class Economy(commands.Cog):
    """Economy system cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('economy', {})
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()
        self.currency_symbol = self.module_config.get('currency_symbol', '💎')
        self.currency_name = self.module_config.get('currency_name', 'ProgrammiCoin')

    def _base_adjust_check(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.manage_guild or perms.administrator or member == member.guild.owner

    async def _can_adjust(self, member: discord.Member) -> bool:
        return await self.perms.check(member, FeatureKey.ECONOMY_ADMIN_ADJUST, self._base_adjust_check)

    async def _log_denial(self, interaction: discord.Interaction, reason: str):
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, "economy", FeatureKey.ECONOMY_ADMIN_ADJUST.value):
            return
        embed = EmbedFactory.warning(
            "Permission Denied",
            f"{interaction.user.mention} denied `{FeatureKey.ECONOMY_ADMIN_ADJUST.value}` in {interaction.guild.name}.\nReason: {reason}"
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
            logger.warning(f"Cannot send economy log to channel {channel} in {guild}")

    # NOTE: /balance command has been moved to games.py as PUBLIC command

    @app_commands.command(name="daily", description="Claim your daily reward (Admin)")
    @is_admin()
    async def daily(self, interaction: discord.Interaction):
        """Claim daily reward"""
        user_data = await self.db.get_user(interaction.user.id, interaction.guild.id)
        if not user_data:
            user_data = await self.db.create_user(interaction.user.id, interaction.guild.id)

        last_daily = user_data.get('last_daily', 0)
        current_time = datetime.utcnow().timestamp()
        cooldown = self.module_config.get('daily_cooldown', 86400)

        if current_time - last_daily < cooldown:
            time_left = cooldown - (current_time - last_daily)
            hours = int(time_left // 3600)
            minutes = int((time_left % 3600) // 60)

            await interaction.response.send_message(
                embed=EmbedFactory.warning(
                    "Cooldown Active",
                    f"You can claim your daily reward in **{hours}h {minutes}m**"
                ),
                ephemeral=True
            )
            return

        daily_amount = self.module_config.get('daily_reward', 100)
        await self.db.add_balance(interaction.user.id, interaction.guild.id, daily_amount)
        await self.db.update_user(interaction.user.id, interaction.guild.id, {'last_daily': current_time})

        new_balance = user_data.get('balance', 0) + daily_amount

        embed = EmbedFactory.success(
            "Daily Reward Claimed!",
            f"You received **{self.currency_symbol} {daily_amount:,}**!\n\n"
            f"New balance: **{self.currency_symbol} {new_balance:,}**"
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"{interaction.user} claimed daily reward in {interaction.guild}")

    @app_commands.command(name="give", description="Give currency to another user (Admin)")
    @app_commands.describe(
        user="User to give to",
        amount="Amount to give"
    )
    @is_admin()
    async def give(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        """Give currency to user"""
        if amount <= 0:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Amount", "Amount must be positive"),
                ephemeral=True
            )
            return

        if user.id == interaction.user.id:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Target", "You can't give currency to yourself"),
                ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Target", "You can't give currency to bots"),
                ephemeral=True
            )
            return

        # Check if sender has enough balance
        sender_data = await self.db.get_user(interaction.user.id, interaction.guild.id)
        if not sender_data:
            sender_data = await self.db.create_user(interaction.user.id, interaction.guild.id)

        if sender_data.get('balance', 0) < amount:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Insufficient Funds", "You don't have enough currency"),
                ephemeral=True
            )
            return

        # Transfer currency
        await self.db.remove_balance(interaction.user.id, interaction.guild.id, amount)
        await self.db.add_balance(user.id, interaction.guild.id, amount)

        embed = EmbedFactory.success(
            "Transfer Complete",
            f"{interaction.user.mention} gave **{self.currency_symbol} {amount:,}** to {user.mention}"
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"{interaction.user} gave {amount} to {user}")

    @app_commands.command(name="coinflip-bet", description="Flip a coin and bet currency (Admin)")
    @app_commands.describe(
        amount="Amount to bet",
        choice="Heads or Tails"
    )
    @is_admin()
    async def coinflip(self, interaction: discord.Interaction, amount: int, choice: str):
        """Coinflip gambling"""
        if amount <= 0:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Amount", "Amount must be positive"),
                ephemeral=True
            )
            return

        choice = choice.lower()
        if choice not in ['heads', 'tails', 'h', 't']:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Choice", "Choose 'heads' or 'tails'"),
                ephemeral=True
            )
            return

        # Normalize choice
        choice = 'heads' if choice in ['heads', 'h'] else 'tails'

        # Check balance
        user_data = await self.db.get_user(interaction.user.id, interaction.guild.id)
        if not user_data:
            user_data = await self.db.create_user(interaction.user.id, interaction.guild.id)

        if user_data.get('balance', 0) < amount:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Insufficient Funds", "You don't have enough currency"),
                ephemeral=True
            )
            return

        # Flip coin
        result = random.choice(['heads', 'tails'])
        won = result == choice

        if won:
            await self.db.add_balance(interaction.user.id, interaction.guild.id, amount)
            new_balance = user_data.get('balance', 0) + amount
            embed = EmbedFactory.success(
                "🎉 You Won!",
                f"The coin landed on **{result}**!\n\n"
                f"You won **{self.currency_symbol} {amount:,}**!\n"
                f"New balance: **{self.currency_symbol} {new_balance:,}**"
            )
        else:
            await self.db.remove_balance(interaction.user.id, interaction.guild.id, amount)
            new_balance = user_data.get('balance', 0) - amount
            embed = EmbedFactory.error(
                "You Lost!",
                f"The coin landed on **{result}**!\n\n"
                f"You lost **{self.currency_symbol} {amount:,}**!\n"
                f"New balance: **{self.currency_symbol} {new_balance:,}**"
            )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="shop", description="View the server shop (Admin)")
    @is_admin()
    async def shop(self, interaction: discord.Interaction):
        """View shop"""
        items = await self.db.get_shop_items(interaction.guild.id)

        if not items:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Empty Shop", "The shop is currently empty"),
                ephemeral=True
            )
            return

        description = ""
        for item in items[:25]:
            description += f"**{item['name']}** - {self.currency_symbol} {item['price']:,}\n"
            description += f"  *{item['description']}*\n\n"

        embed = EmbedFactory.create(
            title="🏪 Server Shop",
            description=description,
            color=EmbedColor.ECONOMY
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="addbalance", description="Add balance to user (Admin)")
    @app_commands.describe(
        user="User to add balance to",
        amount="Amount to add"
    )
    async def add_balance_admin(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int
    ):
        """Add balance to user (ADMIN ONLY)"""
        if not await self._can_adjust(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to adjust balances."),
                ephemeral=True
            )
            await self._log_denial(interaction, "addbalance")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if amount == 0:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Amount", "Amount must be non-zero."),
                ephemeral=True
            )
            return

        await self.db.add_balance(user.id, interaction.guild.id, amount)

        embed = EmbedFactory.success(
            "Balance Added",
            f"Added **{self.currency_symbol} {amount:,}** to {user.mention}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"{interaction.user} added {amount} to {user}")


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Economy(bot, bot.db, bot.config))

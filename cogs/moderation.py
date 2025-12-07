"""
Moderation Cog for Logiq
Comprehensive moderation tools with AI-powered auto-moderation
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
from typing import Optional
import re
import logging

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_moderator, PermissionChecker
from utils.converters import TimeConverter
from database.db_manager import DatabaseManager
from database.models import Warning, Report, FeatureKey
from utils.feature_permissions import FeaturePermissionManager
from utils.denials import DenialLogger

logger = logging.getLogger(__name__)

MESSAGE_LINK_REGEX = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)"
)


class Moderation(commands.Cog):
    """Moderation system cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('moderation', {})
        self.spam_tracker = {}  # Track spam
        self.toxicity_filter_enabled = self.module_config.get('auto_mod', {}).get('toxicity_filter', True)
        self.report_cooldowns = {}
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Auto-moderation on messages"""
        if not self.module_config.get('enabled', True):
            return

        if message.author.bot or not message.guild:
            return

        # Check spam
        if self.module_config.get('auto_mod', {}).get('spam_detection', True):
            await self._check_spam(message)

        # Check excessive mentions
        max_mentions = self.module_config.get('auto_mod', {}).get('max_mentions', 5)
        if len(message.mentions) > max_mentions:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention} Please don't spam mentions!",
                delete_after=5
            )
            return

    async def _check_spam(self, message: discord.Message):
        """Check for spam messages"""
        user_id = message.author.id
        current_time = datetime.utcnow().timestamp()

        if user_id not in self.spam_tracker:
            self.spam_tracker[user_id] = []

        # Add message timestamp
        self.spam_tracker[user_id].append(current_time)

        # Remove old timestamps (older than 5 seconds)
        self.spam_tracker[user_id] = [
            ts for ts in self.spam_tracker[user_id]
            if current_time - ts < 5
        ]

        # Check if spam threshold exceeded
        if len(self.spam_tracker[user_id]) > 5:
            try:
                await message.author.timeout(timedelta(minutes=5), reason="Spam detected")
                await message.channel.send(
                    f"{message.author.mention} has been timed out for 5 minutes due to spam.",
                    delete_after=10
                )
                self.spam_tracker[user_id] = []
                logger.info(f"Auto-muted {message.author} for spam")
            except discord.Forbidden:
                pass

    def _base_mod_check(
        self,
        moderator: discord.Member,
        target: Optional[discord.Member] = None,
        required_permissions: Optional[list[str]] = None,
    ) -> bool:
        """Base moderation check with admin/owner bypass, permissions, and hierarchy."""
        if moderator.guild_permissions.administrator or moderator == moderator.guild.owner:
            return True

        if required_permissions:
            missing = PermissionChecker.get_missing_permissions(moderator, required_permissions)
            if missing:
                return False

        if target is not None:
            can_moderate, _error = PermissionChecker.can_moderate(moderator, target)
            if not can_moderate:
                return False

        return True

    async def _maybe_log_denial(self, interaction: discord.Interaction, feature: FeatureKey, reason: str):
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, interaction.command.qualified_name if interaction.command else "moderation", feature.value):
            return
        embed = EmbedFactory.warning(
            "Permission Denied",
            f"{interaction.user.mention} denied `{feature.value}` in {interaction.guild.name}.\nReason: {reason}"
        )
        await self._log_action(interaction.guild, embed)

    @app_commands.command(name="report", description="Report a user to the moderation team")
    @app_commands.describe(
        user="User you want to report",
        category="Type of issue (spam, harassment, etc.)",
        reason="Explain what happened (10-512 characters)",
        message_link="Optional link to the offending message"
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Spam", value="spam"),
            app_commands.Choice(name="Harassment", value="harassment"),
            app_commands.Choice(name="Hate", value="hate"),
            app_commands.Choice(name="NSFW", value="nsfw"),
            app_commands.Choice(name="Scam", value="scam"),
            app_commands.Choice(name="Other", value="other"),
        ]
    )
    async def report(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        category: app_commands.Choice[str],
        reason: str,
        message_link: Optional[str] = None
    ):
        """Allow members to submit a structured report"""
        if not interaction.guild:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Guild Only", "This command can only be used inside a server."),
                ephemeral=True
            )
            return

        reason_text = reason.strip()
        if len(reason_text) < 10 or len(reason_text) > 512:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Reason", "Reason must be between 10 and 512 characters."),
                ephemeral=True
            )
            return

        now = datetime.utcnow().timestamp()
        last_used = self.report_cooldowns.get(interaction.user.id)
        if last_used and now - last_used < 60:
            retry_after = int(60 - (now - last_used))
            await interaction.response.send_message(
                embed=EmbedFactory.warning(
                    "Slow Down",
                    f"Please wait {retry_after} seconds before sending another report."
                ),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        message_link_value = message_link.strip() if message_link else None
        parsed_link = self._parse_message_link(message_link_value) if message_link_value else None
        message_id = None
        channel_id = interaction.channel_id
        fetched_message: Optional[discord.Message] = None
        message_fetch_error: Optional[str] = None

        if parsed_link:
            link_guild_id, link_channel_id, link_message_id = parsed_link
            if link_guild_id != interaction.guild.id:
                message_fetch_error = "Message link is from a different server."
            else:
                channel_id = link_channel_id
                message_id = link_message_id
                channel = interaction.guild.get_channel(link_channel_id)

                if not channel:
                    try:
                        channel = await interaction.guild.fetch_channel(link_channel_id)
                    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                        channel = None

                if isinstance(channel, (discord.TextChannel, discord.Thread)):
                    try:
                        fetched_message = await channel.fetch_message(link_message_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        message_fetch_error = "Message could not be fetched; it may be invalid or permission-restricted."
                else:
                    message_fetch_error = "Message could not be fetched; it may be invalid or permission-restricted."
        elif message_link_value:
            message_fetch_error = "Message link could not be parsed. It will be stored as provided."

        report = Report(
            guild_id=interaction.guild.id,
            reporter_id=interaction.user.id,
            reported_user_id=user.id,
            category=category.value,
            reason=reason_text,
            message_link=message_link_value,
            message_id=message_id,
            channel_id=channel_id,
        )

        try:
            report_id = await self.db.create_report(report.to_dict())
        except Exception as e:
            logger.error(f"Failed to create report for guild {interaction.guild.id}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Report Failed",
                    "Something went wrong while recording your report. Please try again later or contact a moderator directly."
                ),
                ephemeral=True
            )
            return

        self.report_cooldowns[interaction.user.id] = now

        embed = self._build_report_embed(
            interaction,
            user,
            report,
            report_id,
            fetched_message,
            message_fetch_error
        )
        log_sent = await self._send_report_log(interaction.guild, embed)

        if log_sent:
            confirmation_embed = EmbedFactory.success(
                "Report Received",
                "Your report has been sent to the moderation team. Thank you for helping keep the server safe."
            )
        else:
            confirmation_embed = EmbedFactory.warning(
                "Report Recorded",
                "Your report has been recorded, but no moderation log channel is configured. Please ask an admin to run /setlogchannel."
            )

        await interaction.followup.send(embed=confirmation_embed, ephemeral=True)

    @app_commands.command(name="warn", description="Warn a user")
    @app_commands.describe(
        user="User to warn",
        reason="Reason for warning"
    )
    @is_moderator()
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str
    ):
        """Warn a user"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_WARN,
            base_check=lambda m: self._base_mod_check(m, user, [])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot warn this user."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_WARN, "warn")
            return

        # Create warning
        warning = Warning(
            moderator_id=interaction.user.id,
            reason=reason
        )

        # Get or create user
        user_data = await self.db.get_user(user.id, interaction.guild.id)
        if not user_data:
            user_data = await self.db.create_user(user.id, interaction.guild.id)

        # Add warning
        await self.db.add_warning(user.id, interaction.guild.id, warning.to_dict())

        # Get total warnings
        warnings = await self.db.get_warnings(user.id, interaction.guild.id)

        embed = EmbedFactory.moderation_action("Warning", user, interaction.user, reason)
        embed.add_field(name="Total Warnings", value=str(len(warnings)), inline=False)

        await interaction.response.send_message(embed=embed)

        # DM user
        try:
            dm_embed = EmbedFactory.warning(
                "You have been warned",
                f"**Server:** {interaction.guild.name}\n**Reason:** {reason}\n**Total Warnings:** {len(warnings)}"
            )
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            pass

        # Log
        await self._log_action(interaction.guild, embed)
        logger.info(f"{interaction.user} warned {user} in {interaction.guild}")

    @app_commands.command(name="warnings", description="View user warnings")
    @app_commands.describe(user="User to check")
    @is_moderator()
    async def warnings(self, interaction: discord.Interaction, user: discord.Member):
        """View user warnings"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_WARNINGS,
            base_check=lambda m: self._base_mod_check(m, user, [])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot view warnings for this user."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_WARNINGS, "warnings")
            return
        warnings = await self.db.get_warnings(user.id, interaction.guild.id)

        if not warnings:
            await interaction.response.send_message(
                embed=EmbedFactory.info("No Warnings", f"{user.mention} has no warnings."),
                ephemeral=True
            )
            return

        description = ""
        for i, warning in enumerate(warnings, 1):
            moderator = interaction.guild.get_member(warning['moderator_id'])
            mod_name = moderator.mention if moderator else f"<@{warning['moderator_id']}>"
            timestamp = datetime.fromtimestamp(warning['timestamp']).strftime("%Y-%m-%d %H:%M")
            description += f"**{i}.** {warning['reason']}\n   *By {mod_name} on {timestamp}*\n\n"

        embed = EmbedFactory.create(
            title=f"⚠️ Warnings for {user.display_name}",
            description=description,
            color=EmbedColor.WARNING,
            thumbnail=user.display_avatar.url
        )
        embed.set_footer(text=f"Total warnings: {len(warnings)}")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="timeout", description="Timeout a user")
    @app_commands.describe(
        user="User to timeout",
        duration="Duration (e.g., 1h, 30m, 1d)",
        reason="Reason for timeout"
    )
    @is_moderator()
    async def timeout(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str = "No reason provided"
    ):
        """Timeout a user"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_TIMEOUT,
            base_check=lambda m: self._base_mod_check(m, user, ["moderate_members"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot timeout this user."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_TIMEOUT, "timeout")
            return

        seconds = TimeConverter.parse(duration)
        if not seconds or seconds > 2419200:  # Max 28 days
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Duration", "Duration must be valid and less than 28 days"),
                ephemeral=True
            )
            return

        try:
            await user.timeout(timedelta(seconds=seconds), reason=reason)
            embed = EmbedFactory.moderation_action("Timeout", user, interaction.user, reason)
            embed.add_field(name="Duration", value=TimeConverter.format_seconds(seconds), inline=False)
            await interaction.response.send_message(embed=embed)

            # DM user
            try:
                dm_embed = EmbedFactory.warning(
                    "You have been timed out",
                    f"**Server:** {interaction.guild.name}\n**Duration:** {TimeConverter.format_seconds(seconds)}\n**Reason:** {reason}"
                )
                await user.send(embed=dm_embed)
            except discord.Forbidden:
                pass

            # Log
            await self._log_action(interaction.guild, embed)
            logger.info(f"{interaction.user} timed out {user} for {duration} in {interaction.guild}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to timeout this user"),
                ephemeral=True
            )

    @app_commands.command(name="kick", description="Kick a user")
    @app_commands.describe(
        user="User to kick",
        reason="Reason for kick"
    )
    @is_moderator()
    async def kick(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str = "No reason provided"
    ):
        """Kick a user"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_KICK,
            base_check=lambda m: self._base_mod_check(m, user, ["kick_members"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot kick this user."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_KICK, "kick")
            return

        try:
            # DM user before kicking
            try:
                dm_embed = EmbedFactory.warning(
                    "You have been kicked",
                    f"**Server:** {interaction.guild.name}\n**Reason:** {reason}"
                )
                await user.send(embed=dm_embed)
            except discord.Forbidden:
                pass

            await user.kick(reason=reason)
            embed = EmbedFactory.moderation_action("Kick", user, interaction.user, reason)
            await interaction.response.send_message(embed=embed)

            # Log
            await self._log_action(interaction.guild, embed)
            logger.info(f"{interaction.user} kicked {user} from {interaction.guild}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to kick this user"),
                ephemeral=True
            )

    @app_commands.command(name="ban", description="Ban a user")
    @app_commands.describe(
        user="User to ban",
        reason="Reason for ban",
        delete_messages="Delete messages from last N days (0-7)"
    )
    @is_moderator()
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str = "No reason provided",
        delete_messages: int = 0
    ):
        """Ban a user"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_BAN,
            base_check=lambda m: self._base_mod_check(m, user, ["ban_members"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot ban this user."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_BAN, "ban")
            return

        if delete_messages < 0 or delete_messages > 7:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Parameter", "delete_messages must be between 0-7"),
                ephemeral=True
            )
            return

        try:
            # DM user before banning
            try:
                dm_embed = EmbedFactory.error(
                    "You have been banned",
                    f"**Server:** {interaction.guild.name}\n**Reason:** {reason}"
                )
                await user.send(embed=dm_embed)
            except discord.Forbidden:
                pass

            await user.ban(reason=reason, delete_message_days=delete_messages)
            embed = EmbedFactory.moderation_action("Ban", user, interaction.user, reason)
            await interaction.response.send_message(embed=embed)

            # Log
            await self._log_action(interaction.guild, embed)
            logger.info(f"{interaction.user} banned {user} from {interaction.guild}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to ban this user"),
                ephemeral=True
            )

    @app_commands.command(name="unban", description="Unban a user")
    @app_commands.describe(user_id="ID of user to unban")
    @is_moderator()
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str
    ):
        """Unban a user"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_BAN,
            base_check=lambda m: self._base_mod_check(m, None, ["ban_members"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot unban users."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_BAN, "unban")
            return
        try:
            user_id_int = int(user_id)
            user = await self.bot.fetch_user(user_id_int)
            await interaction.guild.unban(user)

            embed = EmbedFactory.success(
                "User Unbanned",
                f"{user.mention} ({user.id}) has been unbanned by {interaction.user.mention}"
            )
            await interaction.response.send_message(embed=embed)

            # Log
            await self._log_action(interaction.guild, embed)
            logger.info(f"{interaction.user} unbanned {user} in {interaction.guild}")

        except ValueError:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid ID", "Please provide a valid user ID"),
                ephemeral=True
            )
        except discord.NotFound:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", "This user is not banned"),
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to unban users"),
                ephemeral=True
            )

    @app_commands.command(name="clear", description="Clear messages in channel")
    @app_commands.describe(
        amount="Number of messages to delete (1-100)",
        user="Only delete messages from this user (optional)"
    )
    @is_moderator()
    async def clear(
        self,
        interaction: discord.Interaction,
        amount: int,
        user: Optional[discord.Member] = None
    ):
        """Clear messages from channel"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_CLEAR,
            base_check=lambda m: self._base_mod_check(m, None, ["manage_messages"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot clear messages here."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_CLEAR, "clear")
            return
        if amount < 1 or amount > 100:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Amount", "Amount must be between 1 and 100"),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            def check(m):
                if user:
                    return m.author.id == user.id
                return True

            deleted = await interaction.channel.purge(limit=amount, check=check)
            
            target_text = f" from {user.mention}" if user else ""
            embed = EmbedFactory.success(
                "Messages Cleared",
                f"Deleted **{len(deleted)}** messages{target_text}"
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

            # Log action
            log_embed = EmbedFactory.create(
                title="🗑️ Messages Cleared",
                description=f"**Channel:** {interaction.channel.mention}\n"
                           f"**Moderator:** {interaction.user.mention}\n"
                           f"**Amount:** {len(deleted)} messages{target_text}",
                color=EmbedColor.WARNING
            )
            await self._log_action(interaction.guild, log_embed)
            logger.info(f"{interaction.user} cleared {len(deleted)} messages in {interaction.channel}")

        except discord.Forbidden:
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "I don't have permission to delete messages"),
                ephemeral=True
            )

    @app_commands.command(name="slowmode", description="Set slowmode for channel")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)")
    @is_moderator()
    async def slowmode(self, interaction: discord.Interaction, seconds: int):
        """Set slowmode for channel"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_SLOWMODE,
            base_check=lambda m: self._base_mod_check(m, None, ["manage_channels"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot set slowmode here."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_SLOWMODE, "slowmode")
            return
        if seconds < 0 or seconds > 21600:  # Max 6 hours
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Duration", "Slowmode must be between 0 and 21600 seconds (6 hours)"),
                ephemeral=True
            )
            return

        try:
            await interaction.channel.edit(slowmode_delay=seconds)
            
            if seconds == 0:
                embed = EmbedFactory.success("Slowmode Disabled", "Slowmode has been disabled")
            else:
                embed = EmbedFactory.success(
                    "Slowmode Enabled",
                    f"Slowmode set to **{seconds}** seconds"
                )
            
            await interaction.response.send_message(embed=embed)

            # Log action
            log_embed = EmbedFactory.create(
                title="⏱️ Slowmode Updated",
                description=f"**Channel:** {interaction.channel.mention}\n"
                           f"**Moderator:** {interaction.user.mention}\n"
                           f"**Delay:** {seconds} seconds",
                color=EmbedColor.INFO
            )
            await self._log_action(interaction.guild, log_embed)
            logger.info(f"{interaction.user} set slowmode to {seconds}s in {interaction.channel}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to edit this channel"),
                ephemeral=True
            )

    @app_commands.command(name="lock", description="Lock a channel")
    @app_commands.describe(channel="Channel to lock (optional, defaults to current)")
    @is_moderator()
    async def lock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """Lock a channel"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_LOCK,
            base_check=lambda m: self._base_mod_check(m, None, ["manage_channels"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot lock this channel."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_LOCK, "lock")
            return
        target_channel = channel or interaction.channel

        try:
            await target_channel.set_permissions(
                interaction.guild.default_role,
                send_messages=False
            )
            
            embed = EmbedFactory.success("🔒 Channel Locked", f"{target_channel.mention} has been locked")
            await interaction.response.send_message(embed=embed)

            # Log action
            log_embed = EmbedFactory.create(
                title="🔒 Channel Locked",
                description=f"**Channel:** {target_channel.mention}\n"
                           f"**Moderator:** {interaction.user.mention}",
                color=EmbedColor.WARNING
            )
            await self._log_action(interaction.guild, log_embed)
            logger.info(f"{interaction.user} locked {target_channel}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to edit this channel"),
                ephemeral=True
            )

    @app_commands.command(name="unlock", description="Unlock a channel")
    @app_commands.describe(channel="Channel to unlock (optional, defaults to current)")
    @is_moderator()
    async def unlock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """Unlock a channel"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_LOCK,
            base_check=lambda m: self._base_mod_check(m, None, ["manage_channels"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot unlock this channel."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_LOCK, "unlock")
            return
        target_channel = channel or interaction.channel

        try:
            await target_channel.set_permissions(
                interaction.guild.default_role,
                send_messages=None
            )
            
            embed = EmbedFactory.success("🔓 Channel Unlocked", f"{target_channel.mention} has been unlocked")
            await interaction.response.send_message(embed=embed)

            # Log action
            log_embed = EmbedFactory.create(
                title="🔓 Channel Unlocked",
                description=f"**Channel:** {target_channel.mention}\n"
                           f"**Moderator:** {interaction.user.mention}",
                color=EmbedColor.SUCCESS
            )
            await self._log_action(interaction.guild, log_embed)
            logger.info(f"{interaction.user} unlocked {target_channel}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to edit this channel"),
                ephemeral=True
            )

    @app_commands.command(name="nickname", description="Change a user's nickname")
    @app_commands.describe(
        user="User to change nickname",
        nickname="New nickname (leave empty to reset)"
    )
    @is_moderator()
    async def nickname(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        nickname: Optional[str] = None
    ):
        """Change user nickname"""
        allowed = await self.perms.check(
            interaction.user,
            FeatureKey.MOD_NICKNAME,
            base_check=lambda m: self._base_mod_check(m, user, ["manage_nicknames"])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot change that nickname."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.MOD_NICKNAME, "nickname")
            return

        try:
            old_nick = user.display_name
            await user.edit(nick=nickname)
            
            if nickname:
                embed = EmbedFactory.success(
                    "Nickname Changed",
                    f"Changed {user.mention}'s nickname from **{old_nick}** to **{nickname}**"
                )
            else:
                embed = EmbedFactory.success(
                    "Nickname Reset",
                    f"Reset {user.mention}'s nickname"
                )
            
            await interaction.response.send_message(embed=embed)
            logger.info(f"{interaction.user} changed {user}'s nickname to {nickname}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to change this user's nickname"),
                ephemeral=True
            )

    def _parse_message_link(self, link: str) -> Optional[tuple[int, int, int]]:
        """Extract IDs from a Discord message link"""
        match = MESSAGE_LINK_REGEX.match(link)
        if not match:
            return None

        try:
            guild_id = int(match.group("guild_id"))
            channel_id = int(match.group("channel_id"))
            message_id = int(match.group("message_id"))
            return guild_id, channel_id, message_id
        except (TypeError, ValueError):
            return None

    async def _get_log_channel(self, guild: discord.Guild) -> Optional[discord.abc.Messageable]:
        """Fetch the configured log channel if available"""
        guild_config = await self.db.get_guild(guild.id)
        if not guild_config:
            return None

        log_channel_id = guild_config.get('log_channel')
        if not log_channel_id:
            return None

        log_channel = guild.get_channel(log_channel_id)
        if not log_channel:
            try:
                log_channel = await guild.fetch_channel(log_channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None

        return log_channel

    async def _send_report_log(self, guild: discord.Guild, embed: discord.Embed) -> bool:
        """Send report embed to the configured log channel"""
        log_channel = await self._get_log_channel(guild)
        if not log_channel:
            return False

        try:
            await log_channel.send(embed=embed)
            return True
        except discord.Forbidden:
            logger.warning(f"Cannot send report log to channel {log_channel} in {guild}")
        except discord.HTTPException as e:
            logger.warning(f"Failed to send report log in {guild}: {e}")

        return False

    def _build_report_embed(
        self,
        interaction: discord.Interaction,
        reported_user: discord.Member,
        report: Report,
        report_id: str,
        fetched_message: Optional[discord.Message],
        message_fetch_error: Optional[str]
    ) -> discord.Embed:
        """Construct embed for moderator log"""
        reporter_label = f"{interaction.user.mention} ({interaction.user.id})"
        if interaction.user.id == reported_user.id:
            reporter_label += " (self-report)"

        channel_value = f"<#{report.channel_id}>" if report.channel_id else "Unknown"

        message_value = "No message link provided."
        if fetched_message:
            preview = fetched_message.content or "[no text content]"
            if len(preview) > 500:
                preview = f"{preview[:497]}..."

            jump_link = report.message_link or fetched_message.jump_url
            message_value = f"{preview}\n[Jump to message]({jump_link})"
            if fetched_message.attachments:
                message_value += f"\nAttachments: {len(fetched_message.attachments)}"
        elif report.message_link:
            message_value = report.message_link
            if message_fetch_error:
                message_value += f"\n{message_fetch_error}"
        elif message_fetch_error:
            message_value = message_fetch_error

        embed = EmbedFactory.create(
            title="New User Report",
            color=EmbedColor.WARNING,
            fields=[
                {"name": "Reporter", "value": reporter_label, "inline": True},
                {"name": "Reported User", "value": f"{reported_user.mention} ({reported_user.id})", "inline": True},
                {"name": "Category", "value": report.category, "inline": True},
                {"name": "Reason", "value": report.reason, "inline": False},
                {"name": "Channel", "value": channel_value, "inline": True},
                {"name": "Guild", "value": f"{interaction.guild.name} ({interaction.guild.id})", "inline": True},
                {"name": "Message", "value": message_value, "inline": False},
                {"name": "Report ID", "value": report_id, "inline": True}
            ]
        )

        return embed

    async def _log_action(self, guild: discord.Guild, embed: discord.Embed):
        """Log moderation action to log channel"""
        guild_config = await self.db.get_guild(guild.id)
        if not guild_config:
            return

        log_channel_id = guild_config.get('log_channel')
        if not log_channel_id:
            return

        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            try:
                await log_channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"Cannot send to log channel in {guild}")


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Moderation(bot, bot.db, bot.config))

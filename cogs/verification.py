"""
Verification Cog for Logiq
Handles user verification with multiple methods
"""

import discord
from discord import app_commands
from discord.ext import commands
import random
import string
from typing import Optional
import logging

from utils.embeds import EmbedFactory, EmbedColor
from database.db_manager import DatabaseManager
from database.models import FeatureKey
from utils.feature_permissions import FeaturePermissionManager
from utils.security import filter_protected_roles
from utils.logs import resolve_log_channel
from utils.denials import DenialLogger

logger = logging.getLogger(__name__)


class VerificationSetupModal(discord.ui.Modal, title="Verification Setup"):
    """Modal for setting up verification with welcome message"""

    welcome_message = discord.ui.TextInput(
        label="Welcome Message",
        placeholder="Use {username} for name, {user} for @mention. Type channel names like: verify-channel",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )

    def __init__(self, cog, role, welcome_channel, method, verify_channel, verification_type):
        super().__init__()
        self.cog = cog
        self.role = role
        self.welcome_channel = welcome_channel
        self.method = method
        self.verify_channel = verify_channel
        self.verification_type = verification_type

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        guild_config = await self.cog.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.cog.db.create_guild(interaction.guild.id)

        update_data = {
            'verified_role': self.role.id,
            'welcome_channel': self.welcome_channel.id,
            'verification_type': self.verification_type,
            'verification_method': self.method,
            'welcome_message': self.welcome_message.value
        }
        
        if self.method == 'channel' and self.verify_channel:
            update_data['verify_channel'] = self.verify_channel.id

        await self.cog.db.update_guild(interaction.guild.id, update_data)

        if self.method == 'channel':
            method_text = f"**Verification Channel:** {self.verify_channel.mention}"
            location_text = f"in {self.verify_channel.mention}"
        else:
            method_text = "**Method:** DM (Private Messages)"
            location_text = "via DM"
        
        embed = EmbedFactory.success(
            "✅ Verification Setup Complete",
            f"**Verified Role:** {self.role.mention}\n"
            f"**Welcome Channel:** {self.welcome_channel.mention}\n"
            f"{method_text}\n"
            f"**Type:** {self.verification_type}\n"
            f"**Welcome Message:** {self.welcome_message.value[:100]}...\n\n"
            f"New members will receive a welcome message in {self.welcome_channel.mention} and verification will be sent {location_text}."
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"Verification setup completed in {interaction.guild} with method: {self.method}")

logger = logging.getLogger(__name__)


class VerificationButton(discord.ui.View):
    """Button-based verification view"""

    def __init__(self, cog: 'Verification'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green, custom_id="verify_button", emoji="✅")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle verification button click"""
        await self.cog.verify_user(interaction)


class CaptchaModal(discord.ui.Modal, title="Verification Captcha"):
    """Captcha verification modal"""

    def __init__(self, correct_code: str, cog: 'Verification'):
        super().__init__()
        self.correct_code = correct_code
        self.cog = cog

    captcha_code = discord.ui.TextInput(
        label="Enter the code shown",
        placeholder="Enter captcha code",
        required=True,
        max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        """Handle captcha submission"""
        if self.captcha_code.value.upper() == self.correct_code:
            await self.cog.verify_user(interaction)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Verification Failed", "Incorrect captcha code. Please try again."),
                ephemeral=True
            )


class Verification(commands.Cog):
    """Verification system cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('verification', {})
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle new member join - Send welcome message and verification"""
        if not self.module_config.get('enabled', True):
            return

        guild_config = await self.db.get_guild(member.guild.id)
        if not guild_config:
            return

        verified_role_id = guild_config.get('verified_role')
        verification_type = guild_config.get('verification_type', 'button')
        verification_method = guild_config.get('verification_method', 'dm')
        welcome_message_template = guild_config.get('welcome_message',
            f"Welcome to **{member.guild.name}**! 👋\n\n"
            "Please verify yourself to gain access to the server."
        )
        
        # Replace placeholders with actual values
        welcome_message = welcome_message_template.replace('{user}', member.mention)
        welcome_message = welcome_message.replace('{username}', member.display_name)
        welcome_message = welcome_message.replace('{server}', member.guild.name)
        # Replace channel names with mentions (e.g., "verify-channel" -> #verify-channel)
        import re
        for channel in member.guild.text_channels:
            # Replace channel name patterns with actual mentions
            welcome_message = welcome_message.replace(channel.name, channel.mention)
            welcome_message = welcome_message.replace(f"#{channel.name}", channel.mention)
        
        # Send welcome message in welcome channel (PUBLIC - everyone can see)
        welcome_channel_id = guild_config.get('welcome_channel')
        if welcome_channel_id:
            welcome_channel = member.guild.get_channel(welcome_channel_id)
            if welcome_channel:
                # Make sure everyone can see the welcome channel
                welcome_embed = EmbedFactory.create(
                    title=f"👋 Welcome to {member.guild.name}!",
                    description=f"{member.mention}\n\n{welcome_message}",
                    color=EmbedColor.SUCCESS
                )
                welcome_embed.set_thumbnail(url=member.display_avatar.url)
                await welcome_channel.send(embed=welcome_embed)
                logger.info(f"Sent welcome message for {member} in {welcome_channel}")

        # Send verification only if verified_role is configured
        if not verified_role_id:
            return

        # Send verification to verify channel (if configured) - ONLY VISIBLE TO USER
        verify_channel_id = guild_config.get('verify_channel')
        if verification_method == 'channel' and verify_channel_id:
            verify_channel = member.guild.get_channel(verify_channel_id)
            if verify_channel:
                try:
                    if verification_type == 'button':
                        embed = EmbedFactory.create(
                            title=f"🔐 Verification",
                            description=f"{member.mention}, click the button below to verify.",
                            color=EmbedColor.PRIMARY
                        )
                        view = VerificationButton(self)
                        msg = await verify_channel.send(embed=embed, view=view, delete_after=300)
                        logger.info(f"Sent verification to channel for {member}")
                    elif verification_type == 'captcha':
                        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                        
                        # Create a view with button that shows code only to the user
                        class CaptchaView(discord.ui.View):
                            def __init__(self, user_id, verification_code, cog):
                                super().__init__(timeout=None)
                                self.user_id = user_id
                                self.code = verification_code
                                self.cog = cog
                            
                            @discord.ui.button(label="Show My Code", style=discord.ButtonStyle.primary, emoji="🔐")
                            async def show_code(self, interaction: discord.Interaction, button: discord.ui.Button):
                                if interaction.user.id != self.user_id:
                                    await interaction.response.send_message("This is not for you!", ephemeral=True)
                                    return
                                await interaction.response.send_message(
                                    f"Your verification code: `{self.code}`\n\nClick the button below to enter it.",
                                    ephemeral=True,
                                    view=CaptchaEntryView(self.user_id, self.code, self.cog)
                                )
                        
                        class CaptchaEntryView(discord.ui.View):
                            def __init__(self, user_id, verification_code, cog):
                                super().__init__(timeout=None)
                                self.user_id = user_id
                                self.code = verification_code
                                self.cog = cog
                            
                            @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.success, emoji="✅")
                            async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button):
                                if interaction.user.id != self.user_id:
                                    await interaction.response.send_message("This is not for you!", ephemeral=True)
                                    return
                                modal = CaptchaModal(self.code, self.cog)
                                await interaction.response.send_modal(modal)
                        
                        embed = EmbedFactory.create(
                            title=f"🔐 Verification",
                            description=f"{member.mention}, click the button below to see your verification code (only you will see it).",
                            color=EmbedColor.PRIMARY
                        )
                        
                        view = CaptchaView(member.id, code, self)
                        await verify_channel.send(embed=embed, view=view, delete_after=300)
                        logger.info(f"Sent captcha verification to channel for {member}")
                except Exception as e:
                    logger.error(f"Error sending verification to channel: {e}", exc_info=True)
            return

        # Send DM verification (PRIVATE) - fallback or if method is 'dm'
        try:
            if verification_type == 'button':
                embed = EmbedFactory.create(
                    title=f"🔐 Welcome to {member.guild.name}",
                    description=welcome_message,
                    color=EmbedColor.PRIMARY
                )
                embed.set_thumbnail(url=member.guild.icon.url if member.guild.icon else None)
                view = VerificationButton(self)
                await member.send(embed=embed, view=view)

            elif verification_type == 'captcha':
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                embed = EmbedFactory.create(
                    title=f"🔐 Welcome to {member.guild.name}",
                    description=f"{welcome_message}\n\n**Your verification code:** `{code}`\n\nClick the button below and enter this code.",
                    color=EmbedColor.PRIMARY
                )
                embed.set_thumbnail(url=member.guild.icon.url if member.guild.icon else None)

                button = discord.ui.Button(label="Enter Code", style=discord.ButtonStyle.green, custom_id=f"captcha_{member.id}")

                async def captcha_callback(interaction: discord.Interaction):
                    if interaction.user.id != member.id:
                        await interaction.response.send_message("This verification is not for you!", ephemeral=True)
                        return
                    modal = CaptchaModal(code, self)
                    await interaction.response.send_modal(modal)

                button.callback = captcha_callback
                view = discord.ui.View(timeout=None)
                view.add_item(button)

                await member.send(embed=embed, view=view)

            logger.info(f"Sent DM verification to {member} in {member.guild}")

        except discord.Forbidden:
            logger.warning(f"Could not DM {member} in {member.guild} - DMs disabled")
            log_channel = await resolve_log_channel(self.db, member.guild, "moderation")
            if log_channel:
                await log_channel.send(
                    embed=EmbedFactory.error(
                        "Verification DM Failed",
                        f"Could not send verification DM to {member.mention} (DMs disabled)"
                    )
                )

    async def verify_user(self, interaction: discord.Interaction):
        """Verify a user and assign role (SILENT - no public announcements)"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Server not configured"),
                ephemeral=True
            )
            return

        verified_role_id = guild_config.get('verified_role')
        if not verified_role_id:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Verified role not configured"),
                ephemeral=True
            )
            return

        verified_role = interaction.guild.get_role(verified_role_id)
        if not verified_role:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Verified role not found"),
                ephemeral=True
            )
            return

        if verified_role in interaction.user.roles:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Already Verified", "You are already verified!"),
                ephemeral=True
            )
            return

        try:
            # Silently add verified role
            filtered = await filter_protected_roles(self.db, interaction.guild, [verified_role])
            if not filtered:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Protected Role", "The configured verified role is protected and cannot be assigned by the bot."),
                    ephemeral=True
                )
                return

            await interaction.user.add_roles(verified_role)

            # Send private success message
            await interaction.response.send_message(
                embed=EmbedFactory.success(
                    "✅ Verified Successfully!",
                    f"Welcome to **{interaction.guild.name}**!\n\nYou now have access to all channels."
                ),
                ephemeral=True
            )

            # Log silently (no public announcement)
            logger.info(f"Verified user {interaction.user} in {interaction.guild} (silent)")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to assign roles"),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error verifying user: {e}", exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "An error occurred during verification"),
                ephemeral=True
            )

    def _base_verify_config(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.manage_guild or perms.administrator or member == member.guild.owner

    async def _check_verify_config(self, member: discord.Member) -> bool:
        return await self.perms.check(member, FeatureKey.VERIFY_CONFIG, self._base_verify_config)

    async def _log_denial(self, interaction: discord.Interaction, feature: FeatureKey, reason: str):
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, "verification", feature.value):
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
            logger.warning(f"Cannot send verification log to channel {channel} in {guild}")

    @app_commands.command(name="setup-verification", description="Setup verification system (Admin)")
    @app_commands.describe(
        role="Role to assign upon verification",
        welcome_channel="Channel to send welcome messages",
        method="Verification method: 'dm' or 'channel'",
        verify_channel="Channel for verification (REQUIRED if method is 'channel')",
        verification_type="Type of verification (button/captcha)"
    )
    async def setup_verification(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        welcome_channel: discord.TextChannel,
        method: str,
        verify_channel: Optional[discord.TextChannel] = None,
        verification_type: str = "button"
    ):
        """Setup verification system (ADMIN ONLY)"""
        if not await self._check_verify_config(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You must be an administrator to use this command."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.VERIFY_CONFIG, "setup-verification")
            return
        method = method.lower()
        
        if method not in ['dm', 'channel']:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Method", "Method must be 'dm' or 'channel'"),
                ephemeral=True
            )
            return
        
        if method == 'channel' and not verify_channel:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Missing Channel", "You must specify a verify_channel when using 'channel' method"),
                ephemeral=True
            )
            return
        
        if verification_type not in ['button', 'captcha']:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Type", "Verification type must be 'button' or 'captcha'"),
                ephemeral=True
            )
            return

        # Show modal to get welcome message
        modal = VerificationSetupModal(self, role, welcome_channel, method, verify_channel, verification_type)
        await interaction.response.send_modal(modal)

    @app_commands.command(name="set-welcome-message", description="Set custom welcome DM message (Admin)")
    @app_commands.describe(message="Custom welcome message for new members")
    async def set_welcome_message(self, interaction: discord.Interaction, message: str):
        """Set custom welcome message for verification DMs (ADMIN ONLY)"""
        if not await self._check_verify_config(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You must be an administrator to use this command."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.VERIFY_CONFIG, "set-welcome-message")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        await self.db.update_guild(interaction.guild.id, {
            'welcome_message': message
        })

        embed = EmbedFactory.success(
            "✅ Welcome Message Updated",
            f"**New Welcome Message:**\n{message}\n\n"
            "This will be sent in DMs to new members along with the verification button."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"Welcome message updated in {interaction.guild}")

    @app_commands.command(name="send-verification", description="Send verification button in current channel (Admin)")
    async def send_verification(self, interaction: discord.Interaction):
        """Manually send verification button to current channel (ADMIN ONLY)"""
        if not await self._check_verify_config(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You must be an administrator to use this command."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.VERIFY_CONFIG, "send-verification")
            return
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config or not guild_config.get('verified_role'):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Configured", "Please setup verification first with /setup-verification"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.verification_prompt()
        view = VerificationButton(self)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            embed=EmbedFactory.success("Sent", "Verification button sent to this channel!"),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Verification(bot, bot.db, bot.config))

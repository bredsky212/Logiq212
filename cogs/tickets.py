"""
Tickets Cog for Logiq
Support ticket system
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import logging
import asyncio

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


class TicketCreateView(discord.ui.View):
    """Persistent view for creating tickets"""

    def __init__(self, cog: 'Tickets'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket", emoji="🎫")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle ticket creation"""
        await self.cog.create_ticket_for_user(interaction)


class TicketControlView(discord.ui.View):
    """Persistent view for ticket controls"""

    def __init__(self, cog: 'Tickets'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn", emoji="🔒")
    async def close_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle ticket closing via button"""
        await self.cog.close_ticket_for_user(interaction, "Closed by user")


class Tickets(commands.Cog):
    """Support ticket system cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('tickets', {})

    async def create_ticket_for_user(self, interaction: discord.Interaction):
        """Create a ticket for a user"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Configured", "Ticket system not configured"),
                ephemeral=True
            )
            return

        ticket_category_id = guild_config.get('ticket_category')
        if not ticket_category_id:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Configured", "Ticket category not set up"),
                ephemeral=True
            )
            return

        category = interaction.guild.get_channel(ticket_category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Ticket category not found"),
                ephemeral=True
            )
            return

        # Check if user already has an open ticket
        existing_tickets = [
            ch for ch in category.channels
            if ch.name.startswith(f"ticket-{interaction.user.name.lower()}")
        ]

        if existing_tickets:
            await interaction.response.send_message(
                embed=EmbedFactory.warning(
                    "Ticket Exists",
                    f"You already have an open ticket: {existing_tickets[0].mention}"
                ),
                ephemeral=True
            )
            return

        try:
            # Create ticket channel
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }

            # Add support role if configured
            support_role_id = guild_config.get('support_role')
            if support_role_id:
                support_role = interaction.guild.get_role(support_role_id)
                if support_role:
                    overwrites[support_role] = discord.PermissionOverwrite(
                        read_messages=True,
                        send_messages=True
                    )

            channel = await category.create_text_channel(
                name=f"ticket-{interaction.user.name}",
                overwrites=overwrites
            )

            # Create ticket in database
            ticket_data = {
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "channel_id": channel.id,
                "category": "General Support",
                "status": "open"
            }
            ticket_id = await self.db.create_ticket(ticket_data)

            # Send welcome message with close button
            embed = EmbedFactory.create(
                title="🎫 Support Ticket",
                description=f"Hello {interaction.user.mention}!\n\n"
                           f"Thank you for creating a support ticket. Please describe your issue "
                           f"and a staff member will assist you shortly.\n\n"
                           f"**Ticket ID:** {ticket_id}",
                color=EmbedColor.SUCCESS
            )

            # Add close button
            close_view = TicketControlView(self)
            await channel.send(embed=embed, view=close_view)
            
            # Log ticket creation to ticket log channel
            ticket_log_channel_id = guild_config.get('ticket_log_channel')
            if ticket_log_channel_id:
                log_channel = interaction.guild.get_channel(ticket_log_channel_id)
                if log_channel:
                    log_embed = EmbedFactory.create(
                        title="🎫 New Ticket Created",
                        description=f"**Ticket:** {channel.mention}\n"
                                   f"**Created by:** {interaction.user.mention}\n"
                                   f"**Ticket ID:** {ticket_id}\n"
                                   f"**Status:** Open",
                        color=EmbedColor.SUCCESS
                    )
                    await log_channel.send(embed=log_embed)

            await interaction.response.send_message(
                embed=EmbedFactory.success(
                    "Ticket Created",
                    f"Your ticket has been created: {channel.mention}"
                ),
                ephemeral=True
            )

            logger.info(f"Ticket created for {interaction.user} in {interaction.guild}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to create channels"),
                ephemeral=True
            )

    async def close_ticket_for_user(self, interaction: discord.Interaction, reason: str = "Resolved"):
        """Close a ticket (called from button or command)"""
        # Check if in ticket channel
        if not interaction.channel.name.startswith("ticket-"):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not a Ticket", "This can only be used in ticket channels"),
                ephemeral=True
            )
            return

        # Check if user is ticket owner or has admin permissions
        guild_config = await self.db.get_guild(interaction.guild.id)
        support_role_id = guild_config.get('support_role') if guild_config else None

        is_ticket_owner = interaction.channel.name == f"ticket-{interaction.user.name}"
        is_admin = interaction.user.guild_permissions.administrator
        has_support_role = support_role_id and interaction.guild.get_role(support_role_id) in interaction.user.roles

        if not (is_ticket_owner or is_admin or has_support_role):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only the ticket owner or staff can close this ticket"),
                ephemeral=True
            )
            return

        # Find ticket in database by channel_id
        ticket_channel_id = interaction.channel.id
        
        # Log ticket closure to ticket log channel
        ticket_log_channel_id = guild_config.get('ticket_log_channel') if guild_config else None
        if ticket_log_channel_id:
            log_channel = interaction.guild.get_channel(ticket_log_channel_id)
            if log_channel:
                # Get ticket creator from channel name
                ticket_creator_name = interaction.channel.name.replace("ticket-", "")
                
                log_embed = EmbedFactory.create(
                    title="🔒 Ticket Closed",
                    description=f"**Ticket:** {interaction.channel.name}\n"
                               f"**Closed by:** {interaction.user.mention}\n"
                               f"**Reason:** {reason}\n"
                               f"**Status:** Closed",
                    color=EmbedColor.WARNING
                )
                await log_channel.send(embed=log_embed)

        embed = EmbedFactory.warning(
            "🔒 Ticket Closing",
            f"This ticket is being closed by {interaction.user.mention}.\n\n**Reason:** {reason}\n\n"
            f"Channel will be deleted in 5 seconds..."
        )
        await interaction.response.send_message(embed=embed)

        logger.info(f"Ticket {interaction.channel.name} closed by {interaction.user}")

        # Update ticket status in database
        try:
            # Find and update ticket by channel_id
            await self.db.db.tickets.update_one(
                {"channel_id": ticket_channel_id},
                {"$set": {"status": "closed", "closed_by": interaction.user.id, "close_reason": reason}}
            )
        except Exception as e:
            logger.error(f"Error updating ticket in database: {e}")

        # Wait 5 seconds then delete the channel
        await asyncio.sleep(5)

        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
            logger.info(f"Deleted ticket channel: {interaction.channel.name}")
        except discord.Forbidden:
            logger.error(f"No permission to delete ticket channel: {interaction.channel.name}")
        except Exception as e:
            logger.error(f"Error deleting ticket channel: {e}")

    @app_commands.command(name="ticket-setup", description="Setup ticket system (Admin)")
    @app_commands.describe(
        category="Category for ticket channels",
        log_channel="Channel for ticket logs",
        support_role="Role to ping for new tickets (optional)"
    )
    @is_admin()
    async def ticket_setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        log_channel: discord.TextChannel,
        support_role: Optional[discord.Role] = None
    ):
        """Setup ticket system (ADMIN ONLY)"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        update_data = {
            'ticket_category': category.id,
            'ticket_log_channel': log_channel.id
        }
        if support_role:
            update_data['support_role'] = support_role.id

        await self.db.update_guild(interaction.guild.id, update_data)

        embed = EmbedFactory.success(
            "✅ Ticket System Setup",
            f"**Category:** {category.mention}\n"
            f"**Log Channel:** {log_channel.mention}\n" +
            (f"**Support Role:** {support_role.mention}" if support_role else "")
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"Ticket system setup in {interaction.guild}")

    @app_commands.command(name="ticket-panel", description="Send ticket creation panel (Admin)")
    @is_admin()
    async def ticket_panel(self, interaction: discord.Interaction):
        """Send persistent ticket panel (ADMIN ONLY)"""
        embed = EmbedFactory.create(
            title="🎫 Support Tickets",
            description="Need help? Click the button below to create a support ticket!\n\n"
                       "A private channel will be created where you can discuss your issue with staff.",
            color=EmbedColor.PRIMARY
        )

        view = TicketCreateView(self)
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )

    @app_commands.command(name="close-ticket", description="Close a ticket (Admin/Staff)")
    @app_commands.describe(reason="Reason for closing")
    async def close_ticket(self, interaction: discord.Interaction, reason: Optional[str] = "Resolved"):
        """Close a ticket (ADMIN/STAFF ONLY)"""
        await self.close_ticket_for_user(interaction, reason)

    @app_commands.command(name="tickets", description="View all active tickets (Admin)")
    @is_admin()
    async def view_tickets(self, interaction: discord.Interaction):
        """View all active tickets (ADMIN ONLY)"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Configured", "Ticket system not configured"),
                ephemeral=True
            )
            return

        ticket_category_id = guild_config.get('ticket_category')
        if not ticket_category_id:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Configured", "Ticket category not set up"),
                ephemeral=True
            )
            return

        category = interaction.guild.get_channel(ticket_category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Ticket category not found"),
                ephemeral=True
            )
            return

        # Get all ticket channels
        ticket_channels = [ch for ch in category.channels if ch.name.startswith("ticket-")]
        
        if not ticket_channels:
            await interaction.response.send_message(
                embed=EmbedFactory.info("No Active Tickets", "There are currently no active tickets"),
                ephemeral=True
            )
            return

        description = ""
        for channel in ticket_channels[:25]:  # Limit to 25
            ticket_owner = channel.name.replace("ticket-", "")
            description += f"🎫 {channel.mention} - **{ticket_owner}**\n"

        embed = EmbedFactory.create(
            title=f"🎫 Active Tickets ({len(ticket_channels)})",
            description=description,
            color=EmbedColor.INFO
        )
        
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Tickets(bot, bot.db, bot.config))

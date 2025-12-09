"""
Roles Cog for Logiq
Self-assignable roles with modal-based setup
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List
import logging

from utils.embeds import EmbedFactory, EmbedColor
from utils.feature_permissions import FeaturePermissionManager
from utils.denials import DenialLogger
from database.db_manager import DatabaseManager
from database.models import FeatureKey
from utils.security import filter_protected_roles, get_or_bootstrap_security

logger = logging.getLogger(__name__)


class RoleMenuSetupModal(discord.ui.Modal, title="Create Role Menu"):
    """Modal for creating role menus with custom settings"""

    title_input = discord.ui.TextInput(
        label="Menu Title",
        placeholder="e.g., Choose Your Roles",
        required=True,
        max_length=100
    )

    description_input = discord.ui.TextInput(
        label="Menu Description",
        placeholder="e.g., Select your preferred roles from the dropdown below",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    role_mentions = discord.ui.TextInput(
        label="Roles (mention with @)",
        placeholder="Type @ and select roles. Example: @Gamer @Artist @Developer",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )

    exclusive = discord.ui.TextInput(
        label="Exclusive? (yes/no)",
        placeholder="Type 'yes' if users can only pick ONE role",
        required=True,
        max_length=3
    )

    def __init__(self, cog, channel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        import re
        
        # Parse exclusive setting
        is_exclusive = self.exclusive.value.lower() in ['yes', 'y', 'true']

        # Parse role mentions
        role_list = []
        text = self.role_mentions.value
        role_ids = re.findall(r'<@&(\d+)>', text)

        if not role_ids:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Roles Found", "Please mention roles using @. Type @ and select roles from the list that appears."),
                ephemeral=True
            )
            return

        parsed_roles = []
        for role_id in role_ids:
            role = interaction.guild.get_role(int(role_id))
            if role:
                parsed_roles.append(role)

        parsed_roles = await filter_protected_roles(self.cog.db, interaction.guild, parsed_roles)

        for role in parsed_roles:
            # Skip @everyone and bot integration roles
            if role.is_default() or role.is_integration():
                continue

            role_emoji = None
            if role.unicode_emoji:
                role_emoji = role.unicode_emoji
            elif role.icon:
                role_emoji = str(role.icon)

            role_list.append({
                'role': role,
                'emoji': role_emoji or "🎭",
                'label': role.name
            })

        if not role_list:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Valid Roles", f"Found {len(role_ids)} role mentions but they cannot be used (might be bot roles or @everyone)."),
                ephemeral=True
            )
            return

        if len(role_list) > 25:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Too Many Roles", "Discord allows maximum 25 options per menu."),
                ephemeral=True
            )
            return

        # Create embed
        embed = EmbedFactory.create(
            title=self.title_input.value,
            description=self.description_input.value or "Select your roles from the dropdown below.",
            color=EmbedColor.PRIMARY
        )

        # Add field showing available roles
        roles_text = "\n".join([f"{r['emoji']} {r['role'].mention}" for r in role_list])
        embed.add_field(
            name="Available Roles",
            value=roles_text,
            inline=False
        )

        # Create view
        if is_exclusive:
            view = ExclusiveRoleView(role_list, self.title_input.value, self.cog)
        else:
            view = MultiRoleView(role_list, self.cog)

        # Send to channel
        await self.channel.send(embed=embed, view=view)

        # Respond to interaction
        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Role Menu Created!",
                f"{'Exclusive' if is_exclusive else 'Multi-select'} role menu created in {self.channel.mention}"
            ),
            ephemeral=True
        )

        logger.info(f"Role menu created by {interaction.user} with {len(role_list)} roles")


class ExclusiveRoleSelect(discord.ui.Select):
    """Dropdown for exclusive role selection (pick only one)"""

    def __init__(self, role_data: List[dict], category_name: str, cog: 'Roles'):
        options = [
            discord.SelectOption(
                label=r['label'],
                description=f"Get the {r['label']} role",
                value=str(r['role'].id),
                emoji=r['emoji']
            )
            for r in role_data[:25]
        ]

        super().__init__(
            placeholder=f"Choose your option...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"exclusive_role_{category_name[:50]}"
        )
        self.role_ids = [r['role'].id for r in role_data]
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        """Handle exclusive role selection - LOCKED after first selection"""
        try:
            # Check if user already has any role from THIS MENU ONLY
            user_has_role = False
            existing_role = None
            for role_id in self.role_ids:
                role = interaction.guild.get_role(role_id)
                if role and role in interaction.user.roles:
                    user_has_role = True
                    existing_role = role
                    break

            if user_has_role:
                await interaction.response.send_message(
                    embed=EmbedFactory.error(
                        "🔒 Role Already Selected",
                        f"You already have **{existing_role.name}**. You cannot select another role from this menu."
                    ),
                    ephemeral=True
                )
                return

            selected_role_id = int(self.values[0])
            selected_role = interaction.guild.get_role(selected_role_id)

            if not selected_role:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Error", "Role not found"),
                    ephemeral=True
                )
                return

            filtered = await filter_protected_roles(self.cog.db, interaction.guild, [selected_role])
            if not filtered:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Protected Role", "This role is protected and cannot be assigned by the bot."),
                    ephemeral=True
                )
                return

            # Give the selected role (only this one, no removing others)
            await interaction.user.add_roles(selected_role, reason="Exclusive role menu selection")

            embed = EmbedFactory.success(
                "✅ Role Selected!",
                f"You now have the **{selected_role.name}** role!\n\n"
                f"**Note:** You cannot select another role from this menu."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

            logger.info(f"{interaction.user} selected exclusive role {selected_role.name}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage your roles. Please contact an admin."),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in role selection: {e}", exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", f"Failed to assign role: {str(e)}"),
                ephemeral=True
            )


class MultiRoleSelect(discord.ui.Select):
    """Dropdown menu for multiple role selection"""

    def __init__(self, role_data: List[dict], cog: 'Roles'):
        options = [
            discord.SelectOption(
                label=r['label'],
                description=f"Toggle {r['label']} role",
                value=str(r['role'].id),
                emoji=r['emoji']
            )
            for r in role_data[:25]
        ]

        super().__init__(
            placeholder="Select roles to add/remove...",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id="multi_role_select"
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        """Handle role selection"""
        try:
            selected_role_ids = {int(value) for value in self.values}
            current_role_ids = {role.id for role in interaction.user.roles}

            roles_to_add = []
            roles_to_remove = []

            available_role_ids = {int(option.value) for option in self.options}

            for role_id in available_role_ids:
                role = interaction.guild.get_role(role_id)
                if not role:
                    continue

                if role_id in selected_role_ids and role_id not in current_role_ids:
                    roles_to_add.append(role)
                elif role_id not in selected_role_ids and role_id in current_role_ids:
                    roles_to_remove.append(role)

            if roles_to_add:
                roles_to_add = await filter_protected_roles(self.cog.db, interaction.guild, roles_to_add)
                if not roles_to_add:
                    await interaction.response.send_message(
                        embed=EmbedFactory.error("Protected Role", "Selected roles are protected and cannot be assigned by the bot."),
                        ephemeral=True
                    )
                    return
                await interaction.user.add_roles(*roles_to_add, reason="Role menu selection")
            if roles_to_remove:
                roles_to_remove = await filter_protected_roles(self.cog.db, interaction.guild, roles_to_remove)
                if roles_to_remove:
                    await interaction.user.remove_roles(*roles_to_remove, reason="Role menu deselection")

            changes = []
            if roles_to_add:
                changes.append(f"**Added:** {', '.join([r.name for r in roles_to_add])}")
            if roles_to_remove:
                changes.append(f"**Removed:** {', '.join([r.name for r in roles_to_remove])}")

            if not changes:
                changes.append("No changes made")

            embed = EmbedFactory.success(
                "✅ Roles Updated!",
                "\n".join(changes)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage your roles. Please contact an admin."),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in multi-role selection: {e}", exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", f"Failed to update roles: {str(e)}"),
                ephemeral=True
            )


class ExclusiveRoleView(discord.ui.View):
    """View for exclusive role selection"""

    def __init__(self, role_data: List[dict], category_name: str, cog: 'Roles'):
        super().__init__(timeout=None)
        self.add_item(ExclusiveRoleSelect(role_data, category_name, cog))


class MultiRoleView(discord.ui.View):
    """View for multi role selection"""

    def __init__(self, role_data: List[dict], cog: 'Roles'):
        super().__init__(timeout=None)
        self.add_item(MultiRoleSelect(role_data, cog))


class Roles(commands.Cog):
    """Role management cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('roles', {})
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()
        # Register persistent views on startup
        self.bot.loop.create_task(self._register_persistent_views())

    async def _register_persistent_views(self):
        """Register persistent views for role menus"""
        await self.bot.wait_until_ready()
        # Views are automatically re-registered when messages are loaded
        logger.info("Role menu persistent views ready")

    async def _filter_protected(self, roles: List[discord.Role]) -> List[discord.Role]:
        return await filter_protected_roles(self.db, roles[0].guild, roles)

    def _base_role_menu_check(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.manage_roles or perms.manage_guild or perms.administrator or member == member.guild.owner

    async def _can_use_feature(self, member: discord.Member, feature: FeatureKey, base_check) -> bool:
        return await self.perms.check(member, feature, base_check)

    def _hierarchy_block(self, moderator: discord.Member, target: discord.Member) -> Optional[str]:
        if target == moderator.guild.owner:
            return "You cannot act on the server owner."
        if target.guild_permissions.administrator:
            return "You cannot act on an administrator."
        if target.top_role >= moderator.top_role and moderator != moderator.guild.owner:
            return "You cannot act on a member with an equal or higher top role."
        return None

    def _role_position_block(self, moderator: discord.Member, role: discord.Role) -> Optional[str]:
        if moderator == moderator.guild.owner:
            return None
        if role.is_default():
            return "You cannot manage the @everyone role."
        if role >= moderator.top_role:
            return "You cannot manage a role that is equal to or higher than your top role."
        return None

    def _bot_role_block(self, guild: discord.Guild, role: discord.Role) -> Optional[str]:
        bot_member = guild.me
        if not bot_member:
            return None
        if role >= bot_member.top_role:
            return "I cannot manage that role because it is higher than or equal to my top role."
        if not bot_member.guild_permissions.manage_roles:
            return "I do not have the Manage Roles permission."
        return None

    async def _log_denial(self, interaction: discord.Interaction, feature: FeatureKey, reason: str):
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, "roles", feature.value):
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
            logger.warning(f"Cannot send roles log to channel {channel} in {guild}")

    @app_commands.command(name="create-role-menu", description="Create a role menu (Admin)")
    @app_commands.describe(
        title="Title of the role menu",
        description="Description of the role menu",
        role1="First role",
        role2="Second role (optional)",
        role3="Third role (optional)",
        role4="Fourth role (optional)",
        role5="Fifth role (optional)",
        role6="Sixth role (optional)",
        role7="Seventh role (optional)",
        role8="Eighth role (optional)",
        role9="Ninth role (optional)",
        role10="Tenth role (optional)",
        exclusive="Can users only pick ONE role? (yes/no)",
        channel="Channel to send menu (optional)"
    )
    async def create_role_menu(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        role1: discord.Role,
        exclusive: str,
        role2: Optional[discord.Role] = None,
        role3: Optional[discord.Role] = None,
        role4: Optional[discord.Role] = None,
        role5: Optional[discord.Role] = None,
        role6: Optional[discord.Role] = None,
        role7: Optional[discord.Role] = None,
        role8: Optional[discord.Role] = None,
        role9: Optional[discord.Role] = None,
        role10: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None
    ):
        """Create role menu directly with slash command"""
        if not await self._can_use_feature(interaction.user, FeatureKey.ROLES_MENU_MANAGE, self._base_role_menu_check):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to create role menus."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.ROLES_MENU_MANAGE, "create-role-menu")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        target_channel = channel or interaction.channel
        is_exclusive = exclusive.lower() in ['yes', 'y', 'true']

        # Collect all roles
        roles = [role1]
        if role2:
            roles.append(role2)
        if role3:
            roles.append(role3)
        if role4:
            roles.append(role4)
        if role5:
            roles.append(role5)
        if role6:
            roles.append(role6)
        if role7:
            roles.append(role7)
        if role8:
            roles.append(role8)
        if role9:
            roles.append(role9)
        if role10:
            roles.append(role10)
        
        # Build role list
        role_list = []
        protected_filtered = await self._filter_protected(roles)
        for role in protected_filtered:
            if role.is_default() or role.is_integration():
                continue
            
            role_emoji = None
            if role.unicode_emoji:
                role_emoji = role.unicode_emoji
            elif role.icon:
                role_emoji = str(role.icon)
            
            role_list.append({
                'role': role,
                'emoji': role_emoji or "🎭",
                'label': role.name
            })

        if not role_list:
            await interaction.followup.send(
                embed=EmbedFactory.error("No Valid Roles", "Please select valid roles."),
                ephemeral=True
            )
            return

        # Create embed
        embed = EmbedFactory.create(
            title=title,
            description=description,
            color=EmbedColor.PRIMARY
        )
        
        # Add field showing available roles
        roles_text = "\n".join([f"{r['emoji']} {r['role'].mention}" for r in role_list])
        embed.add_field(
            name="Available Roles",
            value=roles_text,
            inline=False
        )
        
        # Create view
        if is_exclusive:
            view = ExclusiveRoleView(role_list, title, self)
        else:
            view = MultiRoleView(role_list, self)

        # Send to channel
        await target_channel.send(embed=embed, view=view)

        await interaction.followup.send(
            embed=EmbedFactory.success(
                "Role Menu Created!",
                f"{'Exclusive' if is_exclusive else 'Multi-select'} role menu created in {target_channel.mention}"
            ),
            ephemeral=True
        )
        
        logger.info(f"Role menu created by {interaction.user} with {len(role_list)} roles")

    @app_commands.command(name="addrole", description="Add a role to a user (Admin)")
    @app_commands.describe(user="User to add role to", role="Role to add")
    async def add_role(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        """Add role to user"""
        if not await self._can_use_feature(interaction.user, FeatureKey.ROLES_FORCE_ASSIGN, self._base_role_menu_check):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to force-assign roles."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.ROLES_FORCE_ASSIGN, "addrole")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        filtered = await filter_protected_roles(self.db, interaction.guild, [role])
        if not filtered:
            await interaction.followup.send(
                embed=EmbedFactory.error("Protected Role", "This role is protected and cannot be assigned by the bot."),
                ephemeral=True
            )
            return

        if role in user.roles:
            await interaction.followup.send(
                embed=EmbedFactory.info("Already Has Role", f"{user.mention} already has {role.mention}"),
                ephemeral=True
            )
            return

        hierarchy_reason = self._hierarchy_block(interaction.user, user)
        if hierarchy_reason:
            await interaction.followup.send(
                embed=EmbedFactory.error("Permission Denied", hierarchy_reason),
                ephemeral=True
            )
            return

        position_block = self._role_position_block(interaction.user, role)
        if position_block:
            await interaction.followup.send(
                embed=EmbedFactory.error("Cannot Manage Role", position_block),
                ephemeral=True
            )
            return

        bot_block = self._bot_role_block(interaction.guild, role)
        if bot_block:
            await interaction.followup.send(
                embed=EmbedFactory.error("Bot Cannot Manage Role", bot_block),
                ephemeral=True
            )
            return

        try:
            await user.add_roles(role, reason="Forced role assignment via /addrole")
            embed = EmbedFactory.success("Role Added", f"Added {role.mention} to {user.mention}")
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"{interaction.user} added role {role} to {user}")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "I don't have permission to manage roles"),
                ephemeral=True
            )

    @app_commands.command(name="removerole", description="Remove a role from a user (Admin)")
    @app_commands.describe(user="User to remove role from", role="Role to remove")
    async def remove_role(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        """Remove role from user"""
        if not await self._can_use_feature(interaction.user, FeatureKey.ROLES_FORCE_ASSIGN, self._base_role_menu_check):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to force-remove roles."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.ROLES_FORCE_ASSIGN, "removerole")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        filtered = await filter_protected_roles(self.db, interaction.guild, [role])
        if not filtered:
            await interaction.followup.send(
                embed=EmbedFactory.error("Protected Role", "This role is protected and cannot be removed by the bot."),
                ephemeral=True
            )
            return

        if role not in user.roles:
            await interaction.followup.send(
                embed=EmbedFactory.info("Doesn't Have Role", f"{user.mention} doesn't have {role.mention}"),
                ephemeral=True
            )
            return

        hierarchy_reason = self._hierarchy_block(interaction.user, user)
        if hierarchy_reason:
            await interaction.followup.send(
                embed=EmbedFactory.error("Permission Denied", hierarchy_reason),
                ephemeral=True
            )
            return

        position_block = self._role_position_block(interaction.user, role)
        if position_block:
            await interaction.followup.send(
                embed=EmbedFactory.error("Cannot Manage Role", position_block),
                ephemeral=True
            )
            return

        bot_block = self._bot_role_block(interaction.guild, role)
        if bot_block:
            await interaction.followup.send(
                embed=EmbedFactory.error("Bot Cannot Manage Role", bot_block),
                ephemeral=True
            )
            return

        try:
            await user.remove_roles(role, reason="Forced role removal via /removerole")
            embed = EmbedFactory.success("Role Removed", f"Removed {role.mention} from {user.mention}")
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"{interaction.user} removed role {role} from {user}")
        except discord.Forbidden:
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "I don't have permission to manage roles"),
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Roles(bot, bot.db, bot.config))

"""
Feature permission management commands
"""

import logging
from typing import List, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from database.models import FeatureKey
from utils.embeds import EmbedFactory, EmbedColor
from utils.feature_permissions import FeaturePermissionManager

logger = logging.getLogger(__name__)


def _is_config_admin(member: discord.Member) -> bool:
    return (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or member == member.guild.owner
    )


class FeaturePermissions(commands.Cog):
    """Configure feature-level permissions"""

    perms = app_commands.Group(
        name="perms",
        description="Configure Logiq feature permissions",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot, db: DatabaseManager):
        self.bot = bot
        self.db = db
        self.manager = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.log = logging.getLogger("logiq.feature_permissions")

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
            logger.warning(f"Cannot send feature-perms log to channel {channel} in {guild}")

    def _feature_choices(self) -> List[app_commands.Choice[str]]:
        return [app_commands.Choice(name=key.value, value=key.value) for key in FeatureKey]

    async def _get_feature_doc(self, guild_id: int, feature_key: str) -> Dict:
        return await self.db.get_feature_permission(guild_id, feature_key) or {}

    @perms.command(name="feature-list", description="List feature permission overrides")
    async def feature_list(self, interaction: discord.Interaction):
        if not _is_config_admin(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can view permissions."),
                ephemeral=True
            )
            return

        docs = {doc["feature_key"]: doc for doc in await self.db.list_feature_permissions(interaction.guild.id)}
        lines = []
        for key in FeatureKey:
            doc = docs.get(key.value)
            if not doc:
                lines.append(f"**{key.value}** - default (no overrides)")
            else:
                allowed = ", ".join(f"<@&{r}>" for r in doc.get('allowed_roles', [])) or "None"
                denied = ", ".join(f"<@&{r}>" for r in doc.get('denied_roles', [])) or "None"
                lines.append(f"**{key.value}**\nAllowed: {allowed}\nDenied: {denied}")

        description = "\n\n".join(lines)
        embed = EmbedFactory.create(
            title="Feature Permissions",
            description=description,
            color=EmbedColor.INFO
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @perms.command(name="feature-allow", description="Allow a role to use a feature")
    @app_commands.choices(feature=[app_commands.Choice(name=k.value, value=k.value) for k in FeatureKey])
    async def feature_allow(self, interaction: discord.Interaction, feature: app_commands.Choice[str], role: discord.Role):
        await self._update_feature(interaction, feature.value, role, action="allow")

    @perms.command(name="feature-deny", description="Deny a role from using a feature")
    @app_commands.choices(feature=[app_commands.Choice(name=k.value, value=k.value) for k in FeatureKey])
    async def feature_deny(self, interaction: discord.Interaction, feature: app_commands.Choice[str], role: discord.Role):
        await self._update_feature(interaction, feature.value, role, action="deny")

    @perms.command(name="feature-clear", description="Remove a role from allow/deny for a feature")
    @app_commands.choices(feature=[app_commands.Choice(name=k.value, value=k.value) for k in FeatureKey])
    async def feature_clear(self, interaction: discord.Interaction, feature: app_commands.Choice[str], role: discord.Role):
        await self._update_feature(interaction, feature.value, role, action="clear")

    @perms.command(name="feature-reset", description="Reset feature permissions to default")
    @app_commands.choices(feature=[app_commands.Choice(name=k.value, value=k.value) for k in FeatureKey])
    async def feature_reset(self, interaction: discord.Interaction, feature: app_commands.Choice[str]):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            logger.exception("Failed to defer perms feature-reset")
            return

        if not _is_config_admin(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can change permissions."),
                ephemeral=True
            )
            return

        old = await self._get_feature_doc(interaction.guild.id, feature.value)
        await self.db.delete_feature_permission(interaction.guild.id, feature.value)
        await self.manager.audit_change(
            interaction.guild.id,
            FeatureKey(feature.value),
            interaction.user.id,
            "reset",
            None,
            old,
            {}
        )

        embed = EmbedFactory.success("Feature Reset", f"{feature.value} reset to default.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        await self._log_to_mod(interaction.guild, EmbedFactory.create(
            title="Feature Permissions Reset",
            description=f"{feature.value} reset by {interaction.user.mention}",
            color=EmbedColor.INFO
        ))

    async def _update_feature(self, interaction: discord.Interaction, feature_key: str, role: discord.Role, action: str):
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except Exception:
                logger.exception("Failed to defer perms _update_feature")
                return

        if not _is_config_admin(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can change permissions."),
                ephemeral=True
            )
            return

        try:
            feature_enum = FeatureKey(feature_key)
        except ValueError:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Feature", "Unknown feature key."),
                ephemeral=True
            )
            return

        old_doc = await self._get_feature_doc(interaction.guild.id, feature_key)
        allowed = set(old_doc.get("allowed_roles", []))
        denied = set(old_doc.get("denied_roles", []))

        if action == "allow":
            allowed.add(role.id)
            denied.discard(role.id)
        elif action == "deny":
            denied.add(role.id)
            allowed.discard(role.id)
        elif action == "clear":
            allowed.discard(role.id)
            denied.discard(role.id)

        try:
            new_doc = await self.db.upsert_feature_permission(
                interaction.guild.id,
                feature_key,
                {
                    "allowed_roles": list(allowed),
                    "denied_roles": list(denied),
                    "updated_by": interaction.user.id,
                }
            )
        except Exception:
            logger.exception("upsert_feature_permission failed for %s", feature_key)
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "Failed to save feature permission. Check server logs."),
                ephemeral=True
            )
            return

        try:
            await self.manager.audit_change(
                interaction.guild.id,
                feature_enum,
                interaction.user.id,
                action,
                role.id,
                old_doc,
                new_doc
            )
        except Exception:
            logger.exception("audit_change failed for %s", feature_key)

        allowed_text = ", ".join(f"<@&{r}>" for r in new_doc.get("allowed_roles", [])) or "None"
        denied_text = ", ".join(f"<@&{r}>" for r in new_doc.get("denied_roles", [])) or "None"

        embed = EmbedFactory.create(
            title="Feature Permissions Updated",
            description=f"**Feature:** {feature_key}\n**Allowed:** {allowed_text}\n**Denied:** {denied_text}",
            color=EmbedColor.INFO
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        log_embed = EmbedFactory.create(
            title="Feature Permissions Updated",
            description=f"{feature_key} updated by {interaction.user.mention}\nAllowed: {allowed_text}\nDenied: {denied_text}",
            color=EmbedColor.INFO
        )
        await self._log_to_mod(interaction.guild, log_embed)

    @perms.command(name="debug", description="Debug perms wiring")
    async def perms_debug(self, interaction: discord.Interaction):
        self.log.info(
            "perms/debug invoked by user=%s (%s) guild=%s",
            interaction.user, interaction.user.id, interaction.guild_id
        )
        await interaction.response.send_message(
            f"✅ perms/debug OK – guild={interaction.guild_id}, admin={interaction.user.guild_permissions.administrator}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(FeaturePermissions(bot, bot.db))

    existing = bot.tree.get_command("perms")
    if existing:
        bot.tree.remove_command("perms", type=discord.AppCommandType.chat_input)
    bot.tree.add_command(FeaturePermissions.perms)

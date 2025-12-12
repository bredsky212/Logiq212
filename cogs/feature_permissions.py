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
from utils.feature_permissions import FeaturePermissionManager, SENSITIVE_FEATURES
from utils.security import get_or_bootstrap_security, security_cache
from utils.logs import resolve_log_channel
from utils.denials import DenialLogger

logger = logging.getLogger(__name__)


def _is_config_admin(member: discord.Member) -> bool:
    return (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or member == member.guild.owner
    )


def _is_cog_enabled(config: dict, cog_name: str) -> bool:
    modules_cfg = (config or {}).get("modules") or {}
    cog_cfg = modules_cfg.get(cog_name)

    if cog_cfg is None:
        return True  # default: enabled
    if isinstance(cog_cfg, dict):
        return bool(cog_cfg.get("enabled", True))
    if isinstance(cog_cfg, bool):
        return cog_cfg
    return True


_FEATURE_PREFIX_TO_COG = {
    "tickets": "tickets",
    "staffapp": "staff_applications",
    "report": "moderation",
    "perms": "feature_permissions",
    "verify": "verification",
    "games": "games",
    "roles": "roles",
    "economy": "economy",
    "leveling": "leveling",
    "giveaway": "giveaways",
    "music": "music",
    "alerts": "social_alerts",
    "tempvoice": "temp_voice",
    "utility": "utility",
    "analytics": "analytics",
}


def _feature_to_cog_name(feature: FeatureKey) -> Optional[str]:
    value = feature.value
    if value.startswith("mod.vc_"):
        return "vcmod"
    if value.startswith("mod."):
        return "moderation"
    prefix = value.split(".", 1)[0]
    return _FEATURE_PREFIX_TO_COG.get(prefix)


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
        self.denials = DenialLogger()
        if hasattr(self.manager, "denials"):
            self.manager.denials = self.denials

    async def _log_to_mod(self, guild: discord.Guild, embed: discord.Embed):
        channel = await resolve_log_channel(self.db, guild, "feature_permissions")
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning(f"Cannot send feature-perms log to channel {channel} in {guild}")

    async def feature_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Autocomplete feature keys (Discord limit: 25 choices)."""
        current_lower = current.lower()
        choices = []
        for key in FeatureKey:
            if current_lower in key.value.lower() or not current_lower:
                choices.append(app_commands.Choice(name=key.value, value=key.value))
            if len(choices) >= 25:
                break
        return choices

    async def _get_feature_doc(self, guild_id: int, feature_key: str) -> Dict:
        return await self.db.get_feature_permission(guild_id, feature_key) or {}

    @perms.command(name="feature-list", description="List feature permission overrides")
    @app_commands.describe(show_all="Show all feature keys (including disabled modules)")
    async def feature_list(self, interaction: discord.Interaction, show_all: bool = False):
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except Exception:
                logger.exception("Failed to defer perms feature-list")
                return

        if not _is_config_admin(interaction.user):
            await interaction.followup.send(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can view permissions."),
                ephemeral=True
            )
            return

        docs = {doc["feature_key"]: doc for doc in await self.db.list_feature_permissions(interaction.guild.id)}
        lines = []
        config = getattr(self.bot, "config", {}) or {}
        for key in FeatureKey:
            if not show_all:
                cog_name = _feature_to_cog_name(key)
                if cog_name and not _is_cog_enabled(config, cog_name):
                    continue
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
        await interaction.followup.send(embed=embed, ephemeral=True)

    @perms.command(name="feature-allow", description="Allow a role to use a feature")
    @app_commands.autocomplete(feature=feature_autocomplete)
    async def feature_allow(self, interaction: discord.Interaction, feature: str, role: discord.Role):
        await self._update_feature(interaction, feature, role, action="allow")

    @perms.command(name="feature-deny", description="Deny a role from using a feature")
    @app_commands.autocomplete(feature=feature_autocomplete)
    async def feature_deny(self, interaction: discord.Interaction, feature: str, role: discord.Role):
        await self._update_feature(interaction, feature, role, action="deny")

    @perms.command(name="feature-clear", description="Remove a role from allow/deny for a feature")
    @app_commands.autocomplete(feature=feature_autocomplete)
    async def feature_clear(self, interaction: discord.Interaction, feature: str, role: discord.Role):
        await self._update_feature(interaction, feature, role, action="clear")

    @perms.command(name="feature-reset", description="Reset feature permissions to default")
    @app_commands.autocomplete(feature=feature_autocomplete)
    async def feature_reset(self, interaction: discord.Interaction, feature: str):
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

        # Soft safeguard for sensitive features when actor is not admin
        if feature_enum in SENSITIVE_FEATURES and not interaction.user.guild_permissions.administrator:
            security = await get_or_bootstrap_security(self.db, interaction.guild)
            protected_ids = set(security.get("protected_role_ids", []))
            protected_roles = [interaction.guild.get_role(rid) for rid in protected_ids]
            highest_protected = max((r.position for r in protected_roles if r), default=-1)
            if not interaction.user.guild_permissions.manage_guild or interaction.user.top_role.position < highest_protected:
                if self.denials.should_log(interaction.guild.id, interaction.user.id, "perms", feature_enum.value):
                    logger.warning("Sensitive feature change denied for %s in guild %s", interaction.user.id, interaction.guild.id)
                await interaction.followup.send(
                    embed=EmbedFactory.error(
                        "Permission Denied",
                        "You must be Manage Guild and at least as high as protected roles to change this sensitive feature."
                    ),
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

    def _format_role_mentions(self, guild: discord.Guild, role_ids: List[int]) -> str:
        roles = []
        for rid in role_ids:
            role = guild.get_role(rid)
            roles.append(role.mention if role else f"<@&{rid}>")
        return ", ".join(roles) if roles else "None"

    @perms.command(name="security-bootstrap", description="Initialize guild security protected roles")
    async def security_bootstrap(self, interaction: discord.Interaction):
        if not _is_config_admin(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can configure security."),
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        security = await self.db.get_guild_security(interaction.guild.id)
        if not security:
            security = await get_or_bootstrap_security(self.db, interaction.guild)
        initialized_before = security.get("initialized", False)
        if not initialized_before:
            security = await self.db.upsert_guild_security(
                interaction.guild.id,
                {
                    "protected_role_ids": security.get("protected_role_ids", []),
                    "initialized": True,
                },
            )
            security_cache.set(interaction.guild.id, security)
        protected_text = self._format_role_mentions(interaction.guild, security.get("protected_role_ids", []))
        embed = EmbedFactory.success(
            "Security Bootstrapped" if not initialized_before else "Security Already Initialized",
            f"Protected roles: {protected_text}\nSecurity initialized: {security.get('initialized', False)}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Security Bootstrap",
                description=f"{interaction.user.mention} initialized security.\nProtected: {protected_text}",
                color=EmbedColor.INFO,
            ),
        )

    @perms.command(name="security-protected-add", description="Add a protected role")
    async def security_protected_add(self, interaction: discord.Interaction, role: discord.Role):
        if not _is_config_admin(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can configure security."),
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        security = await self.db.get_guild_security(interaction.guild.id) or {"protected_role_ids": []}
        protected = set(security.get("protected_role_ids", []))
        protected.add(role.id)
        updated = await self.db.upsert_guild_security(
            interaction.guild.id,
            {"protected_role_ids": list(protected), "initialized": True},
        )
        security_cache.set(interaction.guild.id, updated)
        protected_text = self._format_role_mentions(interaction.guild, updated.get("protected_role_ids", []))
        await interaction.followup.send(
            embed=EmbedFactory.success("Protected Role Added", f"Protected roles: {protected_text}"),
            ephemeral=True,
        )
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Protected Role Added",
                description=f"{role.mention} added by {interaction.user.mention}\nProtected: {protected_text}",
                color=EmbedColor.INFO,
            ),
        )

    @perms.command(name="security-protected-remove", description="Remove a protected role")
    async def security_protected_remove(self, interaction: discord.Interaction, role: discord.Role):
        if not _is_config_admin(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can configure security."),
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        security = await self.db.get_guild_security(interaction.guild.id) or {"protected_role_ids": []}
        protected = set(security.get("protected_role_ids", []))
        if role.id in protected:
            protected.remove(role.id)
        if not protected:
            await interaction.followup.send(
                embed=EmbedFactory.error("Cannot Remove", "At least one protected role must remain."),
                ephemeral=True,
            )
            return
        updated = await self.db.upsert_guild_security(
            interaction.guild.id,
            {"protected_role_ids": list(protected), "initialized": True},
        )
        security_cache.set(interaction.guild.id, updated)
        protected_text = self._format_role_mentions(interaction.guild, updated.get("protected_role_ids", []))
        await interaction.followup.send(
            embed=EmbedFactory.success("Protected Role Removed", f"Protected roles: {protected_text}"),
            ephemeral=True,
        )
        await self._log_to_mod(
            interaction.guild,
            EmbedFactory.create(
                title="Protected Role Removed",
                description=f"{role.mention} removed by {interaction.user.mention}\nProtected: {protected_text}",
                color=EmbedColor.WARNING,
            ),
        )

    @perms.command(name="security-protected-list", description="List protected roles")
    async def security_protected_list(self, interaction: discord.Interaction):
        if not _is_config_admin(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only Admin/Manage Guild can configure security."),
                ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        security = await get_or_bootstrap_security(self.db, interaction.guild)
        protected_text = self._format_role_mentions(interaction.guild, security.get("protected_role_ids", []))
        embed = EmbedFactory.info(
            "Protected Roles",
            f"Initialized: {security.get('initialized', False)}\nProtected: {protected_text}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
    cog = FeaturePermissions(bot, bot.db)
    await bot.add_cog(cog)

    existing = bot.tree.get_command("perms")
    if existing:
        bot.tree.remove_command("perms", type=discord.AppCommandType.chat_input)
    bot.tree.add_command(cog.perms)

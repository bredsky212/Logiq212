"""
Staff Applications Cog
Handles staff application templates, panels, modals, and review workflow
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from database.models import (
    StaffApplicationField,
    StaffApplicationTemplate,
    StaffApplicationAnswer,
    StaffApplication,
)
from utils.embeds import EmbedFactory, EmbedColor
from utils.feature_permissions import FeaturePermissionManager
from utils.denials import DenialLogger
from database.models import FeatureKey

logger = logging.getLogger(__name__)


def _truncate(text: str, limit: int = 1024) -> str:
    """Truncate text safely for embed fields"""
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _shorten(text: str, limit: int) -> str:
    """Hard truncate a string to a limit with ellipsis if needed"""
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


class StaffApplyView(discord.ui.View):
    """Persistent view for Apply panel"""

    def __init__(self, cog: "StaffApplications", template_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.template_id = template_id
        button = discord.ui.Button(
            label="Apply",
            style=discord.ButtonStyle.success,
            custom_id=f"staffapp_apply:{template_id}"
        )
        button.callback = self.on_apply  # type: ignore
        self.add_item(button)

    async def on_apply(self, interaction: discord.Interaction):
        """Handle Apply button click"""
        try:
            template = await self.cog._fetch_template(interaction.guild, self.template_id)
            if not template:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Not Found", "Application template not found or disabled."),
                    ephemeral=True
                )
                return

            modal = StaffApplicationModal(self.cog, template)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.exception(f"Error handling apply button for template {self.template_id}: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Error", "Something went wrong opening the application modal."),
                    ephemeral=True
                )


class StaffApplicationReviewView(discord.ui.View):
    """Persistent view for reviewing applications"""

    def __init__(self, cog: "StaffApplications", application_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.application_id = application_id

        self.add_item(self._build_button("Interview", "interview", discord.ButtonStyle.primary, disabled))
        self.add_item(self._build_button("Accept", "accepted", discord.ButtonStyle.success, disabled))
        self.add_item(self._build_button("Reject", "rejected", discord.ButtonStyle.danger, disabled))

    def _build_button(
        self,
        label: str,
        status: str,
        style: discord.ButtonStyle,
        disabled: bool
    ) -> discord.ui.Button:
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"staffapp_status:{status}:{self.application_id}",
            disabled=disabled
        )
        async def callback(interaction: discord.Interaction):
            await self.cog._handle_status_button(interaction, status, self.application_id)
        button.callback = callback  # type: ignore
        return button


class StaffApplicationModal(discord.ui.Modal):
    """Modal shown to applicants"""

    def __init__(self, cog: "StaffApplications", template: StaffApplicationTemplate):
        modal_title = _shorten(f"{template.name} Application", 45)
        super().__init__(title=modal_title)
        self.cog = cog
        self.template = template
        self.inputs: Dict[str, discord.ui.TextInput] = {}

        for field in template.fields:
            style = discord.TextStyle.short if field.style == "short" else discord.TextStyle.paragraph
            text_input = discord.ui.TextInput(
                label=_shorten(field.label, 45),
                placeholder=_shorten(field.placeholder, 100) if field.placeholder else discord.utils.MISSING,
                required=field.required,
                max_length=field.max_length,
                style=style,
                custom_id=field.key,
            )
            self.inputs[field.key] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            if guild is None:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Guild Only", "Applications must be submitted in a server."),
                    ephemeral=True
                )
                return

            answers = [
                StaffApplicationAnswer(
                    key=field.key,
                    label=field.label,
                    value=self.inputs[field.key].value
                )
                for field in self.template.fields
            ]

            application = StaffApplication(
                guild_id=guild.id,
                template_id=self.template.template_id,
                application_id="",
                applicant_id=interaction.user.id,
                team_role_id=self.template.team_role_id,
                answers=answers,
                status="pending",
                review_channel_id=self.template.review_channel_id,
                review_message_id=0
            )

            application_id = await self.cog.db.create_staff_application(application.to_dict())
            application.application_id = application_id

            review_message_id = await self.cog._post_review_embed(guild, application, self.template)
            if review_message_id:
                await self.cog.db.update_staff_application(
                    guild.id,
                    application_id,
                    {
                        "review_message_id": review_message_id,
                        "updated_at": datetime.utcnow(),
                    }
                )

            await interaction.response.send_message(
                "✅ Your application has been submitted. Staff will review it soon.",
                ephemeral=True
            )

            await self.cog._notify_applicant_status(
                interaction.user,
                self.template,
                application,
                "pending",
                notes=None
            )
        except Exception as e:
            logger.exception(f"Error submitting application for template {self.template.template_id}: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Error", "Failed to submit your application. Please try again."),
                    ephemeral=True
                )


class StaffApplications(commands.Cog):
    """Staff applications system"""

    staffapp = app_commands.Group(
        name="staffapp",
        description="Configure and review staff applications",
        guild_only=True,
    )

    config_group = app_commands.Group(
        name="config",
        description="Configure staff application roles",
        parent=staffapp
    )

    template_group = app_commands.Group(
        name="template",
        description="Manage staff application templates",
        parent=staffapp
    )

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self._register_task = bot.loop.create_task(self._register_persistent_views())
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()
        self._log = bot.logger if hasattr(bot, "logger") else logger

    async def _register_persistent_views(self):
        """Register persistent views for existing templates and open applications"""
        await self.bot.wait_until_ready()
        try:
            templates = await self.db.list_all_staff_templates()
            for tpl in templates:
                if tpl.get("is_active", True):
                    self.bot.add_view(StaffApplyView(self, tpl["template_id"]))

            # Register review views for active applications
            if self.db.staff_applications is not None:
                cursor = self.db.staff_applications.find({
                    "status": {"$in": ["pending", "interview", "accepted", "rejected"]},
                    "review_message_id": {"$ne": 0}
                })
                apps = await cursor.to_list(length=500)
                for app in apps:
                    disabled = app.get("status") in ["accepted", "rejected"]
                    self.bot.add_view(StaffApplicationReviewView(self, app["application_id"], disabled=disabled))
        except Exception as e:
            logger.warning(f"Failed to register persistent views: {e}")

    def _default_fields(self) -> List[StaffApplicationField]:
        """Default fields for a new template"""
        return [
            StaffApplicationField(key="motivation", label="Motivation", style="paragraph", max_length=500),
            StaffApplicationField(key="experience", label="Experience", style="paragraph", max_length=1000),
            StaffApplicationField(key="availability", label="Availability / Timezone", style="short", max_length=200),
            StaffApplicationField(key="basic_info", label="Age or basic info", style="short", required=False, max_length=200),
        ]

    async def _get_config(self, guild_id: int) -> Dict[str, Any]:
        """Fetch or create staff app config"""
        config = await self.db.get_staff_app_config(guild_id)
        if not config:
            config = await self.db.upsert_staff_app_config(guild_id, {
                "creator_roles": [],
                "reviewer_roles": [],
                "default_apply_channel_id": None,
            })
        return config

    def _is_creator(self, member: discord.Member, config: Dict[str, List[int]]) -> bool:
        return member.guild_permissions.administrator or any(
            role.id in config.get("creator_roles", []) for role in member.roles
        )

    def _is_reviewer(self, member: discord.Member, config: Dict[str, List[int]]) -> bool:
        return self._is_creator(member, config) or any(
            role.id in config.get("reviewer_roles", []) for role in member.roles
        )

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
            logger.warning(f"Cannot send staffapp log to {channel} in {guild.id}")

    async def _check_template_manage(self, member: discord.Member, config: Dict[str, Any]) -> bool:
        return await self.perms.check(
            member,
            FeatureKey.STAFFAPP_TEMPLATE_MANAGE,
            base_check=lambda m: self._is_creator(m, config),
        )

    async def _check_review(self, member: discord.Member, config: Dict[str, Any]) -> bool:
        return await self.perms.check(
            member,
            FeatureKey.STAFFAPP_REVIEW,
            base_check=lambda m: self._is_reviewer(m, config),
        )

    async def _maybe_log_denial(self, interaction: discord.Interaction, feature: FeatureKey, reason: str):
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, "staffapp", feature.value):
            return
        embed = EmbedFactory.warning(
            "Permission Denied",
            f"{interaction.user.mention} denied `{feature.value}` in {interaction.guild.name}.\nReason: {reason}"
        )
        await self._log_to_mod(interaction.guild, embed)

    # Config commands
    @config_group.command(name="set-creator-role", description="Set creator role for staff applications")
    async def set_creator_role(self, interaction: discord.Interaction, role: discord.Role):
        config = await self._get_config(interaction.guild.id)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only admins can set creator roles."),
                ephemeral=True
            )
            return

        updated = await self.db.upsert_staff_app_config(interaction.guild.id, {
            "creator_roles": [role.id]
        })
        await interaction.response.send_message(
            embed=EmbedFactory.success("Creator Role Updated", f"Creator role set to {role.mention}"),
            ephemeral=True
        )
        logger.info(f"{interaction.user} set creator role to {role} in {interaction.guild}")
        return updated

    @config_group.command(name="set-apply-channel", description="Set default apply channel for staff applications")
    async def set_apply_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        config = await self._get_config(interaction.guild.id)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only admins can set the default apply channel."),
                ephemeral=True
            )
            return

        await self.db.upsert_staff_app_config(interaction.guild.id, {
            "default_apply_channel_id": channel.id
        })

        await interaction.response.send_message(
            embed=EmbedFactory.success("Default Apply Channel Set", f"Applications will default to {channel.mention}"),
            ephemeral=True
        )
        logger.info(f"{interaction.user} set default apply channel to {channel} in {interaction.guild}")

    @config_group.command(name="add-reviewer-role", description="Add reviewer role for staff applications")
    async def add_reviewer_role(self, interaction: discord.Interaction, role: discord.Role):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_template_manage(interaction.user, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only admins or creators can add reviewer roles."),
                ephemeral=True
            )
            return

        roles = set(config.get("reviewer_roles", []))
        roles.add(role.id)
        await self.db.upsert_staff_app_config(interaction.guild.id, {
            "reviewer_roles": list(roles)
        })
        await interaction.response.send_message(
            embed=EmbedFactory.success("Reviewer Role Added", f"Added reviewer role {role.mention}"),
            ephemeral=True
        )

    @config_group.command(name="remove-reviewer-role", description="Remove reviewer role for staff applications")
    async def remove_reviewer_role(self, interaction: discord.Interaction, role: discord.Role):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_template_manage(interaction.user, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "Only admins or creators can remove reviewer roles."),
                ephemeral=True
            )
            return

        roles = [r for r in config.get("reviewer_roles", []) if r != role.id]
        await self.db.upsert_staff_app_config(interaction.guild.id, {
            "reviewer_roles": roles
        })
        await interaction.response.send_message(
            embed=EmbedFactory.success("Reviewer Role Removed", f"Removed reviewer role {role.mention}"),
            ephemeral=True
        )

    @config_group.command(name="show", description="Show staff application config")
    async def show_config(self, interaction: discord.Interaction):
        config = await self._get_config(interaction.guild.id)
        creator_roles = config.get("creator_roles", [])
        reviewer_roles = config.get("reviewer_roles", [])
        default_apply_channel_id = config.get("default_apply_channel_id")

        def format_roles(ids: List[int]) -> str:
            return ", ".join(f"<@&{r}>" for r in ids) if ids else "Not set"

        embed = EmbedFactory.create(
            title="Staff Application Config",
            color=EmbedColor.INFO,
            fields=[
                {"name": "Creator Roles", "value": format_roles(creator_roles), "inline": False},
                {"name": "Reviewer Roles", "value": format_roles(reviewer_roles), "inline": False},
                {"name": "Default Apply Channel", "value": f"<#{default_apply_channel_id}>" if default_apply_channel_id else "Not set", "inline": False},
            ]
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # Template commands
    @template_group.command(name="create", description="Create a staff application template")
    @app_commands.describe(
        name="Template name",
        team_role="Role for the team (optional)",
        apply_channel="Channel to post the Apply panel (optional; uses default if not set)",
        review_channel="Channel where applications are sent for review",
        description="Description shown on the apply panel (supports \\n for new lines)"
    )
    async def template_create(
        self,
        interaction: discord.Interaction,
        name: str,
        apply_channel: Optional[discord.TextChannel],
        review_channel: discord.TextChannel,
        team_role: Optional[discord.Role] = None,
        description: Optional[str] = None
    ):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_template_manage(interaction.user, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot create staff application templates."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.STAFFAPP_TEMPLATE_MANAGE, "template-create")
            return

        await interaction.response.defer(ephemeral=True)

        fields = self._default_fields()
        if len(fields) > 5:
            await interaction.followup.send(
                embed=EmbedFactory.error("Invalid Template", "Templates cannot have more than 5 fields."),
                ephemeral=True
            )
            return

        description_text = (description or "Click Apply to submit your application.").replace("\\n", "\n")
        apply_channel_id = apply_channel.id if apply_channel else config.get("default_apply_channel_id")
        if not apply_channel_id:
            await interaction.followup.send(
                embed=EmbedFactory.error("Missing Apply Channel", "Set a default apply channel or provide one in the command."),
                ephemeral=True
            )
            return

        resolved_apply_channel = apply_channel or interaction.guild.get_channel(apply_channel_id)
        if resolved_apply_channel is None:
            await interaction.followup.send(
                embed=EmbedFactory.error("Apply Channel Not Found", "Could not resolve the apply channel."),
                ephemeral=True
            )
            return

        template = StaffApplicationTemplate(
            guild_id=interaction.guild.id,
            template_id="",
            name=name,
            description=description_text,
            team_role_id=team_role.id if team_role else None,
            apply_channel_id=apply_channel_id,
            review_channel_id=review_channel.id,
            fields=fields,
            created_by_id=interaction.user.id
        )

        template_id = await self.db.create_staff_template(template.to_dict())
        template.template_id = template_id

        view = StaffApplyView(self, template_id)
        panel_embed = EmbedFactory.create(
            title=f"{template.name} Applications",
            description=template.description,
            color=EmbedColor.PRIMARY,
            footer="Click Apply to submit your application."
        )

        await resolved_apply_channel.send(embed=panel_embed, view=view)
        self.bot.add_view(view)

        await interaction.followup.send(
            embed=EmbedFactory.success(
                "Template Created",
                f"Created application panel in {resolved_apply_channel.mention} for **{template.name}**."
            ),
            ephemeral=True
        )

    @template_group.command(name="list", description="List staff application templates")
    async def template_list(self, interaction: discord.Interaction):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_template_manage(interaction.user, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot view templates."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.STAFFAPP_TEMPLATE_MANAGE, "template-list")
            return

        templates = await self.db.list_staff_templates(interaction.guild.id)
        if not templates:
            await interaction.response.send_message(
                embed=EmbedFactory.info("No Templates", "No staff application templates found."),
                ephemeral=True
            )
            return

        description = ""
        for tpl in templates:
            description += (
                f"**ID:** {tpl.get('template_id')}\n"
                f"**Name:** {tpl.get('name')}\n"
                f"**Apply:** <#{tpl.get('apply_channel_id')}>\n"
                f"**Review:** <#{tpl.get('review_channel_id')}>\n"
                f"**Active:** {tpl.get('is_active', True)}\n\n"
            )

        embed = EmbedFactory.create(
            title="Staff Application Templates",
            description=description,
            color=EmbedColor.INFO
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @template_group.command(name="enable", description="Enable a staff application template")
    async def template_enable(self, interaction: discord.Interaction, template_id: str):
        await self._set_template_active(interaction, template_id, True)

    @template_group.command(name="disable", description="Disable a staff application template")
    async def template_disable(self, interaction: discord.Interaction, template_id: str):
        await self._set_template_active(interaction, template_id, False)

    async def _set_template_active(self, interaction: discord.Interaction, template_id: str, is_active: bool):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_template_manage(interaction.user, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot modify templates."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.STAFFAPP_TEMPLATE_MANAGE, "template-toggle")
            return

        updated = await self.db.set_staff_template_active(interaction.guild.id, template_id, is_active)
        if not updated:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", "Template not found."),
                ephemeral=True
            )
            return

        status_text = "enabled" if is_active else "disabled"
        await interaction.response.send_message(
            embed=EmbedFactory.success("Template Updated", f"Template {template_id} {status_text}."),
            ephemeral=True
        )

    # Queue and status commands
    @staffapp.command(name="queue", description="List staff applications")
    @app_commands.describe(
        team_role="Filter by team role",
        status="Filter by status"
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="interview", value="interview"),
            app_commands.Choice(name="accepted", value="accepted"),
            app_commands.Choice(name="rejected", value="rejected"),
        ]
    )
    async def queue(
        self,
        interaction: discord.Interaction,
        team_role: Optional[discord.Role] = None,
        status: Optional[app_commands.Choice[str]] = None
    ):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_review(interaction.user, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot view the application queue."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.STAFFAPP_REVIEW, "queue")
            return

        filters: Dict[str, Any] = {}
        if team_role:
            filters["team_role_id"] = team_role.id
        if status:
            filters["status"] = status.value
        else:
            filters["status"] = "pending"

        applications = await self.db.query_staff_applications(interaction.guild.id, **filters)
        if not applications:
            await interaction.response.send_message(
                embed=EmbedFactory.info("No Applications", "No applications match that filter."),
                ephemeral=True
            )
            return

        lines = []
        for app in applications[:25]:
            jump_link = ""
            if app.get("review_channel_id") and app.get("review_message_id"):
                jump_link = f"[Jump](https://discord.com/channels/{interaction.guild.id}/{app['review_channel_id']}/{app['review_message_id']})"
            team_label = f"<@&{app['team_role_id']}>" if app.get("team_role_id") else "N/A"
            lines.append(
                f"ID: `{app['application_id']}` — <@{app['applicant_id']}> — {team_label} — {app.get('status','pending')} — {jump_link}"
            )

        embed = EmbedFactory.create(
            title="Application Queue",
            description="\n".join(lines),
            color=EmbedColor.INFO
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staffapp.command(name="set-status", description="Set application status manually")
    @app_commands.describe(
        application_id="Application ID",
        status="New status",
        notes="Optional notes"
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="interview", value="interview"),
            app_commands.Choice(name="accepted", value="accepted"),
            app_commands.Choice(name="rejected", value="rejected"),
        ]
    )
    async def set_status(
        self,
        interaction: discord.Interaction,
        application_id: str,
        status: app_commands.Choice[str],
        notes: Optional[str] = None
    ):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_review(interaction.user, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot update application status."),
                ephemeral=True
            )
            await self._maybe_log_denial(interaction, FeatureKey.STAFFAPP_REVIEW, "set-status")
            return

        await self._handle_status_update(
            interaction=interaction,
            application_id=application_id,
            new_status=status.value,
            reviewer=interaction.user,
            notes=notes
        )

    async def _handle_status_button(self, interaction: discord.Interaction, new_status: str, application_id: str):
        config = await self._get_config(interaction.guild.id)
        if not await self._check_review(interaction.user, config):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=EmbedFactory.error("No Permission", "You cannot review staff applications."),
                    ephemeral=True
                )
            await self._maybe_log_denial(interaction, FeatureKey.STAFFAPP_REVIEW, "review-button")
            return

        await self._handle_status_update(
            interaction=interaction,
            application_id=application_id,
            new_status=new_status,
            reviewer=interaction.user,
            notes=None
        )

    async def _handle_status_update(
        self,
        interaction: discord.Interaction,
        application_id: str,
        new_status: str,
        reviewer: discord.Member,
        notes: Optional[str]
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Guild Only", "Status updates must be done in a server."),
                ephemeral=True
            )
            return

        config = await self._get_config(guild.id)
        if not self._is_reviewer(reviewer, config):
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Permission", "You cannot update application status."),
                ephemeral=True
            )
            return

        application_data = await self.db.get_staff_application(guild.id, application_id)
        if not application_data:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", "Application not found."),
                ephemeral=True
            )
            return

        template_data = await self.db.get_staff_template(guild.id, application_data["template_id"])
        if not template_data:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", "Template not found."),
                ephemeral=True
            )
            return

        application = StaffApplication.from_dict(application_data)
        template = StaffApplicationTemplate.from_dict(template_data)

        await self._apply_status_update(guild, application, template, new_status, reviewer, notes)

        if interaction.response.is_done():
            await interaction.followup.send(
                embed=EmbedFactory.success("Status Updated", f"Application {application_id} -> {new_status}"),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.success("Status Updated", f"Application {application_id} -> {new_status}"),
                ephemeral=True
            )

    async def _apply_status_update(
        self,
        guild: discord.Guild,
        application: StaffApplication,
        template: StaffApplicationTemplate,
        status: str,
        reviewer: discord.Member,
        notes: Optional[str]
    ):
        updated_at = datetime.utcnow()
        await self.db.update_staff_application(
            guild.id,
            application.application_id,
            {
                "status": status,
                "reviewed_by_id": reviewer.id,
                "review_notes": notes,
                "updated_at": updated_at,
            }
        )

        application.status = status
        application.reviewed_by_id = reviewer.id
        application.review_notes = notes
        application.updated_at = updated_at

        await self._edit_review_message(guild, application, template)
        await self._notify_applicant_status(reviewer, template, application, status, notes)

    async def _post_review_embed(
        self,
        guild: discord.Guild,
        application: StaffApplication,
        template: StaffApplicationTemplate
    ) -> Optional[int]:
        channel = guild.get_channel(template.review_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(template.review_channel_id)
            except discord.HTTPException:
                logger.warning(f"Review channel {template.review_channel_id} not accessible in {guild.id}")
                return None

        embed = self._build_review_embed(guild, application, template)
        view = StaffApplicationReviewView(self, application.application_id, disabled=False)
        try:
            message = await channel.send(embed=embed, view=view)
            self.bot.add_view(view)
            return message.id
        except discord.Forbidden:
            logger.warning(f"Cannot send review embed to channel {channel} in guild {guild.id}")
            return None

    def _build_review_embed(
        self,
        guild: discord.Guild,
        application: StaffApplication,
        template: StaffApplicationTemplate
    ) -> discord.Embed:
        applicant = guild.get_member(application.applicant_id)
        applicant_value = applicant.mention if applicant else f"<@{application.applicant_id}>"
        team_value = f"<@&{application.team_role_id}>" if application.team_role_id else template.name

        fields = [
            {"name": "Applicant", "value": applicant_value, "inline": True},
            {"name": "Team", "value": team_value, "inline": True},
        ]
        for answer in application.answers:
            fields.append({"name": answer.label, "value": _truncate(answer.value), "inline": False})

        fields.append({"name": "Status", "value": application.status.title(), "inline": True})
        if application.reviewed_by_id:
            fields.append({"name": "Reviewed by", "value": f"<@{application.reviewed_by_id}>", "inline": True})
        if application.review_notes:
            fields.append({"name": "Notes", "value": application.review_notes, "inline": False})
        fields.append({"name": "Application ID", "value": application.application_id, "inline": False})

        return EmbedFactory.create(
            title=f"New Application - {template.name}",
            description=template.description,
            color=EmbedColor.INFO,
            fields=fields
        )

    async def _edit_review_message(
        self,
        guild: discord.Guild,
        application: StaffApplication,
        template: StaffApplicationTemplate
    ):
        if not application.review_channel_id or not application.review_message_id:
            return

        channel = guild.get_channel(application.review_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(application.review_channel_id)
            except discord.HTTPException:
                logger.warning(f"Cannot fetch review channel {application.review_channel_id} in {guild.id}")
                return

        try:
            message = await channel.fetch_message(application.review_message_id)
        except discord.HTTPException:
            logger.warning(f"Cannot fetch review message {application.review_message_id} in {channel}")
            return

        embed = self._build_review_embed(guild, application, template)
        disabled = application.status in ["accepted", "rejected"]
        view = StaffApplicationReviewView(self, application.application_id, disabled=disabled)
        await message.edit(embed=embed, view=view)
        self.bot.add_view(view)

    async def _notify_applicant_status(
        self,
        actor: discord.Member,
        template: StaffApplicationTemplate,
        application: StaffApplication,
        status: str,
        notes: Optional[str]
    ):
        try:
            user = actor if actor.id == application.applicant_id else await self.bot.fetch_user(application.applicant_id)
        except Exception:
            user = None

        if not user:
            return

        try:
            if status == "pending":
                content = f"Your application for **{template.name}** has been submitted."
            elif status == "interview":
                content = f"You've been moved to interview for **{template.name}**. Staff will contact you."
            elif status == "accepted":
                content = f"You've been accepted for **{template.name}**! Welcome aboard."
            elif status == "rejected":
                content = "We are not proceeding with your application at this time."
                if notes:
                    content += f"\n\nNotes: {notes}"
            else:
                content = f"Your application status is now **{status}**."

            await user.send(content)
        except discord.Forbidden:
            logger.debug(f"Could not DM applicant {application.applicant_id}")
        except Exception as e:
            logger.warning(f"Failed to notify applicant {application.applicant_id}: {e}")

    async def _fetch_template(self, guild: Optional[discord.Guild], template_id: str) -> Optional[StaffApplicationTemplate]:
        if guild is None:
            return None
        data = await self.db.get_staff_template(guild.id, template_id)
        if not data:
            return None
        template = StaffApplicationTemplate.from_dict(data)
        if not template.is_active:
            return None
        return template


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    cog = StaffApplications(bot, bot.db, bot.config)
    await bot.add_cog(cog)

    existing = bot.tree.get_command("staffapp")
    if existing:
        bot.tree.remove_command("staffapp", type=discord.AppCommandType.chat_input)
    bot.tree.add_command(cog.staffapp)

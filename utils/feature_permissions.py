"""
Feature permission manager for fine-grained allow/deny checks.
"""

import logging
from typing import Callable, Optional, Dict, Any

import discord

from database.db_manager import DatabaseManager
from database.models import FeatureKey, FeaturePermissionAudit
from utils.denials import DenialLogger

# Sensitive features that require security bootstrap acknowledgment.
SENSITIVE_FEATURES = {
    FeatureKey.MOD_BAN,
    FeatureKey.MOD_KICK,
    FeatureKey.MOD_TIMEOUT,
    FeatureKey.MOD_CLEAR,
    FeatureKey.MOD_LOCK,
    FeatureKey.MOD_SLOWMODE,
    FeatureKey.MOD_VC_SUSPEND,
    FeatureKey.MOD_VC_UNSUSPEND,
    FeatureKey.TICKETS_ADMIN,
    FeatureKey.STAFFAPP_TEMPLATE_MANAGE,
}
from utils.security import get_or_bootstrap_security

logger = logging.getLogger(__name__)


class FeaturePermissionManager:
    """Manages feature-level allow/deny checks with audit support."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.denials = DenialLogger()

    async def security_ready(self, guild: discord.Guild) -> bool:
        """Return True if security bootstrap initialized."""
        security = await get_or_bootstrap_security(self.db, guild)
        return bool(security.get("initialized", False))

    async def check(
        self,
        member: discord.Member,
        feature_key: FeatureKey,
        base_check: Callable[[discord.Member], bool],
    ) -> bool:
        """
        Evaluate whether a member can use a feature.

        Rules:
        1) Owner/Admin always allowed.
        2) Base check must pass (Discord perms, local logic).
        3) Sensitive features require security bootstrap to be initialized.
        4) If no feature doc -> allow.
        5) Deny if member has any denied role.
        6) If allowed_roles empty -> allow.
        7) Else require at least one allowed role.
        """
        if member.guild is None:
            return False

        if member.guild_permissions.administrator or member == member.guild.owner:
            return True

        if not base_check(member):
            return False

        # Sensitive features locked until security bootstrap completes
        if feature_key in SENSITIVE_FEATURES:
            security = await get_or_bootstrap_security(self.db, member.guild)
            if not security.get("initialized", False):
                if self.denials and hasattr(self.denials, "should_log"):
                    if self.denials.should_log(member.guild.id, member.id, "feature_perms", feature_key.value):
                        logger.warning(
                            "Sensitive feature %s denied because guild security not initialized for guild %s",
                            feature_key.value,
                            member.guild.id,
                        )
                return False

        doc = await self.db.get_feature_permission(member.guild.id, feature_key.value)
        if not doc:
            return True

        member_role_ids = {r.id for r in member.roles}

        denied_roles = set(doc.get("denied_roles", []))
        if member_role_ids.intersection(denied_roles):
            return False

        allowed_roles = set(doc.get("allowed_roles", []))
        if not allowed_roles:
            return True

        return bool(member_role_ids.intersection(allowed_roles))

    async def audit_change(
        self,
        guild_id: int,
        feature_key: FeatureKey,
        changed_by: int,
        change_type: str,
        role_id: Optional[int],
        old_doc: Optional[Dict[str, Any]],
        new_doc: Optional[Dict[str, Any]],
    ) -> None:
        """Insert audit entry for permission change."""
        audit = FeaturePermissionAudit(
            guild_id=guild_id,
            feature_key=feature_key.value,
            changed_by=changed_by,
            change_type=change_type,
            role_id=role_id,
            old_doc=old_doc or {},
            new_doc=new_doc or {}
        )
        try:
            await self.db.add_feature_permission_audit(audit.to_dict())
        except Exception as e:
            logger.warning(f"Failed to record feature permission audit for {feature_key}: {e}")

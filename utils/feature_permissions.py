"""
Feature permission manager for fine-grained allow/deny checks.
"""

import logging
from typing import Callable, Optional, Dict, Any

import discord

from database.db_manager import DatabaseManager
from database.models import FeatureKey, FeaturePermissionAudit

logger = logging.getLogger(__name__)


class FeaturePermissionManager:
    """Manages feature-level allow/deny checks with audit support."""

    def __init__(self, db: DatabaseManager):
        self.db = db

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
        3) If no feature doc -> allow.
        4) Deny if member has any denied role.
        5) If allowed_roles empty -> allow.
        6) Else require at least one allowed role.
        """
        if member.guild is None:
            return False

        if member.guild_permissions.administrator or member == member.guild.owner:
            return True

        if not base_check(member):
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

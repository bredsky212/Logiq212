"""
Security helpers for protected roles and guild security bootstrap.
"""

import logging
from typing import Dict, List, Optional

import discord

from database.db_manager import DatabaseManager
from database.models import GuildSecurityConfig

logger = logging.getLogger(__name__)


class GuildSecurityCache:
    """Lightweight per-guild cache to avoid repeated DB hits."""

    def __init__(self):
        self._cache: Dict[int, Dict] = {}

    def get(self, guild_id: int) -> Optional[Dict]:
        return self._cache.get(guild_id)

    def set(self, guild_id: int, data: Dict) -> None:
        self._cache[guild_id] = data


security_cache = GuildSecurityCache()


async def _detect_default_protected_roles(guild: discord.Guild) -> List[int]:
    """Detect roles with administrator or manage_guild permissions."""
    protected = []
    for role in guild.roles:
        perms = role.permissions
        if perms.administrator or perms.manage_guild:
            protected.append(role.id)
    return protected


async def get_or_bootstrap_security(db: DatabaseManager, guild: discord.Guild) -> Dict:
    """Fetch guild security config, bootstrapping protected roles if missing."""
    cached = security_cache.get(guild.id)
    if cached:
        return cached

    existing = await db.get_guild_security(guild.id)
    if existing:
        security_cache.set(guild.id, existing)
        return existing

    protected_roles = await _detect_default_protected_roles(guild)
    config = GuildSecurityConfig(
        guild_id=guild.id,
        protected_role_ids=protected_roles,
        initialized=False,
    )
    try:
        stored = await db.upsert_guild_security(guild.id, config.to_dict())
    except Exception as exc:
        logger.warning("Failed to bootstrap guild security for %s: %s", guild.id, exc)
        stored = config.to_dict()

    security_cache.set(guild.id, stored)
    return stored


async def is_protected_member(db: DatabaseManager, guild: discord.Guild, member: discord.Member) -> bool:
    """Determine if member is protected: guild owner or has protected roles."""
    if member == guild.owner:
        return True

    security = await get_or_bootstrap_security(db, guild)
    protected_ids = set(security.get("protected_role_ids", []))
    return any(role.id in protected_ids for role in member.roles)


async def filter_protected_roles(db: DatabaseManager, guild: discord.Guild, roles: List[discord.Role]) -> List[discord.Role]:
    """Remove protected roles from a list before assignment/removal."""
    security = await get_or_bootstrap_security(db, guild)
    protected_ids = set(security.get("protected_role_ids", []))
    return [role for role in roles if role.id not in protected_ids]

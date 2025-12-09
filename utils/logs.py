"""
Shared helpers for resolving guild log channels with per-purpose overrides.
"""

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)


async def resolve_log_channel(db, guild: discord.Guild, purpose: str = "default") -> Optional[discord.abc.Messageable]:
    """
    Resolve the log channel for a given purpose.
    Order:
      1) guild_config.log_channels[purpose]
      2) guild_config.log_channel (legacy)
    """
    guild_config = await db.get_guild(guild.id)
    if not guild_config:
        return None

    log_channels = guild_config.get("log_channels", {}) or {}
    channel_id = log_channels.get(purpose) or guild_config.get("log_channel")
    if not channel_id:
        return None

    channel = guild.get_channel(channel_id)
    if channel:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except discord.HTTPException:
        logger.warning("Failed to fetch log channel %s for guild %s", channel_id, guild.id)
        return None


async def set_log_channel(db, guild_id: int, purpose: str, channel_id: int) -> None:
    """Update log_channels map for a guild."""
    await db.update_guild(
        guild_id,
        {"log_channels." + purpose: channel_id}
    )

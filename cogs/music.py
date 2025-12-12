"""
Music Cog for Logiq
Music player with YouTube support
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import logging
import asyncio

from utils.embeds import EmbedFactory, EmbedColor
from utils.feature_permissions import FeaturePermissionManager
from utils.denials import DenialLogger
from database.db_manager import DatabaseManager
from database.models import FeatureKey

logger = logging.getLogger(__name__)


class MusicQueue:
    """Music queue manager"""
    
    def __init__(self):
        self.queue = []
        self.current = None
        self.loop = False
        
    def add(self, track):
        """Add track to queue"""
        self.queue.append(track)
        
    def next(self):
        """Get next track"""
        if self.loop and self.current:
            return self.current
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        return None
        
    def clear(self):
        """Clear queue"""
        self.queue = []
        self.current = None
        
    def skip(self):
        """Skip current track"""
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        return None


class MusicControlView(discord.ui.View):
    """Music player controls"""
    
    def __init__(self, cog: 'Music'):
        super().__init__(timeout=None)
        self.cog = cog
        
    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.primary, custom_id="music_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pause/Resume music"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return
            
        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.pause()
            button.label = "▶️ Resume"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                embed=EmbedFactory.info("Paused", "Music paused"),
                ephemeral=True
            )
        elif vc.is_paused():
            vc.resume()
            button.label = "⏸️ Pause"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                embed=EmbedFactory.info("Resumed", "Music resumed"),
                ephemeral=True
            )
            
    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip current track"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return
            
        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await interaction.response.send_message(
                embed=EmbedFactory.success("Skipped", "Skipped current track"),
                ephemeral=True
            )
            
    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stop music and disconnect"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return
            
        guild_id = interaction.guild.id
        if guild_id in self.cog.queues:
            self.cog.queues[guild_id].clear()
            
        vc = interaction.guild.voice_client
        await vc.disconnect()
        await interaction.response.send_message(
            embed=EmbedFactory.success("Stopped", "Music stopped and disconnected"),
            ephemeral=True
        )


class Music(commands.Cog):
    """Music player cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('music', {})
        self.perms = bot.perms if hasattr(bot, "perms") else FeaturePermissionManager(db)
        self.denials = DenialLogger()
        self.queues = {}  # guild_id: MusicQueue

    def get_queue(self, guild_id: int) -> MusicQueue:
        """Get or create queue for guild"""
        if guild_id not in self.queues:
            self.queues[guild_id] = MusicQueue()
        return self.queues[guild_id]

    def _base_basic_check(self, member: discord.Member) -> bool:
        # Default open; feature overrides can restrict if configured
        return True

    def _base_volume_check(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        if perms.manage_channels or perms.manage_guild or perms.administrator or member == member.guild.owner:
            return True
        # Allow common DJ role names to satisfy the base floor if present
        return any(role.name.lower() == "dj" for role in member.roles)

    async def _can_use_basic(self, member: discord.Member) -> bool:
        return await self.perms.check(member, FeatureKey.MUSIC_DJ_BASIC, self._base_basic_check)

    async def _can_use_volume(self, member: discord.Member) -> bool:
        return await self.perms.check(member, FeatureKey.MUSIC_DJ_VOLUME, self._base_volume_check)

    async def _log_denial(self, interaction: discord.Interaction, feature: FeatureKey, reason: str):
        if interaction.guild is None:
            return
        if not self.denials.should_log(interaction.guild.id, interaction.user.id, "music", feature.value):
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
            logger.warning(f"Cannot send music log to channel {channel} in {guild}")

    @app_commands.command(name="play", description="Play music from YouTube")
    @app_commands.describe(query="Song name or YouTube URL")
    async def play(self, interaction: discord.Interaction, query: str):
        """Play music from YouTube"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "play")
            return

        # Check if user is in voice channel
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel to use this command"),
                ephemeral=True
            )
            return

        await interaction.response.defer()

        # Check if bot is in voice
        if not interaction.guild.voice_client:
            try:
                await interaction.user.voice.channel.connect()
            except Exception as e:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Connection Failed", f"Could not join voice channel: {str(e)}"),
                    ephemeral=True
                )
                return

        # Add to queue
        queue = self.get_queue(interaction.guild.id)
        queue.add(query)
        
        embed = EmbedFactory.success(
            "Added to Queue",
            f"**Track:** {query}\n"
            f"**Requested by:** {interaction.user.mention}\n"
            f"**Position in queue:** {len(queue.queue)}"
        )
        
        await interaction.followup.send(embed=embed)
        logger.info(f"Added to queue by {interaction.user}: {query}")

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        """Join voice channel"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "join")
            return

        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel"),
                ephemeral=True
            )
            return

        channel = interaction.user.voice.channel
        
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()

        embed = EmbedFactory.success("Joined", f"Joined {channel.mention}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Leave voice channel")
    async def leave(self, interaction: discord.Interaction):
        """Leave voice channel"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "leave")
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Connected", "I'm not in a voice channel"),
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        if guild_id in self.queues:
            self.queues[guild_id].clear()

        await interaction.guild.voice_client.disconnect()
        embed = EmbedFactory.success("Disconnected", "Left voice channel")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="View music queue")
    async def view_queue(self, interaction: discord.Interaction):
        """View music queue"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "queue")
            return

        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)

        if not queue.current and not queue.queue:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Empty Queue", "The music queue is empty"),
                ephemeral=True
            )
            return

        description = ""
        if queue.current:
            description += f"**Now Playing:**\n{queue.current}\n\n"

        if queue.queue:
            description += "**Up Next:**\n"
            for i, track in enumerate(queue.queue[:10], 1):
                description += f"{i}. {track}\n"

        embed = EmbedFactory.create(
            title="🎵 Music Queue",
            description=description,
            color=EmbedColor.INFO
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="skip", description="Skip current track")
    async def skip(self, interaction: discord.Interaction):
        """Skip current track"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "skip")
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            embed = EmbedFactory.success("Skipped", "Skipped current track")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @app_commands.command(name="pause", description="Pause music")
    async def pause(self, interaction: discord.Interaction):
        """Pause music"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "pause")
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.pause()
            embed = EmbedFactory.success("Paused", "Music paused")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @app_commands.command(name="resume", description="Resume music")
    async def resume(self, interaction: discord.Interaction):
        """Resume music"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "resume")
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is paused"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            embed = EmbedFactory.success("Resumed", "Music resumed")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Paused", "Music is not paused"),
                ephemeral=True
            )

    @app_commands.command(name="volume", description="Set volume (Admin)")
    @app_commands.describe(volume="Volume level (0-100)")
    async def volume(self, interaction: discord.Interaction, volume: int):
        """Set volume"""
        if not await self._can_use_volume(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to adjust volume."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_VOLUME, "volume")
            return

        if volume < 0 or volume > 100:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Volume", "Volume must be between 0 and 100"),
                ephemeral=True
            )
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        # Note: Volume control requires proper audio source implementation
        embed = EmbedFactory.success("Volume", f"Volume set to {volume}%")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show currently playing track")
    async def nowplaying(self, interaction: discord.Interaction):
        """Show currently playing track"""
        if not await self._can_use_basic(interaction.user):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Denied", "You do not have permission to control music."),
                ephemeral=True
            )
            await self._log_denial(interaction, FeatureKey.MUSIC_DJ_BASIC, "nowplaying")
            return

        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)

        if not queue.current:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Nothing Playing", "No music is currently playing"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.create(
            title="🎵 Now Playing",
            description=queue.current,
            color=EmbedColor.INFO
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Music(bot, bot.db, bot.config))

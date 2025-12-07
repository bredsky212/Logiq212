"""
Logiq - Main Entry Point
AI-Enhanced Discord Bot for Community Management
"""

import discord
from discord.ext import commands
import asyncio
import logging
import os
import sys
from pathlib import Path
import yaml
from dotenv import load_dotenv
from discord import app_commands
from discord.app_commands.errors import CommandSignatureMismatch

from database.db_manager import DatabaseManager
from utils.logger import BotLogger
from utils.embeds import EmbedColor

# Load environment variables
load_dotenv()


class Logiq(commands.Bot):
    """Custom bot class"""

    def __init__(self, config: dict):
        """Initialize bot"""
        # Setup intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True

        # Initialize bot
        super().__init__(
            command_prefix=config['bot']['prefix'],
            intents=intents,
            help_command=None
        )

        self.config = config
        self.start_time = discord.utils.utcnow()

        # Setup logging
        self.logger = BotLogger(config.get('logging', {}))

        # Setup database
        db_config = config.get('database', {})
        mongodb_uri = os.getenv('MONGODB_URI', db_config.get('mongodb_uri', 'mongodb://localhost:27017'))
        database_name = db_config.get('database_name', 'Logiq')
        pool_size = db_config.get('pool_size', 10)

        self.db = DatabaseManager(mongodb_uri, database_name, pool_size)
        self.perms = None  # initialized in setup_hook

    async def setup_hook(self):
        """Setup hook - called when bot is starting"""
        self.logger.info("Starting Logiq...")

        # Connect to database
        try:
            await self.db.connect()
            self.logger.info("Database connected successfully")
            # Attach global feature permission manager
            from utils.feature_permissions import FeaturePermissionManager
            self.perms = FeaturePermissionManager(self.db)
        except Exception as e:
            self.logger.error(f"Failed to connect to database: {e}", exc_info=True)
            sys.exit(1)

        # Load cogs
        await self.load_cogs()

        async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            log = logging.getLogger("logiq.app_commands")
            qualified = getattr(interaction.command, "qualified_name", "unknown")

            if isinstance(error, CommandSignatureMismatch):
                log.warning(
                    "Command signature mismatch for %s; attempting re-sync",
                    qualified,
                )
                # Keep the interaction alive to avoid "Unknown interaction"
                if not interaction.response.is_done():
                    try:
                        await interaction.response.defer(ephemeral=True, thinking=True)
                    except Exception:
                        log.exception("Failed to defer interaction after signature mismatch")
                try:
                    if interaction.guild:
                        await self.tree.sync(guild=interaction.guild)
                    else:
                        await self.tree.sync()
                except Exception:
                    log.exception("Failed to resync after signature mismatch")
                try:
                    msg = "Les commandes ont été rafraîchies. Réessaie dans quelques secondes."
                    if interaction.response.is_done():
                        await interaction.followup.send(msg, ephemeral=True)
                    else:
                        await interaction.response.send_message(msg, ephemeral=True)
                except Exception:
                    log.exception("Failed to send signature-mismatch response")
                return

            log.error(
                "App command error in %s: %r",
                qualified,
                error,
                exc_info=True,
            )
            try:
                msg = "Une erreur interne est survenue lors de l'execution de cette commande."
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                log.exception("Failed to send app command error response")

        self.tree.on_error = on_tree_error

    @commands.command(name="sync")
    @commands.has_permissions(administrator=True)
    async def sync_commands(self, ctx: commands.Context, spec: str | None = None):
        """
        Admin/owner helper to resync application commands.

        Usage:
          !sync            -> global sync (can take time to propagate)
          !sync ~          -> sync this guild only
          !sync *          -> copy globals to this guild, then sync
          !sync ^          -> clear this guild's commands, then sync from globals
        """
        tree = self.tree
        guild = ctx.guild

        if spec == "~":
            synced = await tree.sync(guild=guild)
            await ctx.send(f"✅ Synced {len(synced)} commands to this guild (~).")
        elif spec == "*":
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            await ctx.send(f"✅ Copied globals and synced {len(synced)} commands to this guild (*).")
        elif spec == "^":
            tree.clear_commands(guild=guild)
            synced = await tree.sync(guild=guild)
            await ctx.send(f"✅ Cleared and synced {len(synced)} commands for this guild (^).")
        else:
            synced = await tree.sync()
            await ctx.send(f"✅ Globally synced {len(synced)} commands.")

    async def load_cogs(self):
        """Load all cogs from cogs directory"""
        cogs_dir = Path(__file__).parent / 'cogs'
        cog_files = [f.stem for f in cogs_dir.glob('*.py') if f.stem != '__init__']

        self.logger.info(f"Loading {len(cog_files)} cogs...")

        for cog in cog_files:
            try:
                await self.load_extension(f'cogs.{cog}')
                self.logger.cog_load(cog)
            except Exception as e:
                self.logger.error(f"Failed to load cog {cog}: {e}", exc_info=True)

        self.logger.info(f"Successfully loaded {len(self.cogs)} cogs")

    async def on_ready(self):
        """Called when bot is ready"""
        self.logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        self.logger.info(f"Connected to {len(self.guilds)} guilds")
        self.logger.info(f"Serving {sum(g.member_count for g in self.guilds)} users")

        # Set status
        activity_type = self.config['bot'].get('activity_type', 'watching')
        activity_text = self.config['bot'].get('activity', 'your community')

        activity_types = {
            'playing': discord.ActivityType.playing,
            'watching': discord.ActivityType.watching,
            'listening': discord.ActivityType.listening,
            'streaming': discord.ActivityType.streaming
        }

        activity = discord.Activity(
            type=activity_types.get(activity_type, discord.ActivityType.watching),
            name=activity_text
        )

        await self.change_presence(activity=activity, status=discord.Status.online)

        # Sync commands
        try:
            synced = await self.tree.sync()
            self.logger.info(f"Synced {len(synced)} commands")
        except Exception as e:
            self.logger.error(f"Failed to sync commands: {e}", exc_info=True)

        self.logger.info("Bot is ready!")

    async def on_command_error(self, ctx, error):
        """Global error handler"""
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to use this command.")
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing required argument: {error.param}")
            return

        self.logger.error(f"Command error: {error}", exc_info=True)

    async def on_error(self, event, *args, **kwargs):
        """Global error handler for events"""
        self.logger.error(f"Error in event {event}", exc_info=True)

    async def close(self):
        """Cleanup when bot is shutting down"""
        self.logger.info("Shutting down bot...")
        await self.db.disconnect()
        await super().close()

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        """Global handler for app command errors to avoid timeouts"""
        log = logging.getLogger("logiq.app_commands")
        log.error(
            "App command error in %s: %r",
            getattr(interaction.command, "qualified_name", "unknown"),
            error,
            exc_info=True,
        )
        try:
            msg = "Une erreur interne est survenue lors de l'execution de cette commande."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            log.error("Failed to send app command error response", exc_info=True)


def load_config(config_path: str = 'config.yaml') -> dict:
    """Load configuration from YAML file"""
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Replace environment variables
        def replace_env_vars(obj):
            """Recursively replace ${ENV_VAR} with actual values"""
            if isinstance(obj, dict):
                return {k: replace_env_vars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_env_vars(item) for item in obj]
            elif isinstance(obj, str) and obj.startswith('${') and obj.endswith('}'):
                env_var = obj[2:-1]
                return os.getenv(env_var, obj)
            return obj

        return replace_env_vars(config)

    except FileNotFoundError:
        print(f"Error: Config file '{config_path}' not found")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing config file: {e}")
        sys.exit(1)


async def start_web_server(bot: Logiq):
    """Start web dashboard (if enabled)"""
    if not bot.config.get('web', {}).get('enabled', False):
        return

    try:
        from web.api import create_app
        import uvicorn

        app = create_app(bot)
        web_config = bot.config.get('web', {})
        host = web_config.get('host', '0.0.0.0')
        port = web_config.get('port', 8000)

        # Run in background
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)

        bot.logger.info(f"Starting web server on {host}:{port}")
        await server.serve()

    except ImportError:
        bot.logger.warning("Web server dependencies not installed. Skipping web server.")
    except Exception as e:
        bot.logger.error(f"Error starting web server: {e}", exc_info=True)


async def main():
    """Main entry point"""
    # Load configuration
    config = load_config()

    # Get bot token
    token = os.getenv('DISCORD_BOT_TOKEN', config['bot'].get('token'))
    if not token or token.startswith('${'):
        print("Error: DISCORD_BOT_TOKEN not set in environment variables or config.yaml")
        sys.exit(1)

    # Create and start bot
    bot = Logiq(config)

    async with bot:
        # Start web server in background if enabled
        if config.get('web', {}).get('enabled', False):
            bot.loop.create_task(start_web_server(bot))

        # Start bot
        await bot.start(token)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)

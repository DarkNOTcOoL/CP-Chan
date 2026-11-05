"""
main.py — Entry point for the Codeforces Discord Bot.

Responsibilities
----------------
• Defines CFBot, a subclass of commands.Bot.
• setup_hook: initialises the SQLite database, loads all cogs,
  and syncs slash commands to Discord.
• close: gracefully shuts down the shared aiohttp session.
"""

from __future__ import annotations

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import cf_api
from db import init_db

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

load_dotenv()

TOKEN:              str = os.getenv("DISCORD_BOT_TOKEN", "")
CODING_CHANNEL_ID:  int = int(os.getenv("CODING_CHANNEL_ID", "0"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cf-bot")

# ---------------------------------------------------------------------------
# Cog registry
# ---------------------------------------------------------------------------

COGS = [
    "cogs.solving",
    "cogs.link",
    "cogs.leaderboard",
    "cogs.stats",
    "cogs.tracker",
]


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class CFBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        # Made available to cogs via self.bot.coding_channel_id
        self.coding_channel_id: int = CODING_CHANNEL_ID

    async def setup_hook(self) -> None:
        """Called once after login, before the websocket connects."""
        # 1. Initialise DB tables
        await init_db()

        # 2. Load all cogs
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception:
                log.exception("Failed to load cog: %s", cog)

        # 3. Sync slash commands globally
        #    For faster testing in a single guild, replace with:
        #    await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))
        synced = await self.tree.sync()
        log.info("Synced %d slash command(s) globally.", len(synced))

    async def on_ready(self) -> None:
        log.info("━" * 50)
        log.info("Bot ready  : %s (ID %s)", self.user, self.user.id)
        log.info("Guilds     : %d", len(self.guilds))
        log.info("Channel ID : %s", CODING_CHANNEL_ID or "(not set)")
        log.info("━" * 50)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Codeforces 👀",
            )
        )

    async def close(self) -> None:
        await cf_api.close_session()
        await super().close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        log.error(
            "DISCORD_BOT_TOKEN is not set.\n"
            "Create a .env file with DISCORD_BOT_TOKEN=<your-token> and try again."
        )
        raise SystemExit(1)

    if not CODING_CHANNEL_ID:
        log.warning(
            "CODING_CHANNEL_ID is not set — solve announcements will be disabled.\n"
            "Add CODING_CHANNEL_ID=<channel_id> to your .env file to enable them."
        )

    bot = CFBot()
    bot.run(TOKEN, log_handler=None)

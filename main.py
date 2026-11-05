"""
main.py — Entry point for the Codeforces Discord Bot.

Responsibilities
----------------
• Defines CFBot, a subclass of commands.Bot.
• setup_hook: initialises the SQLite database, loads all cogs,
  and syncs slash commands to Discord.
• Hosts an internal aiohttp web server on PORT (default 8080) for
  Render/UptimeRobot health checks so the bot never sleeps on free tiers.
• close: gracefully shuts down the shared aiohttp session and web runner.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web
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
PORT:               int = int(os.getenv("PORT", "8080"))

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
        self.web_runner: web.AppRunner | None = None

    async def _start_health_server(self) -> None:
        """Starts a lightweight async web server for Render / UptimeRobot keep-alive."""
        async def handle_ping(request: web.Request) -> web.Response:
            bot_status = "online" if self.is_ready() else "starting"
            return web.json_response({
                "status": "healthy",
                "bot": bot_status,
                "latency_ms": round(self.latency * 1000, 2) if self.is_ready() else None
            })

        app = web.Application()
        app.router.add_get("/", handle_ping)
        app.router.add_get("/health", handle_ping)

        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "0.0.0.0", PORT)
        await site.start()
        log.info("Health check server listening on http://0.0.0.0:%d", PORT)

    async def setup_hook(self) -> None:
        """Called once after login, before the websocket connects."""
        # 1. Initialise DB tables
        await init_db()

        # 2. Start HTTP health check server for Render/UptimeRobot
        await self._start_health_server()

        # 3. Load all cogs
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception:
                log.exception("Failed to load cog: %s", cog)

        # 4. Sync slash commands globally
        synced = await self.tree.sync()
        log.info("Synced %d slash command(s) globally.", len(synced))

    async def on_ready(self) -> None:
        log.info("━" * 50)
        log.info("Bot ready  : %s (ID %s)", self.user, self.user.id)
        log.info("Guilds     : %d", len(self.guilds))
        log.info("Channel ID : %s", CODING_CHANNEL_ID or "(not set)")
        log.info("Health Port: %d", PORT)
        log.info("━" * 50)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Codeforces 👀",
            )
        )

    async def close(self) -> None:
        if self.web_runner:
            await self.web_runner.cleanup()
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

"""
cogs/solving.py — /solving <problem_id> slash command.

Migrated from the original main.py into a Cog so the entry-point stays clean.
"""

from __future__ import annotations

import re
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import cf_api

log = logging.getLogger("cf-bot.solving")

EMBED_COLOR = 0x3B82F6
ERROR_COLOR  = 0xEF4444
CF_PROBLEM_URL = "https://codeforces.com/problemset/problem/{contest_id}/{index}"
CF_API_URL     = "https://codeforces.com/api/problemset.problems"

_PROBLEM_ID_RE = re.compile(r"^(\d+)([A-Za-z]\d*)$")


def _parse(problem_id: str) -> tuple[str, str] | None:
    m = _PROBLEM_ID_RE.match(problem_id.strip())
    return (m.group(1), m.group(2).upper()) if m else None


def _error_embed(title: str, desc: str) -> discord.Embed:
    return discord.Embed(
        title=f"❌ {title}", description=desc, color=ERROR_COLOR,
        timestamp=datetime.now(timezone.utc),
    ).set_footer(text="Codeforces • Keep Coding!")


class SolvingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="solving",
        description="Share a Codeforces problem you are currently solving.",
    )
    @app_commands.describe(problem_id="Problem ID, e.g. 213A or 1900B")
    async def solving(self, interaction: discord.Interaction, problem_id: str) -> None:
        await interaction.response.defer()

        parsed = _parse(problem_id)
        if not parsed:
            await interaction.followup.send(
                embed=_error_embed(
                    "Invalid Problem ID",
                    f"**`{problem_id}`** isn't a valid Codeforces problem ID.\n"
                    "Expected format: `<contestId><index>` — e.g. `213A`, `1900B`, `1234C1`.",
                ),
                ephemeral=True,
            )
            return

        contest_id, index = parsed

        import aiohttp
        try:
            session = await cf_api.get_session()
            async with session.get(
                "https://codeforces.com/api/problemset.problems",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
                data: dict = await resp.json(content_type=None)
        except Exception as exc:
            log.warning("CF API error in /solving: %s", exc)
            await interaction.followup.send(
                embed=_error_embed("API Unreachable", "Couldn't reach the Codeforces API. Try again shortly."),
                ephemeral=True,
            )
            return

        if data.get("status") != "OK":
            await interaction.followup.send(
                embed=_error_embed("API Error", f"`{data.get('comment', 'Unknown error')}`"),
                ephemeral=True,
            )
            return

        problems: list[dict] = data.get("result", {}).get("problems", [])
        matched = next(
            (
                p for p in problems
                if str(p.get("contestId")) == contest_id
                and p.get("index", "").upper() == index
            ),
            None,
        )

        if not matched:
            await interaction.followup.send(
                embed=_error_embed(
                    "Problem Not Found",
                    f"No problem found for **`{contest_id}{index}`** in the Codeforces problem set.",
                ),
                ephemeral=True,
            )
            return

        name: str = matched.get("name", "Unknown")
        rating: int | None = matched.get("rating")
        tags: list[str] = matched.get("tags", [])
        url = CF_PROBLEM_URL.format(contest_id=contest_id, index=index)

        embed = discord.Embed(
            title=f"{contest_id}{index} — {name}",
            url=url,
            color=EMBED_COLOR,
            timestamp=datetime.now(timezone.utc),
        )

        # Difficulty with coloured indicator
        if rating is not None:
            if rating < 1200:
                diff = f"🟢 {rating}"
            elif rating < 1900:
                diff = f"🔵 {rating}"
            elif rating < 2400:
                diff = f"🟡 {rating}"
            else:
                diff = f"🔴 {rating}"
        else:
            diff = "N/A"

        embed.add_field(name="Difficulty", value=diff, inline=True)
        embed.add_field(
            name="Tags",
            value=", ".join(f"`{t}`" for t in tags) or "*(none)*",
            inline=False,
        )
        embed.add_field(
            name="Contest",
            value=f"[#{contest_id}](https://codeforces.com/contest/{contest_id})",
            inline=True,
        )
        embed.set_footer(text="Codeforces • Keep Coding!")

        content = (
            f"🚀 {interaction.user.mention} is currently solving "
            f"**[{contest_id}{index} — {name}]({url})**"
        )
        await interaction.followup.send(content=content, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SolvingCog(bot))


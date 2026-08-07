"""Organizer-only Live Arena workbook diagnostic command."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from shared.config import cfg
from shared.theme import colors

from modules.community.live_arena.service import (
    LiveArenaConfigError,
    TournamentSnapshot,
    load_tournament_snapshot,
)

log = logging.getLogger("c1c.community.live_arena")


def build_check_embed(snapshot: TournamentSnapshot) -> discord.Embed:
    embed = discord.Embed(
        title="Live Arena tournament check", colour=discord.Colour.green()
    )
    values = (
        ("Tournament", snapshot.tournament_name),
        ("Tournament ID", snapshot.tournament_id),
        ("Status", snapshot.status),
        ("Eligibility scope", snapshot.eligibility_scope),
        ("Active eligible clans", str(snapshot.active_eligible_clans)),
        ("Enabled availability windows", str(snapshot.enabled_availability_windows)),
        ("Participants", f"{snapshot.min_participants}–{snapshot.max_participants}"),
        ("Signup opens", snapshot.signup_opens_at_utc or "Not configured"),
        ("Signup closes", snapshot.signup_closes_at_utc or "Not configured"),
        ("Configuration", "OK"),
    )
    for name, value in values:
        embed.add_field(name=name, value=value, inline=False)
    return embed


def build_error_embed(message: str) -> discord.Embed:
    embed = discord.Embed(
        title="Live Arena tournament check", colour=discord.Colour.red()
    )
    embed.add_field(name="Configuration", value="Invalid", inline=False)
    embed.add_field(name="Error", value=message, inline=False)
    return embed


def build_message_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="Live Arena tournament",
        description=message,
        colour=colors.c1c_blue,
    )


class LiveArenaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        if not self._sheet_id():
            log.info(
                "📋 Live Arena — disabled • reason=LIVE_ARENA_TOURNAMENT_SHEET_ID not configured"
            )

    @staticmethod
    def _sheet_id() -> str:
        return str(cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or "").strip()

    @commands.group(name="latournament", invoke_without_command=True)
    async def latournament(self, ctx: commands.Context) -> None:
        await ctx.send(embed=build_message_embed("Usage: `!latournament check`"))

    @latournament.command(name="check")
    async def check(self, ctx: commands.Context) -> None:
        sheet_id = self._sheet_id()
        if not sheet_id:
            await ctx.send(
                embed=build_error_embed(
                    "LIVE_ARENA_TOURNAMENT_SHEET_ID is not configured"
                )
            )
            return
        try:
            snapshot = await load_tournament_snapshot(sheet_id)
        except LiveArenaConfigError as exc:
            log.error("❌ Live Arena check — invalid configuration • reason=%s", exc)
            await ctx.send(embed=build_error_embed(str(exc)))
            return

        role_ids = {
            getattr(role, "id", None) for role in getattr(ctx.author, "roles", ())
        }
        if snapshot.organizer_role_id not in role_ids:
            await ctx.send(
                embed=build_message_embed(
                    "You need the configured Live Arena organizer role to run this command."
                )
            )
            return
        await ctx.send(embed=build_check_embed(snapshot))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LiveArenaCog(bot))

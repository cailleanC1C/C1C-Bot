"""Persistent public panel lifecycle for Live Arena PR3."""

from __future__ import annotations

import asyncio
import logging
import os

import discord

from shared.sheets.async_core import acall_with_backoff, aget_worksheet

from .messages import discord_timestamp, load_messages, load_pr3_config
from .repository import LiveArenaRepository
from .service import load_tournament_snapshot

log = logging.getLogger("c1c.community.live_arena.panel")


class LiveArenaPanelManager:
    def __init__(self, bot, sheet_id: str, service_factory=None):
        self.bot = bot
        self.sheet_id = sheet_id
        self.service_factory = service_factory
        self._lock = asyncio.Lock()

    async def sync(self) -> None:
        async with self._lock:
            config, matrix = await load_pr3_config(self.sheet_id)
            messages = await load_messages(self.sheet_id, config["MESSAGES_TAB"])
            tournament = await load_tournament_snapshot(self.sheet_id)
            if tournament.status == "draft":
                return
            if tournament.status != "signup_open":
                return
            repository = LiveArenaRepository(self.sheet_id)
            await repository.initialize()
            participants = await repository.participants()
            count = sum(
                str(row["tournament_id"]).strip() == tournament.tournament_id
                and str(row["status"]).strip() == "confirmed"
                for row in participants
            )
            embed = messages["signup_open"].embed(
                tournament_name=tournament.tournament_name,
                signup_deadline=discord_timestamp(tournament.signup_closes_at_utc),
                confirmed_count=count,
                max_participants=tournament.max_participants,
            )
            channel = self.bot.get_channel(int(config["SIGNUP_CHANNEL_ID"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(config["SIGNUP_CHANNEL_ID"]))
            message = None
            if config["PUBLIC_PANEL_MESSAGE_ID"]:
                try:
                    message = await channel.fetch_message(
                        int(config["PUBLIC_PANEL_MESSAGE_ID"])
                    )
                except discord.NotFound:
                    message = None
                except Exception:
                    log.exception("❌ Live Arena panel — fetch failed")
                    return
            from .views import JoinTournamentView

            view = JoinTournamentView(self)
            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                except Exception:
                    log.exception("❌ Live Arena panel — edit failed")
                return
            created = await channel.send(embed=embed, view=view)
            try:
                await self._persist_message_id(matrix, str(created.id))
            except Exception:
                log.exception("❌ Live Arena panel — message ID persistence failed")
                try:
                    await created.delete()
                except Exception:
                    log.exception(
                        "⚠️ Live Arena panel — untracked message cleanup failed"
                    )
                raise

    async def _persist_message_id(self, matrix, message_id: str) -> None:
        headers = [str(value).strip() for value in matrix[0]]
        key_col, value_col = headers.index("Key"), headers.index("Value")
        rows = [
            index
            for index, row in enumerate(matrix[1:], 2)
            if key_col < len(row)
            and str(row[key_col]).strip() == "PUBLIC_PANEL_MESSAGE_ID"
        ]
        if len(rows) != 1:
            raise RuntimeError(
                "CONFIG: key PUBLIC_PANEL_MESSAGE_ID must occur exactly once"
            )
        worksheet = await aget_worksheet(self.sheet_id, "CONFIG")
        await acall_with_backoff(
            worksheet.update_cell, rows[0], value_col + 1, message_id
        )


async def register_live_arena(bot):
    sheet_id = os.getenv("LIVE_ARENA_TOURNAMENT_SHEET_ID", "").strip()
    if not sheet_id:
        return None
    manager = LiveArenaPanelManager(bot, sheet_id)
    from .views import JoinTournamentView

    bot.add_view(JoinTournamentView(manager))
    await manager.sync()
    return manager

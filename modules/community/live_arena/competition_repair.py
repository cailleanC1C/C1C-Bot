"""Explicit organizer repair action for Live Arena Discord state."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.competition_repair")
_installed = False


def install() -> None:
    """Stack a repair control onto the Live Arena organizer view."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_qualification_with_repair(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_competition_repair_installed", False):
            return True
        manager._competition_repair_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if callable(add_item):
                add_item(
                    RepairCompetitionDiscordButton(
                        manager,
                        disabled=status is not None
                        and status not in {"active", "completed"},
                    )
                )
            return result

        manager.view = view
        return True

    qualification_panel.install_qualification = install_qualification_with_repair


class RepairCompetitionDiscordButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Repair Discord State",
            custom_id="live_arena:organizer:competition:repair_discord",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            warnings = await _repair_missing_match_threads(self.manager)
            sync = getattr(self.manager, "_competition_sync", None)
            if not callable(sync):
                raise RuntimeError("Competition Discord repair is not installed")
            warnings.extend(await sync())
            try:
                await self.manager.sync()
            except Exception:
                log.exception("Live Arena organizer panel repair failed")
                warnings.append("organizer panel")
            warnings = list(dict.fromkeys(warnings))
            if warnings:
                embed = discord.Embed(
                    title="Discord repair incomplete",
                    description=(
                        "Sheet tournament state was left unchanged. These Discord items "
                        "still need attention:\n"
                        + "\n".join(f"• {item}" for item in warnings)
                    )[:4096],
                    color=colors.c1c_blue,
                )
            else:
                embed = discord.Embed(
                    title="Discord state repaired",
                    description=(
                        "The current tournament's Discord presentation was re-synced "
                        "from Sheet truth. No competition state was rolled back."
                    ),
                    color=colors.c1c_blue,
                )
        except Exception as exc:
            log.exception("Live Arena explicit Discord repair failed")
            embed = error_embed(exc)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def _repair_missing_match_threads(manager) -> list[str]:
    """Recreate only missing Duelling Deck threads from Sheet match truth."""
    from modules.community.live_arena import qualification_panel

    factory = getattr(manager, "qualification_service_factory", None)
    service = (
        factory(manager.sheet_id)
        if factory is not None
        else qualification_panel.QualificationService(manager.sheet_id)
    )
    await service.initialize()
    snapshot = await service.snapshot()
    if snapshot.round_row is None or not snapshot.matches:
        return []

    config = service.repository.config
    _, (_, tournament), _, slots = await service.context()
    try:
        forum = await qualification_panel._resolve_channel(
            manager.bot, int(config["MATCH_FORUM_CHANNEL_ID"])
        )
    except Exception:
        log.exception("Live Arena repair could not resolve Duelling Deck forum")
        return ["duelling-decks forum"]

    warnings: list[str] = []
    for match in snapshot.matches:
        match_id = _text(match.get("match_id"))
        thread_id = _text(match.get("thread_id"))
        existing = None
        if thread_id:
            try:
                existing = await qualification_panel._resolve_existing_thread(
                    manager.bot, thread_id
                )
            except Exception:
                log.exception(
                    "Live Arena repair could not verify match thread • match=%s",
                    match_id,
                )
                warnings.append(f"Match {_text(match.get('match_number'))} forum post")
                continue
        if existing is not None:
            continue
        try:
            created = await forum.create_thread(
                name=qualification_panel._thread_name(match),
                content=(
                    f"<@{_text(match['player_a_discord_user_id'])}> "
                    f"<@{_text(match['player_b_discord_user_id'])}>"
                ),
                embed=qualification_panel.match_embed(
                    tournament, snapshot.round_row, match, slots
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
            thread = getattr(created, "thread", None)
            if thread is None and isinstance(created, tuple):
                thread = created[0]
            if thread is None:
                thread = created
            try:
                await service.record_thread_id(match_id, str(thread.id))
            except Exception:
                try:
                    await thread.delete(
                        reason="Live Arena repair thread ID persistence failed"
                    )
                except Exception:
                    log.exception("Live Arena repair cleanup failed")
                raise
        except Exception:
            log.exception(
                "Live Arena repair failed to recreate match thread • match=%s",
                match_id,
            )
            warnings.append(f"Match {_text(match.get('match_number'))} forum post")
    return warnings

"""Round-start weekly availability reminder and shortcut controls."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import discord

from modules.community.live_arena import qualification_panel, result_views, runtime_hooks, views
from modules.community.live_arena.registration import RegistrationService
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.availability_reminder")
_installed = False

_original_match_result_init = result_views.MatchResultView.__init__
_original_match_embed = qualification_panel.match_embed
_original_sync_round_discord = runtime_hooks._sync_round_discord
_original_install_qualification = qualification_panel.install_qualification


class WeeklyAvailabilityShortcutButton(discord.ui.Button):
    def __init__(self, sheet_id: str):
        super().__init__(
            label="Review / Update Weekly Availability",
            style=discord.ButtonStyle.secondary,
            custom_id="live_arena:availability:review_update",
        )
        self.sheet_id = str(sheet_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = RegistrationService(self.sheet_id)
            await service.initialize()
            snapshot = await service.get_registration(str(interaction.user.id))
            if snapshot.participant is None:
                await interaction.followup.send(
                    embed=views.error_embed(
                        "You do not have an active Live Arena registration."
                    ),
                    ephemeral=True,
                )
                return
            if not snapshot.can_update:
                await interaction.followup.send(
                    embed=views.registration_embed(snapshot),
                    ephemeral=True,
                )
                return

            manager = SimpleNamespace(
                sheet_id=self.sheet_id,
                service_factory=None,
            )
            editor = await views._prepare_availability(
                manager,
                interaction.user,
                snapshot.timezone,
                service=service,
                snapshot=snapshot,
            )
            await interaction.followup.send(
                embed=editor.embed(),
                view=editor,
                ephemeral=True,
            )
        except Exception as exc:
            log.exception(
                "Live Arena weekly availability shortcut failed • user=%s",
                getattr(interaction.user, "id", "unknown"),
            )
            await interaction.followup.send(
                embed=views._player_error(exc),
                ephemeral=True,
            )


class WeeklyAvailabilityShortcutView(discord.ui.View):
    def __init__(self, sheet_id: str):
        super().__init__(timeout=None)
        self.add_item(WeeklyAvailabilityShortcutButton(sheet_id))


def _match_result_init_with_availability(
    self,
    sheet_id: str,
    *,
    report_disabled: bool = False,
    dispute_disabled: bool = False,
):
    _original_match_result_init(
        self,
        sheet_id,
        report_disabled=report_disabled,
        dispute_disabled=dispute_disabled,
    )
    self.add_item(WeeklyAvailabilityShortcutButton(sheet_id))


def _match_embed_with_weekly_reminder(tournament, round_row, match, slots):
    embed = _original_match_embed(tournament, round_row, match, slots)
    reminder = (
        "\n\n**Weekly availability reminder**\n"
        "Your saved availability is a recurring **day-of-the-week** schedule, not a set "
        "of calendar dates. If your usual schedule changed, use **Review / Update Weekly "
        "Availability** below. Updating availability never changes your opponent; it only "
        "keeps scheduling suggestions current."
    )
    description = (embed.description or "")
    if "**Weekly availability reminder**" not in description:
        embed.description = (description + reminder)[:4096]
    return embed


async def _sync_round_discord_with_availability(bot, qualification_service, snapshot):
    warnings = list(
        await _original_sync_round_discord(bot, qualification_service, snapshot)
    )
    try:
        round_row = snapshot.round_row
        if round_row is None:
            return warnings
        overview_id = _text(round_row.get("overview_message_id"))
        if not overview_id:
            return warnings
        config = qualification_service.repository.config
        channel = await qualification_panel._resolve_channel(
            bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"])
        )
        message = await channel.fetch_message(int(overview_id))
        await message.edit(
            view=WeeklyAvailabilityShortcutView(qualification_service.sheet_id)
        )
    except discord.NotFound:
        warnings.append("Victory Ledger availability reminder")
    except Exception:
        log.exception("Live Arena Victory Ledger availability reminder sync failed")
        warnings.append("Victory Ledger availability reminder")
    return list(dict.fromkeys(warnings))


def _install_qualification_with_availability(manager) -> bool:
    installed = _original_install_qualification(manager)
    if not installed:
        return False
    if getattr(manager, "_availability_reminder_installed", False):
        return True
    manager._availability_reminder_installed = True
    add_view = getattr(manager.bot, "add_view", None)
    if callable(add_view):
        add_view(WeeklyAvailabilityShortcutView(manager.sheet_id))
    return True


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    result_views.MatchResultView.__init__ = _match_result_init_with_availability
    qualification_panel.match_embed = _match_embed_with_weekly_reminder
    runtime_hooks._sync_round_discord = _sync_round_discord_with_availability
    qualification_panel.install_qualification = _install_qualification_with_availability

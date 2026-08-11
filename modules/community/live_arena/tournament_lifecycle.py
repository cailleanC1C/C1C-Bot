"""Tournament-instance lifecycle integration for the Live Arena organizer panel."""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime
from types import MethodType

import discord

from shared.theme import colors

from modules.community.live_arena.messages import load_messages, load_pr5_config
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.organizer_panel import (
    OrganizerView,
    _add_template_field,
    _role_message_key,
    _role_template_values,
    _roster_message_key,
    _roster_template_values,
    _status_label,
    _status_template_values,
)
from modules.community.live_arena.panel import PanelSyncResult
from modules.community.live_arena.qualification import QualificationService
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.tournament_lifecycle")


def _now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class LifecycleQualificationService(QualificationService):
    """Promote the tournament instance when the approved Q1 actually starts."""

    async def approve_draw(self, actor_id: str):
        snapshot = await super().approve_draw(actor_id)
        lifecycle = OrganizerService(
            self.sheet_id,
            repository=self.registration_repository,
            clock=self.clock,
        )
        await lifecycle.transition("activate", actor_id)
        return snapshot


def install_tournament_lifecycle(manager) -> bool:
    """Install lifecycle controls and tournament-owned organizer panel persistence."""
    if getattr(manager, "_tournament_lifecycle_installed", False):
        return True
    manager._tournament_lifecycle_installed = True

    base_view = manager.view
    accepts_status = bool(inspect.signature(base_view).parameters)

    def view(status=None):
        result = base_view(status) if accepts_status else base_view()
        add_item = getattr(result, "add_item", None)
        if not callable(add_item):
            return result
        add_item(
            TournamentLifecycleButton(
                manager,
                "Complete Tournament",
                "complete",
                discord.ButtonStyle.success,
                disabled=status is not None and status != "active",
            )
        )
        add_item(
            TournamentLifecycleButton(
                manager,
                "Archive Tournament",
                "archive",
                discord.ButtonStyle.danger,
                disabled=status is not None and status != "completed",
            )
        )
        if status == "archived":
            for item in result.children:
                if getattr(item, "custom_id", "") != "live_arena:organizer:roster":
                    item.disabled = True
        return result

    manager.view = view
    if callable(getattr(manager, "data", None)) and callable(
        getattr(manager, "secondary_sync", None)
    ):
        manager.sync = MethodType(_sync_organizer_panel, manager)
    if not hasattr(manager, "qualification_service_factory"):
        manager.qualification_service_factory = LifecycleQualificationService
    return True


async def _sync_organizer_panel(manager) -> PanelSyncResult:
    async with manager._lock:
        config, _ = await load_pr5_config(manager.sheet_id)
        channel = manager.bot.get_channel(int(config["ORGANIZER_CHANNEL_ID"]))
        if channel is None:
            channel = await manager.bot.fetch_channel(int(config["ORGANIZER_CHANNEL_ID"]))
        config, tournament, participants, counts, parity = await manager.data(
            getattr(channel, "guild", None)
        )
        roster_key = _roster_message_key(manager, tournament, counts)
        role_key = _role_message_key(parity)
        messages = await load_messages(
            manager.sheet_id,
            config["MESSAGES_TAB"],
            {"organizer_panel", roster_key, "organizer_statuses", role_key},
        )
        embed = messages["organizer_panel"].embed(
            tournament_name=tournament.tournament_name,
            status=_status_label(tournament.status),
            confirmed_count=counts["confirmed"],
            max_participants=tournament.max_participants,
        )
        if roster_key in messages:
            _add_template_field(
                embed,
                messages[roster_key],
                **_roster_template_values(tournament, counts),
            )
        if "organizer_statuses" in messages:
            _add_template_field(
                embed,
                messages["organizer_statuses"],
                **_status_template_values(counts),
            )
        if role_key in messages:
            _add_template_field(
                embed,
                messages[role_key],
                **_role_template_values(parity, participants),
            )

        repository = LiveArenaRepository(manager.sheet_id)
        await repository.initialize()
        resource = await repository.discord_resource(
            tournament.tournament_id, "organizer_panel", "main"
        )
        if resource is not None and _text(resource["state"]) == "retired":
            return PanelSyncResult(True)

        registry_message_id = _text(resource["message_id"]) if resource else ""
        legacy_message_id = _text(config.get("ORGANIZER_PANEL_MESSAGE_ID", ""))
        message_id = registry_message_id or legacy_message_id
        using_legacy = bool(message_id and not registry_message_id)
        message = None

        if message_id:
            get_partial_message = getattr(channel, "get_partial_message", None)
            if callable(get_partial_message):
                try:
                    message = get_partial_message(int(message_id))
                except Exception as exc:
                    log.exception(
                        "❌ Live Arena organizer panel — direct edit target failed • message_id=%s • error=%s: %s",
                        message_id,
                        type(exc).__name__,
                        exc,
                    )
                    return PanelSyncResult(False, "edit")
            else:
                try:
                    message = await channel.fetch_message(int(message_id))
                except discord.NotFound:
                    message = None
                except Exception as exc:
                    log.exception(
                        "❌ Live Arena organizer panel — fallback fetch failed • message_id=%s • error=%s: %s",
                        message_id,
                        type(exc).__name__,
                        exc,
                    )
                    return PanelSyncResult(False, "fetch")

        if message:
            try:
                await message.edit(embed=embed, view=manager.view(tournament.status))
            except discord.NotFound:
                log.warning(
                    "⚠️ Live Arena organizer panel — saved message missing • tournament=%s • message_id=%s",
                    tournament.tournament_id,
                    message_id,
                )
                message = None
            except Exception as exc:
                log.exception(
                    "❌ Live Arena organizer panel — edit failed • message_id=%s • error=%s: %s",
                    message_id,
                    type(exc).__name__,
                    exc,
                )
                return PanelSyncResult(False, "edit")
            else:
                now = _now_utc()
                if using_legacy or resource is None or tournament.status == "archived":
                    await repository.upsert_discord_resource(
                        tournament_id=tournament.tournament_id,
                        resource_type="organizer_panel",
                        resource_key="main",
                        channel_id=str(channel.id),
                        message_id=message_id,
                        created_at_utc=(
                            _text(resource["created_at_utc"]) if resource else now
                        ),
                        updated_at_utc=now,
                        state="retired" if tournament.status == "archived" else "active",
                        notes=(
                            "Migrated from legacy ORGANIZER_PANEL_MESSAGE_ID."
                            if using_legacy
                            else _text(resource["notes"]) if resource else ""
                        ),
                    )
                return PanelSyncResult(True)

        if tournament.status == "archived":
            if resource is not None:
                await repository.upsert_discord_resource(
                    tournament_id=tournament.tournament_id,
                    resource_type="organizer_panel",
                    resource_key="main",
                    channel_id=_text(resource["channel_id"]) or str(channel.id),
                    message_id=_text(resource["message_id"]),
                    thread_id=_text(resource["thread_id"]),
                    created_at_utc=_text(resource["created_at_utc"]),
                    updated_at_utc=_now_utc(),
                    state="retired",
                    notes=_text(resource["notes"]),
                )
            return PanelSyncResult(True)

        created = await channel.send(embed=embed, view=manager.view(tournament.status))
        try:
            now = _now_utc()
            await repository.upsert_discord_resource(
                tournament_id=tournament.tournament_id,
                resource_type="organizer_panel",
                resource_key="main",
                channel_id=str(channel.id),
                message_id=str(created.id),
                created_at_utc=_text(resource["created_at_utc"]) if resource else now,
                updated_at_utc=now,
                state="active",
                notes=_text(resource["notes"]) if resource else "",
            )
        except Exception:
            log.exception("❌ Live Arena organizer panel — resource persistence failed")
            try:
                await created.delete()
            except Exception:
                log.exception(
                    "⚠️ Live Arena organizer panel — untracked message cleanup failed"
                )
            raise
        return PanelSyncResult(True)


class TournamentLifecycleButton(discord.ui.Button):
    def __init__(self, manager, label, action, style, *, disabled=False):
        super().__init__(
            label=label,
            custom_id=f"live_arena:organizer:tournament:{action}",
            style=style,
            disabled=disabled,
        )
        self.manager = manager
        self.action = action

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        title = (
            "Complete this tournament?"
            if self.action == "complete"
            else "Archive this tournament?"
        )
        description = (
            "This freezes the tournament competition state. Historical results and Discord content are preserved."
            if self.action == "complete"
            else "This retires the tournament from normal workflows. Historical Sheet rows, Victory Ledger posts, and Duelling Deck threads are preserved."
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title=title,
                description=description,
                color=colors.c1c_blue,
            ),
            view=ConfirmTournamentLifecycle(self.manager, self.action),
            ephemeral=True,
        )


class ConfirmTournamentLifecycle(discord.ui.View):
    def __init__(self, manager, action):
        super().__init__(timeout=300)
        self.manager = manager
        self.action = action

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await _execute_lifecycle(interaction, self.manager, self.action)


async def _execute_lifecycle(interaction, manager, action):
    try:
        service = OrganizerService(manager.sheet_id)
        await service.initialize()
        await service.transition(action, str(interaction.user.id))
        warnings = await manager.secondary_sync()
        if action == "archive":
            _, tournament, *_ = await manager.data(interaction.guild)
            repository = LiveArenaRepository(manager.sheet_id)
            await repository.initialize()
            await repository.retire_discord_resources(
                tournament.tournament_id, updated_at_utc=_now_utc()
            )
        embed = discord.Embed(
            title="Tournament updated",
            description=(
                "The tournament is now **Completed**. Historical data and Discord content remain in place."
                if action == "complete"
                else "The tournament is now **Archived**. Historical data and Discord content remain preserved."
            ),
            color=colors.c1c_blue,
        )
        if warnings:
            embed.add_field(
                name="Sync warning",
                value=(
                    "The Sheet lifecycle state was saved, but "
                    + ", ".join(warnings)
                    + " could not be refreshed."
                )[:1024],
                inline=False,
            )
    except Exception as exc:
        log.exception("❌ Live Arena tournament lifecycle transition failed")
        embed = error_embed(exc)
    await interaction.followup.send(embed=embed, ephemeral=True)

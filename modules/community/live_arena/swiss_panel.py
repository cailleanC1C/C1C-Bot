"""Organizer preview, approval, and publication controls for Q2/Q3 Swiss rounds."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.qualification import QualificationService
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.swiss import SwissQualificationService
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.swiss_panel")
_installed = False


def install() -> None:
    """Stack Q2/Q3 controls onto the existing Live Arena organizer panel."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_with_swiss(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_swiss_controls_installed", False):
            return True
        manager._swiss_controls_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if not callable(add_item):
                return result
            disabled = status is not None and status != "active"
            add_item(
                SwissActionButton(
                    manager, "Preview Next Swiss", "preview", disabled=disabled
                )
            )
            add_item(
                SwissActionButton(
                    manager,
                    "Regenerate Swiss Preview",
                    "regenerate",
                    disabled=disabled,
                )
            )
            add_item(
                SwissActionButton(
                    manager, "Approve & Publish Swiss", "publish", disabled=disabled
                )
            )
            return result

        manager.view = view
        return True

    qualification_panel.install_qualification = install_with_swiss


class SwissActionButton(discord.ui.Button):
    def __init__(self, manager, label: str, action: str, *, disabled=False):
        style = (
            discord.ButtonStyle.success
            if action == "publish"
            else discord.ButtonStyle.secondary
        )
        super().__init__(
            label=label,
            custom_id=f"live_arena:organizer:swiss:{action}",
            style=style,
            disabled=disabled,
        )
        self.manager = manager
        self.action = action

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = SwissQualificationService(self.manager.sheet_id)
            await service.initialize()
            target = await _target_round(service)
            actor = str(interaction.user.id)
            if self.action == "preview":
                snapshot = await service.generate_preview(actor, target)
                await interaction.followup.send(
                    embed=preview_embed(snapshot, official=False), ephemeral=True
                )
                return
            if self.action == "regenerate":
                snapshot = await service.generate_preview(actor, target, regenerate=True)
                await interaction.followup.send(
                    embed=preview_embed(snapshot, official=False), ephemeral=True
                )
                return

            approved = await service.approve_preview(actor, target)
            published = await service.publish_approved(actor, target)
            warnings = await SwissPublisher(self.manager.bot, service).reconcile(published)
            try:
                await self.manager.sync()
            except Exception:
                log.exception(
                    "Live Arena organizer panel refresh after Swiss publication failed"
                )
                warnings.append("organizer panel")
            embed = discord.Embed(
                title=f"Qualification Round {target} published",
                description=(
                    f"**{len(approved.matches)}** Swiss matchups are now official. "
                    "The six-day round window starts now."
                ),
                color=colors.c1c_blue,
            )
            if warnings:
                embed.add_field(
                    name="Publication warning",
                    value=(
                        "Sheet state is saved, but these Discord items need repair:\n"
                        + "\n".join(f"• {item}" for item in dict.fromkeys(warnings))
                    )[:1024],
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            log.exception(
                "Live Arena Swiss organizer action failed • action=%s", self.action
            )
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _target_round(service: SwissQualificationService) -> int:
    config = await load_config(service.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    rounds = [
        row
        for row in await service.repository.rounds()
        if _text(row.get("tournament_id")) == tid
        and _text(row.get("round_stage")).lower() == "qualification"
    ]
    by_number = {
        int(_text(row.get("round_number"))): row
        for row in rounds
        if _text(row.get("round_number")).isdigit()
    }
    q3 = by_number.get(3)
    if q3 is not None and _text(q3.get("status")) in {"preview", "approved"}:
        return 3
    q2 = by_number.get(2)
    if q2 is None:
        return 2
    if _text(q2.get("status")) in {"preview", "approved"}:
        return 2
    if _text(q2.get("status")) in {
        "active",
        "published",
        "open",
        "published/open",
        "ready_to_close",
        "closed",
        "correction_in_progress",
    }:
        return 3
    raise RegistrationError(
        "No Q2/Q3 Swiss round is currently available for this action"
    )


def preview_embed(snapshot, *, official: bool) -> discord.Embed:
    number = (
        _text(snapshot.round_row.get("round_number")) if snapshot.round_row else "?"
    )
    embed = discord.Embed(
        title=(
            f"Qualification Round {number} · "
            f"{'Official Draw' if official else 'Organizer Preview'}"
        ),
        description=(
            "This preview is **not official** and creates no player-facing Discord resources."
            if not official
            else "This Swiss draw has been approved."
        ),
        color=colors.c1c_blue,
    )
    for match in snapshot.matches:
        rationale = _text(match.get("notes")) or "Swiss pairing"
        scheduling = (
            "⚠️ no shared availability"
            if _text(match.get("has_scheduling_conflict")).lower() == "true"
            else "shared availability found"
        )
        embed.add_field(
            name=f"Match {_text(match['match_number'])}",
            value=(
                f"**{_text(match['player_a_display_name'])}** vs "
                f"**{_text(match['player_b_display_name'])}**\n"
                f"{rationale}\n{scheduling}"
            )[:1024],
            inline=False,
        )
    return embed


class SwissPublisher:
    """Create only official Q2/Q3 Discord resources after organizer approval."""

    def __init__(self, bot, service: SwissQualificationService):
        self.bot = bot
        self.service = service

    async def reconcile(self, snapshot=None) -> list[str]:
        from modules.community.live_arena import qualification_panel, runtime_hooks

        snapshot = snapshot or await self._current_open_snapshot()
        if snapshot is None or snapshot.round_row is None:
            return []
        if snapshot.status not in {
            "open",
            "active",
            "published",
            "published/open",
            "ready_to_close",
            "closed",
            "correction_in_progress",
        }:
            return []
        config = self.service.repository.config
        organizer = OrganizerService(
            self.service.sheet_id,
            repository=self.service.registration_repository,
            clock=self.service.clock,
        )
        _, (_, tournament), _, slots = await organizer.context()
        warnings = []
        matches = [dict(row) for row in snapshot.matches]
        forum = None
        try:
            forum = await qualification_panel._resolve_channel(
                self.bot, int(config["MATCH_FORUM_CHANNEL_ID"])
            )
        except Exception:
            log.exception("Live Arena Swiss forum resolution failed")
            warnings.append("duelling-decks forum")

        if forum is not None:
            for match in matches:
                try:
                    thread = await qualification_panel._resolve_existing_thread(
                        self.bot, _text(match.get("thread_id"))
                    )
                    if thread is not None:
                        continue
                    created = await forum.create_thread(
                        name=_thread_name(snapshot.round_row, match),
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
                    await self._record_thread_id(
                        _text(match["match_id"]), str(thread.id)
                    )
                    match["thread_id"] = str(thread.id)
                except Exception:
                    log.exception(
                        "Live Arena Swiss matchup publication failed • match=%s",
                        _text(match.get("match_id")),
                    )
                    warnings.append(
                        f"Match {_text(match.get('match_number'))} forum post"
                    )

        refreshed = await self.service.snapshot(
            int(_text(snapshot.round_row["round_number"]))
        )
        try:
            warnings.extend(
                await runtime_hooks._sync_round_discord(
                    self.bot, self.service, refreshed
                )
            )
        except Exception:
            log.exception("Live Arena Swiss public overview synchronization failed")
            warnings.append("Victory Ledger overview")
        return list(dict.fromkeys(warnings))

    async def _current_open_snapshot(self):
        for number in (3, 2):
            snapshot = await self.service.snapshot(number)
            if snapshot.round_row is not None and snapshot.status in {
                "open",
                "active",
                "published",
                "published/open",
                "ready_to_close",
                "closed",
                "correction_in_progress",
            }:
                return snapshot
        return None

    async def _record_thread_id(self, match_id: str, thread_id: str) -> None:
        helper = QualificationService(
            self.service.sheet_id,
            registration_repository=self.service.registration_repository,
            qualification_repository=self.service.repository,
            clock=self.service.clock,
        )
        await helper.record_thread_id(match_id, thread_id)


def _thread_name(round_row, match) -> str:
    number = int(_text(round_row.get("round_number")) or 0)
    raw = (
        f"Q{number} • M{int(_text(match['match_number'])):02d} • "
        f"{_text(match['player_a_display_name'])} vs "
        f"{_text(match['player_b_display_name'])}"
    )
    return raw[:100]

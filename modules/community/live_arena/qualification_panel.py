"""Organizer controls and Discord publication for Live Arena qualification round 1."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta

import discord

from shared.theme import colors

from modules.community.live_arena.organizer_panel import OrganizerView, _send_ephemeral
from modules.community.live_arena.qualification import (
    QualificationService,
    QualificationSnapshot,
)
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _enabled, _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.qualification_panel")


def install_qualification(manager) -> bool:
    """Add persistent Q1 controls to the real organizer manager without changing PR5."""
    if getattr(manager, "_qualification_installed", False):
        return True
    if manager.__class__.__module__ != "modules.community.live_arena.organizer_panel":
        return False
    if manager.__class__.__name__ != "OrganizerPanelManager":
        return False

    base_view = manager.view
    manager._qualification_q1_status = ""
    manager._qualification_installed = True

    def view(status=None):
        result = base_view(status)
        qstatus = getattr(manager, "_qualification_q1_status", "")
        result.add_item(
            QualificationButton(
                manager,
                "Generate Q1 Draw",
                "generate",
                discord.ButtonStyle.success,
                disabled=status is not None
                and not (status == "signup_closed" and not qstatus),
            )
        )
        result.add_item(
            QualificationButton(
                manager,
                "Approve Draw",
                "approve",
                discord.ButtonStyle.success,
                disabled=status is not None
                and not (status == "signup_closed" and qstatus == "proposed"),
            )
        )
        result.add_item(
            QualificationButton(
                manager,
                "Regenerate Draw",
                "regenerate",
                discord.ButtonStyle.secondary,
                disabled=status is not None
                and not (status == "signup_closed" and qstatus == "proposed"),
            )
        )
        result.add_item(
            QualificationButton(
                manager,
                "Swap Players",
                "swap",
                discord.ButtonStyle.secondary,
                disabled=status is not None
                and not (status == "signup_closed" and qstatus == "proposed"),
            )
        )
        return result

    manager.view = view
    return True


async def refresh_qualification_state(manager) -> QualificationSnapshot | None:
    """Refresh the cached Q1 state used to disable organizer controls."""
    if not getattr(manager, "_qualification_installed", False):
        return None
    service = _service(manager)
    await service.initialize()
    snapshot = await service.snapshot()
    manager._qualification_q1_status = snapshot.status
    return snapshot


async def reconcile_qualification_publication(manager) -> list[str]:
    """Best-effort startup retry for an already-approved Q1."""
    snapshot = await refresh_qualification_state(manager)
    if snapshot is None or snapshot.status != "active":
        return []
    service = _service(manager)
    await service.initialize()
    return await QualificationPublisher(manager.bot, service).reconcile()


def _service(manager):
    factory = getattr(manager, "qualification_service_factory", None)
    if factory is not None:
        return factory(manager.sheet_id)
    return QualificationService(manager.sheet_id)


class QualificationButton(discord.ui.Button):
    def __init__(self, manager, label, action, style, *, disabled=False):
        super().__init__(
            label=label,
            custom_id=f"live_arena:organizer:q1:{action}",
            style=style,
            disabled=disabled,
        )
        self.manager = manager
        self.action = action

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        if self.action == "approve":
            await self._approve_prompt(interaction)
            return
        if self.action == "swap":
            await self._swap_prompt(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = _service(self.manager)
            await service.initialize()
            if self.action == "generate":
                snapshot = await service.generate_draw(str(interaction.user.id))
            else:
                snapshot = await service.regenerate_draw(str(interaction.user.id))
            self.manager._qualification_q1_status = snapshot.status
            await _refresh_organizer_panel(self.manager)
            await interaction.followup.send(
                embed=proposal_embed(snapshot), ephemeral=True
            )
        except Exception as exc:
            log.exception("❌ Live Arena Q1 %s failed", self.action)
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    async def _approve_prompt(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = _service(self.manager)
            await service.initialize()
            snapshot = await service.snapshot()
            if snapshot.status != "proposed":
                raise RegistrationError("Only a proposed Q1 draw can be approved")
            embed = proposal_embed(snapshot)
            embed.title = "Approve Qualification Round 1?"
            embed.description = (
                (embed.description or "")
                + "\n\nApproval publishes every matchup and starts the six-day round window."
            )
            await interaction.followup.send(
                embed=embed,
                view=ConfirmApproveDraw(self.manager),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("❌ Live Arena Q1 approval preflight failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    async def _swap_prompt(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = _service(self.manager)
            await service.initialize()
            snapshot = await service.snapshot()
            if snapshot.status != "proposed":
                raise RegistrationError("Players can only be swapped in a proposed Q1 draw")
            await interaction.followup.send(
                embed=proposal_embed(snapshot),
                view=SwapPlayersView(self.manager, snapshot),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("❌ Live Arena Q1 swap preflight failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class ConfirmApproveDraw(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=300)
        self.manager = manager

    @discord.ui.button(label="Approve & Publish", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = _service(self.manager)
            await service.initialize()
            snapshot = await service.approve_draw(str(interaction.user.id))
            self.manager._qualification_q1_status = snapshot.status
            warnings = await QualificationPublisher(
                self.manager.bot, service
            ).reconcile()
            panel_warning = await _refresh_organizer_panel(self.manager)
            if panel_warning:
                warnings.append(panel_warning)
            embed = discord.Embed(
                title="Qualification Round 1 published",
                description=(
                    f"**{len(snapshot.matches)}** matchups are active. "
                    "Players have six days maximum to complete the round."
                ),
                color=colors.c1c_blue,
            )
            if warnings:
                embed.add_field(
                    name="Publication warning",
                    value=(
                        "The Sheet state is saved, but these Discord items need a retry:\n"
                        + "\n".join(f"• {item}" for item in warnings)
                    )[:1024],
                    inline=False,
                )
        except Exception as exc:
            log.exception("❌ Live Arena Q1 approval failed")
            embed = error_embed(exc)
        await interaction.followup.send(embed=embed, ephemeral=True)


class SwapPlayersView(discord.ui.View):
    def __init__(self, manager, snapshot):
        super().__init__(timeout=600)
        self.manager = manager
        seen = set()
        options = []
        for match in snapshot.matches:
            for side in ("a", "b"):
                user_id = _text(match[f"player_{side}_discord_user_id"])
                if user_id in seen:
                    continue
                seen.add(user_id)
                label = _text(match[f"player_{side}_display_name"]) or user_id
                options.append(
                    discord.SelectOption(label=label[:100], value=user_id)
                )
        self.add_item(SwapPlayersSelect(manager, options))


class SwapPlayersSelect(discord.ui.Select):
    def __init__(self, manager, options):
        super().__init__(
            placeholder="Choose two players from different matches",
            min_values=2,
            max_values=2,
            options=options,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = _service(self.manager)
            await service.initialize()
            snapshot = await service.swap_players(
                str(interaction.user.id), self.values[0], self.values[1]
            )
            self.manager._qualification_q1_status = snapshot.status
            await _refresh_organizer_panel(self.manager)
            await interaction.followup.send(
                embed=proposal_embed(snapshot), ephemeral=True
            )
        except Exception as exc:
            log.exception("❌ Live Arena Q1 player swap failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _refresh_organizer_panel(manager) -> str:
    try:
        result = await manager.sync()
        if getattr(result, "ok", True) is False:
            return "organizer panel"
    except Exception:
        log.exception("⚠️ Live Arena Q1 organizer panel refresh failed")
        return "organizer panel"
    return ""


def proposal_embed(snapshot: QualificationSnapshot) -> discord.Embed:
    if snapshot.round_row is None:
        return discord.Embed(
            title="Qualification Round 1",
            description="No Q1 draw has been generated.",
            color=colors.c1c_blue,
        )
    conflicts = sum(
        _text(match["has_scheduling_conflict"]).lower() == "true"
        for match in snapshot.matches
    )
    embed = discord.Embed(
        title="Qualification Round 1 · Proposed Draw",
        description=(
            f"**{len(snapshot.matches)}** matches • "
            f"**{conflicts}** scheduling conflict{'s' if conflicts != 1 else ''}."
        ),
        color=colors.c1c_blue,
    )
    for match in snapshot.matches:
        shared = [
            value for value in _text(match["shared_slot_ids_csv"]).split(",") if value
        ]
        conflict = _text(match["has_scheduling_conflict"]).lower() == "true"
        availability = (
            "⚠️ No shared availability"
            if conflict
            else f"Shared availability: **{len(shared)}** window{'s' if len(shared) != 1 else ''}"
        )
        embed.add_field(
            name=f"Match {_text(match['match_number'])}",
            value=(
                f"**{_text(match['player_a_display_name'])}** vs "
                f"**{_text(match['player_b_display_name'])}**\n{availability}"
            ),
            inline=False,
        )
    return embed


class QualificationPublisher:
    def __init__(self, bot, service: QualificationService):
        self.bot = bot
        self.service = service

    async def reconcile(self) -> list[str]:
        """Create only missing Q1 Discord artifacts and refresh the existing overview."""
        snapshot = await self.service.snapshot()
        if snapshot.round_row is None or snapshot.status != "active":
            return []
        config = self.service.repository.config
        _, (_, tournament), _, slots = await self.service.context()
        warnings: list[str] = []
        matches = [dict(row) for row in snapshot.matches]

        try:
            forum = await _resolve_channel(
                self.bot, int(config["MATCH_FORUM_CHANNEL_ID"])
            )
        except Exception:
            log.exception("❌ Live Arena Q1 forum channel resolution failed")
            forum = None
            warnings.append("duelling-decks forum")

        if forum is not None:
            for match in matches:
                label = f"Match {_text(match['match_number'])} forum post"
                try:
                    existing = await _resolve_existing_thread(
                        self.bot, _text(match["thread_id"])
                    )
                    if existing is not None:
                        continue
                    created = await forum.create_thread(
                        name=_thread_name(match),
                        content=(
                            f"<@{_text(match['player_a_discord_user_id'])}> "
                            f"<@{_text(match['player_b_discord_user_id'])}>"
                        ),
                        embed=match_embed(tournament, snapshot.round_row, match, slots),
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
                        await self.service.record_thread_id(
                            _text(match["match_id"]), str(thread.id)
                        )
                    except Exception:
                        try:
                            await thread.delete(
                                reason="Live Arena thread ID persistence failed"
                            )
                        except Exception:
                            log.exception(
                                "⚠️ Live Arena Q1 untracked forum post cleanup failed"
                            )
                        raise
                    match["thread_id"] = str(thread.id)
                except Exception:
                    log.exception(
                        "❌ Live Arena Q1 forum publication failed • match=%s",
                        _text(match["match_id"]),
                    )
                    warnings.append(label)

        try:
            overview_channel = await _resolve_channel(
                self.bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"])
            )
            embed = overview_embed(tournament, snapshot.round_row, matches)
            overview_id = _text(snapshot.round_row["overview_message_id"])
            message = None
            if overview_id:
                try:
                    message = await overview_channel.fetch_message(int(overview_id))
                except discord.NotFound:
                    message = None
                except Exception:
                    log.exception("❌ Live Arena Q1 overview fetch failed")
                    warnings.append("Victory Ledger overview")
                    return _dedupe(warnings)
            if message is not None:
                await message.edit(embed=embed)
            else:
                created = await overview_channel.send(embed=embed)
                try:
                    await self.service.record_overview_message_id(
                        _text(snapshot.round_row["round_id"]), str(created.id)
                    )
                except Exception:
                    try:
                        await created.delete()
                    except Exception:
                        log.exception(
                            "⚠️ Live Arena Q1 untracked overview cleanup failed"
                        )
                    raise
        except Exception:
            log.exception("❌ Live Arena Q1 overview publication failed")
            warnings.append("Victory Ledger overview")
        return _dedupe(warnings)


async def _resolve_channel(bot, channel_id):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


async def _resolve_existing_thread(bot, thread_id):
    if not thread_id:
        return None
    thread = bot.get_channel(int(thread_id))
    if thread is not None:
        return thread
    try:
        return await bot.fetch_channel(int(thread_id))
    except discord.NotFound:
        return None


def _thread_name(match):
    raw = (
        f"Q1 • M{int(_text(match['match_number'])):02d} • "
        f"{_text(match['player_a_display_name'])} vs {_text(match['player_b_display_name'])}"
    )
    return raw[:100]


def match_embed(tournament, round_row, match, slots):
    deadline = _format_timestamp(_text(round_row["deadline_at_utc"]), "F")
    shared_ids = [
        value for value in _text(match["shared_slot_ids_csv"]).split(",") if value
    ]
    by_id = {
        _text(row["slot_id"]): row for row in slots if _enabled(row["enabled"])
    }
    windows = [
        _render_slot(by_id[slot_id], round_row)
        for slot_id in shared_ids
        if slot_id in by_id
    ]
    if not windows:
        availability = (
            "⚠️ **No shared availability window was found.** "
            "Please coordinate directly in this thread and contact an organiser "
            "if scheduling becomes a problem."
        )
    else:
        availability = "\n".join(f"• {window}" for window in windows)
    description = (
        f"<@{_text(match['player_a_discord_user_id'])}> vs "
        f"<@{_text(match['player_b_discord_user_id'])}>\n\n"
        "**Format:** Best of 3\n"
        f"**Round deadline:** {deadline}\n\n"
        "**Shared availability**\n"
        f"{availability}\n\n"
        "Arrange the exact fight time between yourselves. After the BO3, "
        "**post at least one screenshot of the match result in this thread**. "
        "Result reporting controls will use this thread as the match record."
    )
    return discord.Embed(
        title=(
            f"{_text(round_row['round_name'])} · Match {_text(match['match_number'])}"
        ),
        description=description,
        color=colors.c1c_blue,
    )


def overview_embed(tournament, round_row, matches):
    deadline = _format_timestamp(_text(round_row["deadline_at_utc"]), "F")
    embed = discord.Embed(
        title=_text(round_row["round_name"]),
        description=(
            f"**{_text(tournament['tournament_name'])}**\n"
            f"Round deadline: {deadline}\n"
            f"Completed: **0 / {len(matches)}**"
        ),
        color=colors.c1c_blue,
    )
    for match in sorted(matches, key=lambda row: int(_text(row["match_number"]))):
        thread_id = _text(match["thread_id"])
        # Discord channel mentions are <#ID>; keep a plain fallback while a retry is pending.
        location = f"<#{thread_id}>" if thread_id else "Forum post pending"
        embed.add_field(
            name=f"Match {_text(match['match_number'])}",
            value=(
                f"<@{_text(match['player_a_discord_user_id'])}> vs "
                f"<@{_text(match['player_b_discord_user_id'])}>\n{location}"
            ),
            inline=False,
        )
    return embed


def _format_timestamp(value, style):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return f"<t:{int(parsed.timestamp())}:{style}>"


def _render_slot(slot, round_row):
    """Use a local Discord timestamp for the next in-round occurrence when possible."""
    label = _text(slot["display_label"]) or _text(slot["slot_id"])
    try:
        opened = datetime.fromisoformat(
            _text(round_row["opens_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        deadline = datetime.fromisoformat(
            _text(round_row["deadline_at_utc"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        weekdays = {
            name: index
            for index, name in enumerate(
                (
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                    "Sunday",
                )
            )
        }
        target = weekdays[_text(slot["weekday_utc"])]
        start_clock = time.fromisoformat(_text(slot["start_time_utc"]))
        end_clock = time.fromisoformat(_text(slot["end_time_utc"]))
        days = (target - opened.weekday()) % 7
        start = datetime.combine(opened.date() + timedelta(days=days), start_clock, UTC)
        if start < opened:
            start += timedelta(days=7)
        end = datetime.combine(start.date(), end_clock, UTC)
        if end <= start:
            end += timedelta(days=1)
        if start <= deadline:
            return (
                f"{_format_timestamp(start.isoformat(), 'F')} – "
                f"{_format_timestamp(end.isoformat(), 't')}"
            )
    except (KeyError, ValueError):
        pass
    return label


def _dedupe(values):
    return list(dict.fromkeys(values))

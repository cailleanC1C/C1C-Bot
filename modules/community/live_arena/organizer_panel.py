"""Persistent organizer panel and Discord-side recovery controls."""

from __future__ import annotations

import asyncio
import logging

import discord

from shared.sheets.async_core import acall_with_backoff, aget_worksheet
from shared.theme import colors

from modules.community.live_arena.messages import load_messages, load_pr5_config
from modules.community.live_arena.organizer import OrganizerService, status_counts
from modules.community.live_arena.panel import PanelSyncResult
from modules.community.live_arena.service import (
    LiveArenaConfigError,
    _text,
    load_tournament_snapshot,
)
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.organizer_panel")
_locks: dict[str, asyncio.Lock] = {}


class OrganizerPanelManager:
    def __init__(self, bot, sheet_id, public_manager):
        self.bot, self.sheet_id, self.public_manager = bot, sheet_id, public_manager
        self._lock = _locks.setdefault(sheet_id, asyncio.Lock())

    def view(self, status=None):
        return OrganizerView(self, status)

    async def data(self, guild=None):
        config, _ = await load_pr5_config(self.sheet_id)
        tournament = await load_tournament_snapshot(self.sheet_id)
        service = OrganizerService(self.sheet_id)
        await service.initialize()
        participants = await service.repository.participants()
        counts = status_counts(participants, tournament.tournament_id)
        parity = await role_parity(
            guild, config, participants, tournament.tournament_id
        )
        return config, tournament, participants, counts, parity

    async def sync(self) -> PanelSyncResult:
        async with self._lock:
            config, _ = await load_pr5_config(self.sheet_id)
            channel = self.bot.get_channel(int(config["ORGANIZER_CHANNEL_ID"]))
            if channel is None:
                channel = await self.bot.fetch_channel(
                    int(config["ORGANIZER_CHANNEL_ID"])
                )
            config, tournament, _, counts, parity = await self.data(
                getattr(channel, "guild", None)
            )
            messages = await load_messages(
                self.sheet_id, config["MESSAGES_TAB"], {"organizer_panel"}
            )
            even = counts["confirmed"] % 2 == 0
            parity_summary = (
                "Roster parity: EVEN."
                if even
                else "Roster parity: ODD — qualification cannot start until the active roster is even."
            )
            embed = messages["organizer_panel"].embed(
                tournament_name=tournament.tournament_name,
                status=tournament.status,
                confirmed_count=counts["confirmed"],
                max_participants=tournament.max_participants,
                parity_summary=parity_summary,
            )
            embed.add_field(
                name="Participant statuses",
                value="\n".join(
                    f"{key.title()}: **{value}**" for key, value in counts.items()
                ),
                inline=False,
            )
            embed.add_field(
                name="Participant role parity",
                value=f"Missing: **{len(parity['missing'])}** • Extra: **{len(parity['extra'])}** • Unresolved: **{len(parity['unresolved'])}**",
                inline=False,
            )
            message = None
            if config["ORGANIZER_PANEL_MESSAGE_ID"]:
                try:
                    message = await channel.fetch_message(
                        int(config["ORGANIZER_PANEL_MESSAGE_ID"])
                    )
                except discord.NotFound:
                    pass
                except Exception:
                    log.exception("❌ Live Arena organizer panel — fetch failed")
                    return PanelSyncResult(False, "fetch")
            if message:
                try:
                    await message.edit(embed=embed, view=self.view(tournament.status))
                except Exception:
                    log.exception("❌ Live Arena organizer panel — edit failed")
                    return PanelSyncResult(False, "edit")
                return PanelSyncResult(True)
            created = await channel.send(embed=embed, view=self.view(tournament.status))
            try:
                await self._persist(config, str(created.id))
            except Exception:
                log.exception(
                    "❌ Live Arena organizer panel — message ID persistence failed"
                )
                try:
                    await created.delete()
                except Exception:
                    log.exception(
                        "⚠️ Live Arena organizer panel — untracked message cleanup failed"
                    )
                raise
            return PanelSyncResult(True)

    async def _persist(self, config, message_id):
        from shared.sheets.async_core import afetch_values

        matrix = await afetch_values(self.sheet_id, "CONFIG") or []
        headers = [_text(v) for v in matrix[0]]
        key_col, value_col = headers.index("Key"), headers.index("Value")
        rows = [
            i
            for i, row in enumerate(matrix[1:], 2)
            if key_col < len(row)
            and _text(row[key_col]) == "ORGANIZER_PANEL_MESSAGE_ID"
        ]
        if len(rows) != 1:
            raise LiveArenaConfigError(
                "CONFIG: key ORGANIZER_PANEL_MESSAGE_ID must occur exactly once"
            )
        worksheet = await aget_worksheet(self.sheet_id, "CONFIG")
        await acall_with_backoff(
            worksheet.update_cell, rows[0], value_col + 1, message_id
        )

    async def secondary_sync(self):
        warnings = []
        for label, action in (
            ("public panel", self.public_manager.sync),
            ("organizer panel", self.sync),
        ):
            try:
                result = await action()
                if isinstance(result, PanelSyncResult) and not result.ok:
                    warnings.append(label)
            except Exception:
                log.exception("⚠️ Live Arena %s — post-mutation refresh failed", label)
                warnings.append(label)
        return warnings


async def role_parity(guild, config, participants, tournament_id):
    confirmed = {
        _text(r["discord_user_id"])
        for r in participants
        if _text(r["tournament_id"]) == tournament_id
        and _text(r["status"]) == "confirmed"
    }
    if guild is None:
        guild = None
    role = guild.get_role(int(config["PARTICIPANT_ROLE_ID"])) if guild else None
    members = {_text(m.id): m for m in getattr(role, "members", [])} if role else {}
    resolved = {uid: guild.get_member(int(uid)) for uid in confirmed} if guild else {}
    return {
        "missing": [
            m for uid, m in resolved.items() if m is not None and uid not in members
        ],
        "extra": [m for uid, m in members.items() if uid not in confirmed],
        "unresolved": [uid for uid in confirmed if not resolved.get(uid)],
    }


class OrganizerView(discord.ui.View):
    def __init__(self, manager, status=None):
        super().__init__(timeout=None)
        self.manager = manager
        actions = [
            ("Open Registration", "open", "draft"),
            ("Close Registration", "close", "signup_open"),
            ("Reopen Registration", "reopen", "signup_closed"),
        ]
        for label, action, applicable in actions:
            self.add_item(
                OrganizerButton(
                    label,
                    f"live_arena:organizer:{action}",
                    discord.ButtonStyle.primary,
                    self.transition,
                    action,
                    disabled=status is not None and status != applicable,
                )
            )
        self.add_item(
            OrganizerButton(
                "View Roster",
                "live_arena:organizer:roster",
                discord.ButtonStyle.secondary,
                self.roster,
                "roster",
            )
        )
        self.add_item(
            OrganizerButton(
                "Reconcile Roles",
                "live_arena:organizer:roles",
                discord.ButtonStyle.secondary,
                self.reconcile_prompt,
                "roles",
            )
        )

    async def authorized(self, interaction):
        config, _ = await load_pr5_config(self.manager.sheet_id)
        allowed = any(
            str(r.id) == config["ORGANIZER_ROLE_ID"]
            for r in getattr(interaction.user, "roles", [])
        )
        if not allowed:
            await interaction.response.send_message(
                embed=error_embed(
                    "You need the configured organizer role to use this control."
                ),
                ephemeral=True,
            )
        return allowed

    async def transition(self, interaction, action):
        if not await self.authorized(interaction):
            return
        if action == "close":
            _, tournament, _, counts, _ = await self.manager.data(interaction.guild)
            odd = counts["confirmed"] % 2
            warning = (
                " The active confirmed roster is odd. Close is still allowed; no player will be auto-demoted, but qualification/pairing must not begin until it is even."
                if odd
                else ""
            )
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Confirm close registration",
                    description=f"Close registration with **{counts['confirmed']}** confirmed players?{warning}",
                    color=colors.c1c_blue,
                ),
                view=ConfirmTransition(self.manager, "close"),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await execute_transition(interaction, self.manager, action)

    async def roster(self, interaction, _action):
        if not await self.authorized(interaction):
            return
        embed = await roster_embed(self.manager, interaction.guild)
        await interaction.response.send_message(
            embed=embed, view=RosterActions(self.manager), ephemeral=True
        )

    async def reconcile_prompt(self, interaction, _action):
        if not await self.authorized(interaction):
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Confirm role reconciliation",
                description="Add the Participant role to confirmed members and remove it from members who are not confirmed? No Sheet state will change.",
                color=colors.c1c_blue,
            ),
            view=ConfirmReconcile(self.manager),
            ephemeral=True,
        )


class OrganizerButton(discord.ui.Button):
    def __init__(self, label, custom_id, style, handler, action, disabled=False):
        super().__init__(
            label=label, custom_id=custom_id, style=style, disabled=disabled
        )
        self.handler, self.action = handler, action

    async def callback(self, interaction):
        await self.handler(interaction, self.action)


class ConfirmTransition(discord.ui.View):
    def __init__(self, manager, action):
        super().__init__(timeout=300)
        self.manager, self.action = manager, action

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await execute_transition(interaction, self.manager, self.action)


async def execute_transition(interaction, manager, action):
    try:
        service = OrganizerService(manager.sheet_id)
        await service.initialize()
        await service.transition(action, str(interaction.user.id))
        warnings = await manager.secondary_sync()
        past_tense = {"open": "opened", "close": "closed", "reopen": "reopened"}
        embed = discord.Embed(
            title="Registration updated",
            description=f"Registration was successfully {past_tense[action]}.",
            color=colors.c1c_blue,
        )
        if warnings:
            embed.add_field(
                name="Sync warning",
                value="Core Sheet state was saved, but "
                + ", ".join(warnings)
                + " could not be refreshed.",
                inline=False,
            )
    except Exception as exc:
        log.exception("❌ Live Arena organizer transition failed")
        embed = error_embed(exc)
    await interaction.followup.send(embed=embed, ephemeral=True)


class ConfirmReconcile(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=300)
        self.manager = manager

    @discord.ui.button(label="Confirm Reconcile", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        config, tournament, participants, _, parity = await self.manager.data(
            interaction.guild
        )
        role = interaction.guild.get_role(int(config["PARTICIPANT_ROLE_ID"]))
        added = removed = failures = 0
        if role is None:
            failures += 1
            log.error(
                "❌ Live Arena role reconciliation — configured role missing • tournament=%s • role=%s",
                tournament.tournament_id,
                config["PARTICIPANT_ROLE_ID"],
            )
        for member, add in (
            []
            if role is None
            else [(m, True) for m in parity["missing"]]
            + [(m, False) for m in parity["extra"]]
        ):
            try:
                if add:
                    await member.add_roles(
                        role, reason="Live Arena role reconciliation"
                    )
                    added += 1
                else:
                    await member.remove_roles(
                        role, reason="Live Arena role reconciliation"
                    )
                    removed += 1
            except Exception:
                failures += 1
                log.exception(
                    "❌ Live Arena role reconciliation — user=%s • role=%s",
                    member.id,
                    role.id,
                )
        try:
            await self.manager.sync()
        except Exception:
            log.exception(
                "⚠️ Live Arena organizer panel — reconciliation refresh failed"
            )
        confirmed = sum(
            _text(r["tournament_id"]) == tournament.tournament_id
            and _text(r["status"]) == "confirmed"
            for r in participants
        )
        already_correct = (
            0 if role is None else max(0, confirmed - added - len(parity["unresolved"]))
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="Role reconciliation complete",
                description=f"Added: **{added}**\nRemoved: **{removed}**\nAlready correct: **{already_correct}**\nUnresolved: **{len(parity['unresolved'])}**\nFailures: **{failures}**",
                color=colors.c1c_blue,
            ),
            ephemeral=True,
        )


class RosterActions(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=900)
        self.manager = manager
        self.add_item(TargetSelect(manager, restore=False))
        self.add_item(TargetSelect(manager, restore=True))
        self.add_item(RefreshRoster(manager))


class RefreshRoster(discord.ui.Button):
    def __init__(self, manager):
        super().__init__(
            label="Refresh",
            custom_id="live_arena:organizer:roster:refresh",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        embed = await roster_embed(self.manager, interaction.guild)
        await interaction.response.edit_message(
            embed=embed, view=RosterActions(self.manager)
        )


async def roster_embed(manager, guild):
    """Re-read and render the current roster without mutating workbook state."""
    _, tournament, participants, counts, parity = await manager.data(guild)
    rows = [
        row
        for row in participants
        if _text(row["tournament_id"]) == tournament.tournament_id
    ]
    lines = [
        f"**{_text(row['display_name_at_signup']) or _text(row['discord_user_id'])}** — {_text(row['clan_tag_at_signup']) or '—'} • {_text(row['status']) or 'unknown'} • {_text(row['timezone']) or '—'}"
        for row in rows
    ]
    description = (
        f"**{tournament.tournament_name}** • {tournament.status}\n"
        f"Confirmed: **{counts['confirmed']}/{tournament.max_participants}** • "
        f"{'EVEN' if counts['confirmed'] % 2 == 0 else 'ODD'}\n"
        f"Role parity: **{len(parity['missing'])} missing / {len(parity['extra'])} extra / {len(parity['unresolved'])} unresolved**\n\n"
        + ("\n".join(lines) or "No participants.")
    )
    if len(description) > 4000:
        description = description[:3997] + "…"
    embed = discord.Embed(
        title="Live Arena roster", description=description, color=colors.c1c_blue
    )
    embed.add_field(
        name="Status counts",
        value=" • ".join(f"{key}: {value}" for key, value in counts.items()),
        inline=False,
    )
    return embed


class TargetSelect(discord.ui.UserSelect):
    def __init__(self, manager, restore):
        super().__init__(
            placeholder="Restore Participant" if restore else "Remove Participant",
            min_values=1,
            max_values=1,
            row=1 if restore else 0,
        )
        self.manager, self.restore = manager, restore

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        target = self.values[0]
        verb = "restore" if self.restore else "remove"
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"Confirm {verb}",
                description=f"{verb.title()} {target.mention}?",
                color=colors.c1c_blue,
            ),
            view=ConfirmParticipant(self.manager, target, self.restore),
            ephemeral=True,
        )


class ConfirmParticipant(discord.ui.View):
    def __init__(self, manager, target, restore):
        super().__init__(timeout=300)
        self.manager, self.target, self.restore = manager, target, restore

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        warnings = []
        try:
            service = OrganizerService(self.manager.sheet_id)
            await service.initialize()
            if self.restore:
                await service.restore(
                    str(interaction.user.id), str(self.target.id), self.target
                )
            else:
                await service.remove(str(interaction.user.id), str(self.target.id))
            config, _ = await load_pr5_config(self.manager.sheet_id)
            role = interaction.guild.get_role(int(config["PARTICIPANT_ROLE_ID"]))
            try:
                if role is None:
                    raise RuntimeError("configured Participant role is unavailable")
                if self.restore and role not in self.target.roles:
                    await self.target.add_roles(
                        role, reason="Live Arena participant restored"
                    )
                if not self.restore and role in self.target.roles:
                    await self.target.remove_roles(
                        role, reason="Live Arena participant removed"
                    )
            except Exception:
                warnings.append("Participant role")
                log.exception("⚠️ Live Arena participant role sync failed")
            warnings += await self.manager.secondary_sync()
            embed = discord.Embed(
                title="Participant updated",
                description="The core participant state was saved.",
                color=colors.c1c_blue,
            )
            if warnings:
                embed.add_field(
                    name="Sync warning",
                    value=", ".join(warnings) + " could not be synced.",
                    inline=False,
                )
        except Exception as exc:
            log.exception("❌ Live Arena participant mutation failed")
            embed = error_embed(exc)
        await interaction.followup.send(embed=embed, ephemeral=True)

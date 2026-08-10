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
            config, tournament, participants, counts, parity = await self.data(
                getattr(channel, "guild", None)
            )
            roster_key = _roster_message_key(self, tournament, counts)
            role_key = _role_message_key(parity)
            messages = await load_messages(
                self.sheet_id,
                config["MESSAGES_TAB"],
                {
                    "organizer_panel",
                    roster_key,
                    "organizer_statuses",
                    role_key,
                },
            )
            embed = messages["organizer_panel"].embed(
                tournament_name=tournament.tournament_name,
                status=_status_label(tournament.status),
                confirmed_count=counts["confirmed"],
                max_participants=tournament.max_participants,
            )
            # The production loader validates every requested row before returning.
            # Membership checks keep injected/test loaders focused on the lifecycle
            # behavior they are exercising without adding fallback user-facing copy.
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

            message_id = _text(config["ORGANIZER_PANEL_MESSAGE_ID"])
            message = None
            if message_id:
                # We already own and persist the organizer message ID. Prefer a
                # PartialMessage so refreshing does not depend on Read Message
                # History or a separate GET succeeding before the edit.
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
                    # Compatibility fallback for messageable channel-like objects
                    # without discord.py's get_partial_message helper.
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
                    await message.edit(embed=embed, view=self.view(tournament.status))
                except discord.NotFound:
                    # Only a confirmed 404 should cause recreation. Permission or
                    # transient edit failures must never create a duplicate panel.
                    log.warning(
                        "⚠️ Live Arena organizer panel — saved message missing; recreating • message_id=%s",
                        message_id,
                    )
                except Exception as exc:
                    log.exception(
                        "❌ Live Arena organizer panel — edit failed • message_id=%s • error=%s: %s",
                        message_id,
                        type(exc).__name__,
                        exc,
                    )
                    return PanelSyncResult(False, "edit")
                else:
                    log.info(
                        "✅ Live Arena organizer panel refreshed • message_id=%s • confirmed=%s",
                        message_id,
                        counts["confirmed"],
                    )
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
    role = guild.get_role(int(config["PARTICIPANT_ROLE_ID"])) if guild else None
    members = {_text(m.id): m for m in getattr(role, "members", [])} if role else {}
    resolved = {uid: guild.get_member(int(uid)) for uid in confirmed} if guild else {}
    return {
        "missing": [
            m for uid, m in resolved.items() if m is not None and uid not in members
        ],
        "extra": [m for uid, m in members.items() if uid not in confirmed],
        "unresolved": [uid for uid in confirmed if not resolved.get(uid)],
        "role_missing": role is None,
    }


def _status_label(status: str) -> str:
    return {
        "draft": "Draft",
        "signup_open": "Registration open",
        "signup_closed": "Registration closed",
    }.get(_text(status), _text(status).replace("_", " ").title() or "Unknown")


def _roster_message_key(manager, tournament, counts) -> str:
    confirmed = int(counts.get("confirmed", 0) or 0)
    minimum = int(getattr(tournament, "min_participants", 2) or 2)
    qualification_status = _text(
        getattr(manager, "_qualification_q1_status", "")
    ).lower()

    if qualification_status == "active":
        return "organizer_roster_active"
    if qualification_status == "completed":
        return "organizer_roster_completed"
    if tournament.status == "draft":
        return "organizer_roster_draft"
    if tournament.status == "signup_open":
        return "organizer_roster_open"
    if tournament.status == "signup_closed":
        if confirmed < minimum:
            return "organizer_roster_below_minimum"
        if confirmed % 2:
            return "organizer_roster_odd"
        return "organizer_roster_ready"
    return "organizer_roster_default"


def _roster_template_values(tournament, counts) -> dict[str, object]:
    confirmed = int(counts.get("confirmed", 0) or 0)
    return {
        "confirmed_count": confirmed,
        "player_word": "player" if confirmed == 1 else "players",
        "min_participants": int(getattr(tournament, "min_participants", 2) or 2),
    }


def _status_template_values(counts) -> dict[str, object]:
    return {
        "confirmed_count": counts.get("confirmed", 0),
        "withdrawn_count": counts.get("withdrawn", 0),
        "removed_count": counts.get("removed", 0),
        "disqualified_count": counts.get("disqualified", 0),
    }


def _member_label(member) -> str:
    return (
        _text(getattr(member, "display_name", ""))
        or _text(getattr(member, "name", ""))
        or _text(getattr(member, "id", ""))
        or "Unknown member"
    )


def _role_message_key(parity) -> str:
    if parity.get("role_missing"):
        return "organizer_roles_config_missing"
    parts = []
    if parity.get("missing"):
        parts.append("missing")
    if parity.get("extra"):
        parts.append("extra")
    if parity.get("unresolved"):
        parts.append("unresolved")
    if not parts:
        return "organizer_roles_ok"
    return "organizer_roles_" + "_".join(parts)


def _role_template_values(parity, participants) -> dict[str, object]:
    missing = [_member_label(member) for member in parity.get("missing", [])]
    extra = [_member_label(member) for member in parity.get("extra", [])]
    unresolved_ids = {_text(value) for value in parity.get("unresolved", [])}
    participant_names = {
        _text(row.get("discord_user_id")): (
            _text(row.get("display_name_at_signup"))
            or _text(row.get("discord_user_id"))
        )
        for row in participants
    }
    unresolved = [
        participant_names.get(user_id, user_id) for user_id in sorted(unresolved_ids)
    ]
    return {
        "missing_participants": ", ".join(missing),
        "extra_participants": ", ".join(extra),
        "unresolved_participants": ", ".join(unresolved),
    }


def _add_template_field(embed, template, **values) -> None:
    title, description = template.render(**values)
    embed.add_field(
        name=title or "\u200b",
        value=description or "\u200b",
        inline=False,
    )


def _response_is_done(interaction) -> bool:
    is_done = getattr(interaction.response, "is_done", None)
    return bool(is_done()) if callable(is_done) else False


async def _defer_ephemeral(interaction) -> None:
    defer = getattr(interaction.response, "defer", None)
    if callable(defer):
        await defer(ephemeral=True)


async def _send_ephemeral(interaction, *, embed, view=None) -> None:
    kwargs = {"embed": embed, "ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    if _response_is_done(interaction):
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


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
            await _send_ephemeral(
                interaction,
                embed=error_embed(
                    "You need the configured organizer role to use this control."
                ),
            )
        return allowed

    async def transition(self, interaction, action):
        if action == "close":
            await _defer_ephemeral(interaction)
            if not await self.authorized(interaction):
                return
            try:
                _, tournament, _, counts, _ = await self.manager.data(interaction.guild)
                warning = ""
                if counts["confirmed"] % 2:
                    warning = (
                        " The confirmed roster currently has an odd number of players "
                        f"(**{counts['confirmed']}**). Closing registration is still "
                        "allowed and no player will be auto-demoted, but an even number "
                        "is required before the first qualification round can be paired."
                    )
                embed = discord.Embed(
                    title="Confirm close registration",
                    description=f"Close registration with **{counts['confirmed']}** confirmed players?{warning}",
                    color=colors.c1c_blue,
                )
            except Exception as exc:
                log.exception(
                    "❌ Live Arena organizer close preflight failed • user=%s",
                    interaction.user.id,
                )
                await _send_ephemeral(interaction, embed=error_embed(exc))
                return
            await _send_ephemeral(
                interaction,
                embed=embed,
                view=ConfirmTransition(self.manager, "close"),
            )
            return
        if not await self.authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await execute_transition(interaction, self.manager, action)

    async def roster(self, interaction, _action):
        await _defer_ephemeral(interaction)
        if not await self.authorized(interaction):
            return
        try:
            embed = await roster_embed(self.manager, interaction.guild)
        except Exception as exc:
            log.exception(
                "❌ Live Arena organizer roster load failed • user=%s",
                interaction.user.id,
            )
            await _send_ephemeral(interaction, embed=error_embed(exc))
            return
        await _send_ephemeral(
            interaction,
            embed=embed,
            view=RosterActions(self.manager),
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
            0
            if role is None
            else max(
                0,
                confirmed - len(parity["missing"]) - len(parity["unresolved"]),
            )
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
    config, tournament, participants, counts, parity = await manager.data(guild)
    rows = [
        row
        for row in participants
        if _text(row["tournament_id"]) == tournament.tournament_id
    ]
    roster_key = _roster_message_key(manager, tournament, counts)
    role_key = _role_message_key(parity)
    participant_key = (
        "organizer_roster_participants"
        if rows
        else "organizer_roster_no_participants"
    )
    message_keys = {
        "organizer_roster_view",
        roster_key,
        "organizer_statuses",
        role_key,
        participant_key,
    }
    if rows:
        message_keys.add("organizer_roster_participant_line")
    messages = await load_messages(
        manager.sheet_id,
        config["MESSAGES_TAB"],
        message_keys,
    )
    embed = messages["organizer_roster_view"].embed(
        tournament_name=tournament.tournament_name,
        status=_status_label(tournament.status),
        confirmed_count=counts["confirmed"],
        max_participants=tournament.max_participants,
    )
    _add_template_field(
        embed,
        messages[roster_key],
        **_roster_template_values(tournament, counts),
    )
    _add_template_field(
        embed,
        messages["organizer_statuses"],
        **_status_template_values(counts),
    )
    _add_template_field(
        embed,
        messages[role_key],
        **_role_template_values(parity, participants),
    )

    if not rows:
        _add_template_field(embed, messages["organizer_roster_no_participants"])
        return embed

    line_template = messages["organizer_roster_participant_line"]
    rendered_lines = []
    for row in rows:
        _, line = line_template.render(
            participant_name=(
                _text(row["display_name_at_signup"])
                or _text(row["discord_user_id"])
            ),
            clan_tag=_text(row["clan_tag_at_signup"]) or "—",
            participant_status=_text(row["status"]) or "unknown",
            timezone=_text(row["timezone"]) or "—",
        )
        rendered_lines.append(line)

    participant_template = messages["organizer_roster_participants"]
    chunks = _chunk_lines(rendered_lines)
    for index, chunk in enumerate(chunks):
        title, description = participant_template.render(
            participant_lines="\n".join(chunk)
        )
        embed.add_field(
            name=title if index == 0 else "\u200b",
            value=description,
            inline=False,
        )
    return embed


def _chunk_lines(lines, limit=1000):
    chunks = []
    current = []
    current_size = 0
    for line in lines:
        added = len(line) + (1 if current else 0)
        if current and current_size + added > limit:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(line)
        current_size += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append(current)
    return chunks


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

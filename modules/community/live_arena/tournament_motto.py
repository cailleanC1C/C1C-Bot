"""Optional per-tournament motto support for Live Arena.

The existing workbook contract historically treated TOURNAMENTS as an exact
14-column table.  This feature is deliberately backward-compatible so code can be
deployed before the live workbook gains the new ``tournament_motto`` column.
Once the column exists, Create New Tournament stores the motto with the tournament
and the public signup/final-result surfaces reuse it.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import discord

from shared.sheets.async_core import sheet_read_scope

log = logging.getLogger("c1c.community.live_arena.tournament_motto")
_installed = False
_MOTTO_HEADER = "tournament_motto"
_MAX_MOTTO_LENGTH = 160
_public_motto: contextvars.ContextVar[str] = contextvars.ContextVar(
    "live_arena_public_motto", default=""
)


def _clean_motto(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > _MAX_MOTTO_LENGTH:
        from modules.community.live_arena.registration import RegistrationError

        raise RegistrationError(
            f"tournament motto must be {_MAX_MOTTO_LENGTH} characters or fewer"
        )
    return text


def _tournament_rows(original_rows, service_module):
    """Return an _rows-compatible function that permits one optional TOURNAMENTS field."""

    def tolerant_rows(matrix, expected, tab):
        if expected != service_module.TOURNAMENT_HEADERS:
            return original_rows(matrix, expected, tab)
        if not matrix:
            raise service_module.LiveArenaConfigError(f"{tab}: required header row missing")
        headers = tuple(service_module._text(value) for value in matrix[0])
        missing = [header for header in expected if header not in headers]
        if missing:
            raise service_module.LiveArenaConfigError(
                f"{tab}: required header missing: {', '.join(missing)}"
            )
        unexpected = [
            header
            for header in headers
            if header not in expected and header != _MOTTO_HEADER
        ]
        if unexpected:
            raise service_module.LiveArenaConfigError(
                f"{tab}: unexpected header: {', '.join(unexpected)}"
            )
        if headers.count(_MOTTO_HEADER) > 1:
            raise service_module.LiveArenaConfigError(
                f"{tab}: {_MOTTO_HEADER} header must occur at most once"
            )
        return [
            dict(zip(headers, row))
            for row in matrix[1:]
            if any(service_module._text(value) for value in row)
        ]

    return tolerant_rows


def _install_tournament_row_compatibility() -> None:
    from modules.community.live_arena import service

    original = service._rows
    if getattr(original, "_live_arena_motto_compatible", False):
        return
    tolerant = _tournament_rows(original, service)
    tolerant._live_arena_motto_compatible = True
    service._rows = tolerant

    # Several Live Arena modules import _rows directly. Replace the exact original
    # reference everywhere that already imported it, and future imports will receive
    # service._rows above.
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "_rows", None) is original:
            setattr(module, "_rows", tolerant)


def _inject_motto_after_first_line(description: str | None, motto: str) -> str:
    text = str(description or "")
    motto = _clean_motto(motto)
    if not motto:
        return text
    motto_line = f"*{motto}*"
    first, separator, rest = text.partition("\n")
    if separator:
        return f"{first}\n{motto_line}\n{rest}"
    if text:
        return f"{text}\n{motto_line}"
    return motto_line


def _prepend_motto(description: str | None, motto: str) -> str:
    text = str(description or "")
    motto = _clean_motto(motto)
    if not motto:
        return text
    motto_line = f"*{motto}*"
    if text.startswith(motto_line):
        return text
    return f"{motto_line}\n\n{text}" if text else motto_line


async def _tournament_motto(sheet_id: str, tournament_id: str | None = None) -> str:
    from shared.sheets.async_core import afetch_values
    from modules.community.live_arena import service

    config = await service.load_config(sheet_id)
    tab = config["TOURNAMENTS_TAB"]
    matrix = await afetch_values(sheet_id, tab) or []
    rows = service._rows(matrix, service.TOURNAMENT_HEADERS, tab)
    wanted = str(tournament_id or config["ACTIVE_TOURNAMENT_ID"])
    matches = [
        row for row in rows if service._text(row.get("tournament_id")) == wanted
    ]
    if len(matches) != 1:
        raise service.LiveArenaConfigError(
            f"{tab}: tournament must occur exactly once: {wanted}"
        )
    return _clean_motto(matches[0].get(_MOTTO_HEADER, ""))


def _install_public_panel_motto() -> None:
    from modules.community.live_arena import messages, panel

    original_embed = messages.MessageTemplate.embed
    if not getattr(original_embed, "_live_arena_motto_installed", False):

        def embed_with_motto(self, **values):
            embed = original_embed(self, **values)
            motto = _public_motto.get()
            if self.key == "signup_open" and motto:
                embed.description = _inject_motto_after_first_line(
                    embed.description, motto
                )
            return embed

        embed_with_motto._live_arena_motto_installed = True
        messages.MessageTemplate.embed = embed_with_motto

    original_sync = panel.LiveArenaPanelManager.sync
    if getattr(original_sync, "_live_arena_motto_installed", False):
        return

    async def sync_with_motto(self):
        motto = ""
        with sheet_read_scope():
            try:
                motto = await _tournament_motto(self.sheet_id)
            except Exception:
                # Motto presentation must never make the registration panel fail.
                log.exception("Live Arena tournament motto lookup failed during public panel sync")
            token = _public_motto.set(motto)
            try:
                return await original_sync(self)
            finally:
                _public_motto.reset(token)

    sync_with_motto._live_arena_motto_installed = True
    panel.LiveArenaPanelManager.sync = sync_with_motto


def _install_next_tournament_motto() -> None:
    from modules.community.live_arena import next_tournament
    from modules.community.live_arena.organizer_panel import OrganizerView

    original_draft = next_tournament.NextTournamentDraft
    if not hasattr(original_draft, _MOTTO_HEADER):

        @dataclass(frozen=True)
        class ThemedNextTournamentDraft(original_draft):
            tournament_motto: str = ""

        next_tournament.NextTournamentDraft = ThemedNextTournamentDraft

    next_tournament._NEXT_MESSAGE_KEYS.setdefault("next_tournament_theme", set())
    next_tournament._NEXT_MESSAGE_KEYS.setdefault(
        "next_tournament_review_motto", {"tournament_motto"}
    )

    async def basics_submit_with_theme(self, interaction):
        try:
            name, short, minimum, maximum, timezone = next_tournament._validate_basics(
                str(self.tournament_name.value),
                str(self.short_name.value),
                str(self.minimum.value),
                str(self.maximum.value),
                str(self.timezone.value),
            )
            draft = next_tournament.NextTournamentDraft(
                tournament_name=name,
                short_name=short,
                min_participants=minimum,
                max_participants=maximum,
                timezone=timezone,
            )
            templates = await next_tournament._load_next_messages(
                self.manager.sheet_id, {"next_tournament_theme"}
            )
            await interaction.response.send_message(
                embed=templates["next_tournament_theme"].embed(),
                view=TournamentThemeView(self.manager, draft),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(
                embed=next_tournament.error_embed(exc), ephemeral=True
            )

    next_tournament.NextTournamentBasicsModal.on_submit = basics_submit_with_theme

    async def clan_select_with_motto(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        try:
            draft = replace(self.draft, eligible_role_ids=tuple(self.values))
            by_role = {item.discord_role_id: item for item in self.clan_options}
            selected = [by_role[value] for value in self.values]
            keys = {"next_tournament_review"}
            motto = _clean_motto(getattr(draft, _MOTTO_HEADER, ""))
            if motto:
                keys.add("next_tournament_review_motto")
            templates = await next_tournament._load_next_messages(
                self.manager.sheet_id, keys
            )
            embed = templates["next_tournament_review"].embed(
                tournament_name=draft.tournament_name,
                short_name=draft.short_name,
                min_participants=draft.min_participants,
                max_participants=draft.max_participants,
                timezone=draft.timezone,
                signup_opens=next_tournament.discord_timestamp(
                    draft.signup_opens_at_utc
                ),
                signup_closes=next_tournament.discord_timestamp(
                    draft.signup_closes_at_utc
                ),
                eligible_clans=", ".join(item.label for item in selected),
            )
            if motto:
                motto_embed = templates["next_tournament_review_motto"].embed(
                    tournament_motto=motto
                )
                embed.add_field(
                    name=motto_embed.title or "Motto",
                    value=motto_embed.description or motto,
                    inline=False,
                )
            await interaction.response.edit_message(
                embed=embed,
                view=next_tournament.ConfirmCreateNextTournamentView(
                    self.manager, draft
                ),
            )
        except Exception as exc:
            await interaction.response.send_message(
                embed=next_tournament.error_embed(exc), ephemeral=True
            )

    next_tournament.NextTournamentClanSelect.callback = clan_select_with_motto
    next_tournament.NextTournamentService.create = _create_next_tournament_with_motto


class TournamentThemeView(discord.ui.View):
    def __init__(self, manager, draft):
        super().__init__(timeout=900)
        self.manager, self.draft = manager, draft

    @discord.ui.button(label="Add Motto", style=discord.ButtonStyle.primary)
    async def add_motto(self, interaction, _button):
        from modules.community.live_arena.organizer_panel import OrganizerView

        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.send_modal(
            TournamentMottoModal(self.manager, self.draft)
        )

    @discord.ui.button(label="Skip Motto", style=discord.ButtonStyle.secondary)
    async def skip_motto(self, interaction, _button):
        from modules.community.live_arena import next_tournament
        from modules.community.live_arena.organizer_panel import OrganizerView

        if not await OrganizerView(self.manager).authorized(interaction):
            return
        try:
            templates = await next_tournament._load_next_messages(
                self.manager.sheet_id, {"next_tournament_schedule"}
            )
            await interaction.response.edit_message(
                embed=templates["next_tournament_schedule"].embed(),
                view=next_tournament.NextTournamentScheduleView(
                    self.manager, self.draft
                ),
            )
        except Exception as exc:
            await interaction.response.send_message(
                embed=next_tournament.error_embed(exc), ephemeral=True
            )


class TournamentMottoModal(discord.ui.Modal, title="Tournament Motto"):
    motto = discord.ui.TextInput(
        label="Motto or tagline (optional)",
        placeholder="No maps. No mercy. Just glorious bad decisions.",
        required=False,
        max_length=_MAX_MOTTO_LENGTH,
    )

    def __init__(self, manager, draft):
        super().__init__(timeout=900)
        self.manager, self.draft = manager, draft

    async def on_submit(self, interaction):
        from modules.community.live_arena import next_tournament

        try:
            draft = replace(
                self.draft,
                tournament_motto=_clean_motto(str(self.motto.value)),
            )
            templates = await next_tournament._load_next_messages(
                self.manager.sheet_id, {"next_tournament_schedule"}
            )
            await interaction.response.send_message(
                embed=templates["next_tournament_schedule"].embed(),
                view=next_tournament.NextTournamentScheduleView(
                    self.manager, draft
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(
                embed=next_tournament.error_embed(exc), ephemeral=True
            )


async def _create_next_tournament_with_motto(self, actor_id: str, draft) -> str:
    """Create the next tournament with a schema-aware optional motto column."""
    from modules.community.live_arena import next_tournament

    snapshot = await next_tournament.load_tournament_snapshot(self.sheet_id)
    if snapshot.status != "archived":
        raise next_tournament.RegistrationError(
            "the current tournament must be archived before the next tournament is created"
        )
    if not draft.eligible_role_ids:
        raise next_tournament.RegistrationError("select at least one eligible clan")
    try:
        opens = datetime.fromisoformat(
            draft.signup_opens_at_utc.replace("Z", "+00:00")
        )
        closes = datetime.fromisoformat(
            draft.signup_closes_at_utc.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise next_tournament.RegistrationError("signup window is invalid") from exc
    now = self.clock().astimezone(UTC)
    if closes <= now:
        raise next_tournament.RegistrationError(
            "signup closing time must still be in the future"
        )
    if closes <= opens:
        raise next_tournament.RegistrationError(
            "signup closing time must be after signup opening time"
        )

    config = await next_tournament.load_config(self.sheet_id)
    tournaments_matrix, clans_matrix, config_matrix = await asyncio.gather(
        next_tournament.afetch_values(
            self.sheet_id, config["TOURNAMENTS_TAB"]
        ),
        next_tournament.afetch_values(
            self.sheet_id, config["ELIGIBLE_CLANS_TAB"]
        ),
        next_tournament.afetch_values(self.sheet_id, next_tournament.CONFIG_TAB),
    )
    tournaments = next_tournament._rows(
        tournaments_matrix or [],
        next_tournament.TOURNAMENT_HEADERS,
        config["TOURNAMENTS_TAB"],
    )
    clans = next_tournament._rows(
        clans_matrix or [],
        next_tournament.ELIGIBLE_CLAN_HEADERS,
        config["ELIGIBLE_CLANS_TAB"],
    )
    next_tournament._rows(
        config_matrix or [], next_tournament.CONFIG_HEADERS, next_tournament.CONFIG_TAB
    )

    tournament_headers = [
        next_tournament._text(value) for value in (tournaments_matrix or [[]])[0]
    ]
    motto = _clean_motto(getattr(draft, _MOTTO_HEADER, ""))
    if motto and _MOTTO_HEADER not in tournament_headers:
        raise next_tournament.LiveArenaConfigError(
            "TOURNAMENTS: tournament_motto header is required before creating a tournament with a motto"
        )
    if tournament_headers.count(_MOTTO_HEADER) > 1:
        raise next_tournament.LiveArenaConfigError(
            "TOURNAMENTS: tournament_motto header must occur exactly once"
        )

    new_id = self._new_id(tournaments, now)
    role_map = {}
    for row in clans:
        role_id = next_tournament._text(row["discord_role_id"])
        if role_id:
            role_map[role_id] = row
    missing = [
        role_id
        for role_id in draft.eligible_role_ids
        if role_id not in role_map
    ]
    if missing:
        raise next_tournament.RegistrationError(
            "one or more selected clan roles no longer exist in ELIGIBLE_CLANS"
        )

    old_repository = next_tournament.LiveArenaRepository(self.sheet_id)
    await old_repository.initialize()
    await old_repository.retire_discord_resources(
        snapshot.tournament_id, updated_at_utc=next_tournament.utc_iso(now)
    )

    status = "signup_open" if opens <= now else "draft"
    tournament_values = {
        "tournament_id": new_id,
        "tournament_name": draft.tournament_name,
        "status": status,
        "eligibility_scope": "selected_clans",
        "min_participants": draft.min_participants,
        "max_participants": draft.max_participants,
        "signup_opens_at_utc": draft.signup_opens_at_utc,
        "signup_closes_at_utc": draft.signup_closes_at_utc,
        "notes": "Created through the Discord Create Next Tournament workflow.",
        "tournament_short_name": draft.short_name,
        "created_at_utc": next_tournament.utc_iso(now),
        "completed_at_utc": "",
        "archived_at_utc": "",
        "timezone": draft.timezone,
        _MOTTO_HEADER: motto,
    }
    clan_values = []
    for role_id in draft.eligible_role_ids:
        source = role_map[role_id]
        clan_values.append(
            {
                "tournament_id": new_id,
                "clan_tag": next_tournament._text(source["clan_tag"]),
                "clan_name": next_tournament._text(source["clan_name"]),
                "discord_role_id": role_id,
                "active": "TRUE",
                "notes": "Copied into new tournament by Create Next Tournament.",
            }
        )

    headers = [next_tournament._text(value) for value in config_matrix[0]]
    key_col, value_col = headers.index("Key"), headers.index("Value")
    config_rows = {}
    for key in (
        "ACTIVE_TOURNAMENT_ID",
        "PUBLIC_PANEL_MESSAGE_ID",
        "ORGANIZER_PANEL_MESSAGE_ID",
    ):
        matches = [
            index
            for index, row in enumerate(config_matrix[1:], 2)
            if key_col < len(row) and next_tournament._text(row[key_col]) == key
        ]
        if len(matches) != 1:
            raise next_tournament.LiveArenaConfigError(
                f"CONFIG: key {key} must occur exactly once"
            )
        config_rows[key] = matches[0]

    escaped_tournaments = config["TOURNAMENTS_TAB"].replace("'", "''")
    escaped_clans = config["ELIGIBLE_CLANS_TAB"].replace("'", "''")
    tournament_row = len(tournaments_matrix or []) + 1
    clan_start = len(clans_matrix or []) + 1
    tournament_end_column = next_tournament._column(len(tournament_headers))
    data = [
        {
            "range": (
                f"'{escaped_tournaments}'!A{tournament_row}:"
                f"{tournament_end_column}{tournament_row}"
            ),
            "values": [
                [str(tournament_values.get(header, "")) for header in tournament_headers]
            ],
        },
        {
            "range": (
                f"'{escaped_clans}'!A{clan_start}:F"
                f"{clan_start + len(clan_values) - 1}"
            ),
            "values": [
                [str(row[h]) for h in next_tournament.ELIGIBLE_CLAN_HEADERS]
                for row in clan_values
            ],
        },
        {
            "range": (
                f"'{next_tournament.CONFIG_TAB}'!"
                f"{next_tournament._column(value_col + 1)}"
                f"{config_rows['ACTIVE_TOURNAMENT_ID']}"
            ),
            "values": [[new_id]],
        },
        {
            "range": (
                f"'{next_tournament.CONFIG_TAB}'!"
                f"{next_tournament._column(value_col + 1)}"
                f"{config_rows['PUBLIC_PANEL_MESSAGE_ID']}"
            ),
            "values": [[""]],
        },
        {
            "range": (
                f"'{next_tournament.CONFIG_TAB}'!"
                f"{next_tournament._column(value_col + 1)}"
                f"{config_rows['ORGANIZER_PANEL_MESSAGE_ID']}"
            ),
            "values": [[""]],
        },
    ]
    worksheet = await next_tournament.aget_worksheet(
        self.sheet_id, config["TOURNAMENTS_TAB"]
    )
    await next_tournament.acall_with_backoff(
        worksheet.spreadsheet.values_batch_update,
        body={"valueInputOption": "RAW", "data": data},
    )

    await self._record_event(
        new_id,
        actor_id,
        "tournament_created",
        {
            "previous_tournament_id": snapshot.tournament_id,
            "status": status,
            "eligible_role_ids": list(draft.eligible_role_ids),
            "tournament_motto": motto,
        },
        now,
    )
    return new_id


def _install_final_recap_motto() -> None:
    from modules.community.live_arena import knockout_runtime

    original = knockout_runtime._sync_final_recap
    if getattr(original, "_live_arena_motto_installed", False):
        return

    async def sync_final_recap_with_motto(manager, service, summary):
        await original(manager, service, summary)
        motto = ""
        try:
            motto = await _tournament_motto(
                service.sheet_id, summary.get("tournament_id")
            )
            if not motto:
                return
            resource = await service.registration_repository.discord_resource(
                summary["tournament_id"], "final_recap", "main"
            )
            if not resource:
                return
            channel_id = str(resource.get("channel_id") or "").strip()
            message_id = str(resource.get("message_id") or "").strip()
            if not channel_id or not message_id:
                return
            channel = manager.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await manager.bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            if not getattr(message, "embeds", None):
                return
            embed = discord.Embed.from_dict(message.embeds[0].to_dict())
            embed.description = _prepend_motto(embed.description, motto)
            await message.edit(embed=embed)
        except Exception:
            # Completion state and factual recap remain authoritative even if the
            # decorative motto edit has a transient Discord/Sheet failure.
            log.exception(
                "Live Arena tournament motto final-recap synchronization failed"
            )

    sync_final_recap_with_motto._live_arena_motto_installed = True
    knockout_runtime._sync_final_recap = sync_final_recap_with_motto


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    _install_tournament_row_compatibility()
    _install_next_tournament_motto()
    _install_public_panel_motto()
    _install_final_recap_motto()

"""Organizer-only Create Next Tournament wizard and scheduled signup opening."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

from modules.community.live_arena.messages import MESSAGE_HEADERS, discord_timestamp, load_pr5_config
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.registration import RegistrationError, utc_iso
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import (
    CONFIG_HEADERS,
    CONFIG_TAB,
    ELIGIBLE_CLAN_HEADERS,
    TOURNAMENT_HEADERS,
    LiveArenaConfigError,
    _enabled,
    _rows,
    _text,
    load_config,
    load_tournament_snapshot,
)
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.next_tournament")
_installed = False
_open_tasks: dict[str, asyncio.Task] = {}

_NEXT_MESSAGE_KEYS = {
    "next_tournament_intro": set(),
    "next_tournament_schedule": set(),
    "next_tournament_eligibility": set(),
    "next_tournament_review": {
        "tournament_name",
        "short_name",
        "min_participants",
        "max_participants",
        "timezone",
        "signup_opens",
        "signup_closes",
        "eligible_clans",
    },
    "next_tournament_created": {
        "tournament_name",
        "tournament_id",
        "signup_opens",
        "signup_closes",
    },
}


@dataclass(frozen=True)
class NextTournamentDraft:
    tournament_name: str = ""
    short_name: str = ""
    min_participants: int = 8
    max_participants: int = 16
    timezone: str = "UTC"
    signup_opens_at_utc: str = ""
    signup_closes_at_utc: str = ""
    eligible_role_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClanOption:
    clan_tag: str
    clan_name: str
    discord_role_id: str
    active_current: bool = False

    @property
    def label(self) -> str:
        return f"{self.clan_tag} · {self.clan_name}"[:100]


class _SheetMessage:
    def __init__(self, key: str, title: str, description: str, color: int):
        self.key, self.title, self.description, self.color = key, title, description, color

    def embed(self, **values) -> discord.Embed:
        expected = _NEXT_MESSAGE_KEYS[self.key]
        missing = expected - values.keys()
        if missing:
            raise LiveArenaConfigError(
                f"MESSAGES.{self.key}: missing render value {', '.join(sorted(missing))}"
            )
        return discord.Embed(
            title=self.title.format(**values),
            description=self.description.format(**values),
            color=self.color,
        )


async def _load_next_messages(sheet_id: str, keys: set[str]):
    config, _ = await load_pr5_config(sheet_id)
    matrix = await afetch_values(sheet_id, config["MESSAGES_TAB"]) or []
    rows = _rows(matrix, MESSAGE_HEADERS, config["MESSAGES_TAB"])
    result = {}
    from string import Formatter

    for key in keys:
        if key not in _NEXT_MESSAGE_KEYS:
            raise LiveArenaConfigError(f"unknown next-tournament message key: {key}")
        matches = [
            row
            for row in rows
            if _text(row["message_key"]) == key and _enabled(row["active"])
        ]
        if len(matches) != 1:
            raise LiveArenaConfigError(
                f"MESSAGES: required active row missing or duplicated: {key}"
            )
        row = matches[0]
        color = _text(row["color_hex"])
        if len(color) != 7 or not color.startswith("#"):
            raise LiveArenaConfigError(f"MESSAGES.{key}: color_hex must be #RRGGBB")
        try:
            parsed = int(color[1:], 16)
        except ValueError as exc:
            raise LiveArenaConfigError(f"MESSAGES.{key}: invalid color_hex") from exc
        fields = {
            name
            for _, name, _, _ in Formatter().parse(
                _text(row["title"]) + _text(row["description"])
            )
            if name
        }
        if fields != _NEXT_MESSAGE_KEYS[key]:
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: placeholders must be exactly "
                + ", ".join(sorted(_NEXT_MESSAGE_KEYS[key]))
            )
        result[key] = _SheetMessage(
            key, _text(row["title"]), _text(row["description"]), parsed
        )
    return result


def _parse_local_datetime(value: str, timezone: str, label: str) -> datetime:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RegistrationError("timezone must be a valid IANA timezone") from exc
    text = _text(value)
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise RegistrationError(f"{label} must use YYYY-MM-DD HH:MM")
    return parsed.replace(tzinfo=zone).astimezone(UTC)


def _validate_basics(name: str, short_name: str, minimum: str, maximum: str, timezone: str):
    name = _text(name)
    short_name = _text(short_name)
    timezone = _text(timezone)
    if not name or len(name) > 100:
        raise RegistrationError("tournament name must be 1-100 characters")
    if not short_name or len(short_name) > 32:
        raise RegistrationError("short name must be 1-32 characters")
    try:
        min_value, max_value = int(_text(minimum)), int(_text(maximum))
    except ValueError as exc:
        raise RegistrationError("minimum and maximum players must be whole numbers") from exc
    if min_value < 2 or max_value < min_value or max_value > 64:
        raise RegistrationError("player limits must satisfy 2 ≤ minimum ≤ maximum ≤ 64")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RegistrationError("timezone must be a valid IANA timezone") from exc
    return name, short_name, min_value, max_value, timezone


class NextTournamentService:
    def __init__(self, sheet_id: str, *, clock=None):
        self.sheet_id = str(sheet_id)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def clan_options(self) -> list[ClanOption]:
        config = await load_config(self.sheet_id)
        matrix = await afetch_values(self.sheet_id, config["ELIGIBLE_CLANS_TAB"]) or []
        rows = _rows(matrix, ELIGIBLE_CLAN_HEADERS, config["ELIGIBLE_CLANS_TAB"])
        current = config["ACTIVE_TOURNAMENT_ID"]
        current_active = {
            _text(row["discord_role_id"])
            for row in rows
            if _text(row["tournament_id"]) == current and _enabled(row["active"])
        }
        by_role: dict[str, ClanOption] = {}
        for row in rows:
            role_id = _text(row["discord_role_id"])
            if not role_id:
                continue
            by_role[role_id] = ClanOption(
                clan_tag=_text(row["clan_tag"]),
                clan_name=_text(row["clan_name"]),
                discord_role_id=role_id,
                active_current=role_id in current_active,
            )
        options = sorted(by_role.values(), key=lambda item: (item.clan_tag, item.clan_name))
        if not options:
            raise RegistrationError("no eligible clan definitions are available")
        if len(options) > 25:
            raise RegistrationError(
                "more than 25 clan definitions exist; Create Next Tournament needs a paged selector before it can be used"
            )
        return options

    async def create(self, actor_id: str, draft: NextTournamentDraft) -> str:
        snapshot = await load_tournament_snapshot(self.sheet_id)
        if snapshot.status != "archived":
            raise RegistrationError(
                "the current tournament must be archived before the next tournament is created"
            )
        if not draft.eligible_role_ids:
            raise RegistrationError("select at least one eligible clan")
        try:
            opens = datetime.fromisoformat(draft.signup_opens_at_utc.replace("Z", "+00:00"))
            closes = datetime.fromisoformat(draft.signup_closes_at_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RegistrationError("signup window is invalid") from exc
        now = self.clock().astimezone(UTC)
        if closes <= now:
            raise RegistrationError("signup closing time must still be in the future")
        if closes <= opens:
            raise RegistrationError("signup closing time must be after signup opening time")

        config = await load_config(self.sheet_id)
        tournaments_matrix, clans_matrix, config_matrix = await asyncio.gather(
            afetch_values(self.sheet_id, config["TOURNAMENTS_TAB"]),
            afetch_values(self.sheet_id, config["ELIGIBLE_CLANS_TAB"]),
            afetch_values(self.sheet_id, CONFIG_TAB),
        )
        tournaments = _rows(
            tournaments_matrix or [], TOURNAMENT_HEADERS, config["TOURNAMENTS_TAB"]
        )
        clans = _rows(
            clans_matrix or [], ELIGIBLE_CLAN_HEADERS, config["ELIGIBLE_CLANS_TAB"]
        )
        _rows(config_matrix or [], CONFIG_HEADERS, CONFIG_TAB)

        new_id = self._new_id(tournaments, now)
        role_map = {}
        for row in clans:
            role_id = _text(row["discord_role_id"])
            if role_id:
                role_map[role_id] = row
        missing = [role_id for role_id in draft.eligible_role_ids if role_id not in role_map]
        if missing:
            raise RegistrationError("one or more selected clan roles no longer exist in ELIGIBLE_CLANS")

        old_repository = LiveArenaRepository(self.sheet_id)
        await old_repository.initialize()
        await old_repository.retire_discord_resources(
            snapshot.tournament_id, updated_at_utc=utc_iso(now)
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
            "created_at_utc": utc_iso(now),
            "completed_at_utc": "",
            "archived_at_utc": "",
            "timezone": draft.timezone,
        }
        clan_values = []
        for role_id in draft.eligible_role_ids:
            source = role_map[role_id]
            clan_values.append(
                {
                    "tournament_id": new_id,
                    "clan_tag": _text(source["clan_tag"]),
                    "clan_name": _text(source["clan_name"]),
                    "discord_role_id": role_id,
                    "active": "TRUE",
                    "notes": "Copied into new tournament by Create Next Tournament.",
                }
            )

        headers = [_text(value) for value in config_matrix[0]]
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
                if key_col < len(row) and _text(row[key_col]) == key
            ]
            if len(matches) != 1:
                raise LiveArenaConfigError(f"CONFIG: key {key} must occur exactly once")
            config_rows[key] = matches[0]

        escaped_tournaments = config["TOURNAMENTS_TAB"].replace("'", "''")
        escaped_clans = config["ELIGIBLE_CLANS_TAB"].replace("'", "''")
        tournament_row = len(tournaments_matrix or []) + 1
        clan_start = len(clans_matrix or []) + 1
        data = [
            {
                "range": f"'{escaped_tournaments}'!A{tournament_row}:N{tournament_row}",
                "values": [[str(tournament_values[h]) for h in TOURNAMENT_HEADERS]],
            },
            {
                "range": f"'{escaped_clans}'!A{clan_start}:F{clan_start + len(clan_values) - 1}",
                "values": [[str(row[h]) for h in ELIGIBLE_CLAN_HEADERS] for row in clan_values],
            },
            {
                "range": f"'{CONFIG_TAB}'!{_column(value_col + 1)}{config_rows['ACTIVE_TOURNAMENT_ID']}",
                "values": [[new_id]],
            },
            {
                "range": f"'{CONFIG_TAB}'!{_column(value_col + 1)}{config_rows['PUBLIC_PANEL_MESSAGE_ID']}",
                "values": [[""]],
            },
            {
                "range": f"'{CONFIG_TAB}'!{_column(value_col + 1)}{config_rows['ORGANIZER_PANEL_MESSAGE_ID']}",
                "values": [[""]],
            },
        ]
        worksheet = await aget_worksheet(self.sheet_id, config["TOURNAMENTS_TAB"])
        await acall_with_backoff(
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
            },
            now,
        )
        return new_id

    @staticmethod
    def _new_id(tournaments, now: datetime) -> str:
        base = f"LA-{now:%Y%m%d-%H%M%S}"
        existing = {_text(row["tournament_id"]) for row in tournaments}
        if base not in existing:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    async def _record_event(self, tournament_id, actor_id, event_type, details, now):
        try:
            repository = LiveArenaRepository(self.sheet_id)
            await repository.initialize()
            await repository.append_audit(
                {
                    "event_id": f"next-{tournament_id}-{int(now.timestamp())}",
                    "tournament_id": tournament_id,
                    "event_type": event_type,
                    "actor_discord_user_id": str(actor_id),
                    "target_discord_user_id": "",
                    "details": __import__("json").dumps(
                        details, sort_keys=True, separators=(",", ":")
                    ),
                    "created_at_utc": utc_iso(now),
                }
            )
        except Exception:
            log.exception("Live Arena next-tournament event append failed")


def _column(number: int) -> str:
    if not 1 <= number <= 26:
        raise LiveArenaConfigError("unsupported CONFIG width")
    return chr(64 + number)


class CreateNextTournamentButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Create Next Tournament",
            custom_id="live_arena:organizer:tournament:create_next",
            style=discord.ButtonStyle.success,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        try:
            messages = await _load_next_messages(
                self.manager.sheet_id, {"next_tournament_intro"}
            )
            await interaction.response.send_message(
                embed=messages["next_tournament_intro"].embed(),
                view=NextTournamentStartView(self.manager),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena Create Next Tournament start failed")
            await interaction.response.send_message(embed=error_embed(exc), ephemeral=True)


class NextTournamentStartView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=900)
        self.manager = manager

    @discord.ui.button(label="Enter Tournament Details", style=discord.ButtonStyle.primary)
    async def start(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.send_modal(NextTournamentBasicsModal(self.manager))


class NextTournamentBasicsModal(discord.ui.Modal, title="Next Live Arena Tournament"):
    tournament_name = discord.ui.TextInput(label="Tournament name", max_length=100)
    short_name = discord.ui.TextInput(label="Short name", max_length=32)
    minimum = discord.ui.TextInput(label="Minimum players", default="8", max_length=2)
    maximum = discord.ui.TextInput(label="Maximum players", default="16", max_length=2)
    timezone = discord.ui.TextInput(
        label="Tournament timezone",
        default="UTC",
        placeholder="Europe/Vienna",
        max_length=64,
    )

    def __init__(self, manager):
        super().__init__(timeout=900)
        self.manager = manager

    async def on_submit(self, interaction):
        try:
            name, short, minimum, maximum, timezone = _validate_basics(
                str(self.tournament_name.value),
                str(self.short_name.value),
                str(self.minimum.value),
                str(self.maximum.value),
                str(self.timezone.value),
            )
            draft = NextTournamentDraft(
                tournament_name=name,
                short_name=short,
                min_participants=minimum,
                max_participants=maximum,
                timezone=timezone,
            )
            messages = await _load_next_messages(
                self.manager.sheet_id, {"next_tournament_schedule"}
            )
            await interaction.response.send_message(
                embed=messages["next_tournament_schedule"].embed(),
                view=NextTournamentScheduleView(self.manager, draft),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(embed=error_embed(exc), ephemeral=True)


class NextTournamentScheduleView(discord.ui.View):
    def __init__(self, manager, draft):
        super().__init__(timeout=900)
        self.manager, self.draft = manager, draft

    @discord.ui.button(label="Set Signup Window", style=discord.ButtonStyle.primary)
    async def schedule(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.send_modal(
            NextTournamentScheduleModal(self.manager, self.draft)
        )


class NextTournamentScheduleModal(discord.ui.Modal, title="Signup Window"):
    opens = discord.ui.TextInput(
        label="Signup opens (local)",
        placeholder="2026-09-01 18:00",
        max_length=16,
    )
    closes = discord.ui.TextInput(
        label="Signup closes (local)",
        placeholder="2026-09-06 18:00",
        max_length=16,
    )

    def __init__(self, manager, draft):
        super().__init__(timeout=900)
        self.manager, self.draft = manager, draft

    async def on_submit(self, interaction):
        try:
            opens = _parse_local_datetime(
                str(self.opens.value), self.draft.timezone, "signup opening time"
            )
            closes = _parse_local_datetime(
                str(self.closes.value), self.draft.timezone, "signup closing time"
            )
            if closes <= opens:
                raise RegistrationError("signup closing time must be after signup opening time")
            if closes <= datetime.now(UTC):
                raise RegistrationError("signup closing time must be in the future")
            draft = replace(
                self.draft,
                signup_opens_at_utc=utc_iso(opens),
                signup_closes_at_utc=utc_iso(closes),
            )
            service = NextTournamentService(self.manager.sheet_id)
            options = await service.clan_options()
            messages = await _load_next_messages(
                self.manager.sheet_id, {"next_tournament_eligibility"}
            )
            await interaction.response.send_message(
                embed=messages["next_tournament_eligibility"].embed(),
                view=NextTournamentEligibilityView(self.manager, draft, options),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(embed=error_embed(exc), ephemeral=True)


class NextTournamentEligibilityView(discord.ui.View):
    def __init__(self, manager, draft, options):
        super().__init__(timeout=900)
        self.manager, self.draft, self.options = manager, draft, options
        defaults = [item.discord_role_id for item in options if item.active_current]
        self.add_item(
            NextTournamentClanSelect(
                manager,
                draft,
                options,
                defaults=defaults,
            )
        )


class NextTournamentClanSelect(discord.ui.Select):
    def __init__(self, manager, draft, options, *, defaults):
        super().__init__(
            placeholder="Select eligible clans",
            min_values=1,
            max_values=len(options),
            options=[
                discord.SelectOption(
                    label=item.label,
                    value=item.discord_role_id,
                    default=item.discord_role_id in defaults,
                )
                for item in options
            ],
        )
        self.manager, self.draft, self.clan_options = manager, draft, options

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        try:
            draft = replace(self.draft, eligible_role_ids=tuple(self.values))
            by_role = {item.discord_role_id: item for item in self.clan_options}
            selected = [by_role[value] for value in self.values]
            messages = await _load_next_messages(
                self.manager.sheet_id, {"next_tournament_review"}
            )
            embed = messages["next_tournament_review"].embed(
                tournament_name=draft.tournament_name,
                short_name=draft.short_name,
                min_participants=draft.min_participants,
                max_participants=draft.max_participants,
                timezone=draft.timezone,
                signup_opens=discord_timestamp(draft.signup_opens_at_utc),
                signup_closes=discord_timestamp(draft.signup_closes_at_utc),
                eligible_clans=", ".join(item.label for item in selected),
            )
            await interaction.response.edit_message(
                embed=embed,
                view=ConfirmCreateNextTournamentView(self.manager, draft),
            )
        except Exception as exc:
            await interaction.response.send_message(embed=error_embed(exc), ephemeral=True)


class ConfirmCreateNextTournamentView(discord.ui.View):
    def __init__(self, manager, draft):
        super().__init__(timeout=900)
        self.manager, self.draft = manager, draft

    @discord.ui.button(label="Create Tournament", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, _button):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        warnings = []
        try:
            snapshot = await load_tournament_snapshot(self.manager.sheet_id)
            if snapshot.status == "completed":
                lifecycle = OrganizerService(self.manager.sheet_id)
                await lifecycle.initialize()
                await lifecycle.transition("archive", str(interaction.user.id))
                try:
                    warnings.extend(await self.manager.secondary_sync())
                except Exception:
                    log.exception("Live Arena old tournament archive sync failed")
                    warnings.append("old tournament Discord state")
            elif snapshot.status != "archived":
                raise RegistrationError(
                    "Create Next Tournament is only available after the current tournament is completed"
                )

            service = NextTournamentService(self.manager.sheet_id)
            new_id = await service.create(str(interaction.user.id), self.draft)
            warnings.extend(await _clear_participant_role(interaction.guild, self.manager.sheet_id))
            try:
                warnings.extend(await self.manager.secondary_sync())
            except Exception:
                log.exception("Live Arena new tournament panel sync failed")
                warnings.append("new tournament panels")
            _schedule_open(self.manager)
            messages = await _load_next_messages(
                self.manager.sheet_id, {"next_tournament_created"}
            )
            embed = messages["next_tournament_created"].embed(
                tournament_name=self.draft.tournament_name,
                tournament_id=new_id,
                signup_opens=discord_timestamp(self.draft.signup_opens_at_utc),
                signup_closes=discord_timestamp(self.draft.signup_closes_at_utc),
            )
            if warnings:
                embed.add_field(
                    name="Sync warning",
                    value=(
                        "The new tournament is saved, but these Discord items need review: "
                        + ", ".join(dict.fromkeys(warnings))
                    )[:1024],
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            log.exception("Live Arena Create Next Tournament commit failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _clear_participant_role(guild, sheet_id: str) -> list[str]:
    if guild is None:
        return ["Tournament Participant role cleanup"]
    try:
        config, _ = await load_pr5_config(sheet_id)
        role = guild.get_role(int(config["PARTICIPANT_ROLE_ID"]))
        if role is None:
            return ["Tournament Participant role cleanup"]
        failures = 0
        for member in list(getattr(role, "members", [])):
            try:
                await member.remove_roles(
                    role, reason="Live Arena previous tournament archived"
                )
            except Exception:
                failures += 1
                log.exception(
                    "Live Arena participant role cleanup failed • member=%s",
                    getattr(member, "id", "unknown"),
                )
        return ["Tournament Participant role cleanup"] if failures else []
    except Exception:
        log.exception("Live Arena participant role cleanup failed")
        return ["Tournament Participant role cleanup"]


async def _open_when_due(manager):
    sheet_id = str(manager.sheet_id)
    try:
        snapshot = await load_tournament_snapshot(sheet_id)
        if snapshot.status != "draft":
            return
        opens = datetime.fromisoformat(snapshot.signup_opens_at_utc.replace("Z", "+00:00"))
        delay = max(0.0, (opens - datetime.now(UTC)).total_seconds())
        if delay:
            await asyncio.sleep(delay)
        current = await load_tournament_snapshot(sheet_id)
        if current.tournament_id != snapshot.tournament_id or current.status != "draft":
            return
        service = OrganizerService(sheet_id)
        await service.initialize()
        await service.transition("open", "system")
        try:
            await manager.secondary_sync()
        except Exception:
            log.exception("Live Arena scheduled signup-open panel sync failed")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Live Arena scheduled signup opening failed")
    finally:
        task = _open_tasks.get(sheet_id)
        if task is asyncio.current_task():
            _open_tasks.pop(sheet_id, None)


def _schedule_open(manager) -> None:
    sheet_id = str(manager.sheet_id)
    old = _open_tasks.get(sheet_id)
    if old is not None and not old.done():
        old.cancel()
    try:
        task = asyncio.create_task(
            _open_when_due(manager), name=f"live-arena-signup-open:{sheet_id[-6:]}"
        )
    except RuntimeError:
        return
    _open_tasks[sheet_id] = task


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_with_create_next(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_create_next_tournament_installed", False):
            return True
        manager._create_next_tournament_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if not callable(add_item):
                return result
            add_item(
                CreateNextTournamentButton(
                    manager,
                    disabled=status is not None and status not in {"completed", "archived"},
                )
            )
            return result

        manager.view = view
        _schedule_open(manager)
        return True

    qualification_panel.install_qualification = install_with_create_next

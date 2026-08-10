"""Literal player-flow message and CONFIG contracts for Live Arena Discord UI."""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter

import discord

from shared.sheets.async_core import afetch_values

from modules.community.live_arena.service import (
    CONFIG_HEADERS,
    CONFIG_TAB,
    LiveArenaConfigError,
    _enabled,
    _rows,
    _text,
)

PR3_CONFIG_KEYS = (
    "MESSAGES_TAB",
    "SIGNUP_CHANNEL_ID",
    "PARTICIPANT_ROLE_ID",
    "PUBLIC_PANEL_MESSAGE_ID",
)
PR5_CONFIG_KEYS = (
    "MESSAGES_TAB",
    "SIGNUP_CHANNEL_ID",
    "ORGANIZER_CHANNEL_ID",
    "ORGANIZER_ROLE_ID",
    "PARTICIPANT_ROLE_ID",
    "PUBLIC_PANEL_MESSAGE_ID",
    "ORGANIZER_PANEL_MESSAGE_ID",
)
MESSAGE_HEADERS = (
    "message_key",
    "title",
    "description",
    "color_hex",
    "active",
    "notes",
)
REQUIRED_MESSAGES = {
    "signup_open": {
        "tournament_name",
        "signup_deadline",
        "confirmed_count",
        "max_participants",
    },
    "signup_confirmed": {"participant", "tournament_name", "signup_deadline"},
    "availability_updated": {"participant", "tournament_name"},
    "withdrawal_confirmed": {"participant", "tournament_name"},
}
PR5_MESSAGES = {
    "signup_closed": {"tournament_name", "confirmed_count"},
    "organizer_panel": {
        "tournament_name",
        "status",
        "confirmed_count",
        "max_participants",
    },
    "organizer_roster_draft": set(),
    "organizer_roster_open": {
        "confirmed_count",
        "player_word",
        "min_participants",
    },
    "organizer_roster_below_minimum": {
        "confirmed_count",
        "player_word",
        "min_participants",
    },
    "organizer_roster_odd": {"confirmed_count", "player_word"},
    "organizer_roster_ready": {"confirmed_count"},
    "organizer_roster_active": set(),
    "organizer_roster_completed": set(),
    "organizer_roster_default": {"confirmed_count", "player_word"},
    "organizer_statuses": {
        "confirmed_count",
        "withdrawn_count",
        "removed_count",
        "disqualified_count",
    },
    "organizer_roles_ok": set(),
    "organizer_roles_config_missing": set(),
    "organizer_roles_missing": {"missing_participants"},
    "organizer_roles_extra": {"extra_participants"},
    "organizer_roles_unresolved": {"unresolved_participants"},
    "organizer_roles_missing_extra": {
        "missing_participants",
        "extra_participants",
    },
    "organizer_roles_missing_unresolved": {
        "missing_participants",
        "unresolved_participants",
    },
    "organizer_roles_extra_unresolved": {
        "extra_participants",
        "unresolved_participants",
    },
    "organizer_roles_missing_extra_unresolved": {
        "missing_participants",
        "extra_participants",
        "unresolved_participants",
    },
    "organizer_roster_view": {
        "tournament_name",
        "status",
        "confirmed_count",
        "max_participants",
    },
    "organizer_roster_participants": {"participant_lines"},
}
MESSAGE_CONTRACTS = REQUIRED_MESSAGES | PR5_MESSAGES


@dataclass(frozen=True)
class MessageTemplate:
    key: str
    title: str
    description: str
    color: int

    def render(self, **values: object) -> tuple[str, str]:
        expected = MESSAGE_CONTRACTS[self.key]
        fields = {
            name
            for _, name, _, _ in Formatter().parse(self.title + self.description)
            if name
        }
        if frozenset(fields) != frozenset(expected):
            raise LiveArenaConfigError(
                f"MESSAGES.{self.key}: placeholders must be exactly "
                + ", ".join(sorted(expected))
            )
        missing = fields - values.keys()
        if missing:
            raise LiveArenaConfigError(
                f"MESSAGES.{self.key}: missing render value {', '.join(sorted(missing))}"
            )
        return self.title.format(**values), self.description.format(**values)

    def embed(self, **values: object) -> discord.Embed:
        title, description = self.render(**values)
        return discord.Embed(title=title, description=description, color=self.color)


async def load_pr3_config(sheet_id: str) -> tuple[dict[str, str], list[list[object]]]:
    matrix = await afetch_values(sheet_id, CONFIG_TAB) or []
    rows = _rows(matrix, CONFIG_HEADERS, CONFIG_TAB)
    result: dict[str, str] = {}
    for key in PR3_CONFIG_KEYS:
        matches = [row for row in rows if _text(row["Key"]) == key]
        if len(matches) != 1:
            raise LiveArenaConfigError(f"CONFIG: key {key} must occur exactly once")
        result[key] = _text(matches[0]["Value"])
    for key in ("MESSAGES_TAB", "SIGNUP_CHANNEL_ID", "PARTICIPANT_ROLE_ID"):
        if not result[key]:
            raise LiveArenaConfigError(f"CONFIG: missing required key {key}")
    try:
        int(result["SIGNUP_CHANNEL_ID"])
        int(result["PARTICIPANT_ROLE_ID"])
        if result["PUBLIC_PANEL_MESSAGE_ID"]:
            int(result["PUBLIC_PANEL_MESSAGE_ID"])
    except ValueError as exc:
        raise LiveArenaConfigError("CONFIG: PR3 Discord IDs must be numeric") from exc
    return result, matrix


async def load_pr5_config(sheet_id: str) -> tuple[dict[str, str], list[list[object]]]:
    """Load only the exact Discord/config routing contract used by PR5."""
    matrix = await afetch_values(sheet_id, CONFIG_TAB) or []
    rows = _rows(matrix, CONFIG_HEADERS, CONFIG_TAB)
    result = {}
    for key in PR5_CONFIG_KEYS:
        matches = [row for row in rows if _text(row["Key"]) == key]
        if len(matches) != 1:
            raise LiveArenaConfigError(f"CONFIG: key {key} must occur exactly once")
        result[key] = _text(matches[0]["Value"])
    for key in PR5_CONFIG_KEYS:
        if (
            key not in {"PUBLIC_PANEL_MESSAGE_ID", "ORGANIZER_PANEL_MESSAGE_ID"}
            and not result[key]
        ):
            raise LiveArenaConfigError(f"CONFIG: missing required key {key}")
    try:
        for key in (
            "SIGNUP_CHANNEL_ID",
            "ORGANIZER_CHANNEL_ID",
            "ORGANIZER_ROLE_ID",
            "PARTICIPANT_ROLE_ID",
        ):
            int(result[key])
        for key in ("PUBLIC_PANEL_MESSAGE_ID", "ORGANIZER_PANEL_MESSAGE_ID"):
            if result[key]:
                int(result[key])
    except ValueError as exc:
        raise LiveArenaConfigError("CONFIG: PR5 Discord IDs must be numeric") from exc
    return result, matrix


async def load_messages(
    sheet_id: str, tab: str, keys=None
) -> dict[str, MessageTemplate]:
    rows = _rows(await afetch_values(sheet_id, tab) or [], MESSAGE_HEADERS, tab)
    templates = {}
    for key in keys or REQUIRED_MESSAGES:
        if key not in MESSAGE_CONTRACTS:
            raise LiveArenaConfigError(f"MESSAGES: unknown message contract: {key}")
        matches = [row for row in rows if _text(row["message_key"]) == key]
        if len(matches) != 1 or not _enabled(matches[0]["active"]):
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
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: color_hex must be #RRGGBB"
            ) from exc
        template = MessageTemplate(
            key, _text(row["title"]), _text(row["description"]), parsed
        )
        template.render(**{name: "x" for name in MESSAGE_CONTRACTS[key]})
        templates[key] = template
    return templates


def discord_timestamp(value) -> str:
    from modules.community.live_arena.registration import _parse_close

    return f"<t:{int(_parse_close(value).timestamp())}:F>"

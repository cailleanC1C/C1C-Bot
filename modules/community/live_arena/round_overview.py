"""Canonical Victory Ledger round overview rendering.

One Discord message owns the round overview. Qualification rounds render three
embeds in a fixed order: general data, matchups, standings. Other stages reuse
the same general + matchup renderers and simply omit qualification standings.
All visible copy in this renderer comes from the existing MESSAGES tab.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter

import discord

from shared.sheets.async_core import afetch_values

from modules.community.live_arena.messages import MESSAGE_HEADERS, load_pr5_config
from modules.community.live_arena.service import (
    LiveArenaConfigError,
    _enabled,
    _rows,
    _text,
)

_COPY_CONTRACTS: dict[str, set[str]] = {
    "round_overview_general": {
        "round_name",
        "tournament_name",
        "state_label",
        "round_deadline",
        "completed",
        "total_matches",
    },
    "round_overview_general_closed": {
        "round_name",
        "tournament_name",
        "finalized",
        "completed",
        "total_matches",
    },
    "round_overview_matchups": set(),
    "round_overview_match": {
        "match_number",
        "player_a_mention",
        "player_b_mention",
        "result_line",
        "match_thread_link",
    },
    "round_overview_standings": {"standings_lines"},
    "round_overview_standing_line": {"rank", "player_mention", "record"},
    "round_overview_standing_player": {"rank", "player_mention"},
    "round_overview_standing_record": {"record"},
    "round_overview_bye": {"player_mention"},
    "round_state_open": set(),
    "round_state_ready_to_close": set(),
    "round_state_correction": set(),
    "round_result_pending": set(),
    "round_result_pending_confirmation": set(),
    "round_result_disputed": set(),
    "round_result_late_review": set(),
    "round_result_finalized": {"score_a", "score_b"},
    "round_result_forfeit_with_winner": {"winner_mention"},
    "round_result_forfeit": set(),
    "round_result_double_forfeit": set(),
    "round_result_bye": set(),
    "round_thread_link": set(),
    "round_thread_pending": set(),
    "round_standings_empty": set(),
}

_COPY_CACHE: dict[str, dict[str, "CopyTemplate"]] = {}
_installed = False


@dataclass(frozen=True)
class CopyTemplate:
    key: str
    title: str
    description: str
    color: int

    def render(self, **values: object) -> tuple[str, str]:
        expected = _COPY_CONTRACTS[self.key]
        fields = {
            name
            for _, name, _, _ in Formatter().parse(self.title + self.description)
            if name
        }
        if fields != expected:
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


async def _templates(sheet_id: str) -> dict[str, CopyTemplate]:
    sid = str(sheet_id or "").strip()
    cached = _COPY_CACHE.get(sid)
    if cached is not None:
        return cached

    config, _ = await load_pr5_config(sid)
    messages_tab = config["MESSAGES_TAB"]
    rows = _rows(
        await afetch_values(sid, messages_tab) or [],
        MESSAGE_HEADERS,
        messages_tab,
    )
    result: dict[str, CopyTemplate] = {}
    for key, expected in _COPY_CONTRACTS.items():
        matches = [row for row in rows if _text(row["message_key"]) == key]
        if len(matches) != 1 or not _enabled(matches[0]["active"]):
            raise LiveArenaConfigError(
                f"MESSAGES: required active row missing or duplicated: {key}"
            )
        row = matches[0]
        color_text = _text(row["color_hex"])
        if len(color_text) != 7 or not color_text.startswith("#"):
            raise LiveArenaConfigError(f"MESSAGES.{key}: color_hex must be #RRGGBB")
        try:
            color = int(color_text[1:], 16)
        except ValueError as exc:
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: color_hex must be #RRGGBB"
            ) from exc
        template = CopyTemplate(
            key=key,
            title=_text(row["title"]),
            description=_text(row["description"]),
            color=color,
        )
        fields = {
            name
            for _, name, _, _ in Formatter().parse(template.title + template.description)
            if name
        }
        if fields != expected:
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: placeholders must be exactly "
                + ", ".join(sorted(expected))
            )
        template.render(**{name: "x" for name in expected})
        result[key] = template

    _COPY_CACHE[sid] = result
    return result


def clear_copy_cache(sheet_id: str | None = None) -> None:
    if sheet_id is None:
        _COPY_CACHE.clear()
    else:
        _COPY_CACHE.pop(str(sheet_id), None)


def _title(templates: dict[str, CopyTemplate], key: str, **values: object) -> str:
    return templates[key].render(**values)[0]


def _description(
    templates: dict[str, CopyTemplate], key: str, **values: object
) -> str:
    return templates[key].render(**values)[1]


def _state_label(templates: dict[str, CopyTemplate], status: str) -> str:
    if status in {"active", "published", "open", "published/open"}:
        return _title(templates, "round_state_open")
    if status == "ready_to_close":
        return _title(templates, "round_state_ready_to_close")
    if status == "correction_in_progress":
        return _title(templates, "round_state_correction")
    return status.replace("_", " ")


def _result_line(templates: dict[str, CopyTemplate], match) -> str:
    status = _text(match.get("status"))
    if status == "finalized":
        return _title(
            templates,
            "round_result_finalized",
            score_a=_text(match.get("final_score_a")),
            score_b=_text(match.get("final_score_b")),
        )
    if status == "forfeit":
        winner = _text(match.get("final_winner_discord_user_id"))
        if winner:
            return _title(
                templates,
                "round_result_forfeit_with_winner",
                winner_mention=f"<@{winner}>",
            )
        return _title(templates, "round_result_forfeit")
    if status == "double_forfeit":
        return _title(templates, "round_result_double_forfeit")
    if status == "bye":
        return _title(templates, "round_result_bye")
    if status == "pending_confirmation":
        return _title(templates, "round_result_pending_confirmation")
    if status == "disputed":
        return _title(templates, "round_result_disputed")
    if status == "late_review":
        return _title(templates, "round_result_late_review")
    return _title(templates, "round_result_pending")


def _thread_link(
    templates: dict[str, CopyTemplate], thread_id: str, guild_id: str
) -> str:
    if not thread_id or not guild_id:
        return _title(templates, "round_thread_pending")
    label = _title(templates, "round_thread_link")
    return f"💬 [{label}](https://discord.com/channels/{guild_id}/{thread_id})"


async def render_round_overview_embeds(
    *,
    sheet_id: str,
    tournament,
    round_row,
    matches,
    standings,
    guild_id: str,
) -> list[discord.Embed]:
    """Return the final ordered embed payload for one Victory Ledger message."""
    from modules.community.live_arena import qualification_panel

    templates = await _templates(sheet_id)
    rows = [dict(row) for row in matches]
    terminal = {"finalized", "forfeit", "double_forfeit", "bye"}
    completed = sum(_text(row.get("status")) in terminal for row in rows)
    status = _text(round_row.get("status"))
    stage = _text(round_row.get("round_stage")).lower()
    deadline = qualification_panel._format_timestamp(
        _text(round_row.get("deadline_at_utc")), "F"
    )

    common = dict(
        round_name=_text(round_row.get("round_name")),
        tournament_name=_text(tournament.get("tournament_name")),
        completed=completed,
        total_matches=len(rows),
    )
    if status == "closed":
        completed_at = _text(round_row.get("completed_at_utc"))
        finalized = (
            qualification_panel._format_timestamp(completed_at, "F")
            if completed_at
            else deadline
        )
        general = templates["round_overview_general_closed"].embed(
            finalized=finalized,
            **common,
        )
    else:
        general = templates["round_overview_general"].embed(
            state_label=_state_label(templates, status),
            round_deadline=deadline,
            **common,
        )

    matchups = templates["round_overview_matchups"].embed()
    for match in sorted(rows, key=lambda row: int(_text(row.get("match_number")) or 0)):
        if _text(match.get("status")) == "bye" or not _text(
            match.get("player_b_discord_user_id")
        ):
            title, description = templates["round_overview_bye"].render(
                player_mention=f"<@{_text(match.get('player_a_discord_user_id'))}>"
            )
        else:
            title, description = templates["round_overview_match"].render(
                match_number=_text(match.get("match_number")),
                player_a_mention=f"<@{_text(match.get('player_a_discord_user_id'))}>",
                player_b_mention=f"<@{_text(match.get('player_b_discord_user_id'))}>",
                result_line=_result_line(templates, match),
                match_thread_link=_thread_link(
                    templates,
                    _text(match.get("thread_id")),
                    str(guild_id or ""),
                ),
            )
        matchups.add_field(name=title, value=description, inline=False)

    embeds = [general, matchups]
    if stage == "qualification":
        if standings:
            standings_embed = templates["round_overview_standings"].embed(
                standings_lines=""
            )
            player_lines = [
                _description(
                    templates,
                    "round_overview_standing_player",
                    rank=entry.rank,
                    player_mention=f"<@{entry.discord_user_id}>",
                )
                for entry in standings
            ]
            record_lines = [
                _description(
                    templates,
                    "round_overview_standing_record",
                    record=entry.match_record,
                )
                for entry in standings
            ]
            standings_embed.add_field(
                name="\u200b",
                value="\n".join(player_lines)[:1024],
                inline=True,
            )
            standings_embed.add_field(
                name="\u200b",
                value="\n".join(record_lines)[:1024],
                inline=True,
            )
        else:
            standings_embed = templates["round_overview_standings"].embed(
                standings_lines=_title(templates, "round_standings_empty")
            )
        embeds.append(standings_embed)
    return embeds


def install() -> None:
    """Disable legacy cosmetic re-renderers now that the core renderer is final."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import simulation_ux_hardening

    async def no_legacy_link_refresh(*_args, **_kwargs):
        return None

    # simulation_ux_hardening still performs useful thread renaming after each
    # round sync, but its old link refresh rebuilt the overview as one embed.
    # Keep the rename behavior and retire only that obsolete presentation pass.
    simulation_ux_hardening._refresh_victory_ledger_links = no_legacy_link_refresh

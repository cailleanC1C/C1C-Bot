"""Final Live Arena result lifecycle UX.

Moves confirmation/dispute actions to the reported-result message, lets tournament
organizers proxy the non-reporting participant, raises Captain's Table dispute
alerts, and keeps Victory Ledger result/standings copy aligned with persisted
competition state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC
from string import Formatter
from uuid import uuid4

import discord

from shared.config import cfg
from shared.sheets.async_core import afetch_values, sheet_read_scope

from modules.community.live_arena.competition import (
    MATCH_TERMINAL_STATUSES,
    _finalize_played_result,
    _parse_utc,
    _single_match,
    _single_round,
)
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.messages import MESSAGE_HEADERS, load_pr5_config
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import (
    LiveArenaConfigError,
    _enabled,
    _rows,
    _text,
    load_config,
)

log = logging.getLogger("c1c.community.live_arena.result_lifecycle_ux")
_installed = False
_registered_views: set[str] = set()
_ACTIVE_SHEET_ID = ""
_COPY: dict[str, dict[str, "CopyTemplate"]] = {}
_ALERT_MARKER_PREFIX = "live_arena_result_dispute:"
_RESULT_MARKER_PREFIX = "live_arena_result_lifecycle:"

_COPY_CONTRACTS: dict[str, set[str]] = {
    "button_confirm_result": set(),
    "button_dispute_result": set(),
    "result_reported_player": {"reporter_mention", "score", "opponent_mention", "deadline"},
    "result_reported_staff": {
        "staff_mention",
        "reporter_mention",
        "score",
        "opponent_mention",
        "deadline",
    },
    "result_reported_review": {"reporter_mention", "score", "opponent_mention"},
    "result_reported_review_staff": {
        "staff_mention",
        "reporter_mention",
        "score",
        "opponent_mention",
    },
    "result_report_saved": {"score"},
    "result_confirmed_player": {"participant_mention", "score"},
    "result_confirmed_staff": {"staff_mention", "participant_mention", "score"},
    "result_confirm_success": set(),
    "result_disputed_player": {"participant_mention", "score"},
    "result_disputed_staff": {"staff_mention", "participant_mention", "score"},
    "result_dispute_success": set(),
    "result_finalized_expired": {"score"},
    "result_finalized_organizer": {"score"},
    "dispute_alert_open": {
        "round_name",
        "match_number",
        "player_a_mention",
        "player_b_mention",
        "score",
        "participant_mention",
        "thread_link",
    },
    "dispute_alert_resolved": {
        "round_name",
        "match_number",
        "player_a_mention",
        "player_b_mention",
        "score",
        "thread_link",
    },
    "dispute_alert_reviewed": {
        "round_name",
        "match_number",
        "player_a_mention",
        "player_b_mention",
        "resolution",
        "thread_link",
    },
    "round_result_finalized_confirmed": {"score_a", "score_b"},
    "round_result_finalized_expired": {"score_a", "score_b"},
    "round_result_finalized_organizer": {"score_a", "score_b"},
    "round_standings_heading_live": set(),
    "round_standings_heading_after": {"round_number"},
    "round_standings_heading_final": set(),
    "round_standings_context_live": {"round_number"},
}


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


async def refresh_lifecycle_copy(sheet_id: str) -> dict[str, CopyTemplate]:
    global _ACTIVE_SHEET_ID

    sid = str(sheet_id or "").strip()
    if not sid:
        raise LiveArenaConfigError("Live Arena result lifecycle copy requires a Sheet ID")
    config, _ = await load_pr5_config(sid)
    tab = config["MESSAGES_TAB"]
    rows = _rows(await afetch_values(sid, tab) or [], MESSAGE_HEADERS, tab)
    templates: dict[str, CopyTemplate] = {}
    for key, expected in _COPY_CONTRACTS.items():
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
        templates[key] = template
    _COPY[sid] = templates
    _ACTIVE_SHEET_ID = sid
    return templates


def _templates(sheet_id: str | None = None) -> dict[str, CopyTemplate] | None:
    sid = str(sheet_id or _ACTIVE_SHEET_ID or "").strip()
    return _COPY.get(sid)


def _template(key: str, sheet_id: str | None = None) -> CopyTemplate:
    templates = _templates(sheet_id)
    if templates is None or key not in templates:
        raise LiveArenaConfigError("Live Arena result lifecycle copy is not loaded")
    return templates[key]


def _title(key: str, sheet_id: str | None = None, **values: object) -> str:
    return _template(key, sheet_id).render(**values)[0]


def _description(key: str, sheet_id: str | None = None, **values: object) -> str:
    return _template(key, sheet_id).render(**values)[1]


async def _confirm_result(self, actor_id: str, match_id: str) -> dict[str, object]:
    """Finalize a timely reported result when the non-reporting opponent confirms it."""
    base = await load_config(self.sheet_id)
    tid = base["ACTIVE_TOURNAMENT_ID"]
    async with _locks[(self.sheet_id, tid)]:
        old_rounds = await self.repository.rounds()
        old_matches = await self.repository.matches()
        rounds = [dict(row) for row in old_rounds]
        matches = [dict(row) for row in old_matches]
        match = _single_match(matches, tid, match_id)
        round_row = _single_round(rounds, tid, _text(match["round_id"]))
        if _text(match.get("status")) != "pending_confirmation":
            raise RegistrationError("Only a pending reported result can be confirmed")

        actor = str(actor_id)
        reporter = _text(match.get("reported_by_discord_user_id"))
        players = {
            _text(match.get("player_a_discord_user_id")),
            _text(match.get("player_b_discord_user_id")),
        }
        if actor not in players or actor == reporter:
            raise RegistrationError("Only the non-reporting opponent can confirm this result")
        due = _parse_utc(_text(match.get("confirm_due_at_utc")))
        now_dt = self.clock().astimezone(UTC)
        if due is not None and now_dt > due:
            raise RegistrationError("The confirmation window has already expired")

        now = utc_iso(now_dt)
        _finalize_played_result(match, now, actor)
        match["confirmed_by_discord_user_id"] = actor
        self._mark_ready(rounds, matches, tid, _text(round_row["round_id"]))
        await self.repository.persist_state(
            rounds,
            matches,
            previous_rounds=old_rounds,
            previous_matches=old_matches,
        )
        await self._audit_resolution(
            tid,
            actor,
            "match_result_confirmed",
            {"match_id": match_id, "reported_by": reporter},
            now,
        )
        return dict(match)


async def _audit_proxy_action(
    service,
    match,
    *,
    organizer_id: str,
    participant_id: str,
    action: str,
) -> None:
    try:
        await service.registration_repository.append_audit(
            dict(
                event_id=str(uuid4()),
                tournament_id=_text(match.get("tournament_id")),
                event_type=f"match_result_{action}_on_behalf",
                actor_discord_user_id=str(organizer_id),
                target_discord_user_id=str(participant_id),
                details=json.dumps(
                    {
                        "match_id": _text(match.get("match_id")),
                        "represented_participant": str(participant_id),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at_utc=utc_iso(service.clock().astimezone(UTC)),
            )
        )
    except Exception:
        log.exception(
            "Live Arena organizer proxy audit failed • match=%s • action=%s",
            _text(match.get("match_id")),
            action,
        )


def _opponent_for_report(match) -> str:
    reporter = _text(match.get("reported_by_discord_user_id"))
    a = _text(match.get("player_a_discord_user_id"))
    b = _text(match.get("player_b_discord_user_id"))
    if reporter == a:
        return b
    if reporter == b:
        return a
    return ""


def _score_for_reporter(match, *, final: bool = False) -> str:
    prefix = "final" if final else "reported"
    a_score = _text(match.get(f"{prefix}_score_a"))
    b_score = _text(match.get(f"{prefix}_score_b"))
    reporter = _text(match.get("reported_by_discord_user_id"))
    if reporter and reporter == _text(match.get("player_b_discord_user_id")):
        return f"{b_score}-{a_score}"
    return f"{a_score}-{b_score}"


def _sheet_score(match, *, final: bool = False) -> str:
    prefix = "final" if final else "reported"
    return f"{_text(match.get(f'{prefix}_score_a'))}-{_text(match.get(f'{prefix}_score_b'))}"


def _discord_timestamp(value: str) -> str:
    parsed = _parse_utc(value)
    if parsed is None:
        return "the objection deadline"
    return f"<t:{int(parsed.timestamp())}:F>"


async def _is_organizer(interaction: discord.Interaction, sheet_id: str) -> bool:
    config, _ = await load_pr5_config(sheet_id)
    role_id = _text(config.get("ORGANIZER_ROLE_ID"))
    return bool(
        role_id
        and any(
            str(getattr(role, "id", "")) == role_id
            for role in getattr(interaction.user, "roles", ())
        )
    )


async def _represented_opponent(interaction, sheet_id: str, match) -> tuple[str, bool]:
    actor = str(interaction.user.id)
    opponent = _opponent_for_report(match)
    reporter = _text(match.get("reported_by_discord_user_id"))
    players = {
        _text(match.get("player_a_discord_user_id")),
        _text(match.get("player_b_discord_user_id")),
    }
    if actor == opponent:
        return opponent, False
    if actor in players:
        if actor == reporter:
            raise RegistrationError(
                "The player who reported this result cannot confirm or dispute their own report"
            )
        raise RegistrationError("Only the non-reporting opponent can use this action")
    if await _is_organizer(interaction, sheet_id):
        if not opponent:
            raise RegistrationError("The non-reporting participant could not be resolved")
        return opponent, True
    raise RegistrationError(
        "Only the non-reporting opponent or a configured tournament organizer can use this action"
    )


class ResultDecisionView(discord.ui.View):
    def __init__(self, sheet_id: str):
        super().__init__(timeout=None)
        self.sheet_id = str(sheet_id)
        self.add_item(ResultDecisionButton(self.sheet_id, "confirm"))
        self.add_item(ResultDecisionButton(self.sheet_id, "dispute"))


class ResultDecisionButton(discord.ui.Button):
    def __init__(self, sheet_id: str, action: str):
        self.sheet_id = str(sheet_id)
        self.action = action
        if action == "confirm":
            label = _title("button_confirm_result", self.sheet_id)
            style = discord.ButtonStyle.success
            custom_id = "live_arena:match:confirm_reported_result"
        else:
            label = _title("button_dispute_result", self.sheet_id)
            style = discord.ButtonStyle.danger
            custom_id = "live_arena:match:dispute_reported_result"
        super().__init__(label=label[:80], style=style, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from modules.community.live_arena import result_control_refresh, result_views

            with sheet_read_scope():
                service = CompetitionResolutionService(self.sheet_id)
                await service.initialize()
                match = await service.match_for_thread(str(interaction.channel_id))
                represented, proxied = await _represented_opponent(
                    interaction, self.sheet_id, match
                )
                match_id = _text(match.get("match_id"))
                if self.action == "confirm":
                    updated = await service.confirm_result(represented, match_id)
                    result_views.cancel_match_finalizer(self.sheet_id, match_id)
                    if proxied:
                        await _audit_proxy_action(
                            service,
                            updated,
                            organizer_id=str(interaction.user.id),
                            participant_id=represented,
                            action="confirmed",
                        )
                    key = "result_confirmed_staff" if proxied else "result_confirmed_player"
                    values = {
                        "participant_mention": f"<@{represented}>",
                        "score": _score_for_reporter(updated, final=True),
                    }
                    if proxied:
                        values["staff_mention"] = f"<@{interaction.user.id}>"
                    await _edit_lifecycle_message(
                        interaction.message,
                        _template(key, self.sheet_id).embed(**values),
                        match_id,
                    )
                    await result_control_refresh._refresh_channel_controls(
                        interaction.channel, self.sheet_id, updated
                    )
                    await result_views._run_post_mutation_sync(self.sheet_id)
                    await interaction.followup.send(
                        embed=_template("result_confirm_success", self.sheet_id).embed(),
                        ephemeral=True,
                    )
                    return

                updated = await service.dispute_result(represented, match_id)
                result_views.cancel_match_finalizer(self.sheet_id, match_id)
                if proxied:
                    await _audit_proxy_action(
                        service,
                        updated,
                        organizer_id=str(interaction.user.id),
                        participant_id=represented,
                        action="disputed",
                    )
                key = "result_disputed_staff" if proxied else "result_disputed_player"
                values = {
                    "participant_mention": f"<@{represented}>",
                    "score": _score_for_reporter(updated),
                }
                if proxied:
                    values["staff_mention"] = f"<@{interaction.user.id}>"
                await _edit_lifecycle_message(
                    interaction.message,
                    _template(key, self.sheet_id).embed(**values),
                    match_id,
                )
                rounds = await service.repository.rounds()
                round_name = _round_name(rounds, updated)
                await _sync_dispute_alert(
                    interaction.client,
                    self.sheet_id,
                    updated,
                    round_name=round_name,
                    create_if_missing=True,
                )
                await result_control_refresh._refresh_channel_controls(
                    interaction.channel, self.sheet_id, updated
                )
                await result_views._run_post_mutation_sync(self.sheet_id)
                await interaction.followup.send(
                    embed=_template("result_dispute_success", self.sheet_id).embed(),
                    ephemeral=True,
                )
        except Exception as exc:
            log.exception("Live Arena reported-result action failed • action=%s", self.action)
            from modules.community.live_arena.views import error_embed

            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _submit_report(
    interaction,
    service,
    match,
    *,
    reporter_id: str,
    submitted_by_id: str,
    raw_score: str,
) -> None:
    """Final report path: one lifecycle message owns confirm/dispute controls."""
    from modules.community.live_arena import result_control_refresh, result_views
    from modules.community.live_arena import simulation_ux_hardening as ux

    score_a, score_b = result_views._score_for_sheet_sides(raw_score, reporter_id, match)
    updated = await service.report_result(
        reporter_id,
        _text(match["match_id"]),
        score_a,
        score_b,
        screenshot_present=True,
    )
    if submitted_by_id != reporter_id:
        await ux._audit_organizer_submission(
            service,
            updated,
            organizer_id=submitted_by_id,
            participant_id=reporter_id,
            score_a=score_a,
            score_b=score_b,
        )

    status = _text(updated.get("status"))
    due = _text(updated.get("confirm_due_at_utc"))
    match_id = _text(updated.get("match_id"))
    opponent_id = _opponent_for_report(updated)
    score = str(raw_score or "").strip().replace("–", "-").replace("—", "-")
    public_values = {
        "reporter_mention": f"<@{reporter_id}>",
        "opponent_mention": f"<@{opponent_id}>",
        "score": score,
    }

    view = None
    if status == "pending_confirmation":
        result_views.schedule_match_finalization(self_sheet := service.sheet_id, match_id, due)
        public_values["deadline"] = _discord_timestamp(due)
        if submitted_by_id == reporter_id:
            key = "result_reported_player"
        else:
            key = "result_reported_staff"
            public_values["staff_mention"] = f"<@{submitted_by_id}>"
        public_embed = _template(key, service.sheet_id).embed(**public_values)
        view = ResultDecisionView(service.sheet_id)
        ephemeral_embed = _template("result_report_saved", service.sheet_id).embed(score=score)
    else:
        if submitted_by_id == reporter_id:
            key = "result_reported_review"
        else:
            key = "result_reported_review_staff"
            public_values["staff_mention"] = f"<@{submitted_by_id}>"
        public_embed = _template(key, service.sheet_id).embed(**public_values)
        ephemeral_embed = public_embed

    public_embed.set_footer(text=f"{_RESULT_MARKER_PREFIX}{match_id}")
    await interaction.channel.send(
        embed=public_embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )
    await interaction.followup.send(embed=ephemeral_embed, ephemeral=True)
    await result_control_refresh._refresh_channel_controls(
        interaction.channel, service.sheet_id, updated
    )
    await result_views._run_post_mutation_sync(service.sheet_id)


async def _edit_lifecycle_message(message, embed: discord.Embed, match_id: str) -> None:
    if message is None:
        return
    embed.set_footer(text=f"{_RESULT_MARKER_PREFIX}{match_id}")
    await message.edit(embed=embed, view=None)


def _round_name(rounds, match) -> str:
    rid = _text(match.get("round_id"))
    row = next((row for row in rounds if _text(row.get("round_id")) == rid), None)
    return _text(row.get("round_name")) if row else rid


def _thread_link(thread) -> str:
    guild_id = _text(getattr(getattr(thread, "guild", None), "id", ""))
    channel_id = _text(getattr(thread, "id", ""))
    if not guild_id or not channel_id:
        return "Match thread unavailable"
    return f"[Open match thread](https://discord.com/channels/{guild_id}/{channel_id})"


async def _organizer_channel(bot, sheet_id: str):
    config, _ = await load_pr5_config(sheet_id)
    channel_id = int(config["ORGANIZER_CHANNEL_ID"])
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel, _text(config["ORGANIZER_ROLE_ID"])


async def _find_dispute_alert(channel, bot, match_id: str):
    marker = f"{_ALERT_MARKER_PREFIX}{match_id}"
    bot_id = _text(getattr(getattr(bot, "user", None), "id", ""))
    history = getattr(channel, "history", None)
    if not callable(history):
        return None
    async for message in history(limit=100):
        author_id = _text(getattr(getattr(message, "author", None), "id", ""))
        if bot_id and author_id and author_id != bot_id:
            continue
        for embed in getattr(message, "embeds", ()):
            if _text(getattr(getattr(embed, "footer", None), "text", "")) == marker:
                return message
    return None


async def _sync_dispute_alert(
    bot,
    sheet_id: str,
    match,
    *,
    round_name: str,
    create_if_missing: bool,
) -> None:
    channel, organizer_role_id = await _organizer_channel(bot, sheet_id)
    match_id = _text(match.get("match_id"))
    existing = await _find_dispute_alert(channel, bot, match_id)
    status = _text(match.get("status"))
    thread_id = _text(match.get("thread_id"))
    thread = None
    if thread_id:
        thread = bot.get_channel(int(thread_id))
        if thread is None:
            try:
                thread = await bot.fetch_channel(int(thread_id))
            except Exception:
                thread = None
    thread_link = _thread_link(thread) if thread is not None else f"<#${thread_id}>".replace("$", "")
    base_values = {
        "round_name": round_name,
        "match_number": _text(match.get("match_number")),
        "player_a_mention": f"<@{_text(match.get('player_a_discord_user_id'))}>",
        "player_b_mention": f"<@{_text(match.get('player_b_discord_user_id'))}>",
        "thread_link": thread_link,
    }

    if status == "disputed":
        if existing is None and not create_if_missing:
            return
        values = dict(base_values)
        values.update(
            score=_sheet_score(match),
            participant_mention=f"<@{_text(match.get('disputed_by_discord_user_id'))}>",
        )
        embed = _template("dispute_alert_open", sheet_id).embed(**values)
        embed.set_footer(text=f"{_ALERT_MARKER_PREFIX}{match_id}")
        if existing is not None:
            await existing.edit(embed=embed)
            return
        await channel.send(
            content=f"<@&{organizer_role_id}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=False, roles=True, everyone=False
            ),
        )
        return

    if existing is None:
        return
    if status in MATCH_TERMINAL_STATUSES:
        values = dict(base_values)
        values["score"] = _sheet_score(match, final=True)
        embed = _template("dispute_alert_resolved", sheet_id).embed(**values)
    else:
        values = dict(base_values)
        values["resolution"] = status.replace("_", " ") or "reviewed"
        embed = _template("dispute_alert_reviewed", sheet_id).embed(**values)
    embed.set_footer(text=f"{_ALERT_MARKER_PREFIX}{match_id}")
    await existing.edit(content="", embed=embed)


async def _find_lifecycle_message(thread, bot, match_id: str):
    marker = f"{_RESULT_MARKER_PREFIX}{match_id}"
    bot_id = _text(getattr(getattr(bot, "user", None), "id", ""))
    templates = _templates() or {}
    known_titles = {
        template.title
        for key, template in templates.items()
        if key.startswith("result_") and template.title
    }
    known_titles.update(
        {
            "Result reported",
            "Result reported by organizer",
            "Result disputed",
            "Result disputed by organizer",
            "Result confirmed",
            "Result confirmed by organizer",
            "Result finalized",
            "Result resolved",
        }
    )
    fallback = None
    history = getattr(thread, "history", None)
    if not callable(history):
        return None
    async for message in history(limit=75):
        author_id = _text(getattr(getattr(message, "author", None), "id", ""))
        if bot_id and author_id and author_id != bot_id:
            continue
        for embed in getattr(message, "embeds", ()):
            footer = _text(getattr(getattr(embed, "footer", None), "text", ""))
            if footer == marker:
                return message
            if fallback is None and _text(getattr(embed, "title", "")) in known_titles:
                fallback = message
    return fallback


async def _reconcile_lifecycle_message(bot, sheet_id: str, thread, match) -> None:
    message = await _find_lifecycle_message(thread, bot, _text(match.get("match_id")))
    if message is None:
        return
    status = _text(match.get("status"))
    if status == "pending_confirmation":
        await message.edit(view=ResultDecisionView(sheet_id))
        return

    current_title = ""
    if getattr(message, "embeds", None):
        current_title = _text(getattr(message.embeds[0], "title", ""))
    final_titles = {
        _title("result_confirmed_player", sheet_id),
        _title("result_confirmed_staff", sheet_id),
        _title("result_finalized_expired", sheet_id),
        _title("result_finalized_organizer", sheet_id),
    }
    if status == "disputed":
        if current_title.startswith("Result reported"):
            embed = _template("result_disputed_player", sheet_id).embed(
                participant_mention=f"<@{_text(match.get('disputed_by_discord_user_id'))}>",
                score=_score_for_reporter(match),
            )
            embed.set_footer(text=f"{_RESULT_MARKER_PREFIX}{_text(match.get('match_id'))}")
            await message.edit(embed=embed, view=None)
        else:
            await message.edit(view=None)
        return

    if status == "finalized" and current_title not in final_titles:
        if _text(match.get("confirmed_by_discord_user_id")):
            embed = _template("result_confirmed_player", sheet_id).embed(
                participant_mention=f"<@{_text(match.get('confirmed_by_discord_user_id'))}>",
                score=_score_for_reporter(match, final=True),
            )
        elif _text(match.get("finalized_by_discord_user_id")) == "system":
            embed = _template("result_finalized_expired", sheet_id).embed(
                score=_score_for_reporter(match, final=True)
            )
        else:
            embed = _template("result_finalized_organizer", sheet_id).embed(
                score=_score_for_reporter(match, final=True)
            )
        embed.set_footer(text=f"{_RESULT_MARKER_PREFIX}{_text(match.get('match_id'))}")
        await message.edit(embed=embed, view=None)
        return
    await message.edit(view=None)


async def _reconcile_current_round(bot, qualification_service, snapshot) -> list[str]:
    if snapshot.round_row is None:
        return []
    warnings: list[str] = []
    round_name = _text(snapshot.round_row.get("round_name"))
    for match in snapshot.matches:
        thread_id = _text(match.get("thread_id"))
        if not thread_id:
            continue
        try:
            thread = bot.get_channel(int(thread_id))
            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))
            await _reconcile_lifecycle_message(
                bot, qualification_service.sheet_id, thread, match
            )
            if _text(match.get("status")) == "disputed":
                await _sync_dispute_alert(
                    bot,
                    qualification_service.sheet_id,
                    match,
                    round_name=round_name,
                    create_if_missing=True,
                )
            elif _text(match.get("disputed_at_utc")):
                await _sync_dispute_alert(
                    bot,
                    qualification_service.sheet_id,
                    match,
                    round_name=round_name,
                    create_if_missing=False,
                )
        except Exception:
            log.exception(
                "Live Arena result lifecycle reconciliation failed • match=%s",
                _text(match.get("match_id")),
            )
            warnings.append(
                f"Match {_text(match.get('match_number'))} result lifecycle"
            )
    return warnings


def _patch_round_overview(round_overview) -> None:
    original_result_line = round_overview._result_line
    original_render = round_overview.render_round_overview_embeds

    def result_line_with_final_reason(templates, match):
        if _text(match.get("status")) != "finalized" or _templates() is None:
            return original_result_line(templates, match)
        values = {
            "score_a": _text(match.get("final_score_a")),
            "score_b": _text(match.get("final_score_b")),
        }
        if _text(match.get("disputed_at_utc")):
            return _title("round_result_finalized_organizer", **values)
        if _text(match.get("confirmed_by_discord_user_id")):
            return _title("round_result_finalized_confirmed", **values)
        if _text(match.get("finalized_by_discord_user_id")) == "system":
            return _title("round_result_finalized_expired", **values)
        return original_result_line(templates, match)

    async def render_with_cumulative_heading(**kwargs):
        embeds = await original_render(**kwargs)
        round_row = kwargs["round_row"]
        if (
            len(embeds) < 3
            or _text(round_row.get("round_stage")).lower() != "qualification"
            or _templates(kwargs.get("sheet_id")) is None
        ):
            return embeds
        sheet_id = str(kwargs.get("sheet_id"))
        number = int(_text(round_row.get("round_number")) or 0)
        status = _text(round_row.get("status"))
        if status == "closed":
            if number >= 3:
                heading = _title("round_standings_heading_final", sheet_id)
            else:
                heading = _title(
                    "round_standings_heading_after",
                    sheet_id,
                    round_number=number,
                )
            context = ""
        else:
            heading = _title("round_standings_heading_live", sheet_id)
            context = _description(
                "round_standings_context_live",
                sheet_id,
                round_number=number,
            )
        embeds[2].title = heading[:256]
        embeds[2].description = (context + (embeds[2].description or ""))[:4096]
        return embeds

    round_overview._result_line = result_line_with_final_reason
    round_overview.render_round_overview_embeds = render_with_cumulative_heading


def install() -> None:
    """Install last, after result-control refresh and all prior Live Arena UX layers."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        panel,
        result_views,
        round_overview,
        runtime_hooks,
        simulation_ux_hardening as ux,
    )

    if not hasattr(CompetitionResolutionService, "confirm_result"):
        CompetitionResolutionService.confirm_result = _confirm_result

    original_register = panel.register_live_arena

    async def register_with_lifecycle(bot):
        sid = str(cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or "").strip()
        with sheet_read_scope():
            if sid:
                await refresh_lifecycle_copy(sid)
            result = await original_register(bot)
        if sid and sid not in _registered_views:
            add_view = getattr(bot, "add_view", None)
            if callable(add_view):
                add_view(ResultDecisionView(sid))
            _registered_views.add(sid)
        return result

    panel.register_live_arena = register_with_lifecycle

    original_match_result_init = result_views.MatchResultView.__init__

    def match_result_init_without_dispute(self, sheet_id: str, **kwargs):
        original_match_result_init(self, sheet_id, **kwargs)
        for item in list(getattr(self, "children", ())):
            if getattr(item, "custom_id", "") == "live_arena:match:dispute_result":
                self.remove_item(item)

    result_views.MatchResultView.__init__ = match_result_init_without_dispute

    ux._submit_report = _submit_report
    _patch_round_overview(round_overview)

    original_sync = runtime_hooks._sync_round_discord

    async def sync_round_with_result_lifecycle(bot, qualification_service, snapshot):
        warnings = list(await original_sync(bot, qualification_service, snapshot))
        warnings.extend(
            await _reconcile_current_round(bot, qualification_service, snapshot)
        )
        return list(dict.fromkeys(warnings))

    runtime_hooks._sync_round_discord = sync_round_with_result_lifecycle

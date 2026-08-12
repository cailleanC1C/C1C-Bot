"""Cross-tournament Live Arena Hall of Fame and player history UI."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from string import Formatter

import discord
from shared.sheets.async_core import afetch_values

from modules.community.live_arena.messages import MESSAGE_HEADERS, load_pr5_config
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.qualification import QualificationRepository
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import TOURNAMENT_HEADERS, LiveArenaConfigError, _enabled, _rows, _text, load_config
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.hall_of_fame")
_installed = False
_GLOBAL_RESOURCE_ID = "__LIVE_ARENA_GLOBAL__"
_sync_tasks: dict[str, asyncio.Task] = {}

_MESSAGE_KEYS = {
    "hall_of_fame_panel": {"completed_count", "tournament_lines"},
    "hall_of_fame_empty": set(),
    "player_history": {
        "player_name",
        "appearances",
        "tournament_wins",
        "runner_up_finishes",
        "top8_finishes",
        "semifinal_appearances",
        "final_appearances",
        "match_wins",
        "match_losses",
        "tournament_lines",
    },
    "player_history_empty": {"player_name"},
    "organizer_player_history_prompt": set(),
}


@dataclass(frozen=True)
class TournamentResult:
    tournament_id: str
    tournament_name: str
    completed_at_utc: str
    champion_id: str
    champion_name: str
    runner_up_id: str
    runner_up_name: str


@dataclass(frozen=True)
class PlayerHistory:
    user_id: str
    display_name: str
    appearances: int
    tournament_wins: int
    runner_up_finishes: int
    top8_finishes: int
    semifinal_appearances: int
    final_appearances: int
    match_wins: int
    match_losses: int
    tournament_lines: tuple[str, ...]


class _Template:
    def __init__(self, key, title, description, color):
        self.key, self.title, self.description, self.color = key, title, description, color

    def embed(self, **values):
        expected = _MESSAGE_KEYS[self.key]
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


async def _load_messages(sheet_id: str, keys: set[str]):
    config, _ = await load_pr5_config(sheet_id)
    rows = _rows(
        await afetch_values(sheet_id, config["MESSAGES_TAB"]) or [],
        MESSAGE_HEADERS,
        config["MESSAGES_TAB"],
    )
    result = {}
    for key in keys:
        matches = [
            row for row in rows
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
        fields = {
            name
            for _, name, _, _ in Formatter().parse(
                _text(row["title"]) + _text(row["description"])
            )
            if name
        }
        if fields != _MESSAGE_KEYS[key]:
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: placeholders must be exactly "
                + ", ".join(sorted(_MESSAGE_KEYS[key]))
            )
        result[key] = _Template(
            key,
            _text(row["title"]),
            _text(row["description"]),
            int(color[1:], 16),
        )
    return result


def _name_for(user_id: str, participants, matches) -> str:
    for row in reversed(participants):
        if _text(row.get("discord_user_id")) == user_id and _text(row.get("display_name_at_signup")):
            return _text(row.get("display_name_at_signup"))
    for row in reversed(matches):
        if _text(row.get("player_a_discord_user_id")) == user_id:
            return _text(row.get("player_a_display_name")) or user_id
        if _text(row.get("player_b_discord_user_id")) == user_id:
            return _text(row.get("player_b_display_name")) or user_id
    return user_id


def _completed_tournaments(tournaments):
    return [
        row for row in tournaments
        if _text(row.get("status")) in {"completed", "archived"}
        and _text(row.get("completed_at_utc"))
    ]


def build_history(tournaments, participants, rounds, matches):
    """Derive all historical stats from completed tournament Sheet truth."""
    completed = _completed_tournaments(tournaments)
    completed_ids = {_text(row.get("tournament_id")) for row in completed}
    completed.sort(key=lambda row: _text(row.get("completed_at_utc")))

    tournament_results: list[TournamentResult] = []
    stats = defaultdict(lambda: {
        "appearances": 0,
        "wins": 0,
        "runner": 0,
        "top8": 0,
        "semi": 0,
        "final": 0,
        "mw": 0,
        "ml": 0,
        "lines": [],
    })

    for tournament in completed:
        tid = _text(tournament.get("tournament_id"))
        name = _text(tournament.get("tournament_name")) or tid
        tmatches = [row for row in matches if _text(row.get("tournament_id")) == tid]
        final = next(
            (row for row in tmatches if _text(row.get("round_id")) == f"{tid}-F"),
            None,
        )
        champion_id = _text(final.get("final_winner_discord_user_id")) if final else ""
        runner_id = ""
        if final and champion_id:
            a = _text(final.get("player_a_discord_user_id"))
            b = _text(final.get("player_b_discord_user_id"))
            runner_id = b if champion_id == a else a if champion_id == b else ""
        champion_name = _name_for(champion_id, participants, tmatches) if champion_id else "Unknown"
        runner_name = _name_for(runner_id, participants, tmatches) if runner_id else "Unknown"
        tournament_results.append(
            TournamentResult(
                tid,
                name,
                _text(tournament.get("completed_at_utc")),
                champion_id,
                champion_name,
                runner_id,
                runner_name,
            )
        )

        appearances = set()
        for match in tmatches:
            for key in ("player_a_discord_user_id", "player_b_discord_user_id"):
                uid = _text(match.get(key))
                if uid:
                    appearances.add(uid)
        qf_ids = {
            uid
            for row in tmatches if _text(row.get("round_id")) == f"{tid}-QF"
            for uid in (
                _text(row.get("player_a_discord_user_id")),
                _text(row.get("player_b_discord_user_id")),
            )
            if uid
        }
        sf_ids = {
            uid
            for row in tmatches if _text(row.get("round_id")) == f"{tid}-SF"
            for uid in (
                _text(row.get("player_a_discord_user_id")),
                _text(row.get("player_b_discord_user_id")),
            )
            if uid
        }
        final_ids = {
            uid
            for row in tmatches if _text(row.get("round_id")) == f"{tid}-F"
            for uid in (
                _text(row.get("player_a_discord_user_id")),
                _text(row.get("player_b_discord_user_id")),
            )
            if uid
        }

        records = defaultdict(lambda: [0, 0])
        for match in tmatches:
            if _text(match.get("status")) not in {"finalized", "forfeit", "double_forfeit", "bye"}:
                continue
            if _text(match.get("status")) in {"bye", "double_forfeit"}:
                continue
            winner = _text(match.get("final_winner_discord_user_id"))
            a = _text(match.get("player_a_discord_user_id"))
            b = _text(match.get("player_b_discord_user_id"))
            if not winner or winner not in {a, b}:
                continue
            loser = b if winner == a else a
            records[winner][0] += 1
            if loser:
                records[loser][1] += 1

        for uid in appearances:
            entry = stats[uid]
            entry["appearances"] += 1
            entry["wins"] += int(uid == champion_id)
            entry["runner"] += int(uid == runner_id)
            entry["top8"] += int(uid in qf_ids)
            entry["semi"] += int(uid in sf_ids)
            entry["final"] += int(uid in final_ids)
            entry["mw"] += records[uid][0]
            entry["ml"] += records[uid][1]
            finish = (
                "Champion" if uid == champion_id else
                "Runner-up" if uid == runner_id else
                "Semifinalist" if uid in sf_ids else
                "Top 8" if uid in qf_ids else
                "Qualification"
            )
            entry["lines"].append(
                f"**{name}** · {finish} · {records[uid][0]}-{records[uid][1]}"
            )

    histories = {}
    for uid, entry in stats.items():
        histories[uid] = PlayerHistory(
            user_id=uid,
            display_name=_name_for(uid, participants, matches),
            appearances=entry["appearances"],
            tournament_wins=entry["wins"],
            runner_up_finishes=entry["runner"],
            top8_finishes=entry["top8"],
            semifinal_appearances=entry["semi"],
            final_appearances=entry["final"],
            match_wins=entry["mw"],
            match_losses=entry["ml"],
            tournament_lines=tuple(entry["lines"]),
        )
    return tournament_results, histories


async def _history_data(sheet_id: str):
    config = await load_config(sheet_id)
    repository = LiveArenaRepository(sheet_id)
    qrepo = QualificationRepository(sheet_id)
    await repository.initialize()
    await qrepo.initialize()
    tournaments = _rows(
        await afetch_values(sheet_id, config["TOURNAMENTS_TAB"]) or [],
        TOURNAMENT_HEADERS,
        config["TOURNAMENTS_TAB"],
    )
    participants, rounds, matches = await asyncio.gather(
        repository.participants(), qrepo.rounds(), qrepo.matches()
    )
    return repository, qrepo, build_history(tournaments, participants, rounds, matches)


def _tournament_lines(results: list[TournamentResult]) -> str:
    if not results:
        return ""
    lines = [
        f"**{item.tournament_name}** · 🏆 {item.champion_name} · 🥈 {item.runner_up_name}"
        for item in reversed(results)
    ]
    text = "\n".join(lines)
    return text[:3600]


def _player_lines(history: PlayerHistory) -> str:
    return "\n".join(reversed(history.tournament_lines))[:3000]


async def _player_embed(sheet_id: str, user_id: str, fallback_name: str):
    _, _, (_, histories) = await _history_data(sheet_id)
    history = histories.get(str(user_id))
    keys = {"player_history" if history else "player_history_empty"}
    messages = await _load_messages(sheet_id, keys)
    if history is None:
        return messages["player_history_empty"].embed(player_name=fallback_name)
    return messages["player_history"].embed(
        player_name=history.display_name or fallback_name,
        appearances=history.appearances,
        tournament_wins=history.tournament_wins,
        runner_up_finishes=history.runner_up_finishes,
        top8_finishes=history.top8_finishes,
        semifinal_appearances=history.semifinal_appearances,
        final_appearances=history.final_appearances,
        match_wins=history.match_wins,
        match_losses=history.match_losses,
        tournament_lines=_player_lines(history),
    )


class HallOfFameView(discord.ui.View):
    def __init__(self, sheet_id: str):
        super().__init__(timeout=None)
        self.sheet_id = str(sheet_id)

    @discord.ui.button(
        label="My Tournament History",
        style=discord.ButtonStyle.secondary,
        custom_id="live_arena:history:mine",
    )
    async def mine(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            embed = await _player_embed(
                self.sheet_id,
                str(interaction.user.id),
                getattr(interaction.user, "display_name", str(interaction.user.id)),
            )
        except Exception as exc:
            log.exception("Live Arena player history failed")
            embed = error_embed(exc)
        await interaction.followup.send(embed=embed, ephemeral=True)


class OrganizerPlayerHistoryButton(discord.ui.Button):
    def __init__(self, manager):
        super().__init__(
            label="Player History",
            style=discord.ButtonStyle.secondary,
            custom_id="live_arena:organizer:player_history",
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        try:
            messages = await _load_messages(
                self.manager.sheet_id, {"organizer_player_history_prompt"}
            )
            await interaction.response.send_message(
                embed=messages["organizer_player_history_prompt"].embed(),
                view=OrganizerPlayerHistorySelectView(self.manager),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(embed=error_embed(exc), ephemeral=True)


class OrganizerPlayerHistorySelectView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=300)
        self.add_item(OrganizerPlayerHistorySelect(manager))


class OrganizerPlayerHistorySelect(discord.ui.UserSelect):
    def __init__(self, manager):
        super().__init__(placeholder="Select a player", min_values=1, max_values=1)
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        member = self.values[0]
        try:
            embed = await _player_embed(
                self.manager.sheet_id,
                str(member.id),
                getattr(member, "display_name", str(member.id)),
            )
            await interaction.response.edit_message(embed=embed, view=None)
        except Exception as exc:
            await interaction.response.edit_message(embed=error_embed(exc), view=None)


async def sync_hall_of_fame(manager) -> None:
    repository, qrepo, (results, _) = await _history_data(manager.sheet_id)
    if not results:
        return
    messages = await _load_messages(manager.sheet_id, {"hall_of_fame_panel"})
    embed = messages["hall_of_fame_panel"].embed(
        completed_count=len(results),
        tournament_lines=_tournament_lines(results),
    )
    config = qrepo.config
    channel = manager.bot.get_channel(int(config["ROUND_OVERVIEW_CHANNEL_ID"]))
    if channel is None:
        channel = await manager.bot.fetch_channel(int(config["ROUND_OVERVIEW_CHANNEL_ID"]))

    resource = await repository.discord_resource(
        _GLOBAL_RESOURCE_ID, "hall_of_fame", "main"
    )
    message = None
    if resource and _text(resource.get("message_id")):
        try:
            message = await channel.fetch_message(int(_text(resource["message_id"])))
        except discord.NotFound:
            message = None
    view = HallOfFameView(manager.sheet_id)
    if message is None:
        message = await channel.send(embed=embed, view=view)
    else:
        await message.edit(embed=embed, view=view)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    await repository.upsert_discord_resource(
        tournament_id=_GLOBAL_RESOURCE_ID,
        resource_type="hall_of_fame",
        resource_key="main",
        channel_id=str(channel.id),
        message_id=str(message.id),
        created_at_utc=_text(resource.get("created_at_utc")) if resource else now,
        updated_at_utc=now,
        state="active",
        notes="Cross-tournament Live Arena Hall of Fame",
    )


def _schedule_sync(manager) -> None:
    sheet_id = str(manager.sheet_id)
    existing = _sync_tasks.get(sheet_id)
    if existing is not None and not existing.done():
        return

    async def run():
        try:
            await asyncio.sleep(90)
            await sync_hall_of_fame(manager)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Live Arena Hall of Fame startup sync failed")
        finally:
            if _sync_tasks.get(sheet_id) is asyncio.current_task():
                _sync_tasks.pop(sheet_id, None)

    try:
        _sync_tasks[sheet_id] = asyncio.create_task(
            run(), name=f"live-arena-hall-of-fame:{sheet_id[-6:]}"
        )
    except RuntimeError:
        pass


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import knockout_runtime, qualification_panel

    original_final_recap = knockout_runtime._sync_final_recap

    async def final_recap_with_history(manager, service, summary):
        await original_final_recap(manager, service, summary)
        await sync_hall_of_fame(manager)

    knockout_runtime._sync_final_recap = final_recap_with_history

    original_install = qualification_panel.install_qualification

    def install_with_history(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_hall_of_fame_installed", False):
            return True
        manager._hall_of_fame_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if callable(add_item):
                add_item(OrganizerPlayerHistoryButton(manager))
            return result

        manager.view = view
        add_view = getattr(manager.bot, "add_view", None)
        if callable(add_view):
            add_view(HallOfFameView(manager.sheet_id))
        _schedule_sync(manager)
        return True

    qualification_panel.install_qualification = install_with_history

"""Final PR 6B-1 review fixes for organizer standings and thread evidence/output."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.competition import MATCH_TERMINAL_STATUSES, _is_qualification_round
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.competition_followup")
_installed = False


def install() -> None:
    """Install the final review fixes without changing the persisted Sheet contract."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import competition_admin, qualification_panel, result_views

    original_install = qualification_panel.install_qualification

    def install_qualification_with_organizer_standings(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_competition_followup_installed", False):
            return True
        manager._competition_followup_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if callable(add_item):
                add_item(
                    OrganizerStandingsButton(
                        manager,
                        disabled=status is not None and status != "active",
                    )
                )
            return result

        manager.view = view
        return True

    qualification_panel.install_qualification = install_qualification_with_organizer_standings
    result_views._thread_has_result_screenshot = _thread_has_result_screenshot
    result_views._post_thread_notice = _post_result_thread_embed
    competition_admin._post_ruling_notice = _post_ruling_embed


class OrganizerStandingsButton(discord.ui.Button):
    def __init__(self, manager, *, disabled: bool = False):
        super().__init__(
            label="View Standings",
            custom_id="live_arena:organizer:standings:view",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionResolutionService(self.manager.sheet_id)
            await service.initialize()
            standings = await service.standings()
            matches = await service.repository.matches()
            lines = organizer_standings_lines(standings, matches)
            embed = discord.Embed(
                title="Qualification standings · Organizer",
                description=(
                    "Full tiebreak detail. Public standings show rank + match record only.\n\n"
                    "**How ranking works**\n"
                    "1. **MW** = Match Wins\n"
                    "2. **GD** = Game Differential\n"
                    "3. **SoS** = Strength of Schedule\n"
                    "4. **H2H** = Head-to-Head, used only when it cleanly separates tied players\n\n"
                    + ("\n".join(lines) if lines else "No finalized qualification results yet.")
                )[:4096],
                color=colors.c1c_blue,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            log.exception("Live Arena organizer standings view failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


def organizer_standings_lines(standings, matches) -> list[str]:
    """Render organizer-only ranking detail including H2H only when it resolved a tie."""
    h2h = _clean_head_to_head_notes(standings, matches)
    lines: list[str] = []
    for entry in standings:
        gd = int(entry.game_differential)
        lines.append(
            f"**#{entry.rank}** <@{entry.discord_user_id}> · Record **{entry.match_record}** · "
            f"MW **{entry.match_wins}** · GD **{gd:+d}** · SoS **{entry.strength_of_opponents}** · "
            f"H2H {h2h.get(entry.discord_user_id, '—')}"
        )
    return lines


def _clean_head_to_head_notes(standings, matches) -> dict[str, str]:
    """Show H2H only for a two-player tie remaining after wins/GD/SoS."""
    groups: dict[tuple[int, int, int], list[object]] = {}
    for entry in standings:
        key = (
            int(entry.match_wins),
            int(entry.game_differential),
            int(entry.strength_of_opponents),
        )
        groups.setdefault(key, []).append(entry)

    notes: dict[str, str] = {}
    for entries in groups.values():
        if len(entries) != 2:
            continue
        first, second = entries
        ids = {str(first.discord_user_id), str(second.discord_user_id)}
        head_to_head_rows = [
            row
            for row in matches
            if _is_qualification_round(row)
            and _text(row.get("status")) in MATCH_TERMINAL_STATUSES
            and {
                _text(row.get("player_a_discord_user_id")),
                _text(row.get("player_b_discord_user_id")),
            }
            == ids
            and _text(row.get("final_result_type")) not in {"bye", "double_forfeit"}
        ]
        if len(head_to_head_rows) != 1:
            continue
        winner = _text(head_to_head_rows[0].get("final_winner_discord_user_id"))
        if winner not in ids:
            continue
        loser = next(uid for uid in ids if uid != winner)
        notes[winner] = f"won vs <@{loser}>"
        notes[loser] = f"lost vs <@{winner}>"
    return notes


async def _thread_has_result_screenshot(channel, player_ids: set[str]) -> bool:
    """Search the complete matchup thread so older valid evidence never expires."""
    history = getattr(channel, "history", None)
    if not callable(history):
        return False
    async for message in history(limit=None):
        author_id = str(getattr(getattr(message, "author", None), "id", ""))
        if author_id not in player_ids:
            continue
        for attachment in getattr(message, "attachments", ()):
            content_type = str(getattr(attachment, "content_type", "") or "").lower()
            filename = str(getattr(attachment, "filename", "") or "").lower()
            if content_type.startswith("image/") or filename.endswith(
                (".png", ".jpg", ".jpeg", ".webp")
            ):
                return True
    return False


async def _post_result_thread_embed(channel, content: str) -> None:
    """Keep bot-authored result/dispute information inside an embed."""
    try:
        await channel.send(
            embed=discord.Embed(description=str(content), color=colors.c1c_blue),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        log.exception("Live Arena matchup thread embed notice failed")


async def _post_ruling_embed(manager, match, action: str, reason: str) -> None:
    thread_id = _text(match.get("thread_id"))
    if not thread_id:
        return
    try:
        thread = manager.bot.get_channel(int(thread_id))
        if thread is None:
            thread = await manager.bot.fetch_channel(int(thread_id))
        label = {
            "accept": "reported result accepted",
            "correct": "result corrected",
            "replay": "replay ordered",
            "forfeit_a": "Player A forfeited",
            "forfeit_b": "Player B forfeited",
            "double_forfeit": "double forfeit applied",
        }[action]
        await thread.send(
            embed=discord.Embed(
                title="Organizer ruling",
                description=f"**{label}**\nReason: {reason}",
                color=colors.c1c_blue,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        log.exception("Live Arena organizer ruling thread embed failed")

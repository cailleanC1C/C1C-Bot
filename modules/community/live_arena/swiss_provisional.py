"""Provisional Swiss ranking support for early Q2 previews.

Official standings continue to use finalized MATCHES only. For an organizer-only early
preview, a confirmed player whose current Q1 match is not final yet must still be
pairable. Such a player contributes a provisional 0-0 / 0 GD / 0 SoS state until the
result finalizes, at which point the preview fingerprint invalidation regenerates the
draw automatically.
"""

from __future__ import annotations

from modules.community.live_arena.service import _text
from modules.community.live_arena import swiss as swiss_module
from modules.community.live_arena.swiss import SwissPlayer

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    swiss_module._players_from_roster = _players_from_roster_with_provisional_zeroes


def _players_from_roster_with_provisional_zeroes(roster, standings) -> list[SwissPlayer]:
    standing_by_id = {entry.discord_user_id: entry for entry in standings}
    rows = []
    for row in roster:
        uid = _text(row.get("discord_user_id"))
        standing = standing_by_id.get(uid)
        rows.append(
            {
                "uid": uid,
                "name": _text(row.get("display_name_at_signup")) or uid,
                "wins": standing.match_wins if standing else 0,
                "losses": standing.match_losses if standing else 0,
                "gd": standing.game_differential if standing else 0,
                "sos": standing.strength_of_opponents if standing else 0,
                "official_rank": standing.rank if standing else 1_000_000,
            }
        )
    rows.sort(
        key=lambda item: (
            -int(item["wins"]),
            -int(item["gd"]),
            -int(item["sos"]),
            int(item["official_rank"]),
            str(item["uid"]),
        )
    )
    return [
        SwissPlayer(
            user_id=str(item["uid"]),
            display_name=str(item["name"]),
            wins=int(item["wins"]),
            losses=int(item["losses"]),
            rank=index + 1,
            ranking_index=index,
        )
        for index, item in enumerate(rows)
    ]


install()

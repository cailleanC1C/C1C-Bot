"""Make constrained Swiss manual repair bye-aware."""

from __future__ import annotations

from modules.community.live_arena import swiss_manual
from modules.community.live_arena.bye_support import _is_bye
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    original_conflicted = swiss_manual.conflicted_preview_players
    original_validate = swiss_manual._validate_complete_candidate

    def conflicted_without_bye(current, roster_ids, player_by_id, history):
        bye_rows = [row for row in current if _is_bye(row)]
        if len(bye_rows) > 1:
            raise RegistrationError("Swiss preview contains more than one bye")
        bye_ids = {
            _text(row.get("player_a_discord_user_id"))
            for row in bye_rows
            if _text(row.get("player_a_discord_user_id"))
        }
        normal = [row for row in current if not _is_bye(row)]
        return original_conflicted(
            normal,
            set(roster_ids) - bye_ids,
            {uid: player for uid, player in player_by_id.items() if uid not in bye_ids},
            history,
        )

    def validate_with_bye(current, roster_ids, player_by_id, history):
        bye_rows = [row for row in current if _is_bye(row)]
        if len(bye_rows) > 1:
            raise RegistrationError("Swiss candidate contains more than one bye")
        bye_ids = set()
        for row in bye_rows:
            uid = _text(row.get("player_a_discord_user_id"))
            if not uid or _text(row.get("player_b_discord_user_id")):
                raise RegistrationError("Swiss candidate contains an invalid bye")
            bye_ids.add(uid)
        normal = [row for row in current if not _is_bye(row)]
        original_validate(
            normal,
            set(roster_ids) - bye_ids,
            {uid: player for uid, player in player_by_id.items() if uid not in bye_ids},
            history,
        )
        if bye_ids and not bye_ids.issubset(set(roster_ids)):
            raise RegistrationError("Swiss bye player is outside the confirmed roster")
        normal_ids = {
            _text(row.get(key))
            for row in normal
            for key in ("player_a_discord_user_id", "player_b_discord_user_id")
        }
        if normal_ids & bye_ids:
            raise RegistrationError("Swiss bye player also appears in a matchup")
        if normal_ids | bye_ids != set(roster_ids):
            raise RegistrationError("Swiss candidate must cover the full confirmed roster exactly once")

    swiss_manual.conflicted_preview_players = conflicted_without_bye
    swiss_manual._validate_complete_candidate = validate_with_bye

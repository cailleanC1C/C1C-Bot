"""Full-round hard-rule validation for persisted Q2/Q3 Swiss previews."""

from __future__ import annotations

from modules.community.live_arena.competition import calculate_qualification_standings
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text
from modules.community.live_arena import swiss as swiss_module
from modules.community.live_arena.swiss import _opponent_history, _round_number

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    swiss_module._validate_persisted_draw = validate_persisted_swiss_draw


def validate_persisted_swiss_draw(matches, tid: str, round_number: int) -> None:
    """Revalidate the complete draw, not merely the rows an organizer touched."""
    round_id = f"{tid}-Q{round_number}"
    current = [
        row
        for row in matches
        if _text(row.get("tournament_id")) == tid
        and _text(row.get("round_id")) == round_id
    ]
    if not current:
        raise RegistrationError(f"Q{round_number} has no persisted matches")

    history = _opponent_history(matches, tid, before_round=round_number)
    standings = calculate_qualification_standings(matches, tid)
    standing_by_id = {entry.discord_user_id: entry for entry in standings}
    expected = _prior_round_player_ids(matches, tid, round_number)
    seen: set[str] = set()

    for row in current:
        a = _text(row.get("player_a_discord_user_id"))
        b = _text(row.get("player_b_discord_user_id"))
        if not a or not b or a == b:
            raise RegistrationError("Swiss draw contains an invalid matchup")
        if a in seen or b in seen:
            raise RegistrationError("Swiss draw contains a player more than once")
        if frozenset((a, b)) in history:
            raise RegistrationError("Swiss draw contains a rematch and cannot be approved")
        if a not in standing_by_id or b not in standing_by_id:
            raise RegistrationError(
                "Swiss draw contains a player without a finalized qualification standing"
            )
        if abs(standing_by_id[a].match_wins - standing_by_id[b].match_wins) > 1:
            raise RegistrationError(
                "Swiss draw crosses non-adjacent record groups and cannot be approved"
            )
        seen.update((a, b))

    if expected and seen != expected:
        missing = ", ".join(sorted(expected - seen)) or "none"
        extra = ", ".join(sorted(seen - expected)) or "none"
        raise RegistrationError(
            "Swiss draw must cover the full prior-round field exactly once. "
            f"Missing: {missing}. Extra: {extra}."
        )


def _prior_round_player_ids(matches, tid: str, before_round: int) -> set[str]:
    """Derive the active qualification field from the immediately prior round rows."""
    prior_number = before_round - 1
    result: set[str] = set()
    for row in matches:
        if _text(row.get("tournament_id")) != tid:
            continue
        if _round_number(_text(row.get("round_id"))) != prior_number:
            continue
        for key in ("player_a_discord_user_id", "player_b_discord_user_id"):
            uid = _text(row.get(key))
            if uid:
                result.add(uid)
    return result


install()

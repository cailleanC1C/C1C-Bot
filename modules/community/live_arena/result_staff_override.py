"""Final staff authorization override for Live Arena result decisions.

Participant restrictions still protect ordinary players from confirming or disputing
their own reports. Configured tournament organizers, however, must be able to act on
behalf of the non-reporting participant even when the organizer is also the reporter.
"""

from __future__ import annotations

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text

_installed = False


async def _represented_opponent_with_staff_override(ux, interaction, sheet_id: str, match):
    actor = str(interaction.user.id)
    opponent = ux._opponent_for_report(match)
    reporter = _text(match.get("reported_by_discord_user_id"))
    players = {
        _text(match.get("player_a_discord_user_id")),
        _text(match.get("player_b_discord_user_id")),
    }

    # The actual non-reporting participant keeps their normal player action, even
    # when they also happen to be staff. That preserves the correct personal audit.
    if actor == opponent:
        return opponent, False

    # Staff permission deliberately outranks participant restrictions. This is the
    # production case: an organizer may also be the reporting player but must still
    # be able to confirm/dispute on behalf of the other participant.
    if await ux._is_organizer(interaction, sheet_id):
        if not opponent:
            raise RegistrationError("The non-reporting participant could not be resolved")
        return opponent, True

    if actor in players:
        if actor == reporter:
            raise RegistrationError(
                "The player who reported this result cannot confirm or dispute their own report"
            )
        raise RegistrationError("Only the non-reporting opponent can use this action")

    raise RegistrationError(
        "Only the non-reporting opponent or a configured tournament organizer can use this action"
    )


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import result_lifecycle_ux as ux

    async def represented_opponent(interaction, sheet_id: str, match):
        return await _represented_opponent_with_staff_override(
            ux, interaction, sheet_id, match
        )

    ux._represented_opponent = represented_opponent

"""Keep the knockout completion gate side-effect free until lifecycle completion succeeds."""

from __future__ import annotations

from modules.community.live_arena.competition import MATCH_TERMINAL_STATUSES
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import _text, load_config

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import knockout

    async def validate_complete_tournament(self, actor_id: str) -> dict[str, object]:
        """Validate Final truth and build recap data; OrganizerService owns completion persistence/audit."""
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            rounds = await self.repository.rounds()
            matches = await self.repository.matches()
            final_round = knockout._round_by_id(rounds, tid, f"{tid}-F")
            if final_round is None or _text(final_round.get("status")) != "closed":
                raise RegistrationError(
                    "Close the finalized Final before completing the tournament"
                )
            final_matches = [
                row
                for row in matches
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == f"{tid}-F"
            ]
            if (
                len(final_matches) != 1
                or _text(final_matches[0].get("status")) not in MATCH_TERMINAL_STATUSES
            ):
                raise RegistrationError(
                    "The Final must be finalized before tournament completion"
                )
            champion_id, champion_name = knockout._winner(final_matches[0])
            if not champion_id:
                raise RegistrationError("Tournament completion requires a champion")
            runner_id = knockout._other_player(final_matches[0], champion_id)
            return {
                "tournament_id": tid,
                "champion_discord_user_id": champion_id,
                "champion_display_name": champion_name,
                "runner_up_discord_user_id": runner_id,
                "runner_up_display_name": knockout._display_name(
                    final_matches[0], runner_id
                ),
                "completed_at_utc": utc_iso(self.clock()),
            }

    knockout.KnockoutService.complete_tournament = validate_complete_tournament

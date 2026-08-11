"""Keep Qualification Round 1 opponent selection genuinely random.

Availability is scheduling metadata only. It must never influence who is paired in Q1.
"""

from __future__ import annotations

from modules.community.live_arena import qualification
from modules.community.live_arena.service import _text

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    def random_pairings(self, roster, availability, slots, tid):
        remaining = [dict(row) for row in roster]
        if len(remaining) % 2:
            raise qualification.RegistrationError(
                "Q1 random pairing requires an even roster after any random bye is assigned"
            )

        selected = qualification._enabled_availability(availability, slots, tid)
        slot_rank = qualification._slot_rank(slots)
        pairings = []
        while remaining:
            player_a = self.rng.choice(remaining)
            remaining.remove(player_a)
            player_b = self.rng.choice(remaining)
            remaining.remove(player_b)
            shared = qualification._shared_slots(
                _text(player_a.get("discord_user_id")),
                _text(player_b.get("discord_user_id")),
                selected,
                slot_rank,
            )
            pairings.append((player_a, player_b, shared))
        return pairings

    qualification.QualificationService._optimal_pairings = random_pairings

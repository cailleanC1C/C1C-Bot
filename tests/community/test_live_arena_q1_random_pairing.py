from types import SimpleNamespace

from modules.community.live_arena.qualification import QualificationService


def player(uid: str):
    return {
        "tournament_id": "T1",
        "discord_user_id": uid,
        "display_name_at_signup": f"P{uid}",
        "status": "confirmed",
    }


def test_q1_random_pairing_does_not_optimize_opponents_for_availability():
    service = QualificationService(
        "sheet",
        rng=SimpleNamespace(choice=lambda values: values[0]),
    )
    roster = [player(uid) for uid in ("1", "2", "3", "4")]
    slots = [
        {"slot_id": "A", "enabled": "TRUE", "sort_order": "1"},
        {"slot_id": "B", "enabled": "TRUE", "sort_order": "2"},
    ]
    availability = [
        {"tournament_id": "T1", "discord_user_id": "1", "slot_id": "A"},
        {"tournament_id": "T1", "discord_user_id": "2", "slot_id": "B"},
        {"tournament_id": "T1", "discord_user_id": "3", "slot_id": "A"},
        {"tournament_id": "T1", "discord_user_id": "4", "slot_id": "B"},
    ]

    pairings = service._optimal_pairings(roster, availability, slots, "T1")

    assert [
        (a["discord_user_id"], b["discord_user_id"])
        for a, b, _shared in pairings
    ] == [("1", "2"), ("3", "4")]
    assert all(shared == () for _a, _b, shared in pairings)

    # A zero-conflict arrangement exists (1-3 and 2-4), but availability must not
    # influence who faces whom in Q1. It is attached only after the random draw.

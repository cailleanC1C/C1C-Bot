import asyncio

import pytest

from modules.onboarding.watcher_welcome import cleanup_reservation_for_ticket_close


async def _fresh(**_kwargs):
    return True


async def _noop_recompute(*_args, **_kwargs):
    return None


async def _noop_update(*_args, **_kwargs):
    return None


def _clan_lookup(tags):
    def find(tag, force=False):
        normalized = str(tag).strip().upper()
        if normalized not in tags:
            return None
        return (tags.index(normalized) + 2, ["", "", normalized, "", "2"])

    return find


def test_promo_finishplacement_no_reservation_still_reconciles_open_spots():
    deltas = []

    async def no_reservations(*_args, **_kwargs):
        return []

    async def adjust(tag, delta):
        deltas.append((tag, delta))
        return 1

    result = asyncio.run(
        cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="L0061",
            user="Player",
            user_id=1001,
            final_tag="C1C0",
            previous_final="C1CT",
            require_source_for_open_spot_math=True,
            require_active_reservation=False,
            ensure_fresh_fn=_fresh,
            find_active_reservations_fn=no_reservations,
            find_clan_row_fn=_clan_lookup(["C1CT", "C1C0"]),
            update_reservation_status_fn=_noop_update,
            adjust_manual_open_spots_fn=adjust,
            recompute_clan_availability_fn=_noop_recompute,
        )
    )

    assert sorted(deltas) == [("C1C0", -1), ("C1CT", 1)]
    assert result.reservation_label == "none"
    assert result.reservation_ok is True
    assert result.clan_update_ok is True
    assert result.applied_open_deltas == {"C1CT": 1, "C1C0": -1}


def test_promo_finishplacement_reservation_lookup_failure_does_not_poison_clan_update():
    deltas = []

    async def lookup_fails(*_args, **_kwargs):
        raise RuntimeError("sheets unavailable")

    async def adjust(tag, delta):
        deltas.append((tag, delta))
        return 1

    result = asyncio.run(
        cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="L0061",
            user="Player",
            user_id=1001,
            final_tag="C1C0",
            previous_final="C1CT",
            require_source_for_open_spot_math=True,
            require_active_reservation=False,
            ensure_fresh_fn=_fresh,
            find_active_reservations_fn=lookup_fails,
            find_clan_row_fn=_clan_lookup(["C1CT", "C1C0"]),
            update_reservation_status_fn=_noop_update,
            adjust_manual_open_spots_fn=adjust,
            recompute_clan_availability_fn=_noop_recompute,
        )
    )

    assert sorted(deltas) == [("C1C0", -1), ("C1CT", 1)]
    assert result.reservation_label == "none"
    assert result.reservation_ok is False
    assert result.clan_update_ok is True


def test_promo_finishplacement_final_clan_lookup_failure_marks_clan_partial():
    async def no_reservations(*_args, **_kwargs):
        return []

    def find(tag, force=False):
        normalized = str(tag).strip().upper()
        if normalized == "C1C0":
            raise RuntimeError("lookup failed")
        if normalized == "C1CT":
            return (2, ["", "", normalized, "", "2"])
        return None

    result = asyncio.run(
        cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="L0061",
            user="Player",
            user_id=1001,
            final_tag="C1C0",
            previous_final="C1CT",
            require_source_for_open_spot_math=True,
            require_active_reservation=False,
            ensure_fresh_fn=_fresh,
            find_active_reservations_fn=no_reservations,
            find_clan_row_fn=find,
            update_reservation_status_fn=_noop_update,
            adjust_manual_open_spots_fn=_noop_update,
            recompute_clan_availability_fn=_noop_recompute,
        )
    )

    assert result.clan_update_ok is False
    assert result.reason == "final_clan_lookup_failed"


def test_promo_finishplacement_open_spot_write_failure_marks_clan_partial():
    async def no_reservations(*_args, **_kwargs):
        return []

    async def adjust(tag, delta):
        raise RuntimeError(f"cannot write {tag} {delta}")

    result = asyncio.run(
        cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="L0061",
            user="Player",
            user_id=1001,
            final_tag="C1C0",
            previous_final="C1CT",
            require_source_for_open_spot_math=True,
            require_active_reservation=False,
            ensure_fresh_fn=_fresh,
            find_active_reservations_fn=no_reservations,
            find_clan_row_fn=_clan_lookup(["C1CT", "C1C0"]),
            update_reservation_status_fn=_noop_update,
            adjust_manual_open_spots_fn=adjust,
            recompute_clan_availability_fn=_noop_recompute,
        )
    )

    assert result.clan_update_ok is False
    assert result.applied_open_deltas == {}
    assert result.reason.startswith("adjust_manual_open_spots_failed:")


def test_promo_finishplacement_same_source_destination_has_no_open_spot_delta():
    deltas = []

    async def no_reservations(*_args, **_kwargs):
        return []

    async def adjust(tag, delta):
        deltas.append((tag, delta))
        return 1

    result = asyncio.run(
        cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="L0061",
            user="Player",
            user_id=1001,
            final_tag="C1C0",
            previous_final="C1C0",
            require_source_for_open_spot_math=True,
            require_active_reservation=False,
            ensure_fresh_fn=_fresh,
            find_active_reservations_fn=no_reservations,
            find_clan_row_fn=_clan_lookup(["C1C0"]),
            update_reservation_status_fn=_noop_update,
            adjust_manual_open_spots_fn=adjust,
            recompute_clan_availability_fn=_noop_recompute,
        )
    )

    assert deltas == []
    assert result.clan_update_ok is True
    assert result.applied_open_deltas == {}

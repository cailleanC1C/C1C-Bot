import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import pytest
from modules.community.live_arena_tournament.config import parse_system_config
from modules.community.live_arena_tournament.models import (
    AvailabilityError,
    AvailabilitySlot,
    SchemaError,
    slot_local_datetime,
    validate_availability,
    parse_weekday,
)
from modules.community.live_arena_tournament.service import LiveArenaService
from modules.community.live_arena_tournament.repository import LiveArenaRepository
from modules.community.live_arena_tournament.rendering import configured_embed
from modules.community.live_arena_tournament.views import PersistentPanel
from modules.community.live_arena_tournament.models import Tournament

TABLES = (
    "tournaments",
    "eligible_clans",
    "roles",
    "destinations",
    "participants",
    "availability_slots",
    "participant_availability",
    "messages",
    "message_components",
    "bot_state",
    "audit_log",
)


def config_rows():
    return [
        ["Key", "Value"],
        ["active_tournament_id", "T1"],
        *[[f"tab.{x}", f"route-{x}"] for x in TABLES],
    ]


def test_system_config_routes_without_fallback_names():
    cfg = parse_system_config(config_rows(), "sheet")
    assert cfg.tabs["participants"] == "route-participants"
    assert cfg.tabs["tournaments"] == "route-tournaments"
    assert cfg.tabs["roles"] == "route-roles"


def test_missing_route_is_actionable():
    with pytest.raises(SchemaError, match=r"tab.audit_log"):
        parse_system_config(config_rows()[:-1], "sheet")


def slots():
    return [
        AvailabilitySlot("a", 0, "23:00", "01:00"),
        AvailabilitySlot("b", 1, "12:00", "14:00"),
        AvailabilitySlot("c", 2, "12:00", "14:00"),
        AvailabilitySlot("off", 3, "12:00", "14:00", False),
    ]


def test_availability_deduplicates_and_spans_local_days():
    assert validate_availability(["a", "a", "b", "c"], slots(), "Europe/Vienna") == [
        "a",
        "b",
        "c",
    ]


def test_availability_minimum_and_disabled():
    with pytest.raises(AvailabilityError, match="at least"):
        validate_availability(["a", "b"], slots(), "UTC")
    with pytest.raises(AvailabilityError, match="enabled"):
        validate_availability(["a", "b", "off"], slots(), "UTC")


def test_availability_one_day_fails():
    same = [AvailabilitySlot(str(i), 0, f"{i * 2:02}:00", "") for i in range(3)]
    with pytest.raises(AvailabilityError, match="two local weekdays"):
        validate_availability(["0", "1", "2"], same, "UTC")


def test_timezone_conversion_is_dst_aware():
    winter = slot_local_datetime(
        slots()[0],
        "Europe/Vienna",
        anchor_monday=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    summer = slot_local_datetime(
        slots()[0],
        "Europe/Vienna",
        anchor_monday=datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    assert winter.utcoffset() != summer.utcoffset()


def test_eligibility_uses_role_ids_and_removed_is_detectable():
    rows = [
        {
            "tournament_id": "T1",
            "discord_role_id": "7",
            "clan_tag": "C1CM",
            "active": "true",
        }
    ]
    assert LiveArenaService.eligible_clan({7}, rows, "T1") == "C1CM"
    with pytest.raises(Exception):
        LiveArenaService.eligible_clan({8}, rows, "T1")


def test_transitions_are_registration_only():
    assert LiveArenaService.can_transition("draft", "signup_open")
    assert LiveArenaService.can_transition("signup_open", "signup_closed")
    assert not LiveArenaService.can_transition("draft", "signup_closed")


def test_real_workbook_headers_render_and_route_components():
    embed = configured_embed(
        {
            "title_template": "{tournament_name}",
            "body_template": "{status}",
            "embed_color_hex": "#123456",
        },
        {"tournament_name": "Cup", "status": "Open"},
    )
    assert embed.title == "Cup" and embed.description == "Open"
    view = PersistentPanel(
        object(),
        [
            {
                "action_id": "join_tournament",
                "label": "Join",
                "style": "success",
                "active": "true",
            }
        ],
        ["join_tournament"],
    )
    assert view.children[0].custom_id.endswith("join_tournament")
    assert parse_weekday("Monday") == 0


def test_repository_reads_named_system_config_not_first_tab():
    repo = LiveArenaRepository("sheet")
    with patch(
        "modules.community.live_arena_tournament.repository.asheets_read",
        new=AsyncMock(return_value=config_rows()),
    ) as read:
        asyncio.run(repo.load_config())
    read.assert_awaited_once_with("sheet", "System_Config!A:Z")


def test_shared_sheets_helper_receives_unquoted_worksheet_name(monkeypatch):
    from shared.sheets import core

    workbook = object()
    worksheet = object()
    monkeypatch.setattr(core, "aopen_by_key", AsyncMock(return_value=workbook))
    by_title = AsyncMock(return_value=worksheet)
    values = AsyncMock(return_value=[["Key", "Value"]])
    monkeypatch.setattr(core.async_adapter, "aworksheet_by_title", by_title)
    monkeypatch.setattr(core.async_adapter, "aworksheet_values_get", values)
    asyncio.run(core.asheets_read("sheet", "System_Config!A:Z"))
    assert by_title.await_args.args == (workbook, "System_Config")
    assert values.await_args.args == (worksheet, "A:Z")


def test_config_snapshot_loads_optional_live_arena_sheet_id(monkeypatch):
    from shared import config

    monkeypatch.setenv("LIVE_ARENA_TOURNAMENT_SHEET_ID", " workbook-id ")
    assert (
        config._load_config_snapshot()["LIVE_ARENA_TOURNAMENT_SHEET_ID"]
        == "workbook-id"
    )


def test_all_real_message_placeholders_are_resolved():
    values = {
        "participant_count": 7,
        "max_participants": 8,
        "tournament_status": "signup_open",
        "roster_parity_summary": "Odd roster",
    }
    for key in ("signup_open", "signup_closed", "registration_organizer"):
        embed = configured_embed(
            {
                "message_key": key,
                "body_template": "{participant_count}/{max_participants} {tournament_status} {roster_parity_summary}",
            },
            values,
        )
        assert "{" not in embed.description


def test_register_and_update_use_idempotent_participant_and_availability_writes():
    repo = AsyncMock()
    participant = {
        "_row_number": 2,
        "tournament_id": "T1",
        "participant_slot": "1",
        "status": "open",
        "discord_user_id": "",
    }
    repo.rows.side_effect = [
        [participant],
        [
            {
                "tournament_id": "T1",
                "discord_role_id": "7",
                "clan_tag": "C1C",
                "active": "true",
            }
        ],
        [
            {
                "tournament_id": "T1",
                "slot_id": x,
                "weekday_utc": day,
                "start_time_utc": "12:00",
                "enabled": "true",
            }
            for x, day in (("a", "Monday"), ("b", "Tuesday"), ("c", "Wednesday"))
        ],
    ]
    service = LiveArenaService(repo)
    result = asyncio.run(
        service.register(
            tournament=Tournament("T1", "Cup", "signup_open", 8, 3),
            user_id="42",
            display_name="Player",
            member_role_ids=[7],
            timezone_name="UTC",
            slot_ids=["a", "b", "c"],
        )
    )
    assert result["created"] is True
    repo.replace_row.assert_awaited_once()
    repo.replace_availability.assert_awaited_once_with(
        "T1", "42", ["a", "b", "c"], repo.replace_availability.await_args.args[3]
    )
    repo.audit.assert_awaited_once()


def test_removed_participant_cannot_self_withdraw():
    repo = AsyncMock()
    repo.rows.return_value = [
        {"tournament_id": "T1", "discord_user_id": "42", "status": "removed"}
    ]
    with pytest.raises(Exception, match="Cannot change participant"):
        asyncio.run(
            LiveArenaService(repo).change_participant_status(
                "T1", "42", "withdrawn", "42"
            )
        )
    repo.replace_row.assert_not_awaited()


def test_disqualified_participant_cannot_self_register():
    repo = AsyncMock()
    repo.rows.return_value = [
        {"tournament_id": "T1", "discord_user_id": "42", "status": "disqualified"}
    ]
    with pytest.raises(Exception, match="cannot be changed through self-service"):
        asyncio.run(
            LiveArenaService(repo).register(
                tournament=Tournament("T1", "Cup", "signup_open", 8, 3),
                user_id="42",
                display_name="Player",
                member_role_ids=[7],
                timezone_name="UTC",
                slot_ids=["a", "b", "c"],
            )
        )
    repo.replace_row.assert_not_awaited()


def test_capacity_disables_join_but_not_update():
    view = PersistentPanel(
        object(),
        [
            {"action_id": action, "label": action, "active": "true"}
            for action in ("join_tournament", "update_availability")
        ],
        ["join_tournament", "update_availability"],
    )
    view.disable_actions({"join_tournament"})
    states = {item.action: item.disabled for item in view.children}
    assert states == {"join_tournament": True, "update_availability": False}


class MemoryRepository:
    def __init__(self, participants):
        self.participants = participants
        self.audits = []

    async def rows(self, table, required=()):
        if table == "participants":
            return self.participants
        if table == "eligible_clans":
            return [
                {
                    "tournament_id": "T1",
                    "discord_role_id": "7",
                    "clan_tag": "C1C",
                    "active": "true",
                }
            ]
        if table == "availability_slots":
            return [
                {
                    "tournament_id": "T1",
                    "slot_id": f"s{i}",
                    "weekday_utc": day,
                    "start_time_utc": "12:00",
                    "end_time_utc": "14:00",
                    "end_day_offset": 0,
                    "enabled": "true",
                    "sort_order": i,
                }
                for i, day in enumerate(("Monday", "Tuesday", "Wednesday"), 1)
            ]
        return []

    async def replace_row(self, table, row_number, changes):
        self.participants[row_number - 2].update(changes)

    async def append(self, table, changes):
        if table == "participants":
            self.participants.append(
                {**changes, "_row_number": len(self.participants) + 2}
            )

    async def replace_availability(self, *args):
        return None

    async def availability_slot_ids(self, *args):
        return []

    async def audit(self, *args):
        self.audits.append(args)


async def _register(service, user):
    return await service.register(
        tournament=Tournament("T1", "Cup", "signup_open", 16, 3),
        user_id=str(user),
        display_name=f"P{user}",
        member_role_ids=[7],
        timezone_name="UTC",
        slot_ids=["s1", "s2", "s3"],
        anchor_monday=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


def test_withdrawal_frees_capacity_without_overwriting_history():
    rows = [
        {
            "_row_number": i + 2,
            "tournament_id": "T1",
            "participant_slot": i + 1,
            "status": "confirmed",
            "discord_user_id": str(i + 1),
        }
        for i in range(16)
    ]
    rows[3]["status"] = "withdrawn"
    repo = MemoryRepository(rows)
    result = asyncio.run(_register(LiveArenaService(repo), 99))
    assert result["created"] is True
    assert len(repo.participants) == 17
    assert repo.participants[3]["discord_user_id"] == "4"
    assert repo.participants[-1]["discord_user_id"] == "99"


def test_withdrawn_history_cannot_reregister_as_seventeenth_confirmed():
    rows = [
        {
            "_row_number": i + 2,
            "tournament_id": "T1",
            "participant_slot": i + 1,
            "status": "confirmed",
            "discord_user_id": str(i + 1),
        }
        for i in range(16)
    ]
    rows.append(
        {
            "_row_number": 18,
            "tournament_id": "T1",
            "participant_slot": 17,
            "status": "withdrawn",
            "discord_user_id": "99",
        }
    )
    with pytest.raises(Exception, match="capacity"):
        asyncio.run(_register(LiveArenaService(MemoryRepository(rows)), 99))


def test_confirmed_availability_update_is_allowed_at_capacity():
    rows = [
        {
            "_row_number": i + 2,
            "tournament_id": "T1",
            "participant_slot": i + 1,
            "status": "confirmed",
            "discord_user_id": str(i + 1),
        }
        for i in range(16)
    ]
    result = asyncio.run(_register(LiveArenaService(MemoryRepository(rows)), 1))
    assert result["created"] is False


def test_restore_and_register_share_capacity_lock():
    rows = [
        {
            "_row_number": i + 2,
            "tournament_id": "T1",
            "participant_slot": i + 1,
            "status": "confirmed",
            "discord_user_id": str(i + 1),
        }
        for i in range(15)
    ]
    rows.append(
        {
            "_row_number": 17,
            "tournament_id": "T1",
            "participant_slot": 16,
            "status": "withdrawn",
            "discord_user_id": "50",
        }
    )
    repo = MemoryRepository(rows)
    service = LiveArenaService(repo)

    async def race():
        return await asyncio.gather(
            _register(service, 99),
            service.change_participant_status(
                "T1",
                "50",
                "confirmed",
                "organizer",
                tournament=Tournament("T1", "Cup", "signup_closed", 16, 3),
                member_present=True,
                member_role_ids=[7],
                eligible_rows=await repo.rows("eligible_clans"),
            ),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    assert sum(isinstance(item, Exception) for item in results) == 1
    assert service.confirmed_count(repo.participants, "T1") == 16


def test_configured_embed_rejects_unresolved_placeholders_and_keeps_colour():
    from modules.community.live_arena_tournament.models import SchemaError

    row = {
        "title_template": "Welcome {participant}",
        "body_template": "{tournament_name} by {signup_deadline}",
        "embed_color_hex": "#abcdef",
    }
    embed = configured_embed(
        row,
        {
            "participant": "Player",
            "tournament_name": "Cup",
            "signup_deadline": "<t:1:F>",
        },
    )
    assert embed.title == "Welcome Player" and embed.description == "Cup by <t:1:F>"
    assert embed.colour.value == 0xABCDEF
    with pytest.raises(SchemaError, match="signup_deadline"):
        configured_embed(row, {"participant": "Player", "tournament_name": "Cup"})


def test_local_window_cross_midnight_and_component_sort_order():
    from modules.community.live_arena_tournament.models import slot_local_window

    slot = AvailabilitySlot("night", 0, "23:00", "01:00", True, 2, 1)
    start, end = slot_local_window(
        slot, "UTC", anchor_monday=datetime(2026, 8, 3, tzinfo=timezone.utc)
    )
    assert start.strftime("%a %H:%M") == "Mon 23:00"
    assert end.strftime("%a %H:%M") == "Tue 01:00"
    view = PersistentPanel(
        object(),
        [
            {
                "action_id": "join_tournament",
                "label": "Join",
                "active": "true",
                "sort_order": "2",
            },
            {
                "action_id": "my_registration",
                "label": "Mine",
                "active": "true",
                "sort_order": "1.0",
            },
        ],
        ["join_tournament", "my_registration"],
    )
    assert [item.action for item in view.children] == [
        "my_registration",
        "join_tournament",
    ]


def test_opening_registration_requires_configured_deadline():
    from modules.community.live_arena_tournament.cog import LiveArenaTournamentCog

    with pytest.raises(Exception, match="signup_closes_at is required"):
        LiveArenaTournamentCog._deadline_text("")
    assert LiveArenaTournamentCog._deadline_text("2026-08-10T18:00:00Z").startswith(
        "<t:"
    )

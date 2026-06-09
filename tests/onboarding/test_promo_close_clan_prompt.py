import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.onboarding.watcher_promo import PromoTicketContext, PromoTicketWatcher
from shared.sheets.reservations import ReservationRow


@pytest.fixture(autouse=True)
def _promo_source_header_config(monkeypatch):
    from shared.sheets import onboarding as onboarding_sheets

    config = {
        "promo_source_clan_tag_header": "source_clan_tag",
        "promo_finalization_status_header": "finalization_status",
        "promo_reservation_status_header": "reservation_status",
        "promo_clan_update_status_header": "clan_update_status",
        "promo_finalization_note_header": "finalization_note",
    }
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE", config)
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)
    monkeypatch.setattr(
        "shared.sheets.onboarding.update_ticket_finalization_state",
        lambda *_args, **_kwargs: "updated",
    )

    async def no_reservations(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "modules.onboarding.watcher_promo.reservations_sheets.find_active_reservations_for_recruit",
        no_reservations,
    )


class DummyThread:
    def __init__(self, thread_id=1, name="R1234-user"):
        self.id = thread_id
        self.name = name
        self.archived = False
        self.locked = False
        self.parent_id = 123
        self.sent = []
        self.guild = SimpleNamespace(id=999)

    async def send(self, content=None, **kwargs):
        msg = SimpleNamespace(id=999, content=content or "", created_at=datetime.now(timezone.utc))
        self.sent.append((content, kwargs))
        return msg


class DummyAuthor:
    def __init__(self, bot=False, user_id=42):
        self.bot = bot
        self.id = user_id


class DummyBot:
    user = SimpleNamespace(id=111)


def _context(state="open", clan_tag="", source_clan_tag="NONE"):
    return PromoTicketContext(
        thread_id=1,
        ticket_number="R1234",
        username="user",
        promo_type="Returning",
        thread_created="2026-01-01 00:00:00",
        year="2026",
        month="January",
        state=state,
        clan_tag=clan_tag,
        source_clan_tag=source_clan_tag,
    )


def _setup_watcher(monkeypatch):
    monkeypatch.setattr("modules.common.feature_flags.is_enabled", lambda *_: True)
    monkeypatch.setattr("modules.onboarding.watcher_promo.get_promo_channel_id", lambda: 123)
    monkeypatch.setattr("modules.onboarding.watcher_promo.get_ticket_tool_bot_id", lambda: 555)
    monkeypatch.setattr("modules.onboarding.watcher_promo.thread_scopes.is_promo_parent", lambda *_: True)
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sessions.get_by_thread_id", lambda *_: None)
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sessions.mark_completed", lambda *_: True)
    monkeypatch.setattr("modules.onboarding.watcher_promo.discord.Thread", DummyThread)
    return PromoTicketWatcher(bot=DummyBot())


def test_close_no_detected_tag_shows_picker(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context()
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()

    before = DummyThread(1)
    after = DummyThread(1)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_awaited_once_with(after, ctx)


def test_close_preloaded_tag_still_shows_picker(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(clan_tag="C1CE")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()

    before = DummyThread(2)
    after = DummyThread(2)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_awaited_once_with(after, ctx)


def test_close_with_existing_row_tag_still_shows_picker(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(clan_tag="C1CW")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()

    before = DummyThread(3)
    after = DummyThread(3)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_awaited_once_with(after, ctx)


def test_finalize_not_called_during_close_handling(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context()
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()
    watcher._finalize_clan_tag = AsyncMock()

    before = DummyThread(4)
    after = DummyThread(4)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._finalize_clan_tag.assert_not_awaited()


def test_typed_tag_while_awaiting_clan_finalizes(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    watcher._finalize_clan_tag = AsyncMock()

    thread = DummyThread(5)
    message = SimpleNamespace(channel=thread, author=DummyAuthor(bot=False, user_id=77), content="c1ce")
    asyncio.run(watcher.on_message(message))
    watcher._finalize_clan_tag.assert_awaited_once()


def test_dropdown_selection_finalizes(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    watcher._finalize_clan_tag = AsyncMock()
    interaction = SimpleNamespace(channel=DummyThread(6), user=DummyAuthor(), message=SimpleNamespace(), followup=SimpleNamespace(send=AsyncMock()))
    view = SimpleNamespace()
    asyncio.run(watcher.finalize_from_interaction(ctx, "C1CE", interaction, view))
    watcher._finalize_clan_tag.assert_awaited_once()


def test_after_clan_confirm_progression_still_completes(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    thread = DummyThread(7, "closed-R7777-user")
    thread.edit = AsyncMock()

    message = SimpleNamespace(channel=thread, author=DummyAuthor(bot=False), content="C1CE")
    asyncio.run(watcher.on_message(message))
    assert ctx.state == "closed"
    assert ctx.clan_tag == "C1CE"
    assert any("Please reply with progression" in (content or "") for content, _ in thread.sent) is False


def test_finalize_never_enters_awaiting_details(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    thread = DummyThread(8, "closed-R8888-user")
    thread.edit = AsyncMock()
    asyncio.run(
        watcher._finalize_clan_tag(
            thread,
            ctx,
            "C1CE",
            actor=None,
            prompt_message=None,
            view=None,
        )
    )
    assert ctx.state == "closed"


def test_promo_close_with_no_active_reservation_skips_cleanup(monkeypatch, caplog):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row", lambda *_: (2, {"clantag": ""}))
    monkeypatch.setattr("modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row", lambda *_: (10, ["", "", "C1CE"]))
    adjust = AsyncMock()
    recompute = AsyncMock()
    monkeypatch.setattr("modules.onboarding.watcher_promo._ensure_fresh_clans_for_placement", AsyncMock(return_value=True))
    monkeypatch.setattr("modules.onboarding.watcher_promo.reservations_sheets.find_active_reservations_for_recruit", AsyncMock(return_value=[]))
    monkeypatch.setattr("modules.onboarding.watcher_promo.availability.adjust_manual_open_spots", adjust)
    monkeypatch.setattr("modules.onboarding.watcher_promo.availability.recompute_clan_availability", recompute)
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    thread = DummyThread(10, "closed-R1111-user")
    thread.edit = AsyncMock()
    with caplog.at_level(logging.INFO):
        asyncio.run(
            watcher._finalize_clan_tag(
                thread,
                ctx,
                "C1CE",
                actor=None,
                prompt_message=None,
                view=None,
            )
        )
    assert "scope=promo" in caplog.text
    assert "decision_result=applied_open_delta" in caplog.text
    assert "promo_reservation_cleanup" in caplog.text
    adjust.assert_awaited_once()
    recompute.assert_awaited_once()
    thread.edit.assert_awaited()
    assert any("Logged clan tag" in (content or "") for content, _ in thread.sent)


def test_promo_close_with_active_reservation_releases_and_recomputes(monkeypatch, caplog):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    ctx.user_id = 4242
    row = ReservationRow(
        row_number=7,
        thread_id="10",
        ticket_user_id=4242,
        recruiter_id=None,
        clan_tag="C1CE",
        reserved_until=None,
        created_at=None,
        status="active",
        notes="",
        username_snapshot="user",
        raw=[],
    )
    updates = []
    recomputed = []
    sync_clan_lookups = []
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row", lambda *_: (2, {"clantag": ""}))
    monkeypatch.setattr("modules.onboarding.watcher_promo._ensure_fresh_clans_for_placement", AsyncMock(return_value=True))
    monkeypatch.setattr("modules.onboarding.watcher_promo.reservations_sheets.find_active_reservations_for_recruit", AsyncMock(return_value=[row]))
    monkeypatch.setattr("modules.onboarding.watcher_promo.reservations_sheets.update_reservation_status", AsyncMock(side_effect=lambda row_number, status: updates.append((row_number, status))))

    def sync_find_clan_row(*_args, **_kwargs):
        sync_clan_lookups.append((_args, _kwargs))
        return 10, ["", "", "C1CE"]

    monkeypatch.setattr("modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row", sync_find_clan_row)
    monkeypatch.setattr("modules.onboarding.watcher_promo.availability.adjust_manual_open_spots", AsyncMock())
    monkeypatch.setattr("modules.onboarding.watcher_promo.availability.recompute_clan_availability", AsyncMock(side_effect=lambda tag, guild=None: recomputed.append(tag)))
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    thread = DummyThread(10, "closed-R1111-user")
    thread.edit = AsyncMock()
    with caplog.at_level(logging.INFO):
        asyncio.run(watcher._finalize_clan_tag(thread, ctx, "C1CE", actor=None, prompt_message=None, view=None))
    assert updates == [(7, "closed_same_clan")]
    assert sync_clan_lookups
    assert recomputed == ["C1CE"]
    assert "scope=promo" in caplog.text
    assert "reservation=row7" in caplog.text
    assert "old_status=active" in caplog.text
    assert "new_status=closed_same_clan" in caplog.text
    assert "recomputed:C1CE" in caplog.text
    thread.edit.assert_awaited()
    assert any("Logged clan tag" in (content or "") for content, _ in thread.sent)


def test_promo_close_active_other_clan_recalculates_summary_clans(monkeypatch, caplog):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    row = ReservationRow(
        row_number=8, thread_id="10", ticket_user_id=None, recruiter_id=None,
        clan_tag="C1CK", reserved_until=None, created_at=None, status="active",
        notes="", username_snapshot="user", raw=[],
    )
    updates = []
    adjustments = []
    recomputed = []
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row", lambda *_: (2, {"clantag": ""}))
    monkeypatch.setattr("modules.onboarding.watcher_promo._ensure_fresh_clans_for_placement", AsyncMock(return_value=True))
    monkeypatch.setattr("modules.onboarding.watcher_promo.reservations_sheets.find_active_reservations_for_recruit", AsyncMock(return_value=[row]))
    monkeypatch.setattr("modules.onboarding.watcher_promo.reservations_sheets.update_reservation_status", AsyncMock(side_effect=lambda row_number, status: updates.append((row_number, status))))
    monkeypatch.setattr("modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row", lambda *_args, **_kwargs: (10, ["", "", "C1CE"]))
    monkeypatch.setattr("modules.onboarding.watcher_promo.availability.adjust_manual_open_spots", AsyncMock(side_effect=lambda tag, delta: adjustments.append((tag, delta))))
    monkeypatch.setattr("modules.onboarding.watcher_promo.availability.recompute_clan_availability", AsyncMock(side_effect=lambda tag, guild=None: recomputed.append(tag)))
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE", "C1CK"])
    with caplog.at_level(logging.INFO):
        asyncio.run(watcher._finalize_clan_tag(DummyThread(10), ctx, "C1CE", actor=None, prompt_message=None, view=None))
    assert updates == [(8, "closed_other_clan")]
    assert sorted(adjustments) == [("C1CE", -1), ("C1CK", 1)]
    assert recomputed == ["C1CK", "C1CE"]
    assert "reservation=row8" in caplog.text


def test_duplicate_close_signal_ignored_after_closed(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="closed", clan_tag="C1CE")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()
    before = DummyThread(9)
    after = DummyThread(9)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_not_awaited()

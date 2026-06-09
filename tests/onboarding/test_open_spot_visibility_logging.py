import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from modules.onboarding.watcher_welcome import WelcomeTicketWatcher, TicketContext, _NO_PLACEMENT_TAG
from modules.onboarding.watcher_promo import PromoTicketWatcher, PromoTicketContext
from shared.sheets import onboarding as onboarding_sheets


@pytest.fixture(autouse=True)
def _finalization_and_cache_mocks(monkeypatch):
    config = {
        "welcome_finalization_status_header": "finalization_status",
        "welcome_reservation_status_header": "reservation_status",
        "welcome_clan_update_status_header": "clan_update_status",
        "welcome_finalization_note_header": "finalization_note",
        "promo_finalization_status_header": "finalization_status",
        "promo_reservation_status_header": "reservation_status",
        "promo_clan_update_status_header": "clan_update_status",
        "promo_finalization_note_header": "finalization_note",
        "promo_source_clan_tag_header": "source_clan_tag",
    }
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE", config)
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    async def fresh(*_args, **_kwargs):
        return True

    async def no_reservations(*_args, **_kwargs):
        return []

    def state_update(*_args, **_kwargs):
        return "updated"

    monkeypatch.setattr("modules.onboarding.watcher_welcome._ensure_fresh_clans_for_placement", fresh)
    monkeypatch.setattr("modules.onboarding.watcher_promo._ensure_fresh_clans_for_placement", fresh)
    async def preflight_ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr("modules.onboarding.watcher_promo.reservations_sheets.find_active_reservations_for_recruit", no_reservations)
    monkeypatch.setattr("modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update", preflight_ok)
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
    monkeypatch.setattr("modules.onboarding.watcher_welcome.onboarding_sheets.update_ticket_finalization_state", state_update)
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.update_ticket_finalization_state", state_update)



class DummyThread:
    def __init__(self):
        self.id = 1
        self.name = "W0001-user"
        self.guild = SimpleNamespace(get_channel=lambda *_: None, guilds=[])
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))
        return SimpleNamespace(id=111)

    async def edit(self, **kwargs):
        return None


class DummyBot:
    user = SimpleNamespace(id=123)


def test_welcome_same_tag_logs_idempotent_reason(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        ctx = TicketContext(thread_id=1, ticket_number='W1', username='u', recruit_id=1, recruit_display='u')
        ctx.state = 'awaiting_clan'
        logs = []
        monkeypatch.setattr('modules.onboarding.watcher_welcome.log_sheet_write', lambda **kwargs: asyncio.sleep(0, result='updated'))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', lambda _t: (2, ['W1','u','','','','','','','','','','']) )
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.WELCOME_CLAN_TAG_INDEX', 2)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','C1CE','','4'] + ['']*30) if tag=='C1CE' else None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit', lambda *a,**k: asyncio.sleep(0, result=[]))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots', lambda *a,**k: asyncio.sleep(0, result=4))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.recompute_clan_availability', lambda *a,**k: asyncio.sleep(0, result=None))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
        monkeypatch.setattr('modules.onboarding.watcher_welcome.rt.send_log_message', lambda m: logs.append(m) or asyncio.sleep(0))
        await watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CE', actor=None, source='s', prompt_message=None, view=None)
        await bot.close()
        assert any('decision:' in m for m in logs)
    asyncio.run(run())


def test_welcome_first_time_applies_delta(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        ctx = TicketContext(thread_id=1, ticket_number='W2', username='u', recruit_id=1, recruit_display='u')
        ctx.state = 'awaiting_clan'
        logs = []
        deltas = []
        monkeypatch.setattr('modules.onboarding.watcher_welcome.log_sheet_write', lambda **kwargs: asyncio.sleep(0, result='updated'))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', lambda _t: (2, ['W0','u','','','','','1','','open','','','','pending','pending','pending','']))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','C1CE','','4'] + ['']*30) if tag=='C1CE' else None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit', lambda *a,**k: asyncio.sleep(0, result=[]))
        async def fake_adjust(tag, delta):
            deltas.append((tag, delta))
            return 3
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots', fake_adjust)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.recompute_clan_availability', lambda *a,**k: asyncio.sleep(0, result=None))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
        monkeypatch.setattr('modules.onboarding.watcher_welcome.rt.send_log_message', lambda m: logs.append(m) or asyncio.sleep(0))
        await watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CE', actor=None, source='s', prompt_message=None, view=None)
        await bot.close()
        assert ('C1CE', -1) in deltas
        assert any('decision_result=applied_open_delta' in m for m in logs)
    asyncio.run(run())


def test_welcome_non_real_logs_skip_reason(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CZ", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        ctx = TicketContext(thread_id=1, ticket_number='W3', username='u', recruit_id=1, recruit_display='u')
        ctx.state = 'awaiting_clan'
        logs = []
        monkeypatch.setattr('modules.onboarding.watcher_welcome.log_sheet_write', lambda **kwargs: asyncio.sleep(0, result='updated'))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', lambda _t: (2, ['W0','u','','','','','1','','open','','','','pending','pending','pending','']))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row', lambda _tag: None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit', lambda *a,**k: asyncio.sleep(0, result=[]))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
        monkeypatch.setattr('modules.onboarding.watcher_welcome.rt.send_log_message', lambda m: logs.append(m) or asyncio.sleep(0))
        await watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CZ', actor=None, source='s', prompt_message=None, view=None)
        await bot.close()
        assert any('skip_reason=non_real_final_tag' in m for m in logs)
    asyncio.run(run())


def test_welcome_changed_clan_releases_old_and_consumes_new(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CA", "C1CK", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        ctx = TicketContext(thread_id=1, ticket_number='W4', username='u', recruit_id=1, recruit_display='u')
        ctx.state = 'awaiting_clan'
        deltas = []
        logs = []
        monkeypatch.setattr('modules.onboarding.watcher_welcome.log_sheet_write', lambda **kwargs: asyncio.sleep(0, result='updated'))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', lambda _t: (2, ['W4','u','C1CA','','','','','','','','','']))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','X','','6']) if tag in {'C1CA','C1CK'} else None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit', lambda *a,**k: asyncio.sleep(0, result=[]))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=0))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.recompute_clan_availability', lambda *a,**k: asyncio.sleep(0, result=None))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
        monkeypatch.setattr('modules.onboarding.watcher_welcome.rt.send_log_message', lambda m: logs.append(m) or asyncio.sleep(0))
        await watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CK', actor=None, source='s', prompt_message=None, view=None)
        await bot.close()
        assert sorted(deltas) == [('C1CA', 1), ('C1CK', -1)]
        assert any('previous_final=C1CA' in m for m in logs)
    asyncio.run(run())


def test_welcome_reserved_placement_does_not_double_decrement(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CK", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        ctx = TicketContext(thread_id=1, ticket_number='W5', username='u', recruit_id=1, recruit_display='u')
        ctx.state = 'awaiting_clan'
        deltas = []
        reservation = SimpleNamespace(row_number=7, normalized_clan_tag='C1CK', clan_tag='C1CK')
        monkeypatch.setattr('modules.onboarding.watcher_welcome.log_sheet_write', lambda **kwargs: asyncio.sleep(0, result='updated'))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', lambda _t: (2, ['W0','u','','','','','1','','open','','','','pending','pending','pending','']))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','C1CK','','6']) if tag=='C1CK' else None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit', lambda *a,**k: asyncio.sleep(0, result=[reservation]))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status', lambda *a,**k: asyncio.sleep(0, result=True))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=0))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.recompute_clan_availability', lambda *a,**k: asyncio.sleep(0, result=None))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
        monkeypatch.setattr('modules.onboarding.watcher_welcome.rt.send_log_message', lambda _m: asyncio.sleep(0))
        await watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CK', actor=None, source='s', prompt_message=None, view=None)
        await bot.close()
        assert deltas == []
    asyncio.run(run())


def test_welcome_reads_previous_final_before_overwrite(monkeypatch):
    async def run():
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CA", "C1CK", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        ctx = TicketContext(thread_id=1, ticket_number='W6', username='u', recruit_id=1, recruit_display='u')
        ctx.state = 'awaiting_clan'
        state = {"clantag": "C1CA"}
        logs = []
        deltas = []
        def fake_find(_ticket):
            return (2, ['W6','u',state["clantag"],'','','','','','','','',''])
        def fake_log_sheet_write(**_kwargs):
            state["clantag"] = "C1CK"
            return asyncio.sleep(0, result='updated')
        monkeypatch.setattr('modules.onboarding.watcher_welcome.log_sheet_write', fake_log_sheet_write)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', fake_find)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','X','','6']) if tag in {'C1CA','C1CK'} else None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit', lambda *a,**k: asyncio.sleep(0, result=[]))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=0))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.availability.recompute_clan_availability', lambda *a,**k: asyncio.sleep(0, result=None))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
        monkeypatch.setattr('modules.onboarding.watcher_welcome.rt.send_log_message', lambda m: logs.append(m) or asyncio.sleep(0))
        await watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CK', actor=None, source='s', prompt_message=None, view=None)
        await bot.close()
        assert any('previous_final=C1CA' in m for m in logs)
        assert sorted(deltas) == [('C1CA', 1), ('C1CK', -1)]
    asyncio.run(run())


def test_repair_alert_mentions_not_performed():
    onboarding_sheets._queue_welcome_repair_alert({'repaired':0,'flagged':1,'legacy_rows':0,'welcome_rows':1,'reservation_rows':0,'malformed_rows':0})
    msg = onboarding_sheets.consume_welcome_repair_alert()
    assert 'open_spots_repair=not_performed' in (msg or '')


def test_promo_first_time_placement_decrements_once(monkeypatch, caplog):
    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', lambda *_: 'updated')
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row', lambda *_: (2, {"clantag": ""}))
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','C1CE','','6']) if tag == 'C1CE' else None)
    deltas = []
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=5))
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.recompute_clan_availability', lambda *a, **k: asyncio.sleep(0, result=None))
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R1',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CE'])
    caplog.set_level(logging.INFO)
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CE', actor=None, prompt_message=None, view=None))
    assert deltas == [('C1CE', -1)]
    assert 'decision_result=applied_open_delta' in caplog.text


def test_promo_repeat_finalize_does_not_double_decrement(monkeypatch):
    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', lambda *_: 'updated')
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row', lambda *_: (2, {"clantag": "C1CE"}))
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','C1CE','','5']) if tag == 'C1CE' else None)
    deltas = []
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=5))
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.recompute_clan_availability', lambda *a, **k: asyncio.sleep(0, result=None))
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R2',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CE'])
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CE', actor=None, prompt_message=None, view=None))
    assert deltas == []


def test_promo_changed_final_releases_old_and_consumes_new(monkeypatch):
    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', lambda *_: 'updated')
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row', lambda *_: (2, {"clantag": "C1CK"}))
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','X','','']) if tag in {'C1CK', 'C1CE'} else None)
    deltas = []
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=5))
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.recompute_clan_availability', lambda *a, **k: asyncio.sleep(0, result=None))
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R3',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CK', 'C1CE'])
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CE', actor=None, prompt_message=None, view=None))
    assert sorted(deltas) == [('C1CE', -1), ('C1CK', 1)]


def test_promo_reads_previous_final_before_overwrite(monkeypatch):
    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    state = {"clantag": "C1CA"}
    def fake_find(_ticket):
        return (2, {"clantag": state["clantag"]})
    def fake_upsert(*_args, **_kwargs):
        state["clantag"] = "C1CK"
        return "updated"
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row', fake_find)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', fake_upsert)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row', lambda tag: (10, ['','','X','','']) if tag in {'C1CA', 'C1CK'} else None)
    deltas = []
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=0))
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.recompute_clan_availability', lambda *a, **k: asyncio.sleep(0, result=None))
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R5',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CA', 'C1CK'])
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CK', actor=None, prompt_message=None, view=None))
    assert sorted(deltas) == [('C1CA', 1), ('C1CK', -1)]


def test_promo_blank_previous_not_rehydrated_from_post_write(monkeypatch, caplog):
    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    row_state = {"clantag": ""}
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row', lambda *_: (2, {"clantag": row_state["clantag"]}))
    def fake_upsert(*_args, **_kwargs):
        row_state["clantag"] = "C1CK"
        return 'updated'
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', fake_upsert)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row', lambda tag, *a, **k: (10, ['','','C1CK','','6']) if tag == 'C1CK' else None)
    deltas = []
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.adjust_manual_open_spots', lambda tag, delta: deltas.append((tag, delta)) or asyncio.sleep(0, result=5))
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.recompute_clan_availability', lambda *a, **k: asyncio.sleep(0, result=None))
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R6',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CK'])
    caplog.set_level(logging.INFO)
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CK', actor=None, prompt_message=None, view=None))
    assert deltas == [('C1CK', -1)]
    assert 'previous_final=NONE' in caplog.text


    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', lambda *_: 'updated')
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row', lambda *_: (2, {'clantag': '', 'source_clan_tag': '', 'finalization_status': 'pending'}))
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    adjust_calls = []
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.adjust_manual_open_spots', lambda *a, **k: adjust_calls.append((a, k)) or asyncio.sleep(0, result=0))
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.recompute_clan_availability', lambda *a, **k: asyncio.sleep(0, result=None))
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row', lambda *_, **__: (10, ['','','C1CK','','6']))
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R7',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CK'])
    caplog.set_level(logging.WARNING)
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CK', actor=None, prompt_message=None, view=None))
    assert ctx.state == 'closed'
    assert adjust_calls == [(('C1CK', -1), {})]
    assert 'reason_if_not_real=source_clan_none' in caplog.text


def test_promo_non_real_tag_logs_skip_reason(monkeypatch, caplog):
    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', lambda *_: 'updated')
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.find_promo_row', lambda *_: (2, {"clantag": ""}))
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.recruitment_sheets.find_clan_row', lambda *_: None)
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.adjust_manual_open_spots', lambda *a, **k: asyncio.sleep(0, result=0))
    monkeypatch.setattr('modules.onboarding.watcher_promo.availability.recompute_clan_availability', lambda *a, **k: asyncio.sleep(0, result=None))
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R4',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CZ'])
    caplog.set_level(logging.INFO)
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CZ', actor=None, prompt_message=None, view=None))
    assert 'skip_reason=non_real_final_tag' in caplog.text
    assert 'reason=no_promo_open_spots_reconcile_currently' not in caplog.text

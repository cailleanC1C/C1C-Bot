import asyncio
import logging
from types import SimpleNamespace

import discord
from discord.ext import commands

from modules.onboarding.watcher_welcome import WelcomeTicketWatcher, TicketContext, _NO_PLACEMENT_TAG
from modules.onboarding.watcher_promo import PromoTicketWatcher, PromoTicketContext
from shared.sheets import onboarding as onboarding_sheets


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
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', lambda _t: None)
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
        monkeypatch.setattr('modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row', lambda _t: None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row', lambda _tag: None)
        monkeypatch.setattr('modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit', lambda *a,**k: asyncio.sleep(0, result=[]))
        monkeypatch.setattr('modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map', lambda: {'open_spots':4})
        monkeypatch.setattr('modules.onboarding.watcher_welcome.rt.send_log_message', lambda m: logs.append(m) or asyncio.sleep(0))
        await watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CZ', actor=None, source='s', prompt_message=None, view=None)
        await bot.close()
        assert any('skip_reason=non_real_final_tag' in m for m in logs)
    asyncio.run(run())


def test_repair_alert_mentions_not_performed():
    onboarding_sheets._queue_welcome_repair_alert({'repaired':0,'flagged':1,'legacy_rows':0,'welcome_rows':1,'reservation_rows':0,'malformed_rows':0})
    msg = onboarding_sheets.consume_welcome_repair_alert()
    assert 'open_spots_repair=not_performed' in (msg or '')


def test_promo_logs_no_reconcile(monkeypatch, caplog):
    monkeypatch.setattr('modules.common.feature_flags.is_enabled', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_promo_channel_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.get_ticket_tool_bot_id', lambda: 1)
    monkeypatch.setattr('modules.onboarding.watcher_promo.thread_scopes.is_promo_parent', lambda *_: True)
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo', lambda *_: 'updated')
    monkeypatch.setattr('modules.onboarding.watcher_promo.onboarding_sessions.mark_completed', lambda *_: True)
    watcher = PromoTicketWatcher(bot=DummyBot())
    ctx = PromoTicketContext(thread_id=1,ticket_number='R1',username='u',promo_type='Returning',thread_created='x',year='2026',month='Jan',state='awaiting_clan')
    watcher._load_clan_tags = lambda : asyncio.sleep(0, result=['C1CE'])
    caplog.set_level(logging.INFO)
    asyncio.run(watcher._finalize_clan_tag(DummyThread(), ctx, 'C1CE', actor=None, prompt_message=None, view=None))
    assert 'reason=no_promo_open_spots_reconcile_currently' in caplog.text

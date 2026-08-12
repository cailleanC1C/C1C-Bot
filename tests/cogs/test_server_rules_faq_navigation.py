import asyncio
from unittest.mock import AsyncMock, Mock

from cogs.server_rules import ServerRulesCog
from modules.ops import server_rules as base
from modules.ops import server_rules_faq_navigation as faq_navigation
from modules.ops import server_rules_interactive as interactive


class Group:
    def __init__(self, key, message_id=""):
        self.key = key
        self.stored_message_id = message_id


def test_navigation_hook_adds_faq_panel_as_final_jump_target(monkeypatch):
    async def run():
        target = object()
        bot = type("Bot", (), {"user": type("User", (), {"id": 42})()})()
        summary = base.Summary()
        rule_nav = Group("rules_navigation", "111111111111111111")
        rule_one = Group("rule_1", "222222222222222222")
        faq_nav = Group("faq_navigation", "333333333333333333")
        nav_message = type("Message", (), {"edit": AsyncMock()})()
        payload = [object()]

        monkeypatch.setattr(
            base,
            "preflight",
            AsyncMock(return_value=(target, "Rules", {}, [], None)),
        )
        monkeypatch.setattr(base, "build_groups", Mock(return_value=([], [])))
        monkeypatch.setattr(
            interactive,
            "_navigation",
            Mock(return_value=(rule_nav, [rule_one], faq_nav, [])),
        )
        monkeypatch.setattr(
            interactive,
            "_fetch_group",
            AsyncMock(return_value=(nav_message, "")),
        )
        nav_payload = Mock(return_value=payload)
        monkeypatch.setattr(interactive, "_nav_payload", nav_payload)

        await faq_navigation.ensure_faq_jump_link(bot, target, summary)

        nav_payload.assert_called_once_with(
            target,
            rule_nav,
            [rule_one, faq_nav],
        )
        nav_message.edit.assert_awaited_once_with(content=None, embeds=payload)
        assert summary.failed == 0

    asyncio.run(run())


def test_serverrules_interactive_command_runs_faq_navigation_hook(monkeypatch):
    async def run():
        target = type("Target", (), {"id": 9, "guild": None})()
        bot = type("Bot", (), {"user": type("User", (), {"id": 42})()})()
        summary = base.Summary(refreshed=1)
        ctx = type(
            "Ctx",
            (),
            {
                "reply": AsyncMock(),
                "author": type("Author", (), {"id": 5})(),
                "guild": None,
            },
        )()

        monkeypatch.setattr("cogs.server_rules.feature_flags.is_enabled", lambda _key: True)
        monkeypatch.setattr("cogs.server_rules.runtime_helpers.send_log_message", AsyncMock())
        monkeypatch.setattr(interactive, "refresh", AsyncMock(return_value=(summary, target)))
        hook = AsyncMock()
        monkeypatch.setattr(faq_navigation, "ensure_faq_jump_link", hook)

        cog = ServerRulesCog(bot, operations=interactive)
        await cog.refresh.callback(cog, ctx)

        hook.assert_awaited_once_with(bot, target, summary)

    asyncio.run(run())

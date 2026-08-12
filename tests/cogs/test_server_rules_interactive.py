import asyncio
from unittest.mock import AsyncMock

import discord

from modules.ops import server_rules as base
from modules.ops import server_rules_interactive as interactive


def row(number, key, section, order, *, title="Title", description="Answer", topic_key="", topic_title="", message_id=""):
    item = base.Row(
        number,
        [],
        {
            "message_key": key,
            "section": section,
            "order": str(order),
            "enabled": "TRUE",
            "title": title,
            "description": description,
            "colour": "#607d8b" if section == "faq" else "#4472c4",
            "thumbnail_url": "",
            "footer": "",
            "message_id": message_id,
            "topic_key": topic_key,
            "topic_title": topic_title,
        },
        True,
    )
    item.order = float(order)
    item.topic_key = topic_key
    item.topic_title = topic_title
    item.embed, errors = base.build_embed(item)
    assert not errors
    return item


def group(item):
    payload = [discord.Embed.from_dict(item.embed.to_dict())]
    return base.MessageGroup(item.key, [item], [item.embed], item.message_id, payload)


def ui():
    return interactive.UI(
        group_select_placeholder="Choose a FAQ group",
        question_list_heading="Available questions",
        question_list_instruction="Choose Show all or one question.",
        question_select_placeholder="Show all or choose a question",
        show_all_label="Show all",
        show_all_description="View every answer in this FAQ group",
        share_answer_label="Share answer",
        share_group_label="Share FAQ group",
        shared_footer="Shared from the C1C Community FAQ",
        unavailable_text="FAQ unavailable",
        share_channel_placeholder="Choose where to share",
        share_success_text="Shared to {channel}.",
        share_permission_text="No permission",
        share_failure_text="Share failed",
    )


def test_catalog_and_first_step_list_questions_without_answers():
    rows = [
        row(2, "q1", "faq", 100, title="Who may join?", description="Answer one", topic_key="membership", topic_title="Membership"),
        row(3, "q2", "faq", 101, title="Is Discord required?", description="Answer two", topic_key="membership", topic_title="Membership"),
    ]
    topics, errors = interactive._catalog(rows)
    assert errors == []
    assert [topic.key for topic in topics] == ["membership"]
    embed = interactive._question_list(topics[0], ui())
    assert "Who may join?" in embed.description
    assert "Is Discord required?" in embed.description
    assert "Answer one" not in embed.description
    assert "Answer two" not in embed.description


def test_second_step_is_optional_show_all_or_specific_question():
    topic = interactive.Topic(
        "membership",
        "Membership",
        (
            row(2, "q1", "faq", 100, title="Question one", topic_key="membership", topic_title="Membership"),
            row(3, "q2", "faq", 101, title="Question two", topic_key="membership", topic_title="Membership"),
        ),
    )
    select = interactive.FAQQuestionSelect(object(), topic, ui())
    assert [option.value for option in select.options] == [interactive.FAQ_SHOW_ALL, "q1", "q2"]
    assert select.options[0].label == "Show all"


def test_permanent_builder_keeps_navigation_but_filters_faq_answers():
    rule = group(row(2, "rule_1", "rules", 40))
    faq_panel = group(row(3, "faq_navigation", "faq_navigation", 95))
    faq_answer = group(row(4, "faq_q", "faq", 100, topic_key="topic", topic_title="Topic"))

    def original(_rows):
        return [rule, faq_panel, faq_answer], []

    groups, errors = interactive._filter_builder(original)([])
    assert errors == []
    assert groups == [rule, faq_panel]


def test_rule_navigation_builds_jump_links_for_six_rule_messages():
    target = type("Target", (), {"id": 1415377357761413241, "guild": type("Guild", (), {"id": 123456789012345678})()})()
    nav = group(row(2, "rules_navigation", "navigation", 35, title="C1C Community Rules", description="Jump straight to a rule."))
    rules = []
    for index in range(1, 7):
        rules.append(group(row(2 + index, f"rule_{index}", "rules", 40 + index, title=f"Rule {index}", message_id=str(1536798493736312920 + index))))
    payload = interactive._nav_payload(target, nav, rules)
    description = payload[0].description
    for index, item in enumerate(rules, 1):
        assert f"[Rule {index}]" in description
        assert f"/{item.stored_message_id}" in description


def test_share_button_opens_channel_picker_not_current_channel():
    async def run():
        view = interactive.FAQShareView(object(), "topic", "q1", ui())
        button = view.children[0]
        current = AsyncMock()
        interaction = type(
            "Interaction",
            (),
            {
                "response": type("Response", (), {"edit_message": AsyncMock()})(),
                "channel": type("Channel", (), {"send": current})(),
            },
        )()
        await button.callback(interaction)
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], interactive.FAQShareChannelView)
        assert kwargs["view"].children[0].custom_id == interactive.FAQ_SHARE_CHANNEL_CUSTOM_ID
        current.assert_not_awaited()

    asyncio.run(run())


def test_channel_picker_only_offers_message_destinations():
    picker = interactive.FAQShareChannelSelect(object(), "topic", None, ui())
    assert discord.ChannelType.text in picker.channel_types
    assert discord.ChannelType.public_thread in picker.channel_types
    assert discord.ChannelType.private_thread in picker.channel_types
    assert discord.ChannelType.voice not in picker.channel_types
    assert discord.ChannelType.forum not in picker.channel_types


def test_share_permission_checks_require_member_and_bot_post_access():
    user_permissions = type("Permissions", (), {"view_channel": True, "send_messages": True, "send_messages_in_threads": False})()
    selected = type(
        "Selected",
        (),
        {
            "type": discord.ChannelType.text,
            "guild_id": 1,
            "permissions": user_permissions,
            "archived": False,
            "locked": False,
        },
    )()
    interaction = type("Interaction", (), {"guild_id": 1})()
    assert interactive._selected_ok(selected, interaction)
    user_permissions.send_messages = False
    assert not interactive._selected_ok(selected, interaction)

    bot_permissions = type("Permissions", (), {"view_channel": True, "send_messages": True, "send_messages_in_threads": False, "embed_links": True})()
    channel = type("Channel", (), {"type": discord.ChannelType.text, "permissions_for": lambda self, member: bot_permissions})()
    bot_interaction = type("Interaction", (), {"guild": type("Guild", (), {"me": object()})()})()
    assert interactive._bot_ok(channel, bot_interaction)
    bot_permissions.embed_links = False
    assert not interactive._bot_ok(channel, bot_interaction)


def test_shared_footer_preserves_existing_sheet_footer():
    item = row(2, "q1", "faq", 100, topic_key="topic", topic_title="Topic")
    item.embed.set_footer(text="C1C Community FAQ • Topic")
    batches = interactive._shared_batches((item,), ui())
    footer = batches[0][-1].footer.text
    assert "C1C Community FAQ • Topic" in footer
    assert "Shared from the C1C Community FAQ" in footer


def test_multi_message_share_rolls_back_partial_send():
    async def run():
        deleted = []

        class Sent:
            async def delete(self):
                deleted.append(True)

        class Target:
            def __init__(self):
                self.calls = 0

            async def send(self, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise discord.HTTPException(response=None, message="boom")
                return Sent()

        ok = await interactive._send_batches(Target(), [[discord.Embed(description="one")], [discord.Embed(description="two")]])
        assert not ok
        assert deleted == [True]

    asyncio.run(run())


def test_faq_cache_reuses_state_and_invalidation_forces_reload(monkeypatch):
    async def run():
        interactive.invalidate_cache()
        calls = 0
        topic = interactive.Topic("topic", "Topic", (row(2, "q1", "faq", 100, topic_key="topic", topic_title="Topic"),))

        async def load():
            nonlocal calls
            calls += 1
            return [topic], ui(), [], 300

        monkeypatch.setattr(interactive, "_load_state_uncached", load)
        first = await interactive.load_state()
        second = await interactive.load_state()
        assert first[0][0].key == second[0][0].key == "topic"
        assert calls == 1
        interactive.invalidate_cache()
        await interactive.load_state()
        assert calls == 2

    asyncio.run(run())


def test_persistent_group_view_registers_once():
    bot = type("Bot", (), {})()
    calls = []
    bot.add_view = calls.append
    interactive.register_persistent_view(bot)
    interactive.register_persistent_view(bot)
    assert len(calls) == 1
    assert isinstance(calls[0], interactive.FAQGroupView)
    assert calls[0].timeout is None

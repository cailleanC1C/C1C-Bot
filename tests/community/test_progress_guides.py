from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modules.community.progress_guides import service
from modules.community.progress_guides.cog import ProgressGuidesCog


class FakeDiscordNotFound(Exception):
    pass


@pytest.fixture(autouse=True)
def clear_progress_guide_cache():
    service.clear_progress_guide_cache()
    yield
    service.clear_progress_guide_cache()


class FakeMessage:
    def __init__(self, message_id=99, *, delete_error=None):
        self.id = message_id
        self.edits = []
        self.delete_error = delete_error
        self.delete_attempted = False
        self.deleted = False
        self.jump_url = f"https://discord.com/channels/1/2/{message_id}"

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.delete_attempted = True
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True


class FakeChannel:
    def __init__(
        self, *, existing=None, missing=False, fetch_error=None, send_message=None
    ):
        self.existing = existing
        self.missing = missing
        self.fetch_error = fetch_error
        self.send_message = send_message
        self.sent = []
        self.sent_messages = []

    async def fetch_message(self, message_id):
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.missing or self.existing is None:
            raise FakeDiscordNotFound("missing")
        return self.existing

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = self.send_message or FakeMessage(12345)
        self.sent_messages.append(message)
        return message


class FakeWorksheet:
    def __init__(self):
        self.updates = []
        self.appended = []

    def update(self, cell, values, **kwargs):
        self.updates.append((cell, values, kwargs))

    def append_row(self, row, **kwargs):
        self.appended.append((row, kwargs))


class FakeBot:
    def __init__(self):
        self.views = []

    def get_channel(self, channel_id):
        return None

    def add_view(self, view):
        self.views.append(view)


class FakeInteractionResponse:
    def __init__(self):
        self.deferred = []
        self.sent = []
        self.modals = []
        self.edits = []

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeUser:
    id = 123456


class FakeInteraction:
    def __init__(self):
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.user = FakeUser()


async def _run(monkeypatch, data, channels, worksheet, *, refresh=True):
    async def load_data():
        return data

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service.discord, "NotFound", FakeDiscordNotFound)

    async def get_ws(sheet, tab):
        return worksheet

    async def fetch_values(sheet, tab):
        return [
            [
                "category",
                "guide_panel_message_id",
                "guide_post_url",
                "help_panel_message_id",
                "help_post_url",
                "help_panel_title",
                "help_panel_description",
                "help_panel_footer",
                "help_back_button_label",
            ]
        ]

    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(service, "afetch_values", fetch_values)

    async def resolve(_bot, channel_id):
        return channels.get(channel_id)

    async def call(func, *args, **kwargs):
        kwargs.pop("attempts", None)
        return func(*args, **kwargs)

    monkeypatch.setattr(service, "_resolve_messageable", resolve)
    monkeypatch.setattr(service, "acall_with_backoff", call)
    return await service.publish_or_refresh(FakeBot(), refresh=refresh)


def _data(*, post_overrides=None, guides=None, faq=None):
    row = {
        "category": "ARB",
        "label": "Arena Rush Basics",
        "guide_title": "🏟️ Arena Rush Basics",
        "faq_title": "🏟️ Arena Rush Questions",
        "faq_description": "Sheet-authored FAQ intro.",
        "faq_button_label": "Read FAQ",
        "help_button_label": "Ask the Helpers",
        "guide_channel_id": "10",
        "guide_thread_id": "",
        "guide_panel_message_id": "",
        "help_post_url": "https://discord.com/channels/1/2/3",
        "guide_asset_key": "hero",
        "guide_post_url": "",
        "help_channel_id": "",
        "help_thread_id": "",
        "help_panel_message_id": "",
        "help_panel_title": "Help for Arbiter",
        "help_panel_description": "Use these sheet-authored buttons for help.",
        "help_panel_footer": "Sheet footer.",
        "help_back_button_label": "Back to Guide",
        "how_to_use_button_label": "",
        "how_to_use_title": "",
        "how_to_use_description": "",
        "guide_footer": "",
        "progress_tracking_enabled": "TRUE",
        "leaderboard_enabled": "FALSE",
        "notes": "",
        "updated_at_utc": "",
        "mission_list_button_label": "",
        "mission_list_title": "",
        "my_progress_button_label": "",
        "my_progress_title": "Arbiter Progress",
        "my_progress_body_template": "Current mission: {current_step_index} / {total_steps}\n\nMission: {mission_description}\n\nProgress: {percent_complete}% complete\n{remaining_steps} missions remaining\nStatus: {status}",
        "my_progress_empty_description": "No progress saved yet.",
        "my_progress_set_button_label": "Set Progress",
        "my_progress_modal_title": "Set Arbiter Progress",
        "my_progress_modal_step_label": "Current mission number",
        "my_progress_saved_description": "Progress saved.",
        "my_progress_invalid_step_description": "Sheet says enter a valid mission number.",
        "my_progress_missing_step_description": "Sheet says that mission is not available.",
        "my_progress_unavailable_description": "Sheet says progress is temporarily unavailable.",
        "questions_enabled": "TRUE",
        "sort_order": "1",
        "enabled": "TRUE",
    }
    row.update(post_overrides or {})
    post = service.ForumPost.from_row(2, row)
    return service.ProgressGuideData(
        posts=[post],
        guides_by_category={
            "ARB": (
                guides
                if guides is not None
                else [
                    {
                        "category": "ARB",
                        "title": "Overview",
                        "body": "Use stamina wisely.",
                        "sort_order": "1",
                        "enabled": "TRUE",
                    }
                ]
            )
        },
        faq_by_category={
            "ARB": (
                faq
                if faq is not None
                else [
                    {
                        "category": "ARB",
                        "question": "What first?",
                        "answer": "Start with the overview.",
                        "sort_order": "1",
                        "enabled": "TRUE",
                    }
                ]
            )
        },
        assets_by_category_key={
            ("ARB", "hero"): {
                "asset_url": "https://example.com/image.png",
                "asset_type": "image",
                "enabled": "TRUE",
            }
        },
        forum_posts_tab="ProgressForumPosts",
    )


def _button_by_label(view, label):
    return next(item for item in view.children if getattr(item, "label", "") == label)


def test_publish_posts_only_guide_panels_and_writes_message_id(monkeypatch):
    worksheet = FakeWorksheet()
    guide = FakeChannel()
    help_channel = FakeChannel()
    summary = asyncio.run(
        _run(monkeypatch, _data(), {10: guide, 20: help_channel}, worksheet)
    )
    assert summary.created == 1
    assert len(guide.sent) == 1
    assert help_channel.sent == []
    assert worksheet.updates == [
        ("B2", [["12345"]], {"value_input_option": "RAW"}),
        (
            "C2",
            [["https://discord.com/channels/1/2/12345"]],
            {"value_input_option": "RAW"},
        ),
    ]


def test_refresh_creates_managed_help_post_and_updates_guide_link(monkeypatch):
    worksheet = FakeWorksheet()
    guide = FakeChannel()
    help_channel = FakeChannel(send_message=FakeMessage(555))
    data = _data(
        post_overrides={
            "help_channel_id": "20",
            "help_post_url": "",
            "mission_list_button_label": "Mission List",
            "mission_list_title": "Missions",
        }
    )
    summary = asyncio.run(
        _run(monkeypatch, data, {10: guide, 20: help_channel}, worksheet)
    )
    assert summary.created == 2
    assert (
        help_channel.sent[0]["content"] is None
        if "content" in help_channel.sent[0]
        else True
    )
    help_embed = help_channel.sent[0]["embed"]
    assert help_embed.title == "Help for Arbiter"
    assert help_embed.description == "Use these sheet-authored buttons for help."
    labels = [
        getattr(item, "label", "") for item in help_channel.sent[0]["view"].children
    ]
    assert labels == ["Read FAQ", "Mission List", "Back to Guide"]
    final_guide_view = guide.sent_messages[0].edits[-1]["view"]
    guide_labels = [getattr(item, "label", "") for item in final_guide_view.children]
    assert "Ask the Helpers" in guide_labels
    ask = _button_by_label(final_guide_view, "Ask the Helpers")
    assert ask.url == "https://discord.com/channels/1/2/555"
    assert ("D2", [["555"]], {"value_input_option": "RAW"}) in worksheet.updates
    assert (
        "E2",
        [["https://discord.com/channels/1/2/555"]],
        {"value_input_option": "RAW"},
    ) in worksheet.updates


def test_existing_guide_panel_with_stale_guide_post_url_updates_sheet(monkeypatch):
    worksheet = FakeWorksheet()
    message = FakeMessage(777)
    guide = FakeChannel(existing=message)
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "guide_panel_message_id": "777",
                    "guide_post_url": "https://discord.com/channels/1/2/old",
                }
            ),
            {10: guide},
            worksheet,
        )
    )
    assert summary.refreshed == 1
    assert (
        "C2",
        [["https://discord.com/channels/1/2/777"]],
        {"value_input_option": "RAW"},
    ) in worksheet.updates


def test_recreated_guide_panel_updates_help_back_button_target(monkeypatch):
    worksheet = FakeWorksheet()
    guide = FakeChannel(missing=True, send_message=FakeMessage(444))
    help_channel = FakeChannel(send_message=FakeMessage(555))
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "guide_panel_message_id": "777",
                    "guide_post_url": "https://discord.com/channels/1/2/deleted",
                    "help_channel_id": "20",
                    "help_post_url": "",
                }
            ),
            {10: guide, 20: help_channel},
            worksheet,
        )
    )
    assert summary.created == 2
    assert ("B2", [["444"]], {"value_input_option": "RAW"}) in worksheet.updates
    assert (
        "C2",
        [["https://discord.com/channels/1/2/444"]],
        {"value_input_option": "RAW"},
    ) in worksheet.updates
    assert (
        _button_by_label(help_channel.sent[0]["view"], "Back to Guide").url
        == "https://discord.com/channels/1/2/444"
    )


def test_refresh_edits_existing_help_post(monkeypatch):
    worksheet = FakeWorksheet()
    message = FakeMessage(888)
    help_channel = FakeChannel(existing=message)
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "help_channel_id": "20",
                    "help_panel_message_id": "888",
                    "help_post_url": "https://discord.com/channels/1/2/888",
                }
            ),
            {10: FakeChannel(), 20: help_channel},
            worksheet,
        )
    )
    assert summary.refreshed == 1
    assert message.edits
    assert help_channel.sent == []
    assert not any(update[0] in {"D2", "E2"} for update in worksheet.updates)


def test_refresh_recreates_deleted_help_post(monkeypatch):
    worksheet = FakeWorksheet()
    help_channel = FakeChannel(missing=True, send_message=FakeMessage(999))
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={"help_channel_id": "20", "help_panel_message_id": "888"}
            ),
            {10: FakeChannel(), 20: help_channel},
            worksheet,
        )
    )
    assert summary.created == 2
    assert ("D2", [["999"]], {"value_input_option": "RAW"}) in worksheet.updates
    assert (
        "E2",
        [["https://discord.com/channels/1/2/999"]],
        {"value_input_option": "RAW"},
    ) in worksheet.updates


def test_help_post_back_button_uses_guide_post_url(monkeypatch):
    worksheet = FakeWorksheet()
    help_channel = FakeChannel()
    asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "help_channel_id": "20",
                    "guide_post_url": "https://discord.com/channels/1/2/333",
                }
            ),
            {10: FakeChannel(), 20: help_channel},
            worksheet,
        )
    )
    labels = [
        getattr(item, "label", "") for item in help_channel.sent[0]["view"].children
    ]
    assert "Back to Guide" in labels
    assert (
        _button_by_label(help_channel.sent[0]["view"], "Back to Guide").url
        == "https://discord.com/channels/1/2/12345"
    )


def test_help_view_omits_back_button_without_guide_post_url():
    data = _data(post_overrides={"guide_post_url": ""})
    view = service.build_help_view(data.posts[0], data)
    labels = [getattr(item, "label", "") for item in view.children]
    assert "Back to Guide" not in labels


def test_progress_forum_posts_help_columns_are_appended_when_missing(monkeypatch):
    worksheet = FakeWorksheet()
    data = _data(post_overrides={"help_channel_id": "20", "help_post_url": ""})

    async def load_data():
        return data

    async def get_ws(sheet, tab):
        return worksheet

    async def fetch_values(sheet, tab):
        return [
            [
                "category",
                "guide_panel_message_id",
                "guide_post_url",
                "help_panel_message_id",
                "help_post_url",
            ]
        ]

    async def resolve(_bot, channel_id):
        return {10: FakeChannel(), 20: FakeChannel()}.get(channel_id)

    async def call(func, *args, **kwargs):
        kwargs.pop("attempts", None)
        return func(*args, **kwargs)

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service.discord, "NotFound", FakeDiscordNotFound)
    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(service, "afetch_values", fetch_values)
    monkeypatch.setattr(service, "_resolve_messageable", resolve)
    monkeypatch.setattr(service, "acall_with_backoff", call)
    asyncio.run(service.publish_or_refresh(FakeBot(), refresh=True))
    assert worksheet.updates[0] == (
        "A1:I1",
        [
            [
                "category",
                "guide_panel_message_id",
                "guide_post_url",
                "help_panel_message_id",
                "help_post_url",
                "help_panel_title",
                "help_panel_description",
                "help_panel_footer",
                "help_back_button_label",
            ]
        ],
        {"value_input_option": "RAW"},
    )


def test_publish_primes_progress_guide_cache(monkeypatch):
    worksheet = FakeWorksheet()
    data = _data()
    summary = asyncio.run(_run(monkeypatch, data, {10: FakeChannel()}, worksheet))
    assert summary.created == 1
    assert service.get_cached_progress_guide_data() is data


def test_refresh_edits_existing_guide_panel(monkeypatch):
    worksheet = FakeWorksheet()
    message = FakeMessage(777)
    guide = FakeChannel(existing=message)
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "guide_panel_message_id": "777",
                    "guide_post_url": "https://discord.com/channels/1/2/777",
                }
            ),
            {10: guide},
            worksheet,
        )
    )
    assert summary.refreshed == 1
    assert message.edits
    assert guide.sent == []
    assert worksheet.updates == []


def test_missing_deleted_stored_message_recreates_and_updates_id(monkeypatch):
    worksheet = FakeWorksheet()
    guide = FakeChannel(missing=True)
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "guide_panel_message_id": "777",
                    "guide_post_url": "https://discord.com/channels/1/2/777",
                }
            ),
            {10: guide},
            worksheet,
        )
    )
    assert summary.created == 1
    assert len(guide.sent) == 1
    assert worksheet.updates[0][0] == "B2"


def test_generic_fetch_error_does_not_recreate(monkeypatch):
    worksheet = FakeWorksheet()
    guide = FakeChannel(fetch_error=RuntimeError("transient"))
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "guide_panel_message_id": "777",
                    "guide_post_url": "https://discord.com/channels/1/2/777",
                }
            ),
            {10: guide},
            worksheet,
        )
    )
    assert summary.created == 0
    assert guide.sent == []
    assert worksheet.updates == []
    assert "RuntimeError" in summary.failures[0]


def test_help_panel_message_id_is_never_written(monkeypatch):
    worksheet = FakeWorksheet()
    asyncio.run(_run(monkeypatch, _data(), {10: FakeChannel()}, worksheet))
    assert [cell for cell, _values, _kw in worksheet.updates] == ["B2", "C2"]


def test_disabled_rows_are_skipped(monkeypatch):
    worksheet = FakeWorksheet()
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(post_overrides={"enabled": "FALSE"}),
            {10: FakeChannel()},
            worksheet,
        )
    )
    assert summary.created == 0
    assert summary.skipped == ["Arena Rush Basics: disabled"]
    assert worksheet.updates == []


def test_missing_guide_destination_is_reported_not_fatal(monkeypatch):
    worksheet = FakeWorksheet()
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(post_overrides={"guide_channel_id": "", "guide_thread_id": ""}),
            {},
            worksheet,
        )
    )
    assert summary.created == 0
    assert any("missing guide destination" in item for item in summary.skipped)


def test_source_urls_are_not_rendered_in_embed():
    data = _data(
        guides=[
            {
                "category": "ARB",
                "title": "Overview",
                "body": "Read https://secret.example/source but do not show asset URLs.",
                "sort_order": "1",
                "enabled": "TRUE",
            }
        ]
    )
    embed = service.build_guide_embed(data.posts[0], data)
    rendered = str(embed.to_dict())
    assert (
        "https://example.com/image.png" in rendered
    )  # allowed only as Discord image metadata
    assert "secret.example" not in rendered
    assert "asset_url" not in rendered


def test_url_only_guide_fields_are_skipped():
    data = _data(
        guides=[
            {
                "category": "ARB",
                "title": "Source only",
                "body": "https://secret.example/source",
                "sort_order": "1",
                "enabled": "TRUE",
            }
        ]
    )
    embed = service.build_guide_embed(data.posts[0], data)
    assert len(embed.fields) == 0


def test_guide_title_comes_from_guide_title_column():
    data = _data()
    embed = service.build_guide_embed(data.posts[0], data)
    assert embed.title == "🏟️ Arena Rush Basics"


def test_guide_title_falls_back_to_label_then_category():
    label_data = _data(post_overrides={"guide_title": ""})
    label_embed = service.build_guide_embed(label_data.posts[0], label_data)
    assert label_embed.title == "Arena Rush Basics"

    category_data = _data(post_overrides={"guide_title": "", "label": ""})
    category_embed = service.build_guide_embed(category_data.posts[0], category_data)
    assert category_embed.title == "ARB"


def test_faq_embed_uses_sheet_title_and_description():
    data = _data()
    embed = service.build_faq_embed("ARB", data)
    assert embed.title == "🏟️ Arena Rush Questions"
    assert embed.description == "Sheet-authored FAQ intro."


def test_faq_title_falls_back_through_public_display_values():
    guide_title_data = _data(post_overrides={"faq_title": ""})
    assert (
        service.build_faq_embed("ARB", guide_title_data).title
        == "🏟️ Arena Rush Basics FAQ"
    )

    label_data = _data(post_overrides={"faq_title": "", "guide_title": ""})
    assert service.build_faq_embed("ARB", label_data).title == "Arena Rush Basics FAQ"

    category_data = _data(
        post_overrides={"faq_title": "", "guide_title": "", "label": ""}
    )
    assert service.build_faq_embed("ARB", category_data).title == "ARB FAQ"


def test_blank_faq_description_is_omitted():
    data = _data(post_overrides={"faq_description": ""})
    embed = service.build_faq_embed("ARB", data)
    assert embed.description is None


def test_button_labels_and_order_come_from_forum_post_columns():
    data = _data()
    view = service.build_guide_view(data.posts[0], data)
    assert [getattr(item, "label", "") for item in view.children] == [
        "Read FAQ",
        "Ask the Helpers",
    ]


def test_button_labels_fallback_and_help_remains_last():
    data = _data(post_overrides={"faq_button_label": "", "help_button_label": ""})
    view = service.build_guide_view(data.posts[0], data)
    assert [getattr(item, "label", "") for item in view.children] == [
        "FAQ",
        "Ask in Help",
    ]


def test_how_to_use_button_appears_after_ask_and_sends_sheet_embed():
    data = _data(
        post_overrides={
            "how_to_use_button_label": "❔ How to use",
            "how_to_use_title": "How to use this Interactive Guide",
            "how_to_use_description": "Mission List shows the full mission chain.",
        }
    )
    service.set_progress_guide_cache(data)
    view = service.build_guide_view(data.posts[0], data)
    assert [getattr(item, "label", "") for item in view.children] == [
        "Read FAQ",
        "Ask the Helpers",
        "❔ How to use",
    ]
    button = _button_by_label(view, "❔ How to use")
    assert button.custom_id == "progressguides:howto:ARB"

    interaction = FakeInteraction()
    asyncio.run(button.callback(interaction))

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert sent["embed"].title == "How to use this Interactive Guide"
    assert sent["embed"].description == "Mission List shows the full mission chain."


def test_how_to_use_button_is_omitted_without_label_or_description():
    for overrides in (
        {
            "how_to_use_button_label": "",
            "how_to_use_description": "Mission List shows the full mission chain.",
        },
        {"how_to_use_button_label": "How to use", "how_to_use_description": ""},
    ):
        data = _data(post_overrides=overrides)
        view = service.build_guide_view(data.posts[0], data)
        assert "progressguides:howto:ARB" not in [
            getattr(item, "custom_id", "") for item in view.children
        ]


def test_how_to_use_button_is_not_added_to_managed_help_posts():
    data = _data(
        post_overrides={
            "guide_post_url": "https://discord.com/channels/1/2/333",
            "how_to_use_button_label": "How to use",
            "how_to_use_title": "How to use this Interactive Guide",
            "how_to_use_description": "Mission List shows the full mission chain.",
        }
    )
    view = service.build_help_view(data.posts[0], data)
    assert [getattr(item, "label", "") for item in view.children] == [
        "Read FAQ",
        "Back to Guide",
    ]


def test_main_guide_embed_uses_sheet_footer_when_populated():
    data = _data(post_overrides={"guide_footer": "New here? Use How to use."})
    embed = service.build_guide_embed(data.posts[0], data)
    assert embed.footer.text == "New here? Use How to use."


def test_sheet_driven_visible_values_are_limited_for_discord():
    data = _data(
        post_overrides={
            "guide_title": "G" * 300,
            "faq_title": "F" * 300,
            "faq_description": "D" * 5000,
            "faq_button_label": "Q" * 100,
            "help_button_label": "H" * 100,
        }
    )

    guide_embed = service.build_guide_embed(data.posts[0], data)
    faq_embed = service.build_faq_embed("ARB", data)
    view = service.build_guide_view(data.posts[0], data)

    assert guide_embed.title == "G" * 256
    assert faq_embed.title == "F" * 256
    assert faq_embed.description == "D" * 4096
    assert [getattr(item, "label", "") for item in view.children] == [
        "Q" * 80,
        "H" * 80,
    ]


def test_progress_guides_title_renders_exactly_as_sheet_value():
    exact_title = "✨ Top 1 Tip - Sheet Value"
    data = _data(
        guides=[
            {
                "category": "ARB",
                "title": exact_title,
                "body": "Use the title exactly.",
                "sort_order": "1",
                "enabled": "TRUE",
            }
        ]
    )
    embed = service.build_guide_embed(data.posts[0], data)
    assert embed.fields[0].name == exact_title


def test_progress_guides_optional_display_columns_are_not_required():
    data = _data(
        guides=[
            {
                "category": "ARB",
                "title": "Sheet Title",
                "body": "No display_style, button_label, or image_asset_key needed.",
                "sort_order": "1",
                "enabled": "TRUE",
            }
        ]
    )
    embed = service.build_guide_embed(data.posts[0], data)
    assert embed.fields[0].name == "Sheet Title"


def test_faq_button_opens_faq_content_from_progress_faq(monkeypatch):
    data = _data(
        faq=[
            {
                "category": "ARB",
                "question": "Second?",
                "answer": "After first.",
                "sort_order": "2",
                "enabled": "TRUE",
            },
            {
                "category": "ARB",
                "question": "First?",
                "answer": "Start here. https://source.example/hidden",
                "sort_order": "1",
                "enabled": "TRUE",
            },
        ]
    )

    async def load_data():
        return data

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    button = service.ProgressGuideFAQButton("ARB")
    interaction = FakeInteraction()

    asyncio.run(button.callback(interaction))

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    embed = sent["embed"]
    assert embed.title == "🏟️ Arena Rush Questions"
    assert embed.description == "Sheet-authored FAQ intro."
    assert embed.fields == []
    view = sent["view"]
    select = next(
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQSelect)
    )
    assert [option.label for option in select.options] == ["First?", "Second?"]
    assert [option.value for option in select.options] == ["idx:0", "idx:1"]


def test_faq_picker_uses_faq_key_values_and_selects_single_answer(monkeypatch):
    data = _data(
        faq=[
            {
                "category": "ARB",
                "faq_key": "second_key",
                "question": "Second?",
                "answer": "Second answer.",
                "sort_order": "2",
                "enabled": "TRUE",
                "tags": "internal",
            },
            {
                "category": "ARB",
                "faq_key": "first_key",
                "question": "📌 First?",
                "answer": "First answer. https://source.example/hidden",
                "sort_order": "1",
                "enabled": "TRUE",
            },
            {
                "category": "ARB",
                "faq_key": "disabled_key",
                "question": "Disabled?",
                "answer": "Hidden.",
                "sort_order": "0",
                "enabled": "FALSE",
            },
        ]
    )
    view = service.ProgressGuideFAQPickerView(
        "ARB", data, service._faq_rows_for_category("ARB", data)
    )
    select = next(
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQSelect)
    )
    assert [option.label for option in select.options] == ["📌 First?", "Second?"]
    assert [option.value for option in select.options] == ["first_key", "second_key"]
    monkeypatch.setattr(
        service.ProgressGuideFAQSelect, "values", property(lambda _self: ["first_key"])
    )
    interaction = FakeInteraction()

    asyncio.run(select.callback(interaction))

    edit = interaction.response.edits[0]
    assert edit["embed"].title == "🏟️ Arena Rush Questions"
    assert "**📌 First?**" in edit["embed"].description
    assert "First answer." in edit["embed"].description
    assert "source.example" not in edit["embed"].description
    assert "first_key" not in edit["embed"].description
    assert "internal" not in edit["embed"].description
    assert any(
        isinstance(item, service.ProgressGuideFAQSelect)
        for item in edit["view"].children
    )


def test_faq_picker_paginates_and_page_change_resets_to_intro():
    faq = [
        {
            "category": "ARB",
            "faq_key": f"q{i:02d}",
            "question": f"Question {i:02d}?",
            "answer": f"Answer {i:02d}.",
            "sort_order": str(i),
            "enabled": "TRUE",
        }
        for i in range(1, 27)
    ]
    data = _data(faq=faq)
    rows = service._faq_rows_for_category("ARB", data)
    view = service.ProgressGuideFAQPickerView("ARB", data, rows)
    embed = service.build_faq_picker_embed("ARB", data, rows, page=0)

    assert "Page 1 / 2" in embed.description
    buttons = [
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQPageButton)
    ]
    assert [(button.label, button.disabled) for button in buttons] == [
        ("Previous", True),
        ("Next", False),
    ]
    select = next(
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQSelect)
    )
    assert len(select.options) == 25
    assert select.options[0].label == "Question 01?"
    assert select.options[-1].label == "Question 25?"

    interaction = FakeInteraction()
    asyncio.run(buttons[1].callback(interaction))

    edit = interaction.response.edits[0]
    assert "Page 2 / 2" in edit["embed"].description
    assert "Answer 26" not in edit["embed"].description
    next_select = next(
        item
        for item in edit["view"].children
        if isinstance(item, service.ProgressGuideFAQSelect)
    )
    assert [option.label for option in next_select.options] == ["Question 26?"]
    next_buttons = [
        item
        for item in edit["view"].children
        if isinstance(item, service.ProgressGuideFAQPageButton)
    ]
    assert [(button.label, button.disabled) for button in next_buttons] == [
        ("Previous", False),
        ("Next", True),
    ]


def test_faq_picker_does_not_show_paging_for_twenty_five_rows():
    data = _data(
        faq=[
            {
                "category": "ARB",
                "question": f"Question {i}?",
                "answer": f"Answer {i}.",
                "sort_order": str(i),
                "enabled": "TRUE",
            }
            for i in range(1, 26)
        ]
    )
    rows = service._faq_rows_for_category("ARB", data)
    view = service.ProgressGuideFAQPickerView("ARB", data, rows)
    embed = service.build_faq_picker_embed("ARB", data, rows, page=0)

    assert "Page" not in embed.description
    assert not any(
        isinstance(item, service.ProgressGuideFAQPageButton) for item in view.children
    )


def test_long_selected_faq_answer_is_shortened_with_note():
    data = _data()
    embed = service.build_selected_faq_embed(
        "ARB", data, {"question": "Long?", "answer": "x" * 5000}
    )

    assert len(embed.description) <= service._EMBED_DESCRIPTION_LIMIT
    assert "Answer shortened" in embed.description


def test_faq_picker_tag_select_parses_formats_and_labels():
    data = _data(
        faq=[
            {
                "category": "ARB",
                "faq_key": "one",
                "question": "One?",
                "answer": "One.",
                "sort_order": "1",
                "enabled": "TRUE",
                "tags": "great_hall, clan_boss; Great_Hall ; ",
            },
            {
                "category": "ARB",
                "faq_key": "two",
                "question": "Two?",
                "answer": "Two.",
                "sort_order": "2",
                "enabled": "TRUE",
                "tags": "fire_knight",
            },
            {
                "category": "ARB",
                "faq_key": "disabled",
                "question": "Disabled?",
                "answer": "Hidden.",
                "sort_order": "3",
                "enabled": "FALSE",
                "tags": "secret_tag",
            },
            {
                "category": "ARB",
                "faq_key": "blank_answer",
                "question": "Blank?",
                "answer": "",
                "sort_order": "4",
                "enabled": "TRUE",
                "tags": "empty_answer_tag",
            },
        ]
    )
    view = service.ProgressGuideFAQPickerView(
        "ARB", data, service._faq_rows_for_category("ARB", data)
    )

    tag_select = next(
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQTagSelect)
    )
    assert [option.label for option in tag_select.options] == [
        "All questions",
        "Clan Boss",
        "Fire Knight",
        "Great Hall",
    ]
    assert "Secret Tag" not in [option.label for option in tag_select.options]
    assert "Empty Answer Tag" not in [option.label for option in tag_select.options]


def test_faq_picker_omits_tag_select_when_no_tags_exist():
    data = _data()
    view = service.ProgressGuideFAQPickerView(
        "ARB", data, service._faq_rows_for_category("ARB", data)
    )

    assert not any(
        isinstance(item, service.ProgressGuideFAQTagSelect) for item in view.children
    )
    assert any(
        isinstance(item, service.ProgressGuideFAQSelect) for item in view.children
    )


def test_faq_tag_selection_filters_resets_page_and_all_restores(monkeypatch):
    faq = [
        {
            "category": "ARB",
            "faq_key": f"keep{i:02d}",
            "question": f"Keep {i:02d}?",
            "answer": f"Keep answer {i:02d}.",
            "sort_order": str(i),
            "enabled": "TRUE",
            "tags": "clan_boss",
        }
        for i in range(1, 28)
    ] + [
        {
            "category": "ARB",
            "faq_key": "other",
            "question": "Other?",
            "answer": "Other answer.",
            "sort_order": "99",
            "enabled": "TRUE",
            "tags": "great_hall",
        }
    ]
    data = _data(faq=faq)
    rows = service._faq_rows_for_category("ARB", data)
    view = service.ProgressGuideFAQPickerView("ARB", data, rows, page=1)
    tag_select = next(
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQTagSelect)
    )
    clan_key = next(
        option.value for option in tag_select.options if option.label == "Clan Boss"
    )
    monkeypatch.setattr(
        service.ProgressGuideFAQTagSelect, "values", property(lambda _self: [clan_key])
    )
    interaction = FakeInteraction()

    asyncio.run(tag_select.callback(interaction))

    edit = interaction.response.edits[0]
    assert "Filter: Clan Boss" in edit["embed"].description
    assert "Page 1 / 2" in edit["embed"].description
    filtered_view = edit["view"]
    assert filtered_view.page == 0
    faq_select = next(
        item
        for item in filtered_view.children
        if isinstance(item, service.ProgressGuideFAQSelect)
    )
    assert len(faq_select.options) == 25
    assert all(option.label.startswith("Keep") for option in faq_select.options)

    all_select = next(
        item
        for item in filtered_view.children
        if isinstance(item, service.ProgressGuideFAQTagSelect)
    )
    monkeypatch.setattr(
        service.ProgressGuideFAQTagSelect, "values", property(lambda _self: ["__all__"])
    )
    second = FakeInteraction()
    asyncio.run(all_select.callback(second))

    restored = second.response.edits[0]
    assert "Filter:" not in restored["embed"].description
    assert len(restored["view"].filtered_rows) == 28


def test_faq_page_preserves_tag_filter_and_selected_answer_scrubs_metadata(monkeypatch):
    faq = [
        {
            "category": "ARB",
            "faq_key": f"keep{i:02d}",
            "question": f"Keep {i:02d}?",
            "answer": f"Keep answer {i:02d}. https://example.com/hidden",
            "sort_order": str(i),
            "enabled": "TRUE",
            "tags": "clan_boss",
        }
        for i in range(1, 27)
    ]
    data = _data(faq=faq)
    rows = service._faq_rows_for_category("ARB", data)
    tag_key = service._faq_tag_key("clan_boss")
    view = service.ProgressGuideFAQPickerView("ARB", data, rows, selected_tag=tag_key)
    next_button = next(
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQPageButton) and item.label == "Next"
    )
    interaction = FakeInteraction()

    asyncio.run(next_button.callback(interaction))

    edit = interaction.response.edits[0]
    assert "Filter: Clan Boss" in edit["embed"].description
    assert "Page 2 / 2" in edit["embed"].description
    assert edit["view"].selected_tag == tag_key

    select = next(
        item
        for item in edit["view"].children
        if isinstance(item, service.ProgressGuideFAQSelect)
    )
    monkeypatch.setattr(
        service.ProgressGuideFAQSelect, "values", property(lambda _self: ["keep26"])
    )
    answer_interaction = FakeInteraction()
    asyncio.run(select.callback(answer_interaction))

    answer = answer_interaction.response.edits[0]
    assert "**Keep 26?**" in answer["embed"].description
    assert "Keep answer 26." in answer["embed"].description
    assert "example.com" not in answer["embed"].description
    assert "keep26" not in answer["embed"].description
    assert "clan_boss" not in answer["embed"].description
    assert any(
        isinstance(item, service.ProgressGuideFAQTagSelect)
        for item in answer["view"].children
    )


def test_faq_tag_select_caps_options_at_discord_limit():
    faq = [
        {
            "category": "ARB",
            "faq_key": f"q{i:02d}",
            "question": f"Question {i:02d}?",
            "answer": f"Answer {i:02d}.",
            "sort_order": str(i),
            "enabled": "TRUE",
            "tags": f"tag_{i:02d}",
        }
        for i in range(1, 31)
    ]
    data = _data(faq=faq)
    view = service.ProgressGuideFAQPickerView(
        "ARB", data, service._faq_rows_for_category("ARB", data)
    )
    tag_select = next(
        item
        for item in view.children
        if isinstance(item, service.ProgressGuideFAQTagSelect)
    )

    assert len(tag_select.options) == 25
    assert tag_select.options[0].label == "All questions"


def test_faq_button_sends_clean_error_embed_when_loading_fails(monkeypatch):
    async def load_data():
        raise RuntimeError("sheets unavailable")

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    button = service.ProgressGuideFAQButton("ARB")
    interaction = FakeInteraction()

    asyncio.run(button.callback(interaction))

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert sent["embed"].title == "Progress guide FAQ unavailable"
    assert "sheets unavailable" not in sent["embed"].description


def test_faq_button_is_not_shown_when_no_faq_rows_exist():
    view = service.build_guide_view(_data(faq=[]).posts[0], _data(faq=[]))
    assert view is not None
    assert [getattr(item, "label", "") for item in view.children] == ["Ask the Helpers"]


def test_faq_button_does_not_link_to_help_post_url():
    data = _data()
    view = service.build_guide_view(data.posts[0], data)
    faq_button = _button_by_label(view, "Read FAQ")
    assert faq_button.custom_id == "progressguides:faq:ARB"
    assert faq_button.url is None


def test_ask_in_help_still_links_to_help_post_url():
    data = _data()
    view = service.build_guide_view(data.posts[0], data)
    ask_button = _button_by_label(view, "Ask the Helpers")
    assert ask_button.url == "https://discord.com/channels/1/2/3"


def test_progress_guides_cog_setup_does_not_load_sheet_data(monkeypatch):
    loads = 0

    async def load_data():
        nonlocal loads
        loads += 1
        raise AssertionError("startup should not load sheets")

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    ProgressGuidesCog(FakeBot())
    assert loads == 0


def test_first_faq_click_loads_once_and_caches_data(monkeypatch):
    data = _data()
    loads = 0

    async def load_data():
        nonlocal loads
        loads += 1
        return data

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    interaction = FakeInteraction()

    asyncio.run(service.ProgressGuideFAQButton("ARB").callback(interaction))

    assert loads == 1
    assert service.get_cached_progress_guide_data() is data
    assert interaction.followup.sent[0]["embed"].title == "🏟️ Arena Rush Questions"


def test_second_faq_click_uses_cache_without_loading_sheet(monkeypatch):
    data = _data()
    service.set_progress_guide_cache(data)

    async def load_data():
        raise AssertionError("cached FAQ click should not load sheets")

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    interaction = FakeInteraction()

    asyncio.run(service.ProgressGuideFAQButton("ARB").callback(interaction))

    assert interaction.followup.sent[0]["embed"].title == "🏟️ Arena Rush Questions"


def test_concurrent_faq_clicks_share_single_sheet_load(monkeypatch):
    data = _data()
    loads = 0

    async def load_data():
        nonlocal loads
        loads += 1
        await asyncio.sleep(0)
        return data

    async def click_twice():
        monkeypatch.setattr(service, "load_progress_guide_data", load_data)
        first = FakeInteraction()
        second = FakeInteraction()
        await asyncio.gather(
            service.ProgressGuideFAQButton("ARB").callback(first),
            service.ProgressGuideFAQButton("ARB").callback(second),
        )
        return first, second

    first, second = asyncio.run(click_twice())

    assert loads == 1
    assert first.followup.sent[0]["embed"].title == "🏟️ Arena Rush Questions"
    assert second.followup.sent[0]["embed"].title == "🏟️ Arena Rush Questions"


def test_progress_guides_cog_registers_persistent_faq_view():
    bot = FakeBot()
    ProgressGuidesCog(bot)
    assert bot.views
    assert {item.custom_id for item in bot.views[0].children} >= {
        "progressguides:faq:ARB",
        "progressguides:faq:RAM",
        "progressguides:faq:MAR",
        "progressguides:faq:FW_N",
        "progressguides:faq:FW_H",
    }


def test_refresh_existing_stored_message_does_not_read_worksheet_or_header(monkeypatch):
    data = _data(
        post_overrides={
            "guide_panel_message_id": "777",
            "guide_post_url": "https://discord.com/channels/1/2/777",
        }
    )
    message = FakeMessage(777)
    guide = FakeChannel(existing=message)

    async def load_data():
        return data

    async def forbidden_ws(*_args, **_kwargs):
        raise AssertionError("existing refresh must not fetch worksheet")

    async def forbidden_header(*_args, **_kwargs):
        raise AssertionError("existing refresh must not load header")

    async def resolve(_bot, channel_id):
        return {10: guide}.get(channel_id)

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service.discord, "NotFound", FakeDiscordNotFound)
    monkeypatch.setattr(service, "aget_worksheet", forbidden_ws)
    monkeypatch.setattr(service, "_load_header", forbidden_header)
    monkeypatch.setattr(service, "_resolve_messageable", resolve)

    summary = asyncio.run(service.publish_or_refresh(FakeBot(), refresh=True))

    assert summary.refreshed == 1
    assert message.edits
    assert guide.sent == []


def test_refresh_existing_stored_message_does_not_write_guide_panel_message_id(
    monkeypatch,
):
    worksheet = FakeWorksheet()
    message = FakeMessage(777)
    guide = FakeChannel(existing=message)
    summary = asyncio.run(
        _run(
            monkeypatch,
            _data(
                post_overrides={
                    "guide_panel_message_id": "777",
                    "guide_post_url": "https://discord.com/channels/1/2/777",
                }
            ),
            {10: guide},
            worksheet,
        )
    )

    assert summary.refreshed == 1
    assert worksheet.updates == []


def test_create_path_still_writes_guide_panel_message_id(monkeypatch):
    worksheet = FakeWorksheet()
    summary = asyncio.run(_run(monkeypatch, _data(), {10: FakeChannel()}, worksheet))

    assert summary.created == 1
    assert worksheet.updates == [
        ("B2", [["12345"]], {"value_input_option": "RAW"}),
        (
            "C2",
            [["https://discord.com/channels/1/2/12345"]],
            {"value_input_option": "RAW"},
        ),
    ]


class FakeCtx:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


def test_quota_config_load_failure_produces_clean_admin_embed(monkeypatch):
    from modules.community.progress_guides import cog
    from shared.sheets import milestones_config

    async def fail(*_args, **_kwargs):
        raise milestones_config.MilestonesConfigLoadFailed("RESOURCE_EXHAUSTED 429")

    ctx = FakeCtx()
    monkeypatch.setattr(cog, "publish_or_refresh", fail)

    asyncio.run(
        cog._send_publish_result(ctx, FakeBot(), action="refresh", refresh=True)
    )

    embed = ctx.sent[0]["embed"]
    assert embed.title == "Progress guides refresh unavailable"
    assert "Google Sheets read quota was temporarily exceeded" in embed.description
    assert "RESOURCE_EXHAUSTED" not in embed.description


def test_quota_error_does_not_expose_raw_commandinvokeerror_to_discord(monkeypatch):
    from modules.community.progress_guides import cog

    async def fail(*_args, **_kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED raw traceback details")

    ctx = FakeCtx()
    monkeypatch.setattr(cog, "publish_or_refresh", fail)

    asyncio.run(
        cog._send_publish_result(ctx, FakeBot(), action="refresh", refresh=True)
    )

    embed = ctx.sent[0]["embed"]
    rendered = f"{embed.title}\n{embed.description}"
    assert "CommandInvokeError" not in rendered
    assert "traceback" not in rendered.casefold()
    assert "RESOURCE_EXHAUSTED" not in rendered


class FakeRateLimitError(RuntimeError):
    status_code = 429


async def _run_refresh_command_with_lazy_write_failure(monkeypatch, *, fail_at):
    from modules.community.progress_guides import cog

    data = _data()
    worksheet = FakeWorksheet()

    async def load_data():
        return data

    async def resolve(_bot, channel_id):
        return {10: FakeChannel()}.get(channel_id)

    async def get_ws(_sheet, _tab):
        if fail_at == "aget_worksheet":
            raise FakeRateLimitError("APIError 429 RESOURCE_EXHAUSTED")
        return worksheet

    async def load_header(_sheet, _tab):
        if fail_at == "_load_header":
            raise FakeRateLimitError("APIError 429 RESOURCE_EXHAUSTED")
        return ["category", "guide_panel_message_id"]

    async def call(_func, *_args, **_kwargs):
        if fail_at == "acall_with_backoff":
            raise FakeRateLimitError("APIError 429 RESOURCE_EXHAUSTED")
        return None

    ctx = FakeCtx()
    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service, "_resolve_messageable", resolve)
    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(service, "_load_header", load_header)
    monkeypatch.setattr(service, "acall_with_backoff", call)

    await cog._send_publish_result(ctx, FakeBot(), action="refresh", refresh=True)
    return ctx


@pytest.mark.parametrize(
    "fail_at", ["aget_worksheet", "_load_header", "acall_with_backoff"]
)
def test_lazy_writeback_quota_errors_send_clean_embed_without_failure_details(
    monkeypatch, fail_at
):
    ctx = asyncio.run(
        _run_refresh_command_with_lazy_write_failure(monkeypatch, fail_at=fail_at)
    )

    assert len(ctx.sent) == 1
    embed = ctx.sent[0]["embed"]
    rendered = str(embed.to_dict())
    assert embed.title == "Progress guides refresh unavailable"
    assert "Google Sheets read quota was temporarily exceeded" in embed.description
    assert "Failure details" not in rendered
    assert "APIError" not in rendered
    assert "RESOURCE_EXHAUSTED" not in rendered


def test_non_quota_lazy_writeback_failure_stays_in_summary_failures(monkeypatch):
    from modules.community.progress_guides import cog

    data = _data()

    async def load_data():
        return data

    async def resolve(_bot, channel_id):
        return {10: FakeChannel()}.get(channel_id)

    async def get_ws(_sheet, _tab):
        raise RuntimeError("ordinary writeback problem")

    ctx = FakeCtx()
    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service, "_resolve_messageable", resolve)
    monkeypatch.setattr(service, "aget_worksheet", get_ws)

    asyncio.run(
        cog._send_publish_result(ctx, FakeBot(), action="refresh", refresh=True)
    )

    embed = ctx.sent[0]["embed"]
    assert embed.title == "Progress guides refresh"
    assert any(field.name == "Failure details" for field in embed.fields)
    assert "ordinary writeback problem" in str(embed.to_dict())


def test_quota_writeback_failure_deletes_untracked_sent_message(monkeypatch):
    from modules.community.progress_guides import cog

    data = _data()
    sent_message = FakeMessage(12345)
    guide = FakeChannel(send_message=sent_message)

    async def load_data():
        return data

    async def resolve(_bot, channel_id):
        return {10: guide}.get(channel_id)

    async def call(_func, *_args, **_kwargs):
        raise FakeRateLimitError("APIError 429 RESOURCE_EXHAUSTED")

    ctx = FakeCtx()
    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service, "_resolve_messageable", resolve)
    monkeypatch.setattr(service, "acall_with_backoff", call)
    monkeypatch.setattr(
        service,
        "aget_worksheet",
        lambda *_args: asyncio.sleep(0, result=FakeWorksheet()),
    )
    monkeypatch.setattr(
        service,
        "_load_header",
        lambda *_args: asyncio.sleep(0, result=["category", "guide_panel_message_id"]),
    )

    asyncio.run(
        cog._send_publish_result(ctx, FakeBot(), action="refresh", refresh=True)
    )

    assert sent_message.delete_attempted is True
    assert sent_message.deleted is True
    embed = ctx.sent[0]["embed"]
    rendered = str(embed.to_dict())
    assert embed.title == "Progress guides refresh unavailable"
    assert "APIError" not in rendered
    assert "RESOURCE_EXHAUSTED" not in rendered


def test_quota_writeback_failure_still_clean_embed_when_rollback_delete_fails(
    monkeypatch,
):
    from modules.community.progress_guides import cog

    data = _data()
    sent_message = FakeMessage(12345, delete_error=RuntimeError("delete forbidden"))
    guide = FakeChannel(send_message=sent_message)

    async def load_data():
        return data

    async def resolve(_bot, channel_id):
        return {10: guide}.get(channel_id)

    async def call(_func, *_args, **_kwargs):
        raise FakeRateLimitError("APIError 429 RESOURCE_EXHAUSTED")

    ctx = FakeCtx()
    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service, "_resolve_messageable", resolve)
    monkeypatch.setattr(service, "acall_with_backoff", call)
    monkeypatch.setattr(
        service,
        "aget_worksheet",
        lambda *_args: asyncio.sleep(0, result=FakeWorksheet()),
    )
    monkeypatch.setattr(
        service,
        "_load_header",
        lambda *_args: asyncio.sleep(0, result=["category", "guide_panel_message_id"]),
    )

    asyncio.run(
        cog._send_publish_result(ctx, FakeBot(), action="refresh", refresh=True)
    )

    assert sent_message.delete_attempted is True
    assert sent_message.deleted is False
    embed = ctx.sent[0]["embed"]
    rendered = str(embed.to_dict())
    assert embed.title == "Progress guides refresh unavailable"
    assert "delete forbidden" not in rendered
    assert "RESOURCE_EXHAUSTED" not in rendered


def test_non_quota_writeback_failure_deletes_untracked_message_and_reports_failure(
    monkeypatch,
):
    data = _data()
    sent_message = FakeMessage(12345)
    guide = FakeChannel(send_message=sent_message)

    async def load_data():
        return data

    async def resolve(_bot, channel_id):
        return {10: guide}.get(channel_id)

    async def call(_func, *_args, **_kwargs):
        raise RuntimeError("ordinary update failure")

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service, "_resolve_messageable", resolve)
    monkeypatch.setattr(service, "acall_with_backoff", call)
    monkeypatch.setattr(
        service,
        "aget_worksheet",
        lambda *_args: asyncio.sleep(0, result=FakeWorksheet()),
    )
    monkeypatch.setattr(
        service,
        "_load_header",
        lambda *_args: asyncio.sleep(0, result=["category", "guide_panel_message_id"]),
    )

    summary = asyncio.run(service.publish_or_refresh(FakeBot(), refresh=True))

    assert sent_message.delete_attempted is True
    assert sent_message.deleted is True
    assert summary.created == 0
    assert "ordinary update failure" in summary.failures[0]


def test_guide_button_order_includes_my_progress_with_sheet_label():
    data = _data(
        post_overrides={
            "mission_list_button_label": "View Missions",
            "mission_list_title": "Arbiter Mission List",
            "my_progress_button_label": "My Progress",
        }
    )
    view = service.build_guide_view(data.posts[0], data)
    assert [getattr(item, "label", "") for item in view.children] == [
        "View Missions",
        "My Progress",
        "Read FAQ",
        "Ask the Helpers",
    ]
    assert view.children[0].custom_id == "progressguides:missions:ARB"
    assert view.children[1].custom_id == "progressguides:myprogress:ARB"


@pytest.mark.parametrize("category", ["FW_N", "FW_H"])
def test_fw_guides_do_not_get_mission_button(category):
    data = _data(
        post_overrides={
            "category": category,
            "mission_list_button_label": "Mission List",
            "mission_list_title": "Faction Wars Mission List",
        }
    )
    data.faq_by_category[category] = data.faq_by_category.pop("ARB")
    view = service.build_guide_view(data.posts[0], data)
    assert f"progressguides:missions:{category}" not in [
        getattr(item, "custom_id", "") for item in view.children
    ]


def test_mission_rows_sort_skip_and_hide_internal_metadata():
    rows = [
        {
            "step_index": "2",
            "description": "Second https://source.example/x",
            "source_url": "https://source.example",
            "system_tags": "secret",
        },
        {"step_index": "", "description": "Fallback order", "resource_tags": "hidden"},
        {"step_index": "1", "description": "First", "mission_key": "internal-key"},
        {"step_index": "3", "mission_text": ""},
    ]
    missions = service._parse_mission_rows(rows)
    data = _data(post_overrides={"mission_list_title": "Arbiter Mission List"})
    embed = service.build_mission_embed("ARB", data, missions, page=0)
    rendered = embed.description
    assert [m.sequence_number for m in missions] == [1, 2, 3]
    assert [m.step_index for m in missions] == [2, 2, 1]
    assert "1. Second" in rendered
    assert "2. Fallback order" in rendered
    assert "3. First" in rendered
    assert "source.example" not in rendered
    assert "secret" not in rendered
    assert "hidden" not in rendered
    assert "internal-key" not in rendered


def test_mission_rows_use_description_before_mission_text_fallback():
    rows = [
        {
            "step_index": "1",
            "description": "Description mission",
            "mission_text": "Legacy text",
        },
        {"step_index": "2", "mission_text": "Fallback mission"},
        {"step_index": "3", "title": "Missing text"},
    ]

    missions = service._parse_mission_rows(rows)

    assert [(mission.number, mission.text) for mission in missions] == [
        (1, "Description mission"),
        (2, "Fallback mission"),
    ]


def test_first_mission_click_reads_only_configured_category_tab_and_caches(monkeypatch):
    data = _data(post_overrides={"mission_list_title": "Arbiter Mission List"})
    service.set_progress_guide_cache(data)
    calls = []

    async def require_value(key):
        calls.append(("config", key))
        return {
            "PROGRESS_CATEGORIES_TAB": "ProgressCategories",
            "PROGRESS_MISSIONS_ARB_TAB": "Configured_ARB_Tab",
        }[key]

    async def fetch_records(_sheet, tab):
        calls.append(("tab", tab))
        if tab == "ProgressCategories":
            return [
                {
                    "category": "ARB",
                    "mission_tab_config_key": "PROGRESS_MISSIONS_ARB_TAB",
                }
            ]
        if tab == "Configured_ARB_Tab":
            return [
                {
                    "step_index": "1",
                    "mission_text": "Do the first mission",
                    "source_url": "https://hide.example",
                }
            ]
        raise AssertionError(f"unexpected tab {tab}")

    monkeypatch.setattr(service.milestones_config, "arequire_value", require_value)
    monkeypatch.setattr(service, "afetch_records", fetch_records)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")

    first = FakeInteraction()
    second = FakeInteraction()
    asyncio.run(service.ProgressGuideMissionButton("ARB").callback(first))
    asyncio.run(service.ProgressGuideMissionButton("ARB").callback(second))

    assert first.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert calls == [
        ("config", "PROGRESS_CATEGORIES_TAB"),
        ("tab", "ProgressCategories"),
        ("config", "PROGRESS_MISSIONS_ARB_TAB"),
        ("tab", "Configured_ARB_Tab"),
    ]
    assert first.followup.sent[0]["embed"].title == "Arbiter Mission List"
    assert "Do the first mission" in first.followup.sent[0]["embed"].description
    assert "hide.example" not in first.followup.sent[0]["embed"].description
    assert second.followup.sent[0]["embed"].title == "Arbiter Mission List"


def test_concurrent_mission_clicks_share_one_mission_tab_load(monkeypatch):
    data = _data(post_overrides={"mission_list_title": "Arbiter Mission List"})
    service.set_progress_guide_cache(data)
    tab_loads = 0

    async def require_value(key):
        return {
            "PROGRESS_CATEGORIES_TAB": "ProgressCategories",
            "PROGRESS_MISSIONS_ARB_TAB": "ARB_Tab",
        }[key]

    async def fetch_records(_sheet, tab):
        nonlocal tab_loads
        if tab == "ProgressCategories":
            return [
                {
                    "category": "ARB",
                    "mission_tab_config_key": "PROGRESS_MISSIONS_ARB_TAB",
                }
            ]
        tab_loads += 1
        await asyncio.sleep(0)
        return [{"step_index": "1", "mission_text": "Only once"}]

    monkeypatch.setattr(service.milestones_config, "arequire_value", require_value)
    monkeypatch.setattr(service, "afetch_records", fetch_records)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")

    async def click_twice():
        await asyncio.gather(
            service.ProgressGuideMissionButton("ARB").callback(FakeInteraction()),
            service.ProgressGuideMissionButton("ARB").callback(FakeInteraction()),
        )

    asyncio.run(click_twice())
    assert tab_loads == 1


def test_mission_pagination_emoji_only_and_disabled_states():
    data = _data(post_overrides={"mission_list_title": "Arbiter Mission List"})
    missions = [service.MissionRow(i, f"Mission {i}") for i in range(1, 17)]
    first = service.MissionListPaginationView("ARB", data, missions, 0)
    assert [item.emoji.name for item in first.children] == ["⏮️", "◀️", "▶️", "⏭️"]
    assert [item.label for item in first.children] == [None, None, None, None]
    assert [item.disabled for item in first.children] == [True, True, False, False]
    final = service.MissionListPaginationView("ARB", data, missions, 1)
    assert [item.disabled for item in final.children] == [False, False, True, True]
    assert (
        "Missions 1-15 of 16"
        in service.build_mission_embed("ARB", data, missions, page=0).description
    )
    assert (
        "Missions 16-16 of 16"
        in service.build_mission_embed("ARB", data, missions, page=1).description
    )


def test_mission_click_quota_error_is_clean(monkeypatch):
    service.clear_mission_cache()
    data = _data(post_overrides={"mission_list_title": "Arbiter Mission List"})
    service.set_progress_guide_cache(data)

    async def fail(_key):
        raise FakeRateLimitError("APIError 429 RESOURCE_EXHAUSTED traceback")

    monkeypatch.setattr(service.milestones_config, "arequire_value", fail)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    interaction = FakeInteraction()
    asyncio.run(service.ProgressGuideMissionButton("ARB").callback(interaction))
    embed = interaction.followup.sent[0]["embed"]
    rendered = str(embed.to_dict())
    assert embed.title == "Mission list unavailable"
    assert "Google Sheets read quota was temporarily exceeded" in embed.description
    assert "APIError" not in rendered
    assert "RESOURCE_EXHAUSTED" not in rendered
    assert "traceback" not in rendered.casefold()


def test_publish_refresh_clears_mission_cache_without_reading_mission_tabs(monkeypatch):
    data = _data(post_overrides={"mission_list_title": "Arbiter Mission List"})
    service.set_progress_guide_cache(data)
    mission_tab_loads = 0
    category_tab_loads = 0

    async def require_value(key):
        return {
            "PROGRESS_CATEGORIES_TAB": "ProgressCategories",
            "PROGRESS_MISSIONS_ARB_TAB": "Configured_ARB_Tab",
        }[key]

    async def fetch_records(_sheet, tab):
        nonlocal category_tab_loads, mission_tab_loads
        if tab == "ProgressCategories":
            category_tab_loads += 1
            return [
                {
                    "category": "ARB",
                    "mission_tab_config_key": "PROGRESS_MISSIONS_ARB_TAB",
                }
            ]
        if tab == "Configured_ARB_Tab":
            mission_tab_loads += 1
            return [{"step_index": "1", "mission_text": f"Loaded {mission_tab_loads}"}]
        raise AssertionError(f"unexpected tab {tab}")

    monkeypatch.setattr(service.milestones_config, "arequire_value", require_value)
    monkeypatch.setattr(service, "afetch_records", fetch_records)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")

    first = FakeInteraction()
    asyncio.run(service.ProgressGuideMissionButton("ARB").callback(first))
    assert category_tab_loads == 1
    assert mission_tab_loads == 1
    assert "Loaded 1" in first.followup.sent[0]["embed"].description

    worksheet = FakeWorksheet()
    asyncio.run(_run(monkeypatch, data, {10: FakeChannel()}, worksheet))
    assert category_tab_loads == 1
    assert mission_tab_loads == 1

    second = FakeInteraction()
    asyncio.run(service.ProgressGuideMissionButton("ARB").callback(second))
    assert mission_tab_loads == 2
    assert "Loaded 2" in second.followup.sent[0]["embed"].description


def test_my_progress_button_hides_when_label_blank_or_tracking_false():
    data = _data(post_overrides={"my_progress_button_label": ""})
    assert "progressguides:myprogress:ARB" not in [
        getattr(i, "custom_id", "")
        for i in service.build_guide_view(data.posts[0], data).children
    ]
    data = _data(post_overrides={"progress_tracking_enabled": "FALSE"})
    assert "progressguides:myprogress:ARB" not in [
        getattr(i, "custom_id", "")
        for i in service.build_guide_view(data.posts[0], data).children
    ]


@pytest.mark.parametrize("category", ["FW_N", "FW_H"])
def test_fw_guides_do_not_get_my_progress_button(category):
    data = _data(
        post_overrides={
            "category": category,
            "my_progress_button_label": "My Progress",
            "progress_tracking_enabled": "TRUE",
        }
    )
    data.faq_by_category[category] = data.faq_by_category.pop("ARB")
    view = service.build_guide_view(data.posts[0], data)
    assert f"progressguides:myprogress:{category}" not in [
        getattr(item, "custom_id", "") for item in view.children
    ]


def test_my_progress_empty_state_uses_sheet_copy_and_set_button():
    data = _data()
    embed = service.build_my_progress_embed(data.posts[0], None, None, [])
    assert embed.title == "Arbiter Progress"
    assert embed.description == "No progress saved yet."
    view = service.MyProgressView(data.posts[0])
    assert [getattr(item, "label", "") for item in view.children] == ["Set Progress"]


def test_existing_progress_renders_template_prefers_mission_key_and_hides_metadata():
    data = _data()
    category = service.ProgressCategory(
        "ARB", "Arena Rush Basics", 286, "PROGRESS_MISSIONS_ARB_TAB"
    )
    missions = [
        service.MissionRow(10, "Wrong fallback", "old"),
        service.MissionRow(
            145, "Clear Stage 20 of the Dragon’s Lair 10 times on Auto.", "dragon-20"
        ),
    ]
    state = {
        "current_step_index": "10",
        "current_mission_key": "dragon-20",
        "status": "in_progress",
    }
    embed = service.build_my_progress_embed(data.posts[0], category, state, missions)
    rendered = embed.description
    assert "145 / 286" in rendered
    assert "50.3% complete" in rendered
    assert "142 missions remaining" in rendered
    assert "Clear Stage 20" in rendered
    assert "dragon-20" not in rendered
    assert "system_tags" not in rendered
    assert "resource_tags" not in rendered
    assert "source_url" not in rendered
    assert "Wrong fallback" not in rendered


def test_existing_progress_falls_back_to_step_index_and_description_column():
    data = _data()
    category = service.ProgressCategory(
        "ARB", "Arena Rush Basics", 3, "PROGRESS_MISSIONS_ARB_TAB"
    )
    missions = service._parse_mission_rows(
        [
            {
                "step_index": "2",
                "mission_key": "two",
                "description": "Description column text",
                "mission_text": "Do not use",
            }
        ]
    )
    embed = service.build_my_progress_embed(
        data.posts[0],
        category,
        {"current_step_index": "2", "current_mission_key": "", "status": "in_progress"},
        missions,
    )
    assert "Description column text" in embed.description
    assert "Do not use" not in embed.description


def test_upsert_progress_user_state_appends_and_updates_with_exact_headers(monkeypatch):
    worksheet = FakeWorksheet()
    headers = [
        "user_id",
        "category",
        "current_step_index",
        "current_mission_key",
        "status",
        "notify_plan_ahead",
        "private_thread_id",
        "last_panel_message_id",
        "notes",
        "updated_at_utc",
    ]

    async def config_value(key):
        assert key == "PROGRESS_USER_STATE_TAB"
        return "ProgressUserState"

    async def fetch_records(_sheet, _tab):
        return []

    async def fetch_values(_sheet, _tab):
        return [headers]

    async def get_ws(_sheet, _tab):
        return worksheet

    async def call(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service.milestones_config, "arequire_value", config_value)
    monkeypatch.setattr(service, "afetch_records", fetch_records)
    monkeypatch.setattr(service, "afetch_values", fetch_values)
    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(service, "acall_with_backoff", call)
    asyncio.run(
        service.upsert_progress_user_state(
            123456, "ARB", service.MissionRow(145, "x", "dragon-20")
        )
    )
    appended = worksheet.appended[0][0]
    assert appended[:5] == ["123456", "ARB", "145", "dragon-20", "in_progress"]
    assert appended[5:9] == ["", "", "", ""]

    async def fetch_existing(_sheet, _tab):
        return [
            {
                "user_id": "123456",
                "category": "ARB",
                "notify_plan_ahead": "yes",
                "private_thread_id": "7",
                "last_panel_message_id": "8",
                "notes": "keep",
            }
        ]

    worksheet.appended.clear()
    monkeypatch.setattr(service, "afetch_records", fetch_existing)
    asyncio.run(
        service.upsert_progress_user_state(
            123456, "ARB", service.MissionRow(146, "x", "next")
        )
    )
    assert len(worksheet.updates) == 1
    cell, values, kwargs = worksheet.updates[0]
    assert cell == "A2:J2"
    assert values[0][:5] == ["123456", "ARB", "146", "next", "in_progress"]
    assert values[0][5:9] == ["yes", "7", "8", "keep"]
    assert kwargs == {"value_input_option": "RAW"}
    assert worksheet.appended == []


def test_my_progress_persistent_view_has_no_startup_sheet_reads(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("startup must not read sheets")

    monkeypatch.setattr(service, "afetch_records", forbidden)
    bot = FakeBot()
    ProgressGuidesCog(bot)
    custom_ids = [
        getattr(item, "custom_id", "") for view in bot.views for item in view.children
    ]
    assert "progressguides:myprogress:ARB" in custom_ids


def _picker_post_data():
    return _data(
        post_overrides={
            "my_progress_button_label": "My Progress",
            "my_progress_picker_title": "Pick Progress",
            "my_progress_picker_description": "Choose where you are.",
            "my_progress_chapter_select_placeholder": "Choose a chapter",
            "my_progress_mission_select_placeholder": "Choose a mission",
            "my_progress_no_missions_description": "No missions from sheet.",
        }
    )


def _select_from_view(view, select_type):
    return next(item for item in view.children if isinstance(item, select_type))


def test_set_progress_button_defers_then_opens_chapter_picker_via_followup(monkeypatch):
    data = _picker_post_data()
    service.set_progress_guide_cache(data)
    events = []

    async def missions(_category):
        events.append(("load_missions", list(interaction.response.deferred)))
        return [service.MissionRow(1, "First mission", "first", "Chapter One")]

    monkeypatch.setattr(service, "get_or_load_missions", missions)
    button = service.SetProgressButton("ARB", "Set Progress")
    interaction = FakeInteraction()
    asyncio.run(button.callback(interaction))

    assert interaction.response.modals == []
    assert interaction.response.sent == []
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert events == [("load_missions", [{"ephemeral": True, "thinking": True}])]
    sent = interaction.followup.sent[0]
    assert "content" not in sent
    assert sent["embed"].title == "Pick Progress"
    assert isinstance(sent["view"], service.ProgressChapterPickerView)
    select = _select_from_view(sent["view"], service.ProgressChapterSelect)
    assert select.placeholder == "Choose a chapter"
    assert [option.label for option in select.options] == ["Chapter One"]


def test_chapter_dropdown_options_are_built_from_mission_title():
    data = _picker_post_data()
    missions = [
        service.MissionRow(2, "Second", "second", "Later"),
        service.MissionRow(1, "First", "first", "Earlier"),
        service.MissionRow(3, "Third", "third", "Earlier"),
    ]
    view = service.ProgressChapterPickerView("ARB", data.posts[0], missions)
    select = _select_from_view(view, service.ProgressChapterSelect)

    assert [option.label for option in select.options] == ["Earlier", "Later"]
    assert [option.value for option in select.options] == ["0", "1"]


def test_mission_dropdown_uses_description_step_and_mission_key_without_hidden_fields():
    data = _picker_post_data()
    mission = service.MissionRow(
        56,
        "Earn 3 Stars on Stage 7 https://example.test/source",
        "stable-key",
        "Tilshire",
    )
    view = service.ProgressMissionPickerView("ARB", data.posts[0], [mission], 0)
    select = _select_from_view(view, service.ProgressMissionSelect)
    option = select.options[0]

    assert option.label.startswith("56. Earn 3 Stars on Stage 7")
    assert option.value == "stable-key"
    assert "stable-key" not in option.label
    assert "system_tags" not in option.label
    assert "resource_tags" not in option.label
    assert "source_url" not in option.label


def test_selecting_mission_defers_before_writing(monkeypatch):
    data = _picker_post_data()
    service.set_progress_guide_cache(data)
    mission = service.MissionRow(1, "First", "first", "Chapter")
    view = service.ProgressMissionPickerView("ARB", data.posts[0], [mission], 0)
    select = _select_from_view(view, service.ProgressMissionSelect)
    select._values = ["first"]
    events = []

    async def category(_category):
        return service.ProgressCategory("ARB", "Arena Rush Basics", 2, "TAB")

    async def write(user_id, category_name, selected):
        events.append(("write", list(interaction.response.deferred)))
        assert (user_id, category_name, selected.number, selected.key) == (
            123456,
            "ARB",
            1,
            "first",
        )

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "upsert_progress_user_state", write)
    interaction = FakeInteraction()
    asyncio.run(select.callback(interaction))

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert events == [("write", [{"ephemeral": True, "thinking": True}])]
    sent = interaction.followup.sent[0]
    assert "content" not in sent
    assert sent["embed"].footer.text == "Progress saved."


def test_chapters_and_missions_paginate_after_25_options():
    data = _picker_post_data()
    chapter_missions = [
        service.MissionRow(i, f"Mission {i}", f"key-{i}", "Big Chapter")
        for i in range(1, 28)
    ]
    many_chapters = [
        service.MissionRow(i, f"Mission {i}", f"key-{i}", f"Chapter {i}")
        for i in range(1, 28)
    ]

    chapter_view = service.ProgressChapterPickerView(
        "ARB", data.posts[0], many_chapters
    )
    chapter_select = _select_from_view(chapter_view, service.ProgressChapterSelect)
    assert len(chapter_select.options) == 25
    assert any(getattr(item, "emoji", None) for item in chapter_view.children)

    mission_view = service.ProgressMissionPickerView(
        "ARB", data.posts[0], chapter_missions, 0
    )
    mission_select = _select_from_view(mission_view, service.ProgressMissionSelect)
    assert len(mission_select.options) == 25
    assert any(getattr(item, "emoji", None) for item in mission_view.children)


def test_marius_final_part_uses_sequence_not_local_step_index():
    data = _data(
        post_overrides={
            "my_progress_body_template": "Current mission: {chapter_title}, {chapter_step_index} / {chapter_total_steps}\nOverall progress: {completed_steps} / {total_steps} complete\nProgress: {percent_complete}% complete\n{remaining_steps} mission remaining"
        }
    )
    category = service.ProgressCategory("MAR", "Marius", 180, "TAB")
    missions = [
        service.MissionRow(
            i,
            f"Mission {i}",
            f"mar_{i}",
            f"Marius - Part {((i - 1) // 60) + 1}",
            ((i - 1) % 60) + 1,
        )
        for i in range(1, 181)
    ]
    embed = service.build_my_progress_embed(
        data.posts[0],
        category,
        {
            "current_step_index": "60",
            "current_mission_key": "mar_180",
            "status": "in_progress",
        },
        missions,
    )
    assert "Current mission: Marius - Part 3, 60 / 60" in embed.description
    assert "Overall progress: 179 / 180 complete" in embed.description
    assert "Progress: 99.4% complete" in embed.description
    assert "1 mission remaining" in embed.description


def test_ramantu_style_part_progress_counts_global_completed_steps():
    data = _data(
        post_overrides={
            "my_progress_body_template": "{completed_steps} / {total_steps} complete"
        }
    )
    category = service.ProgressCategory("RAM", "Ramantu", 183, "TAB")
    missions = [
        service.MissionRow(i, f"Part 1 Mission {i}", f"ram_{i}", "Ramantu - Part 1", i)
        for i in range(1, 61)
    ] + [
        service.MissionRow(
            i, f"Part 2 Mission {i - 60}", f"ram_{i}", "Ramantu - Part 2", i - 60
        )
        for i in range(61, 184)
    ]
    embed = service.build_my_progress_embed(
        data.posts[0],
        category,
        {
            "current_step_index": "39",
            "current_mission_key": "ram_99",
            "status": "in_progress",
        },
        missions,
    )
    assert embed.description == "98 / 183 complete"


def test_mark_mission_done_on_final_marius_sets_done_and_renders_100(monkeypatch):
    worksheet = FakeWorksheet()
    headers = [
        "user_id",
        "category",
        "current_step_index",
        "current_mission_key",
        "status",
        "notify_plan_ahead",
        "private_thread_id",
        "last_panel_message_id",
        "notes",
        "updated_at_utc",
    ]
    state = {
        "user_id": "123456",
        "category": "MAR",
        "current_step_index": "60",
        "current_mission_key": "mar_180",
        "status": "in_progress",
        "notify_plan_ahead": "yes",
        "private_thread_id": "7",
        "last_panel_message_id": "8",
        "notes": "keep",
    }

    async def config_value(_key):
        return "ProgressUserState"

    async def fetch_records(_sheet, _tab):
        return [state]

    async def fetch_values(_sheet, _tab):
        return [headers]

    async def get_ws(_sheet, _tab):
        return worksheet

    async def call(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service.milestones_config, "arequire_value", config_value)
    monkeypatch.setattr(service, "afetch_records", fetch_records)
    monkeypatch.setattr(service, "afetch_values", fetch_values)
    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(service, "acall_with_backoff", call)
    missions = [
        service.MissionRow(
            i,
            f"Mission {i}",
            f"mar_{i}",
            f"Marius - Part {((i - 1) // 60) + 1}",
            ((i - 1) % 60) + 1,
        )
        for i in range(1, 181)
    ]
    new_state = asyncio.run(
        service.complete_current_mission(123456, "MAR", state, missions)
    )
    assert new_state["status"] == "done"
    assert new_state["current_step_index"] == "60"
    assert new_state["current_mission_key"] == "mar_180"
    assert len(worksheet.updates) == 1
    assert worksheet.updates[0][0] == "A2:J2"
    assert worksheet.updates[0][1][0][5:9] == ["yes", "7", "8", "keep"]

    data = _data(
        post_overrides={
            "my_progress_body_template": "{completed_steps} {remaining_steps} {percent_complete}"
        }
    )
    category = service.ProgressCategory("MAR", "Marius", 180, "TAB")
    embed = service.build_my_progress_embed(
        data.posts[0], category, new_state, missions
    )
    assert embed.description == "180 0 100"


def test_mission_dropdown_keeps_local_step_index_with_global_sequence():
    data = _picker_post_data()
    mission = service.MissionRow(
        180, "Deal 30,000,000 damage", "mar_180", "Marius - Part 3", 60
    )
    view = service.ProgressMissionPickerView("MAR", data.posts[0], [mission], 0)
    option = _select_from_view(view, service.ProgressMissionSelect).options[0]
    assert option.label.startswith("60. Deal 30,000,000 damage")
    assert option.value == "mar_180"


def test_my_progress_unavailable_embed_uses_sheet_description():
    data = _data(post_overrides={"my_progress_button_label": "My Progress"})
    embed = service._progress_unavailable_embed(data.posts[0])
    assert embed.title == "Arbiter Progress"
    assert embed.description == "Sheet says progress is temporarily unavailable."


def _plan_data(**overrides):
    base = {
        "mission_list_button_label": "Mission List",
        "mission_list_title": "Mission List",
        "my_progress_button_label": "My Progress",
        "plan_ahead_button_label": "Plan Ahead",
        "plan_ahead_title": "Plan Ahead Title",
        "plan_ahead_intro_template": "Planning {category_label}: {chapter_title} {chapter_step_index}/{chapter_total_steps}; scan {lookahead_count}; current {mission_description}",
        "plan_ahead_no_progress_description": "Set progress first.",
        "plan_ahead_no_items_description": "Nothing to plan.",
        "plan_ahead_upcoming_field_title": "Coming soon",
        "plan_ahead_save_field_title": "Save or prepare",
        "plan_ahead_avoid_field_title": "Do not do too early",
        "plan_ahead_time_gate_field_title": "Time-gated",
        "plan_ahead_warning_field_title": "Watch-outs",
        "plan_ahead_footer": "Sheet footer",
        "plan_ahead_lookahead_count": "2",
        "my_progress_complete_button_label": "Mark Mission Done",
    }
    base.update(overrides)
    return _data(post_overrides=base)


def _plan_missions():
    return [
        service.MissionRow(1, "Current text", "current-key", "Marius - Part 1", 1),
        service.MissionRow(
            2,
            "Clear Stage 1",
            "next-key",
            "Marius - Part 1",
            2,
            tips="Save energy",
            resource_tags="hydra_keys, arena_refills",
            time_gate=True,
            difficulty_note="wall",
            guide_priority="high",
            retroactive_note="Must be active",
        ),
        service.MissionRow(
            3,
            "Upgrade gear",
            "third-key",
            "Marius - Part 1",
            3,
            tips="Save energy",
            avoid_doing="Do not claim reward",
            resource_tags="hydra_keys; silver",
            difficulty_note="medium",
            guide_priority="normal",
            completion_rule="active_only",
        ),
        service.MissionRow(4, "Hidden future", "future-key", "Marius - Part 1", 4),
    ]


def test_plan_ahead_main_guide_button_order_and_visibility_rules():
    data = _plan_data(
        how_to_use_button_label="❔ How to use",
        how_to_use_title="How to use this Interactive Guide",
        how_to_use_description="Mission List shows the full mission chain.",
    )
    view = service.build_guide_view(data.posts[0], data)
    assert [getattr(item, "label", "") for item in view.children] == [
        "Mission List",
        "My Progress",
        "Plan Ahead",
        "Read FAQ",
        "Ask the Helpers",
        "❔ How to use",
    ]
    assert view.children[2].custom_id == "progressguides:planahead:ARB"

    assert "progressguides:planahead:ARB" not in [
        getattr(item, "custom_id", "")
        for item in service.build_guide_view(
            _plan_data(plan_ahead_button_label="").posts[0], data
        ).children
    ]
    assert "progressguides:planahead:ARB" not in [
        getattr(item, "custom_id", "")
        for item in service.build_guide_view(
            _plan_data(progress_tracking_enabled="FALSE").posts[0], data
        ).children
    ]
    fw = _plan_data(category="FW_N")
    fw.faq_by_category["FW_N"] = fw.faq_by_category.pop("ARB")
    assert "progressguides:planahead:FW_N" not in [
        getattr(item, "custom_id", "")
        for item in service.build_guide_view(fw.posts[0], fw).children
    ]


def test_plan_ahead_persistent_view_has_no_startup_sheet_reads(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("startup must not read sheets")

    monkeypatch.setattr(service, "afetch_records", forbidden)
    bot = FakeBot()
    ProgressGuidesCog(bot)
    assert "progressguides:planahead:ARB" in [
        getattr(item, "custom_id", "") for view in bot.views for item in view.children
    ]


def test_plan_ahead_no_saved_progress_uses_sheet_copy_and_set_progress(monkeypatch):
    data = _plan_data()
    service.set_progress_guide_cache(data)

    async def category(_category):
        return service.ProgressCategory("ARB", "Arena Rush Basics", 4, "TAB")

    async def rows():
        return "ProgressUserState", []

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "_user_state_rows", rows)
    interaction = FakeInteraction()
    asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))
    sent = interaction.followup.sent[0]
    assert sent["embed"].description == "Set progress first."
    assert [getattr(item, "label", "") for item in sent["view"].children] == [
        "Set Progress"
    ]
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]


def test_plan_ahead_saved_progress_builds_fields_from_future_missions(monkeypatch):
    data = _plan_data()
    service.set_progress_guide_cache(data)
    state = {
        "user_id": "123456",
        "category": "ARB",
        "current_step_index": "99",
        "current_mission_key": "current-key",
        "status": "in_progress",
    }

    async def category(_category):
        return service.ProgressCategory("ARB", "Arena Rush Basics", 4, "TAB")

    async def rows():
        return "ProgressUserState", [state]

    async def missions(_category):
        return _plan_missions()

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "_user_state_rows", rows)
    monkeypatch.setattr(service, "get_or_load_missions", missions)
    interaction = FakeInteraction()
    asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))
    embed = interaction.followup.sent[0]["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert "Clear Stage 1" in fields["Coming soon"]
    assert "Upgrade gear" in fields["Coming soon"]
    assert "Current text" not in fields["Coming soon"]
    assert "Save energy" in fields["Save or prepare"]
    assert "hydra keys" not in fields["Save or prepare"]
    assert "arena refills" not in fields["Save or prepare"]
    assert "hydra_keys" not in fields["Save or prepare"]
    assert "high" not in fields["Save or prepare"]
    assert fields["Save or prepare"].count("Save energy") == 1
    assert "- 3: Do not claim reward" in fields["Do not do too early"]
    assert "Must be active" in fields["Time-gated"]
    assert "Watch-outs" not in fields
    rendered = embed.description + "\n" + "\n".join(fields.values())
    assert "current-key" not in rendered
    assert "next-key" not in rendered
    assert "source_url" not in rendered
    assert "Hidden future" not in rendered


def test_plan_ahead_prep_window_rank_supports_numeric_and_text_values():
    assert service._prep_window_rank("30") > service._prep_window_rank("20")
    assert service._prep_window_rank("20") > service._prep_window_rank("8")
    assert service._prep_window_rank("") == 0
    assert service._prep_window_rank("critical") > service._prep_window_rank(
        "major_wall"
    )
    assert service._prep_window_rank("major-wall") == service._prep_window_rank(
        "major_wall"
    )
    assert service._prep_window_rank("high") > service._prep_window_rank("normal")


def test_plan_ahead_warning_selection_prefers_numeric_prep_window_values():
    warnings = service._plan_warning_candidates(
        [
            service.MissionRow(
                41,
                "Lower priority",
                "low",
                "Ramantu - Part 2",
                41,
                difficulty_note="Lower priority warning.",
                prep_window="20",
            ),
            service.MissionRow(
                42,
                "Higher priority",
                "high",
                "Ramantu - Part 2",
                42,
                difficulty_note="Higher priority warning.",
                prep_window="30",
            ),
            service.MissionRow(
                43,
                "Lowest priority",
                "lowest",
                "Ramantu - Part 2",
                43,
                difficulty_note="Lowest priority warning.",
                prep_window="8",
            ),
        ]
    )

    assert warnings == ["Higher priority warning.", "Lower priority warning."]


def test_plan_ahead_renderer_uses_player_facing_columns_and_limits_output():
    data = _plan_data(plan_ahead_lookahead_count="5", plan_ahead_footer="")
    post = data.posts[0]
    missions = [
        service.MissionRow(40, "Current", "ram-40", "Ramantu - Part 2", 40),
        service.MissionRow(
            41,
            "Earn 25,000 Points in Tournaments",
            "ram-41",
            "Ramantu - Part 2",
            41,
            tips="Save energy, gems, and refill resources for this point mission.",
            resource_tags="Keep: energy",
            avoid_doing="Do not chase points before this mission is active.",
            retroactive_note="Earn the points while this mission is active.",
            difficulty_note="normal",
            guide_priority="critical",
            prep_window="normal",
            completion_rule="active_only",
        ),
        service.MissionRow(
            42,
            "Clear Secret Room 1",
            "ram-42",
            "Ramantu - Part 2",
            42,
            resource_tags="Keep: silver keys",
            avoid_doing="Do not spend Silver Keys early.",
            retroactive_note="Doom Tower access depends on rotation and reset.",
            difficulty_note="Doom Tower missions can force a wait if the room, boss, or rotation is not available.",
            guide_priority="major_wall",
            prep_window="critical",
        ),
        service.MissionRow(
            43,
            "Clear Stage 7",
            "ram-43",
            "Ramantu - Part 2",
            43,
            tips="Prepare a safe Nightmare Campaign duo for this stage.",
            avoid_doing="Do not farm the wrong stage.",
            retroactive_note="Use keys after reset.",
            difficulty_note="Campaign steps are easy with a duo ready.",
            prep_window="medium",
        ),
        service.MissionRow(
            44,
            "Craft 10 Artifacts",
            "ram-44",
            "Ramantu - Part 2",
            44,
            tips="Save Accuracy Charms and enough Forge materials.",
            avoid_doing="Do not spend Accuracy Charms before this mission if you are low.",
            retroactive_note="Craft or equip after this mission is active.",
            difficulty_note="Forge steps are only easy if the exact charms and materials were saved.",
            prep_window="high",
            completion_rule="active_only",
        ),
        service.MissionRow(
            184,
            "Iron Twins",
            "ram-pt3-1",
            "Ramantu - Part 3",
            1,
            tips="Save fortress keys.",
            avoid_doing="Do not spend fortress keys.",
            retroactive_note="Wait for Force affinity.",
            difficulty_note="critical",
            guide_priority="normal",
            prep_window="wall",
            completion_rule="active_only",
        ),
        service.MissionRow(
            185,
            "Hidden sixth",
            "ram-pt3-2",
            "Ramantu - Part 3",
            2,
            tips="Should not render",
        ),
    ]

    embed = service.build_plan_ahead_embed(
        post,
        service.ProgressCategory("RAM", "Ramantu", 185, "TAB"),
        {"current_mission_key": "ram-40", "current_step_index": "40"},
        missions,
    )
    fields = {field.name: field.value for field in embed.fields}

    assert "- 41: Earn 25,000 Points" in fields["Coming soon"]
    assert "Ramantu - Part 2, 41" not in fields["Coming soon"]
    assert "- Ramantu - Part 3, 1: Iron Twins" in fields["Coming soon"]
    assert "184: Iron Twins" not in fields["Coming soon"]
    assert "Hidden sixth" not in fields["Coming soon"]
    assert "Keep: energy" not in fields["Save or prepare"]
    assert "Keep: silver keys" in fields["Save or prepare"]
    assert fields["Save or prepare"].count("- ") == 4
    assert "Earn the points" not in fields["Do not do too early"]
    assert "Keep:" not in fields["Do not do too early"]
    assert (
        "- 41: Do not chase points before this mission is active."
        in fields["Do not do too early"]
    )
    assert "Ramantu - Part 2, 41" not in fields["Do not do too early"]
    assert "- 44: Do not spend Accuracy Charms" in fields["Do not do too early"]
    assert (
        "- Ramantu - Part 3, 1: Do not spend fortress keys."
        in fields["Do not do too early"]
    )
    assert "Do not spend Silver Keys early." not in fields["Do not do too early"]
    assert "Earn the points while this mission is active." not in fields["Time-gated"]
    assert "Craft or equip after this mission is active." not in fields["Time-gated"]
    assert "Ramantu - Part 2, 41" not in fields["Time-gated"]
    assert "Doom Tower access depends on rotation and reset." in fields["Time-gated"]
    assert "Use keys after reset." in fields["Time-gated"]
    assert "Wait for Force affinity." in fields["Time-gated"]
    assert "Doom Tower missions can force a wait" in fields["Watch-outs"]
    assert "Forge steps are only easy" in fields["Watch-outs"]
    assert "Campaign steps" not in fields["Watch-outs"]
    assert "guide_priority" not in "\n".join(fields.values())
    assert "prep_window" not in "\n".join(fields.values())
    assert "major_wall" not in "\n".join(fields.values())
    assert "ram-41" not in "\n".join(fields.values())
    assert "system_tags" not in "\n".join(fields.values())
    assert not embed.footer.text


def test_plan_ahead_timing_note_filter_keeps_real_timing_gates():
    active_only_notes = [
        "Spend medals after this mission is active.",
        "Farm potions while this mission is active.",
        "Claim it if it does not auto-complete.",
        "Earn tournament points while the mission is active.",
        "Do the upgrade after the mission is active.",
        "Claim it if the mission does not auto-complete.",
    ]
    real_timing_notes = [
        "Arcane Keep is open every day.",
        "Magic Keep opens Monday and Thursday.",
        "Doom Tower access depends on rotation and reset.",
        "Crypt access depends on Faction Wars rotation.",
        "Rank checks depend on weekly reset.",
    ]

    assert all(
        service._is_active_only_instruction_note(note) for note in active_only_notes
    )
    assert not any(
        service._is_active_only_instruction_note(note) for note in real_timing_notes
    )


def test_plan_ahead_saved_progress_does_not_pass_none_view(monkeypatch):
    data = _plan_data()
    service.set_progress_guide_cache(data)
    state = {
        "user_id": "123456",
        "category": "ARB",
        "current_step_index": "99",
        "current_mission_key": "current-key",
        "status": "in_progress",
    }

    async def category(_category):
        return service.ProgressCategory("ARB", "Arena Rush Basics", 4, "TAB")

    async def rows():
        return "ProgressUserState", [state]

    async def missions(_category):
        return _plan_missions()

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "_user_state_rows", rows)
    monkeypatch.setattr(service, "get_or_load_missions", missions)
    interaction = FakeInteraction()

    asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))

    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert "view" not in sent
    assert sent["embed"].title == "Plan Ahead Title"


def test_plan_ahead_non_quota_failure_after_defer_sends_fallback_logs_and_does_not_reraise(
    monkeypatch, caplog
):
    data = _plan_data()
    service.set_progress_guide_cache(data)

    async def category(_category):
        raise RuntimeError("raw google schema boom current_mission_key")

    monkeypatch.setattr(service, "_progress_category", category)
    interaction = FakeInteraction()

    with caplog.at_level("ERROR", logger="c1c.community.progress_guides.service"):
        asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert (
        sent["embed"].description == "Sheet says progress is temporarily unavailable."
    )
    assert "raw google schema boom" not in sent["embed"].description
    record = next(
        r for r in caplog.records if r.message == "plan ahead callback failed"
    )
    assert record.exc_info is not None
    assert record.category == "ARB"
    assert record.user_id == 123456
    assert record.guild_id is None
    assert record.channel_id is None
    assert record.exc_type == "RuntimeError"
    assert record.exc_message == "raw google schema boom current_mission_key"
    assert record.post_found is True
    assert record.state_found is False
    assert record.current_mission_key == ""
    assert record.current_step_index == ""
    assert record.missions_loaded_count == 0
    assert record.category_info_found is False


def test_plan_ahead_failure_after_state_and_missions_logs_diagnostics(
    monkeypatch, caplog
):
    data = _plan_data()
    service.set_progress_guide_cache(data)
    state = {
        "user_id": "123456",
        "category": "ARB",
        "current_step_index": "40",
        "current_mission_key": "ram_100",
        "status": "in_progress",
    }

    async def category(_category):
        return service.ProgressCategory("ARB", "Arena Rush Basics", 4, "TAB")

    async def rows():
        return "ProgressUserState", [state]

    async def missions(_category):
        return _plan_missions()

    def broken_embed(*_args, **_kwargs):
        raise ValueError("embed render boom")

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "_user_state_rows", rows)
    monkeypatch.setattr(service, "get_or_load_missions", missions)
    monkeypatch.setattr(service, "build_plan_ahead_embed", broken_embed)
    interaction = FakeInteraction()

    with caplog.at_level("ERROR", logger="c1c.community.progress_guides.service"):
        asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))

    sent = interaction.followup.sent[0]
    assert (
        sent["embed"].description == "Sheet says progress is temporarily unavailable."
    )
    assert "embed render boom" not in sent["embed"].description
    record = next(
        r for r in caplog.records if r.message == "plan ahead callback failed"
    )
    assert record.exc_type == "ValueError"
    assert record.exc_message == "embed render boom"
    assert record.current_mission_key == "ram_100"
    assert record.current_step_index == "40"
    assert record.missions_loaded_count == 4
    assert record.category_info_found is True
    assert record.state_found is True


def test_plan_ahead_failure_before_context_assignment_logs_safe_defaults(
    monkeypatch, caplog
):
    service.set_progress_guide_cache(None)

    async def data():
        raise LookupError("data load failed")

    monkeypatch.setattr(service, "get_or_load_progress_guide_data", data)
    interaction = FakeInteraction()

    with caplog.at_level("ERROR", logger="c1c.community.progress_guides.service"):
        asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))

    sent = interaction.followup.sent[0]
    assert sent["embed"].description == "Unavailable."
    assert "data load failed" not in sent["embed"].description
    record = next(
        r for r in caplog.records if r.message == "plan ahead callback failed"
    )
    assert record.exc_type == "LookupError"
    assert record.exc_message == "data load failed"
    assert record.post_found is False
    assert record.state_found is False
    assert record.current_mission_key == ""
    assert record.current_step_index == ""
    assert record.missions_loaded_count == 0
    assert record.category_info_found is False


def test_plan_ahead_quota_failure_keeps_clean_unavailable_embed(monkeypatch):
    data = _plan_data()
    service.set_progress_guide_cache(data)

    async def category(_category):
        raise RuntimeError("quota details should not leak")

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "_is_quota_failure", lambda exc: True)
    interaction = FakeInteraction()

    asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))

    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert (
        sent["embed"].description == "Sheet says progress is temporarily unavailable."
    )
    assert "quota details" not in sent["embed"].description


def test_plan_ahead_saved_progress_missing_mission_key_sends_clean_no_progress(
    monkeypatch,
):
    data = _plan_data()
    service.set_progress_guide_cache(data)
    state = {
        "user_id": "123456",
        "category": "ARB",
        "current_step_index": "99",
        "current_mission_key": "missing-key",
        "status": "in_progress",
    }

    async def category(_category):
        return None

    async def rows():
        return "ProgressUserState", [state]

    async def missions(_category):
        return _plan_missions()

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "_user_state_rows", rows)
    monkeypatch.setattr(service, "get_or_load_missions", missions)
    interaction = FakeInteraction()

    asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))

    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert sent["embed"].description == "Set progress first."
    assert "missing-key" not in sent["embed"].description


def test_plan_ahead_does_not_write_sheets(monkeypatch):
    data = _plan_data()
    service.set_progress_guide_cache(data)

    async def category(_category):
        return service.ProgressCategory("ARB", "Arena Rush Basics", 4, "TAB")

    async def rows():
        return "ProgressUserState", []

    async def forbidden_write(*_args, **_kwargs):
        raise AssertionError("Plan Ahead must not write sheets")

    monkeypatch.setattr(service, "_progress_category", category)
    monkeypatch.setattr(service, "_user_state_rows", rows)
    monkeypatch.setattr(service, "acall_with_backoff", forbidden_write)
    interaction = FakeInteraction()

    asyncio.run(service.PlanAheadButton("ARB", "Plan Ahead").callback(interaction))

    assert interaction.followup.sent[0]["embed"].description == "Set progress first."


def test_empty_plan_uses_no_items_description_and_my_progress_shortcut():
    data = _plan_data(plan_ahead_lookahead_count="12")
    post = data.posts[0]
    embed = service.build_plan_ahead_embed(
        post,
        service.ProgressCategory("ARB", "Arena Rush Basics", 1, "TAB"),
        {"current_mission_key": "current-key", "current_step_index": "1"},
        [service.MissionRow(1, "Current text", "current-key", "Chapter", 1)],
    )
    assert embed.description == "Nothing to plan."
    assert [
        getattr(item, "label", "") for item in service.MyProgressView(post).children
    ] == [
        "Set Progress",
        "Mark Mission Done",
        "Plan Ahead",
    ]


def _fw_data(category="FW_H"):
    data = _data(
        post_overrides={
            "category": category,
            "label": (
                "Faction Wars Hard" if category == "FW_H" else "Faction Wars Normal"
            ),
            "guide_title": "Faction Wars",
            "counter_stars_button_label": "Sheet My Stars",
            "counter_progress_button_label": "Sheet Progress",
            "faction_guide_button_label": "Sheet Faction Guide",
            "conditions_button_label": "Sheet Conditions",
            "counter_stars_title": "Sheet Stars Title",
            "counter_stars_empty_description": "No sheet stars saved.",
            "counter_stars_saved_description": "Stars saved for {counter_label}: {current_value}/{goal_value} in {category_label} ({counter_key}, {category}).",
            "counter_stars_invalid_value_description": "Sheet invalid stars.",
            "counter_stars_faction_select_placeholder": "Pick stars faction",
            "counter_stars_modal_value_label": "Sheet Stars Value",
            "counter_progress_title": "Sheet Progress Title",
            "counter_progress_intro_template": "{category_label}: {current_value}/{goal_value} stars, {percent_complete}% done, {remaining_value} left.",
            "counter_progress_empty_description": "No sheet progress saved.",
            "counter_progress_footer": "Sheet progress footer.",
            "counter_progress_finished_field_title": "Sheet Finished",
            "counter_progress_close_field_title": "Sheet Close",
            "counter_progress_focus_field_title": "Sheet Focus",
            "faction_guide_title": "Sheet Faction Guide Title",
            "faction_guide_select_placeholder": "Pick guide faction",
            "faction_guide_champions_field_title": "Sheet Champions",
            "faction_guide_roles_field_title": "Sheet Roles",
            "faction_guide_accessible_field_title": "Sheet Accessible",
            "faction_guide_note_field_title": "Sheet Note",
            "faction_guide_empty_description": "Sheet guide empty.",
            "conditions_title": "Sheet Conditions Title",
            "conditions_select_placeholder": "Pick conditions faction",
            "conditions_summary_field_title": "Sheet Summary",
            "conditions_stages_field_title": "Sheet Stages",
            "conditions_empty_description": "Sheet conditions empty.",
        },
        guides=[
            {
                "category": category,
                "title": "Overview",
                "body": "Beat crypt stages.",
                "sort_order": "1",
                "enabled": "TRUE",
            }
        ],
    )
    data.guides_by_category[category] = data.guides_by_category.pop("ARB")
    data.faq_by_category[category] = data.faq_by_category.pop("ARB")
    return data


def _fw_static_data():
    return service.FactionWarsData(
        factions=[
            {
                "faction_key": "banner_lords",
                "name": "Banner Lords",
                "stages": "21",
                "stars_per_stage": "3",
                "max_stars": "63",
            },
            {
                "faction_key": "high_elves",
                "name": "High Elves",
                "stages": "21",
                "stars_per_stage": "3",
                "max_stars": "63",
            },
            {
                "faction_key": "orcs",
                "name": "Orcs",
                "stages": "21",
                "stars_per_stage": "3",
                "max_stars": "63",
            },
            {
                "faction_key": "ogryn_tribes",
                "name": "Ogryn Tribes",
                "stages": "21",
                "stars_per_stage": "3",
                "max_stars": "63",
            },
        ],
        champion_guides=[
            {
                "category": "FW_H",
                "mode": "hard",
                "faction_key": "banner_lords",
                "faction_name": "Banner Lords",
                "recommended_champions": "Ragash, Baron",
                "core_roles": "Revive, decrease ATK",
                "accessible_options": "Stag Knight, Valerie",
                "planning_note": "Build control first.",
                "enabled": "TRUE",
            }
        ],
        hard_stage_conditions=[
            {
                "category": "FW_H",
                "mode": "hard",
                "faction_key": "banner_lords",
                "faction_name": "Banner Lords",
                "stage_1": "Use only Rare champions.",
                "stage_2": "No support champions.",
                "stage_21": "No revives.",
                "challenge_summary": "Hard conditions rotate by stage.",
                "enabled": "TRUE",
            }
        ],
        hard_stage_solvers=[
            {
                "category": "FW_H",
                "mode": "hard",
                "faction_key": "banner_lords",
                "faction_name": "Banner Lords",
                "stage_number": "21",
                "condition": "No revives.",
                "solver_roles": "Shields and control",
                "suggested_champions": "Ragash",
                "notes": "Manual boss wave.",
                "enabled": "TRUE",
            },
            {
                "category": "FW_H",
                "mode": "hard",
                "faction_key": "ogryn_tribes",
                "faction_name": "Ogryn Tribes",
                "stage_number": "3",
                "condition": "Stun application",
                "solver_roles": "Control",
                "suggested_champions": "Bellower / stun set options",
                "notes": "Keep waves locked.",
                "enabled": "TRUE",
            },
            {
                "category": "FW_H",
                "mode": "hard",
                "faction_key": "orcs",
                "faction_name": "Orcs",
                "stage_number": "5",
                "condition": "Turn meter control",
                "solver_roles": "Decrease speed",
                "suggested_champions": "Robar",
                "notes": "Target Valk waves.",
                "enabled": "TRUE",
            },
        ],
        boss_solvers=[
            {
                "category": "FW_H",
                "mode": "any",
                "faction_key": "banner_lords",
                "faction_name": "Banner Lords",
                "boss_stage": "21",
                "boss_type": "Health-swap boss",
                "boss_problem": "Sustain pressure",
                "recommended_roles": "Shield and control",
                "strategy_note": "Save cooldowns",
                "accessible_options": "Valerie",
                "source_url": "https://boss.example/hide",
                "enabled": "TRUE",
            },
            {
                "category": "FW_N",
                "mode": "normal",
                "faction_key": "high_elves",
                "faction_name": "High Elves",
                "boss_stage": "7",
                "boss_type": "Turn meter boss",
                "boss_problem": "Turn meter cuts",
                "recommended_roles": "Decrease speed",
                "strategy_note": "Control the boss",
                "accessible_options": "Apothecary",
                "source_url": "https://normal.example/hide",
                "enabled": "TRUE",
            },
        ],
    )


def test_fw_guide_view_uses_sheet_labels_and_hard_conditions_only():
    hard = _fw_data("FW_H")
    hard_view = service.build_guide_view(hard.posts[0], hard)
    hard_labels = [getattr(i, "label", "") for i in hard_view.children]
    hard_ids = [getattr(i, "custom_id", "") for i in hard_view.children]
    assert "Sheet My Stars" in hard_labels
    assert "Set Stages" in hard_labels
    assert "Mark Perfect" in hard_labels
    assert "Sheet Progress" in hard_labels
    assert "Sheet Faction Guide" in hard_labels
    assert "Sheet Conditions" in hard_labels
    assert "progressguides:fwstars:FW_H" in hard_ids
    assert "progressguides:fwsetstages:FW_H" in hard_ids
    assert "progressguides:fwperfect:FW_H" in hard_ids
    assert "progressguides:fwprogress:FW_H" in hard_ids
    assert "progressguides:fwguide:FW_H" in hard_ids
    assert "progressguides:fwconditions:FW_H" in hard_ids

    normal = _fw_data("FW_N")
    normal_view = service.build_guide_view(normal.posts[0], normal)
    normal_ids = [getattr(i, "custom_id", "") for i in normal_view.children]
    assert "progressguides:fwstars:FW_N" in normal_ids
    assert "progressguides:fwprogress:FW_N" in normal_ids
    assert "progressguides:fwguide:FW_N" in normal_ids
    assert "progressguides:fwconditions:FW_N" not in normal_ids


def test_fw_help_view_uses_sheet_labels_without_user_progress_actions():
    hard = _fw_data("FW_H")
    hard.posts[0].guide_post_url = "https://discord.com/channels/1/2/333"
    hard_view = service.build_help_view(hard.posts[0], hard)
    hard_labels = [getattr(i, "label", "") for i in hard_view.children]
    hard_ids = [getattr(i, "custom_id", "") for i in hard_view.children]
    assert hard_labels == [
        "Read FAQ",
        "Sheet Faction Guide",
        "Sheet Conditions",
        "Back to Guide",
    ]
    assert "progressguides:fwstars:FW_H" not in hard_ids
    assert "progressguides:fwprogress:FW_H" not in hard_ids
    assert "progressguides:fwguide:FW_H" in hard_ids
    assert "progressguides:fwconditions:FW_H" in hard_ids

    normal = _fw_data("FW_N")
    normal.posts[0].guide_post_url = "https://discord.com/channels/1/2/444"
    normal_view = service.build_help_view(normal.posts[0], normal)
    normal_labels = [getattr(i, "label", "") for i in normal_view.children]
    normal_ids = [getattr(i, "custom_id", "") for i in normal_view.children]
    assert normal_labels == [
        "Read FAQ",
        "Sheet Faction Guide",
        "Back to Guide",
    ]
    assert "progressguides:fwstars:FW_N" not in normal_ids
    assert "progressguides:fwprogress:FW_N" not in normal_ids
    assert "progressguides:fwguide:FW_N" in normal_ids
    assert "progressguides:fwconditions:FW_N" not in normal_ids


def test_fw_my_stars_reads_progress_user_counters(monkeypatch):
    data = _fw_data("FW_H")
    service.set_progress_guide_cache(data)
    service._FW_DATA_CACHE = _fw_static_data()
    calls = []

    async def require_value(key):
        calls.append(("config", key))
        return {
            "PROGRESS_USER_COUNTERS_TAB": "ProgressUserCounters",
            "PROGRESS_FW_STAGE_STARS_TAB": "ProgressUserFactionStageStars",
        }[key]

    async def fetch_records(_sheet, tab):
        calls.append(("tab", tab))
        if tab == "ProgressUserFactionStageStars":
            return []
        return [
            {
                "user_id": "123456",
                "category": "FW_H",
                "counter_key": "banner_lords",
                "counter_label": "Banner Lords",
                "current_value": "61",
                "goal_value": "63",
                "status": "in_progress",
                "notes": "",
                "updated_at_utc": "",
            },
            {
                "user_id": "999",
                "category": "FW_H",
                "counter_key": "orcs",
                "counter_label": "Orcs",
                "current_value": "63",
                "goal_value": "63",
            },
        ]

    monkeypatch.setattr(service.milestones_config, "arequire_value", require_value)
    monkeypatch.setattr(service, "afetch_records", fetch_records)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")

    interaction = FakeInteraction()
    asyncio.run(
        service.FactionWarsPanelButton("FW_H", "Sheet My Stars", "stars").callback(
            interaction
        )
    )

    assert calls == [
        ("config", "PROGRESS_USER_COUNTERS_TAB"),
        ("tab", "ProgressUserCounters"),
        ("config", "PROGRESS_FW_STAGE_STARS_TAB"),
        ("tab", "ProgressUserFactionStageStars"),
    ]
    embed = interaction.followup.sent[0]["embed"]
    assert embed.title == "Sheet Stars Title"
    assert "Banner Lords: 61/63" in embed.description
    assert "Orcs" not in embed.description
    assert (
        interaction.followup.sent[0]["view"].children[0].placeholder
        == "Pick stars faction"
    )


def test_fw_my_stars_uses_status_icons_without_raw_status_text():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_stars_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "banner_lords",
                "counter_label": "Banner Lords",
                "current_value": "63",
                "goal_value": "63",
                "status": "complete",
            },
            {
                "category": "FW_H",
                "counter_key": "high_elves",
                "counter_label": "High Elves",
                "current_value": "63",
                "goal_value": "63",
                "status": "",
            },
            {
                "category": "FW_H",
                "counter_key": "ogryn_tribes",
                "counter_label": "Ogryn Tribes",
                "current_value": "2",
                "goal_value": "63",
                "status": "in_progress",
            },
        ],
    )

    assert "— in_progress" not in embed.description
    assert "— complete" not in embed.description
    assert "✅ Banner Lords: 63/63⭐" in embed.description
    assert "✅ High Elves: 63/63⭐" in embed.description
    assert "⏳ Ogryn Tribes: 2/63⭐" in embed.description


def test_fw_star_save_appends_and_updates_by_headers(monkeypatch):
    header = [
        "user_id",
        "category",
        "counter_key",
        "counter_label",
        "current_value",
        "goal_value",
        "status",
        "notes",
        "updated_at_utc",
    ]
    worksheet = FakeWorksheet()
    rows = []

    async def user_rows():
        return "ProgressUserCounters", rows

    async def get_ws(_sheet, _tab):
        return worksheet

    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service, "_fw_user_counter_rows", user_rows)
    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(
        service, "_load_header", lambda *_args: asyncio.sleep(0, result=header)
    )
    monkeypatch.setattr(
        service,
        "acall_with_backoff",
        lambda func, *a, **kw: asyncio.sleep(
            0, result=func(*a, **{k: v for k, v in kw.items() if k != "attempts"})
        ),
    )

    asyncio.run(
        service.upsert_faction_wars_counter(
            123456, "FW_H", "banner_lords", "Banner Lords", 61, 63
        )
    )
    assert worksheet.appended[0][0][:7] == [
        "123456",
        "FW_H",
        "banner_lords",
        "Banner Lords",
        "61",
        "63",
        "in_progress",
    ]

    rows[:] = [
        {
            "user_id": "123456",
            "category": "FW_H",
            "counter_key": "banner_lords",
            "counter_label": "Banner Lords",
            "current_value": "61",
            "goal_value": "63",
            "status": "in_progress",
            "notes": "keep this",
            "updated_at_utc": "old",
        }
    ]
    asyncio.run(
        service.upsert_faction_wars_counter(
            123456, "FW_H", "banner_lords", "Banner Lords", 63, 63
        )
    )
    assert worksheet.updates[0][0] == "A2:I2"
    assert worksheet.updates[0][1][0][:8] == [
        "123456",
        "FW_H",
        "banner_lords",
        "Banner Lords",
        "63",
        "63",
        "complete",
        "keep this",
    ]


def test_fw_star_modal_uses_value_label_and_formats_messages(monkeypatch):
    post = _fw_data("FW_H").posts[0]
    modal = service.FactionWarsStarModal(post, "banner_lords", "Banner Lords", 63)
    assert modal.stars.label == "Sheet Stars Value"

    saved = []

    async def save(*args):
        saved.append(args)

    monkeypatch.setattr(service, "upsert_faction_wars_counter", save)
    interaction = FakeInteraction()
    modal.stars._value = "62"
    asyncio.run(modal.on_submit(interaction))

    assert saved == [(123456, "FW_H", "banner_lords", "Banner Lords", 62, 63)]
    description = interaction.response.sent[0]["embed"].description
    assert description == (
        "Stars saved for Banner Lords: 62/63 in Faction Wars Hard "
        "(banner_lords, FW_H)."
    )
    assert "{counter_label}" not in description


def test_fw_star_modal_invalid_input_uses_configured_text():
    post = _fw_data("FW_H").posts[0]
    modal = service.FactionWarsStarModal(post, "banner_lords", "Banner Lords", 63)
    interaction = FakeInteraction()
    modal.stars._value = "many"

    asyncio.run(modal.on_submit(interaction))

    assert interaction.response.sent[0]["embed"].description == "Sheet invalid stars."


def test_fw_my_stars_uses_status_icons_without_raw_status_text():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_stars_embed(
        post,
        _fw_static_data(),
        [
            {
                "user_id": "123456",
                "category": "FW_H",
                "counter_key": "banner_lords",
                "counter_label": "Banner Lords",
                "current_value": "63",
                "goal_value": "63",
                "status": "complete",
            },
            {
                "user_id": "123456",
                "category": "FW_H",
                "counter_key": "high_elves",
                "counter_label": "High Elves",
                "current_value": "60",
                "goal_value": "63",
                "status": "in_progress",
            },
        ],
    )

    assert "- ✅ Banner Lords: 63/63⭐" in embed.description
    assert "- ⏳ High Elves: 60/63⭐" in embed.description
    assert "complete" not in embed.description
    assert "in_progress" not in embed.description


def test_fw_progress_summary_uses_saved_user_counters():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "user_id": "123456",
                "category": "FW_H",
                "counter_key": "banner_lords",
                "counter_label": "Banner Lords",
                "current_value": "63",
                "goal_value": "63",
            },
            {
                "user_id": "123456",
                "category": "FW_H",
                "counter_key": "high_elves",
                "counter_label": "High Elves",
                "current_value": "61",
                "goal_value": "63",
            },
        ],
    )
    fields = {field.name: field.value for field in embed.fields}
    assert (
        embed.description == "Faction Wars Hard: 124/252 stars, 49.2% done, 128 left."
    )
    assert embed.footer.text == "Sheet progress footer."
    assert "Banner Lords: 63/63" in fields["Sheet Finished"]
    assert "High Elves: 61/63" in fields["Sheet Close"]
    assert "Ogryn Tribes: 0/63" in fields["Sheet Focus"]


def test_fw_progress_is_summary_only_without_solver_hints():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "ogryn_tribes",
                "counter_label": "Ogryn Tribes",
                "current_value": "2",
                "goal_value": "63",
            },
            {
                "category": "FW_H",
                "counter_key": "orcs",
                "counter_label": "Orcs",
                "current_value": "12",
                "goal_value": "63",
            },
        ],
    )
    focus = {field.name: field.value for field in embed.fields}["Sheet Focus"]

    assert "Sheet Finished" in [field.name for field in embed.fields]
    assert "Sheet Close" in [field.name for field in embed.fields]
    assert "Sheet Focus" in [field.name for field in embed.fields]
    assert "Ogryn Tribes: 2/63⭐" in focus
    assert "Stage 3" not in focus
    assert "Tip:" not in focus
    assert "Keep waves locked" not in focus


def test_fw_normal_progress_does_not_include_solver_hints():
    post = _fw_data("FW_N").posts[0]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_N",
                "counter_key": "ogryn_tribes",
                "counter_label": "Ogryn Tribes",
                "current_value": "2",
                "goal_value": "63",
            }
        ],
    )
    focus = {field.name: field.value for field in embed.fields}["Sheet Focus"]

    assert "Ogryn Tribes: 2/63⭐" in focus
    assert "Tip:" not in focus
    assert "Stage 3" not in focus


def test_fw_partial_stage_rows_do_not_override_manual_total():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {"category": "FW_H", "faction_key": "orcs", "stage_number": "1", "stars": "3"},
        {"category": "FW_H", "faction_key": "orcs", "stage_number": "2", "stars": "2"},
    ]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "orcs",
                "current_value": "63",
                "goal_value": "63",
            }
        ],
        stage_rows,
    )
    fields = {field.name: field.value for field in embed.fields}

    assert "Orcs: 63/63⭐" in fields["Sheet Finished"]
    assert "Orcs: 5/63⭐" not in fields["Sheet Focus"]


def test_fw_full_stage_rows_override_manual_total():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {
            "category": "FW_H",
            "faction_key": "orcs",
            "stage_number": str(stage),
            "stars": "3" if stage <= 20 else "2",
        }
        for stage in range(1, 22)
    ]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "orcs",
                "current_value": "10",
                "goal_value": "63",
            }
        ],
        stage_rows,
    )
    fields = {field.name: field.value for field in embed.fields}

    assert "Orcs: 62/63⭐" in fields["Sheet Close"]
    assert "Orcs: 10/63⭐" not in fields["Sheet Focus"]


def test_fw_progress_does_not_show_stale_early_solver_hint_for_late_progress():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "banner_lords",
                "counter_label": "Banner Lords",
                "current_value": "63",
                "goal_value": "63",
            },
            {
                "category": "FW_H",
                "counter_key": "high_elves",
                "counter_label": "High Elves",
                "current_value": "63",
                "goal_value": "63",
            },
            {
                "category": "FW_H",
                "counter_key": "ogryn_tribes",
                "counter_label": "Ogryn Tribes",
                "current_value": "63",
                "goal_value": "63",
            },
            {
                "category": "FW_H",
                "counter_key": "orcs",
                "counter_label": "Orcs",
                "current_value": "62",
                "goal_value": "63",
            },
        ],
    )
    focus = {field.name: field.value for field in embed.fields}["Sheet Focus"]

    assert "Orcs: 62/63⭐" in focus
    assert "Tip:" not in focus
    assert "Stage 5" not in focus
    assert "Turn meter control" not in focus


def test_fw_faction_guide_uses_live_headers_and_sheet_field_titles():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_guide_embed(
        post, _fw_static_data(), "banner_lords"
    )
    fields = {field.name: field.value for field in embed.fields}
    assert embed.title == "Sheet Faction Guide Title"
    assert fields["Sheet Champions"] == "Ragash, Baron"
    assert fields["Sheet Roles"] == "Revive, decrease ATK"
    assert fields["Sheet Accessible"] == "Stag Knight, Valerie"
    assert fields["Sheet Note"] == "Build control first."


def test_fw_conditions_uses_stage_columns_and_solver_stage_number():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_conditions_embed(
        post, _fw_static_data(), "banner_lords"
    )
    fields = {field.name: field.value for field in embed.fields}
    assert embed.title == "Sheet Conditions Title"
    assert fields["Sheet Summary"] == "Hard conditions rotate by stage."
    assert "1: Use only Rare champions." in fields["Sheet Stages"]
    assert "2: No support champions." in fields["Sheet Stages"]
    assert "21: No revives." in fields["Sheet Stages"]
    assert (
        "21 solver: No revives. • Shields and control • Ragash • Manual boss wave."
        in fields["Sheet Stages"]
    )


def test_fw_empty_states_use_configured_copy():
    post = _fw_data("FW_H").posts[0]
    fw = _fw_static_data()

    guide = service.build_faction_wars_guide_embed(post, fw, "orcs")
    conditions = service.build_faction_wars_conditions_embed(post, fw, "orcs")
    progress = service.build_faction_wars_progress_embed(post, fw, [])

    assert guide.description == "Sheet guide empty."
    assert conditions.description == "Sheet conditions empty."
    assert progress.description == "No sheet progress saved."


def test_fw_cleanup_has_no_invented_progress_forum_post_fields():
    forbidden = [
        "counter_stars_modal_" + "label",
        "faction_guide_recommended_" + "field_title",
        "faction_guide_planning_" + "field_title",
        "counter_progress_total_" + "field_title",
        "conditions_solvers_" + "field_title",
    ]
    service_text = Path("modules/community/progress_guides/service.py").read_text()
    test_text = Path("tests/community/test_progress_guides.py").read_text()
    for field in forbidden:
        assert field not in service_text
        assert field not in test_text


def test_faction_wars_persistent_view_has_no_startup_sheet_reads(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("startup must not read sheets")

    monkeypatch.setattr(service, "afetch_records", forbidden)
    bot = FakeBot()
    ProgressGuidesCog(bot)
    assert "progressguides:fwstars:FW_H" in [
        getattr(item, "custom_id", "") for view in bot.views for item in view.children
    ]


def test_fw_data_loads_boss_solvers_from_configured_header_tab(monkeypatch):
    service._FW_DATA_CACHE = None
    calls = []

    async def require_value(key):
        calls.append(("config", key))
        return {
            "PROGRESS_FW_FACTIONS_TAB": "FWFactions",
            "PROGRESS_FW_CHAMPION_GUIDES_TAB": "FWChampionGuides",
            "PROGRESS_FW_HARD_STAGE_CONDITIONS_TAB": "FWHardStageConditions",
            "PROGRESS_FW_HARD_STAGE_SOLVERS_TAB": "FWHardStageSolvers",
            "PROGRESS_FW_BOSS_SOLVERS_TAB": "ConfiguredBossSolvers",
        }[key]

    async def fetch_records(_sheet, tab):
        calls.append(("tab", tab))
        if tab == "ConfiguredBossSolvers":
            return [
                {
                    "category": "FW_N",
                    "mode": "normal",
                    "faction_key": "high_elves",
                    "boss_stage": "7",
                    "boss_type": "Turn meter boss",
                    "enabled": "TRUE",
                }
            ]
        return []

    monkeypatch.setattr(service.milestones_config, "arequire_value", require_value)
    monkeypatch.setattr(service, "afetch_records", fetch_records)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")

    fw = asyncio.run(service.get_or_load_faction_wars_data())

    assert fw.boss_solvers[0]["boss_type"] == "Turn meter boss"
    assert calls == [
        ("config", "PROGRESS_FW_FACTIONS_TAB"),
        ("config", "PROGRESS_FW_CHAMPION_GUIDES_TAB"),
        ("config", "PROGRESS_FW_HARD_STAGE_CONDITIONS_TAB"),
        ("config", "PROGRESS_FW_HARD_STAGE_SOLVERS_TAB"),
        ("config", "PROGRESS_FW_BOSS_SOLVERS_TAB"),
        ("tab", "FWFactions"),
        ("tab", "FWChampionGuides"),
        ("tab", "FWHardStageConditions"),
        ("tab", "FWHardStageSolvers"),
        ("tab", "ConfiguredBossSolvers"),
    ]


def test_fw_normal_progress_shows_matching_boss_tip_for_boss_stage_only():
    post = _fw_data("FW_N").posts[0]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_N",
                "counter_key": "high_elves",
                "current_value": "18",
                "goal_value": "63",
            }
        ],
    )
    focus = {field.name: field.value for field in embed.fields}["Sheet Focus"]

    assert "High Elves: 18/63" in focus
    assert " — " not in focus
    assert "Boss 7:" not in focus
    assert "Turn meter cuts" not in focus
    assert "Apothecary" not in focus
    assert "normal.example" not in focus

    no_tip = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_N",
                "counter_key": "high_elves",
                "current_value": "21",
                "goal_value": "63",
            }
        ],
    )
    no_tip_focus = {field.name: field.value for field in no_tip.fields}["Sheet Focus"]
    assert "High Elves: 21/63" in no_tip_focus
    assert "Boss 7:" not in no_tip_focus


def test_fw_hard_progress_combines_hard_solver_and_boss_tip_without_notes():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "banner_lords",
                "current_value": "60",
                "goal_value": "63",
            }
        ],
    )
    focus = {field.name: field.value for field in embed.fields}["Sheet Focus"]

    assert " — " not in focus
    assert "Stage 21:" not in focus
    assert "Boss 21:" not in focus
    assert "Sustain pressure" not in focus
    assert "Valerie" not in focus
    assert "boss.example" not in focus
    assert "Manual boss wave" not in focus


def test_fw_progress_does_not_fallback_to_earlier_boss_stage():
    post = _fw_data("FW_N").posts[0]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_N",
                "counter_key": "high_elves",
                "current_value": "39",
                "goal_value": "63",
            }
        ],
    )
    focus = {field.name: field.value for field in embed.fields}["Sheet Focus"]

    assert "High Elves: 39/63" in focus
    assert "Boss 7:" not in focus
    assert "Boss 14:" not in focus


def test_fw_completion_tips_requires_saved_stage_rows():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_completion_tips_embed(
        post, _fw_static_data(), "banner_lords", []
    )

    assert embed.description == (
        "Completion tips need stage stars for this faction. Use Set Stages to save them, "
        "or use I'm Stuck for one specific stage."
    )


def test_fw_im_stuck_renders_exact_stage_hints_without_saved_progress():
    post = _fw_data("FW_H").posts[0]
    embed = service.build_faction_wars_stage_hints_embed(
        post, _fw_static_data(), "banner_lords", 21
    )

    assert "Stage condition: No revives." in embed.description
    assert (
        "Stage solver: No revives. • Shields and control • Ragash" in embed.description
    )
    assert (
        "Boss 21: Health-swap boss • Shield and control • Save cooldowns"
        in embed.description
    )
    assert "Manual boss wave" not in embed.description
    assert "Sustain pressure" not in embed.description
    assert "Valerie" not in embed.description
    assert "boss.example" not in embed.description


def test_fw_partial_stage_rows_do_not_produce_completion_tips():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {
            "category": "FW_H",
            "faction_key": "banner_lords",
            "stage_number": "21",
            "stars": "2",
        }
    ]
    embed = service.build_faction_wars_completion_tips_embed(
        post, _fw_static_data(), "banner_lords", stage_rows
    )

    assert embed.description == (
        "Stage tracking is incomplete for this faction. Use Set Stages to save all 21 "
        "stages, or use I'm Stuck for one specific stage."
    )


def test_fw_full_stage_rows_produce_completion_tips_below_three_stars():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {
            "category": "FW_H",
            "faction_key": "banner_lords",
            "stage_number": str(stage),
            "stars": "2" if stage == 21 else "3",
        }
        for stage in range(1, 22)
    ]
    embed = service.build_faction_wars_completion_tips_embed(
        post, _fw_static_data(), "banner_lords", stage_rows
    )

    assert "Stage 21: Stage condition: No revives." in embed.description
    assert "Stage 1:" not in embed.description
    assert "Manual boss wave" not in embed.description


def test_fw_boss_hints_only_render_on_exact_boss_stage():
    post = _fw_data("FW_N").posts[0]
    boss = service.build_faction_wars_stage_hints_embed(
        post, _fw_static_data(), "high_elves", 7
    )
    non_boss = service.build_faction_wars_stage_hints_embed(
        post, _fw_static_data(), "high_elves", 8
    )

    assert (
        "Boss 7: Turn meter boss • Decrease speed • Control the boss"
        in boss.description
    )
    assert "Boss 7:" not in non_boss.description


def test_fw_mark_perfect_upserts_all_stage_rows_as_three(monkeypatch):
    header = [
        "user_id",
        "category",
        "faction_key",
        "faction_name",
        "stage_number",
        "stars",
        "updated_at_utc",
    ]
    worksheet = FakeWorksheet()
    rows = [
        {
            "user_id": "123456",
            "category": "FW_H",
            "faction_key": "banner_lords",
            "faction_name": "Banner Lords",
            "stage_number": "1",
            "stars": "1",
            "updated_at_utc": "old",
        }
    ]

    async def stage_rows():
        return "ProgressUserFactionStageStars", rows

    async def get_ws(_sheet, _tab):
        return worksheet

    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service, "_fw_stage_star_rows", stage_rows)
    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(
        service, "_load_header", lambda *_args: asyncio.sleep(0, result=header)
    )
    monkeypatch.setattr(
        service,
        "acall_with_backoff",
        lambda func, *a, **kw: asyncio.sleep(
            0, result=func(*a, **{k: v for k, v in kw.items() if k != "attempts"})
        ),
    )

    asyncio.run(
        service.upsert_all_faction_wars_stage_stars(
            123456, "FW_H", "banner_lords", "Banner Lords", 3
        )
    )

    assert worksheet.updates[0][0] == "A2:G2"
    assert worksheet.updates[0][1][0][4:6] == ["1", "3"]
    assert len(worksheet.appended) == 20
    assert {row[0][5] for row in worksheet.appended} == {"3"}
    assert {row[0][4] for row in worksheet.appended} == {str(i) for i in range(2, 22)}


def test_fw_mark_perfect_with_existing_incomplete_stage_rows_makes_progress_complete():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {
            "category": "FW_H",
            "faction_key": "banner_lords",
            "stage_number": str(stage),
            "stars": "3",
        }
        for stage in range(1, 22)
    ]
    embed = service.build_faction_wars_progress_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "banner_lords",
                "counter_label": "Banner Lords",
                "current_value": "63",
                "goal_value": "63",
                "status": "complete",
            }
        ],
        stage_rows,
    )
    fields = {field.name: field.value for field in embed.fields}

    assert "Banner Lords: 63/63⭐" in fields["Sheet Finished"]
    assert "Banner Lords" not in fields["Sheet Focus"]


def test_fw_my_stars_uses_complete_stage_rows_over_manual_total():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {
            "category": "FW_H",
            "faction_key": "banner_lords",
            "stage_number": str(stage),
            "stars": "3" if stage <= 20 else "2",
        }
        for stage in range(1, 22)
    ]
    embed = service.build_faction_wars_stars_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "banner_lords",
                "counter_label": "Banner Lords",
                "current_value": "10",
                "goal_value": "63",
            }
        ],
        stage_rows,
    )

    assert "Banner Lords: 62/63⭐" in embed.description
    assert "Banner Lords: 10/63⭐" not in embed.description


def test_fw_my_stars_includes_complete_stage_faction_without_counter_row():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {
            "category": "FW_H",
            "faction_key": "orcs",
            "stage_number": str(stage),
            "stars": "3",
        }
        for stage in range(1, 22)
    ]
    embed = service.build_faction_wars_stars_embed(
        post, _fw_static_data(), [], stage_rows
    )

    assert "Orcs: 63/63⭐" in embed.description
    assert "Total saved stars: **63**" in embed.description


def test_fw_my_stars_partial_stage_rows_do_not_override_manual_total():
    post = _fw_data("FW_H").posts[0]
    stage_rows = [
        {"category": "FW_H", "faction_key": "orcs", "stage_number": "1", "stars": "2"}
    ]
    embed = service.build_faction_wars_stars_embed(
        post,
        _fw_static_data(),
        [
            {
                "category": "FW_H",
                "counter_key": "orcs",
                "counter_label": "Orcs",
                "current_value": "40",
                "goal_value": "63",
            }
        ],
        stage_rows,
    )

    assert "Orcs: 40/63⭐" in embed.description
    assert "Orcs: 2/63⭐" not in embed.description


def test_fw_set_stages_picker_uses_dedicated_copy(monkeypatch):
    data = _fw_data("FW_H")
    service.set_progress_guide_cache(data)
    service._FW_DATA_CACHE = _fw_static_data()
    interaction = FakeInteraction()

    asyncio.run(
        service.FactionWarsPanelButton("FW_H", "Set Stages", "setstages").callback(
            interaction
        )
    )

    embed = interaction.followup.sent[0]["embed"]
    assert embed.title == "Set Stage Stars"
    assert embed.description == "Choose a faction to save stage stars."
    assert embed.title != data.posts[0].conditions_title
    assert embed.description != "Choose a faction to view details."


def test_fw_new_stage_helper_titles_use_colons_not_em_dashes():
    post = _fw_data("FW_H").posts[0]
    stage_help = service.build_faction_wars_stage_hints_embed(
        post, _fw_static_data(), "banner_lords", 21
    )
    tips = service.build_faction_wars_completion_tips_embed(
        post,
        _fw_static_data(),
        "banner_lords",
        [
            {
                "category": "FW_H",
                "faction_key": "banner_lords",
                "stage_number": str(stage),
                "stars": "2" if stage == 21 else "3",
            }
            for stage in range(1, 22)
        ],
    )
    stuck_picker_title = service._embed_title("Banner Lords: Choose a Stage")

    assert stage_help.title == "Banner Lords: Stage 21 Help"
    assert tips.title == "Banner Lords: Completion Tips"
    assert stuck_picker_title == "Banner Lords: Choose a Stage"
    assert " — " not in stage_help.title
    assert " — " not in tips.title
    assert " — " not in stuck_picker_title

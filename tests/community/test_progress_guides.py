from __future__ import annotations

import asyncio

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
    def __init__(self, message_id=99):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, *, existing=None, missing=False, fetch_error=None):
        self.existing = existing
        self.missing = missing
        self.fetch_error = fetch_error
        self.sent = []

    async def fetch_message(self, message_id):
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.missing or self.existing is None:
            raise FakeDiscordNotFound("missing")
        return self.existing

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return FakeMessage(12345)


class FakeWorksheet:
    def __init__(self):
        self.updates = []

    def update(self, cell, values, **kwargs):
        self.updates.append((cell, values, kwargs))


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

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeInteraction:
    def __init__(self):
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()


async def _run(monkeypatch, data, channels, worksheet, *, refresh=True):
    async def load_data():
        return data

    monkeypatch.setattr(service, "load_progress_guide_data", load_data)
    monkeypatch.setattr(service, "get_milestones_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(service.discord, "NotFound", FakeDiscordNotFound)

    async def get_ws(sheet, tab):
        return worksheet

    async def fetch_values(sheet, tab):
        return [["category", "guide_panel_message_id", "help_panel_message_id"]]

    monkeypatch.setattr(service, "aget_worksheet", get_ws)
    monkeypatch.setattr(service, "afetch_values", fetch_values)

    async def resolve(_bot, channel_id):
        return channels.get(channel_id)

    async def call(func, *args, **kwargs):
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
    assert worksheet.updates == [("B2", [["12345"]], {"value_input_option": "RAW"})]


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
            _data(post_overrides={"guide_panel_message_id": "777"}),
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
            _data(post_overrides={"guide_panel_message_id": "777"}),
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
            _data(post_overrides={"guide_panel_message_id": "777"}),
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
    assert [cell for cell, _values, _kw in worksheet.updates] == ["B2"]


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
    assert "missing guide destination" in summary.skipped[0]


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
    assert [field.name for field in embed.fields] == ["First?", "Second?"]
    assert "source.example" not in embed.fields[0].value


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

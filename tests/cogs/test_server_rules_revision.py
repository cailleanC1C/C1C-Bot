import asyncio

from modules.ops import server_rules as base
from modules.ops import server_rules_interactive as interactive
from modules.ops import server_rules_revision as revision


def row(
    *,
    description="Answer",
    footer="",
    last_updated="2026-August-30",
    content_hash="",
    message_id="",
    review_status="",
    review_notes="",
):
    item = base.Row(
        2,
        [],
        {
            "message_key": "faq_one",
            "section": "faq",
            "order": "100",
            "enabled": "TRUE",
            "title": "Question",
            "description": description,
            "colour": "#607d8b",
            "thumbnail_url": "",
            "footer": footer,
            "message_id": message_id,
            "topic_key": "membership",
            "topic_title": "Membership and Clans",
            "review_status": review_status,
            "review_notes": review_notes,
            "last_updated": last_updated,
            "content_hash": content_hash,
        },
        True,
    )
    item.order = 100.0
    item.topic_key = "membership"
    item.topic_title = "Membership and Clans"
    return item


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


def test_content_hash_tracks_user_visible_content_not_management_fields():
    original = row(
        message_id="111111111111111111",
        review_status="draft",
        review_notes="note one",
    )
    management_change = row(
        message_id="222222222222222222",
        review_status="approved",
        review_notes="note two",
        last_updated="2026-September-01",
        content_hash="f" * 64,
    )
    assert revision._content_hash(original) == revision._content_hash(management_change)

    content_change = row(description="Changed answer")
    assert revision._content_hash(original) != revision._content_hash(content_change)


def test_blank_hash_seeds_hash_without_overwriting_existing_baseline_date():
    item = row(last_updated="2026-August-30", content_hash="")
    updates, errors = revision._plan_updates([item], today="2026-September-04")
    assert errors == []
    assert len(updates) == 1
    assert updates[0].last_updated == "2026-August-30"
    assert updates[0].content_hash == revision._content_hash(item)


def test_blank_hash_and_blank_date_initialise_both():
    item = row(last_updated="", content_hash="")
    updates, errors = revision._plan_updates([item], today="2026-September-04")
    assert errors == []
    assert updates[0].last_updated == "2026-September-04"
    assert updates[0].content_hash == revision._content_hash(item)


def test_unchanged_hash_does_not_write_revision_metadata():
    item = row()
    item.data["content_hash"] = revision._content_hash(item)
    updates, errors = revision._plan_updates([item], today="2026-September-04")
    assert errors == []
    assert updates == []


def test_changed_content_hash_advances_date():
    item = row(content_hash="a" * 64)
    updates, errors = revision._plan_updates([item], today="2026-September-04")
    assert errors == []
    assert updates[0].last_updated == "2026-September-04"
    assert updates[0].content_hash == revision._content_hash(item)


def test_invalid_revision_metadata_is_rejected():
    bad_date = row(last_updated="30/08/2026")
    bad_hash = row(content_hash="not-a-hash")
    updates, errors = revision._plan_updates(
        [bad_date, bad_hash], today="2026-September-04"
    )
    assert updates == []
    assert any("last_updated" in reason for _key, reason in errors)
    assert any("content_hash" in reason for _key, reason in errors)


def test_revision_footer_preserves_existing_footer():
    item = row(footer="C1C Community FAQ • Membership and Clans")
    embed, errors = revision.build_embed(item)
    assert errors == []
    assert embed.footer.text == (
        "C1C Community FAQ • Membership and Clans • "
        "Last updated: 2026-August-30"
    )


def test_revision_footer_is_added_when_sheet_footer_is_blank():
    embed, errors = revision.build_embed(row(footer=""))
    assert errors == []
    assert embed.footer.text == "Last updated: 2026-August-30"


def test_generated_question_list_uses_latest_date_in_group():
    older = row(last_updated="2026-August-30")
    newer = row(last_updated="2026-September-04")
    newer.data["message_key"] = "faq_two"
    newer.data["title"] = "Question two"
    topic = interactive.Topic(
        "membership", "Membership and Clans", (older, newer)
    )
    embed = revision.question_list(topic, ui())
    assert embed.footer.text == "Last updated: 2026-September-04"


def test_revision_batch_write_is_header_order_independent(monkeypatch):
    async def run():
        item = row()
        update = revision.RevisionUpdate(
            item, "2026-August-30", revision._content_hash(item)
        )
        payloads = []

        class Worksheet:
            def batch_update(self, payload):
                payloads.append(payload)

        async def worksheet(_tab):
            return Worksheet()

        async def call(func, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr(base, "_worksheet", worksheet)
        monkeypatch.setattr(base.sheets_core, "acall_with_backoff", call)
        await revision._write_updates(
            "Rules",
            {"content_hash": 2, "last_updated": 5},
            [update],
        )
        assert payloads == [
            [
                {"range": "F2", "values": [["2026-August-30"]]},
                {"range": "C2", "values": [[update.content_hash]]},
            ]
        ]

    asyncio.run(run())

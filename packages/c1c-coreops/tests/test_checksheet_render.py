from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "packages" / "c1c-coreops" / "src"
for path in (ROOT, SRC):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from c1c_coreops.render import (
    ChecksheetEmbedData,
    ChecksheetSheetEntry,
    ChecksheetTabEntry,
    build_checksheet_tabs_embed,
    build_checksheet_tabs_embeds,
)


def _field_text(embeds):
    return "\n".join(str(field.value) for embed in embeds for field in embed.fields)


def _assert_discord_embed_limits(embeds):
    assert embeds
    for embed in embeds:
        assert len(embed.fields) <= 25
        total = len(embed.title or "") + len(embed.description or "")
        total += len(getattr(embed.footer, "text", None) or "")
        for field in embed.fields:
            assert len(field.name) <= 256
            assert len(field.value) <= 1024
            total += len(field.name) + len(field.value)
        assert total <= 6000


def test_checksheet_long_field_value_is_split_safely_and_preserved():
    long_error = "schema-error-" * 120
    data = ChecksheetEmbedData(
        sheets=[
            ChecksheetSheetEntry(
                title="Templates",
                sheet_id="sheet-1",
                tabs=[
                    ChecksheetTabEntry(
                        name="Templates",
                        ok=False,
                        rows="42",
                        headers="Name, Body",
                        error=long_error,
                    )
                ],
            )
        ],
        bot_version="test",
    )

    embeds = build_checksheet_tabs_embeds(data)

    _assert_discord_embed_limits(embeds)
    values = [field.value for embed in embeds for field in embed.fields]
    assert any("schema-error-" in value for value in values)
    assert long_error in _field_text(embeds).replace("\n", "")


def test_checksheet_multiple_long_sections_produce_valid_embeds_with_full_text():
    errors = [f"section-{idx}-" * 180 for idx in range(8)]
    sheets = [
        ChecksheetSheetEntry(
            title=f"Sheet {idx}",
            sheet_id=f"sheet-{idx}",
            tabs=[
                ChecksheetTabEntry(
                    name=f"Tab {idx}",
                    ok=False,
                    rows="n/a",
                    headers="Expected, Headers",
                    error=error,
                )
            ],
        )
        for idx, error in enumerate(errors)
    ]

    embeds = build_checksheet_tabs_embeds(ChecksheetEmbedData(sheets=sheets, bot_version="test"))

    _assert_discord_embed_limits(embeds)
    combined = _field_text(embeds).replace("\n", "")
    for error in errors:
        assert error in combined
    assert len(embeds) > 1 or sum(len(embed.fields) for embed in embeds) > len(sheets)


def test_checksheet_many_errors_never_exceed_field_count():
    sheets = [
        ChecksheetSheetEntry(
            title=f"Sheet {idx}",
            sheet_id=f"sheet-{idx}",
            tabs=[
                ChecksheetTabEntry(
                    name=f"Tab {idx}",
                    ok=False,
                    rows="1",
                    headers="H",
                    error="error details " * 90,
                )
            ],
        )
        for idx in range(40)
    ]

    embeds = build_checksheet_tabs_embeds(ChecksheetEmbedData(sheets=sheets, bot_version="test"))

    _assert_discord_embed_limits(embeds)
    assert len(embeds) >= 2


def test_checksheet_short_output_matches_legacy_single_embed_shape():
    data = ChecksheetEmbedData(
        sheets=[
            ChecksheetSheetEntry(
                title="Recruitment",
                sheet_id="sheet-short",
                tabs=[
                    ChecksheetTabEntry(name="Templates", ok=True, rows="3", headers="Name, Body")
                ],
            )
        ],
        bot_version="test",
    )

    embeds = build_checksheet_tabs_embeds(data)
    legacy = build_checksheet_tabs_embed(data)

    _assert_discord_embed_limits(embeds)
    assert len(embeds) == 1
    assert embeds[0].to_dict() == legacy.to_dict()
    assert embeds[0].fields[0].name == "Google Sheets"
    assert embeds[0].fields[0].value == "Public client"
    assert "✅ Templates — 3 rows" in embeds[0].fields[1].value

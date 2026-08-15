from __future__ import annotations

import asyncio

from modules.community.live_arena import round_overview


def run(awaitable):
    return asyncio.run(awaitable)


def test_standings_copy_contract_uses_one_mobile_safe_line_template(monkeypatch):
    rows = [
        ["message_key", "title", "description", "color_hex", "active", "notes"],
        *[
            [
                key,
                ("x" if not expected else ""),
                "" if not expected else " ".join(f"{{{name}}}" for name in sorted(expected)),
                "#1A73E8",
                "TRUE",
                "",
            ]
            for key, expected in round_overview._COPY_CONTRACTS.items()
        ],
    ]
    for row in rows[1:]:
        if row[0] == "round_overview_standing_line":
            row[2] = "**#{rank} · {record}** {player_mention}"

    async def fake_config(_sheet_id):
        return {"MESSAGES_TAB": "MESSAGES"}, {}

    async def fake_values(_sheet_id, _tab):
        return rows

    monkeypatch.setattr(round_overview, "load_pr5_config", fake_config)
    monkeypatch.setattr(round_overview, "afetch_values", fake_values)
    round_overview.clear_copy_cache()

    templates = run(round_overview._templates("sheet-live"))

    assert templates["round_overview_standing_line"].description == (
        "**#{rank} · {record}** {player_mention}"
    )
    assert "round_overview_standing_player" not in templates
    assert "round_overview_standing_record" not in templates

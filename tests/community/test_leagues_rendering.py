from __future__ import annotations

from types import SimpleNamespace

from modules.community.leagues.cog import LeaguesCog
from modules.community.leagues.config import LeagueBundle, LeagueSpec


def _bundle(slug: str, name: str) -> LeagueBundle:
    spec = LeagueSpec(key=f"{slug}_h", slug=slug, kind="header", index=None, sheet_name="Tab", cell_range="A1:B2")
    return LeagueBundle(slug=slug, display_name=name, header=spec, boards=[spec])


def test_build_announcement_uses_plain_title_and_bold_league_names() -> None:
    cog = LeaguesCog(SimpleNamespace())
    bundles = [
        _bundle("legendary", "Legendary League"),
        _bundle("rising", "Rising Stars League"),
        _bundle("storm", "Stormforged League"),
    ]
    text = cog._build_announcement(
        bundles,
        {
            "legendary": "https://discord.test/legendary",
            "rising": "https://discord.test/rising",
            "storm": "https://discord.test/storm",
        },
    )

    assert "# Shifting Echoes from the C1CLeague …" in text
    assert "@C1CLeague" not in text
    assert "🦅 **Legendary League**" in text
    assert "🌟 **Rising Stars League**" in text
    assert "⚡ **Stormforged League**" in text
    assert "🔹 **Legendary League** – [Jump to this week’s update](https://discord.test/legendary)" in text


def test_league_role_mention_prefers_role_id(monkeypatch) -> None:
    cog = LeaguesCog(SimpleNamespace())
    monkeypatch.setenv("C1C_LEAGUE_ROLE_ID", "12345")
    assert cog._league_role_mention() == "<@&12345>"

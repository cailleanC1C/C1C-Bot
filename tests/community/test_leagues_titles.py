import datetime as dt
from types import SimpleNamespace

from modules.community.leagues.cog import LeaguesCog


def _bundle(slug: str, display_name: str):
    return SimpleNamespace(slug=slug, display_name=display_name)


def test_stormforged_title_uses_previous_calendar_week() -> None:
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=dt.timezone.utc)

    title = LeaguesCog._league_title(_bundle("storm", "Stormforged League"), now)

    assert title == "Stormforged League – Calendar Week 37 Results"


def test_stormforged_title_handles_year_boundary() -> None:
    now = dt.datetime(2027, 1, 3, 10, 0, tzinfo=dt.timezone.utc)

    title = LeaguesCog._league_title(_bundle("storm", "Stormforged League"), now)

    assert title == "Stormforged League – Calendar Week 52 Results"


def test_legendary_and_rising_keep_posting_date() -> None:
    now = dt.datetime(2026, 9, 20, 10, 0, tzinfo=dt.timezone.utc)

    assert LeaguesCog._league_title(
        _bundle("legendary", "Legendary League"), now
    ) == "Legendary League – Weekly Update 2026-09-20"
    assert LeaguesCog._league_title(
        _bundle("rising", "Rising Stars League"), now
    ) == "Rising Stars League – Weekly Update 2026-09-20"

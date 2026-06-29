from modules.recruitment import clan_ads
from shared.sheets import recruitment


def test_clan_ads_bracket_resolves_from_progression_header(monkeypatch):
    monkeypatch.setattr(
        recruitment,
        "get_clan_header_map",
        lambda: {"clan_tag": 0, "clan_name": 1, "bracket": 2},
    )
    record = recruitment.RecruitmentClanRecord(
        row=("C1CE", "Clan One", "Late Game"),
        open_spots=3,
        inactives=0,
        reserved=0,
        roster="",
    )

    data = clan_ads.clan_data(record)

    assert data.bracket == "Late Game"


def test_clan_ads_bracket_falls_back_to_record_roster(monkeypatch):
    monkeypatch.setattr(
        recruitment,
        "get_clan_header_map",
        lambda: {"clan_tag": 0, "clan_name": 1},
    )
    record = recruitment.RecruitmentClanRecord(
        row=("C1CE", "Clan One"),
        open_spots=1,
        inactives=0,
        reserved=0,
        roster="Elite End Game",
    )

    data = clan_ads.clan_data(record)

    assert data.bracket == "Elite End Game"


def test_clan_ads_run_reports_when_all_clans_fail_required_field_resolution(
    monkeypatch,
):
    import asyncio

    class Bot:
        pass

    class Channel:
        pass

    async def fake_load_config(*args, **kwargs):
        return clan_ads.Config(
            messages_tab="ClanAdMessages",
            rules_tab="ClanAdRules",
            channel_id=123,
            raid_role_id="",
            notification="",
            interval_hours=24,
            last_posted="",
        )

    async def fake_resolve_channel(*args, **kwargs):
        return Channel()

    async def fake_load_rules(*args, **kwargs):
        return {}

    async def fake_load_messages(*args, **kwargs):
        return {}, None, {}

    async def fake_fetch_clan_records(*args, **kwargs):
        return [
            recruitment.RecruitmentClanRecord(
                row=("C1CE",),
                open_spots=1,
                inactives=0,
                reserved=0,
                roster="Late Game",
            )
        ]

    async def fake_send_log_message(*args, **kwargs):
        return None

    monkeypatch.setattr(clan_ads.feature_flags, "is_enabled", lambda _key: True)
    monkeypatch.setattr(clan_ads, "load_config", fake_load_config)
    monkeypatch.setattr(clan_ads, "_resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(clan_ads, "load_rules", fake_load_rules)
    monkeypatch.setattr(clan_ads, "load_messages", fake_load_messages)
    monkeypatch.setattr(clan_ads.sheets, "fetch_clan_records", fake_fetch_clan_records)
    monkeypatch.setattr(
        clan_ads.runtime_helpers, "send_log_message", fake_send_log_message
    )
    monkeypatch.setattr(recruitment, "get_clan_header_map", lambda: {"clan_tag": 0})

    result = asyncio.run(clan_ads.run(Bot(), scheduled=False))

    assert result["message"] == (
        "Clan ads could not evaluate any clans because required clan data fields are missing. "
        "Check the bot logging channel for details."
    )
    assert result["skipped"] == 1

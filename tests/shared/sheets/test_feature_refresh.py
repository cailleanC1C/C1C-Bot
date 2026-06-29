import asyncio

from shared.cache import telemetry
from shared.sheets import cache_service, feature_refresh, runtime


EXPECTED_NEW_BUCKETS = {
    "clan_ad_messages",
    "clan_ad_rules",
    "reservations",
    "recruitment_reports",
    "c1c_ad",
    "c1c_ad_text",
    "cleanup_rules",
    "keepalive_targets",
    "whoweare_role_map",
    "reset_reminders",
    "shard_mercy",
    "shard_clans",
    "shard_reminders",
    "shard_share_copy",
    "shard_voice_targets",
}


def _clear_buckets() -> None:
    cache_service.cache._buckets.clear()


def test_feature_refresh_buckets_register_without_duplicates() -> None:
    _clear_buckets()

    feature_refresh.register_cache_buckets()
    feature_refresh.register_cache_buckets()

    names = telemetry.list_buckets()
    for name in EXPECTED_NEW_BUCKETS:
        assert name in names
    assert len(names) == len(set(names))


def test_default_registration_includes_existing_and_new_buckets(monkeypatch) -> None:
    _clear_buckets()
    monkeypatch.setattr("shared.sheets.onboarding.register_cache_buckets", lambda: None)
    monkeypatch.setattr(
        "shared.sheets.onboarding_questions.register_cache_buckets", lambda: None
    )
    monkeypatch.setattr(
        "shared.sheets.config_service.register_cache_buckets", lambda: None
    )
    monkeypatch.setattr("shared.sheets.fusion.register_cache_buckets", lambda: None)
    monkeypatch.setattr(
        "shared.sheets.reaction_roles.register_cache_buckets", lambda: None
    )

    runtime.register_default_cache_buckets()

    names = telemetry.list_buckets()
    assert "clans" in names
    assert "templates" in names
    assert "clan_ad_messages" in names
    assert "clan_ad_rules" in names
    assert "shard_mercy" in names
    assert len(names) == len(set(names))


def test_clan_ad_buckets_use_config_pointers(monkeypatch) -> None:
    _clear_buckets()
    requested_keys = []
    fetched_tabs = []

    def fake_config_value(key, default=None, *, force=False):
        requested_keys.append((key, force))
        return {
            "clan_ad_messages_tab": "AdMessages",
            "clan_ad_rules_tab": "AdRules",
        }.get(key, default)

    async def fake_fetch(sheet_id, tab_name):
        fetched_tabs.append((sheet_id, tab_name))
        return [["header"], ["row"]]

    monkeypatch.setattr("shared.sheets.recruitment.get_config_value", fake_config_value)
    monkeypatch.setattr(
        "shared.sheets.recruitment.get_recruitment_sheet_id", lambda: "sheet123"
    )
    monkeypatch.setattr("shared.sheets.async_core.afetch_values", fake_fetch)
    feature_refresh.register_cache_buckets()

    asyncio.run(cache_service.cache.refresh_now("clan_ad_messages", actor="test"))
    asyncio.run(cache_service.cache.refresh_now("clan_ad_rules", actor="test"))

    assert ("clan_ad_messages_tab", True) in requested_keys
    assert ("clan_ad_rules_tab", True) in requested_keys
    assert ("sheet123", "AdMessages") in fetched_tabs
    assert ("sheet123", "AdRules") in fetched_tabs


def test_missing_config_pointer_is_readable_failure(monkeypatch) -> None:
    _clear_buckets()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(cache_service.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        "shared.sheets.recruitment.get_config_value",
        lambda key, default=None, *, force=False: default,
    )
    feature_refresh.register_cache_buckets()

    result = asyncio.run(telemetry.refresh_now("clan_ad_rules", actor="test"))

    assert not result.ok
    assert "missing Config key clan_ad_rules_tab" in (result.error or "")


def test_bad_tab_name_is_readable_failure(monkeypatch) -> None:
    _clear_buckets()

    async def no_sleep(_seconds):
        return None

    async def bad_fetch(sheet_id, tab_name):
        raise RuntimeError("not found")

    monkeypatch.setattr(cache_service.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        "shared.sheets.recruitment.get_config_value",
        lambda key, default=None, *, force=False: "BogusTab",
    )
    monkeypatch.setattr(
        "shared.sheets.recruitment.get_recruitment_sheet_id", lambda: "sheet123"
    )
    monkeypatch.setattr("shared.sheets.async_core.afetch_values", bad_fetch)
    feature_refresh.register_cache_buckets()

    result = asyncio.run(telemetry.refresh_now("clan_ad_messages", actor="test"))

    assert not result.ok
    assert "could not read configured tab BogusTab" in (result.error or "")


def test_new_bucket_labels_are_readable() -> None:
    from c1c_coreops.cog import _format_bucket_label

    assert _format_bucket_label("clan_ad_messages") == "Clan Ad Messages"
    assert _format_bucket_label("clan_ad_rules") == "Clan Ad Rules"
    assert _format_bucket_label("c1c_ad") == "C1C Ad"
    assert _format_bucket_label("whoweare_role_map") == "WhoWeAre Role Map"


def test_added_config_keys_are_declared_in_expected_sources() -> None:
    from modules.community import reset_reminders
    from modules.community.shard_tracker import data as shard_data
    from modules.housekeeping import c1c_ad, cleanup, keepalive
    from modules.recruitment import clan_ads
    from shared.sheets import recruitment

    assert "clan_ad_messages_tab" in clan_ads.CONFIG_KEYS
    assert "clan_ad_rules_tab" in clan_ads.CONFIG_KEYS
    assert recruitment.get_reservations_tab_name.__name__ == "get_reservations_tab_name"
    assert recruitment.get_reports_tab_name.__name__ == "get_reports_tab_name"
    assert recruitment.get_role_map_tab_name.__name__ == "get_role_map_tab_name"
    assert "C1C_AD_TAB" in c1c_ad.CONFIG_KEYS
    assert "C1C_AD_TEXT_TAB" in c1c_ad.CONFIG_KEYS
    assert cleanup.CONFIG_TAB == "HOUSEKEEPING_CLEANUP_TAB"
    assert keepalive.CONFIG_TAB == "HOUSEKEEPING_KEEPALIVE_TAB"
    assert reset_reminders.scheduler._RESET_REMINDER_TAB_KEY == "RESET_REMINDER_TAB"

    shard_config_keys = {
        "shard_mercy_tab",
        "SHARD_CLANS_TAB",
        "SHARD_REMINDER_TAB",
        "shard_share_copy_tab",
        "shard_share_voice_targets_tab",
    }
    import inspect

    shard_source = "\n".join(
        inspect.getsource(obj)
        for obj in (
            shard_data.ShardSheetStore.get_config,
            shard_data.ShardSheetStore._load_shard_clans,
            shard_data.ShardSheetStore.get_sent_weekly_reminder_keys,
            shard_data.ShardSheetStore.get_share_copy_rows,
            shard_data.ShardSheetStore.get_share_voice_target_rows,
        )
    )
    for key in shard_config_keys:
        assert key in shard_source

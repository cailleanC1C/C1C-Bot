import datetime as dt
import sys
import time
import types
from pathlib import Path

import discord
import pytest
from discord.ext import commands


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[3]
    src = root / "packages" / "c1c-coreops" / "src"
    for path in (root, src):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


_ensure_src_on_path()

rt_stub = types.SimpleNamespace(monotonic_ms=lambda: int(time.monotonic() * 1000))
sys.modules.setdefault("modules.common.runtime", rt_stub)

import c1c_coreops.cog as coreops_cog
from c1c_coreops.cog import CoreOpsCog, _EnvEntry, UTC, _MAX_EMBED_LENGTH, _embed_length
from shared import config as shared_config
from shared.testing.environment import apply_required_test_environment


apply_required_test_environment()


@pytest.fixture()
def bot():
    instance = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    yield instance


def _entry(key: str, value: object) -> _EnvEntry:
    display = "—" if value is None else str(value)
    return _EnvEntry(key=key, normalized=value, display=display)


def test_env_overview_grouping(monkeypatch, bot):
    cog = CoreOpsCog(bot)

    entries = {
        "BOT_NAME": _entry("BOT_NAME", "TestBot"),
        "BOT_VERSION": _entry("BOT_VERSION", "1.2.3"),
        "ENV_NAME": _entry("ENV_NAME", "dev"),
        "GUILD_IDS": _entry("GUILD_IDS", [1234]),
        "LOG_CHANNEL_ID": _entry("LOG_CHANNEL_ID", 2345),
        "WELCOME_CHANNEL_ID": _entry("WELCOME_CHANNEL_ID", 2346),
        "WELCOME_GENERAL_CHANNEL_ID": _entry("WELCOME_GENERAL_CHANNEL_ID", 2347),
        "NOTIFY_CHANNEL_ID": _entry("NOTIFY_CHANNEL_ID", 2348),
        "PROMO_CHANNEL_ID": _entry("PROMO_CHANNEL_ID", 2349),
        "RECRUITERS_CHANNEL_ID": _entry("RECRUITERS_CHANNEL_ID", 2350),
        "RECRUITERS_THREAD_ID": _entry("RECRUITERS_THREAD_ID", 3456),
        "REPORT_RECRUITERS_DEST_ID": _entry("REPORT_RECRUITERS_DEST_ID", 2351),
        "PANEL_FIXED_THREAD_ID": _entry("PANEL_FIXED_THREAD_ID", 2352),
        "PANEL_THREAD_MODE": _entry("PANEL_THREAD_MODE", "same"),
        "ROLEMAP_CHANNEL_ID": _entry("ROLEMAP_CHANNEL_ID", 4567),
        "ADMIN_ROLE_IDS": _entry("ADMIN_ROLE_IDS", [1111]),
        "STAFF_ROLE_IDS": _entry("STAFF_ROLE_IDS", [2222]),
        "LEAD_ROLE_IDS": _entry("LEAD_ROLE_IDS", [3333]),
        "RECRUITER_ROLE_IDS": _entry("RECRUITER_ROLE_IDS", [4444]),
        "NOTIFY_PING_ROLE_ID": _entry("NOTIFY_PING_ROLE_ID", 5555),
        "ROLEMAP_TAB": _entry("ROLEMAP_TAB", "WhoWeAre"),
        "RECRUITMENT_SHEET_ID": _entry("RECRUITMENT_SHEET_ID", "abc"),
        "ONBOARDING_TAB": _entry("ONBOARDING_TAB", "Onboarding"),
        "SERVER_MAP_CHANNEL_ID": _entry("SERVER_MAP_CHANNEL_ID", 9876),
        "SERVER_MAP_MESSAGE_ID_1": _entry("SERVER_MAP_MESSAGE_ID_1", "13579"),
    }

    monkeypatch.setattr(coreops_cog, "get_feature_toggles", lambda: {"example_toggle": True})
    monkeypatch.setattr(
        type(cog._id_resolver), "resolve", lambda _self, _bot, _sid: "#channel"
    )

    embeds, warnings, _ = cog._build_env_embeds(
        bot_name="TestBot",
        env="dev",
        version="1.2.3",
        guild_name="Guild",
        entries=entries,
        sheet_sections=[],
        footer_text="footer",
        timestamp=dt.datetime.now(UTC),
    )

    assert len(embeds) == 4
    assert [field.name for field in embeds[1].fields][:3] == [
        "Channels",
        "Threads",
        "Other",
    ]

    channels_value = "\n".join(field.value for field in embeds[1].fields)
    assert "ROLEMAP_CHANNEL_ID" in channels_value

    roles_value = "\n".join(field.value for field in embeds[2].fields)
    assert "ROLEMAP_CHANNEL_ID" not in roles_value

    sheet_fields = [field.name for field in embeds[3].fields]
    assert sheet_fields[:3] == ["Sheets", "Tabs", "Config"]
    assert "Warnings" not in [field.name for field in embeds[0].fields]
    assert warnings == []


def test_env_overview_warnings(monkeypatch, bot):
    cog = CoreOpsCog(bot)
    entries = {
        "BOT_NAME": _entry("BOT_NAME", "TestBot"),
        "BOT_VERSION": _entry("BOT_VERSION", "1.2.3"),
        "ENV_NAME": _entry("ENV_NAME", "dev"),
        "GUILD_IDS": _entry("GUILD_IDS", [1]),
        "LOG_CHANNEL_ID": _entry("LOG_CHANNEL_ID", 2),
        "WELCOME_CHANNEL_ID": _entry("WELCOME_CHANNEL_ID", 3),
        "WELCOME_GENERAL_CHANNEL_ID": _entry("WELCOME_GENERAL_CHANNEL_ID", 4),
        "NOTIFY_CHANNEL_ID": _entry("NOTIFY_CHANNEL_ID", 5),
        "PROMO_CHANNEL_ID": _entry("PROMO_CHANNEL_ID", 6),
        "RECRUITERS_CHANNEL_ID": _entry("RECRUITERS_CHANNEL_ID", 7),
        "RECRUITERS_THREAD_ID": _entry("RECRUITERS_THREAD_ID", 8),
        "REPORT_RECRUITERS_DEST_ID": _entry("REPORT_RECRUITERS_DEST_ID", 9),
        "PANEL_FIXED_THREAD_ID": _entry("PANEL_FIXED_THREAD_ID", 10),
        "PANEL_THREAD_MODE": _entry("PANEL_THREAD_MODE", "same"),
        "ROLEMAP_CHANNEL_ID": _entry("ROLEMAP_CHANNEL_ID", 11),
        "SERVER_MAP_CHANNEL_ID": _entry("SERVER_MAP_CHANNEL_ID", 12),
        "LOGGING_CHANNEL_ID": _entry("LOGGING_CHANNEL_ID", None),
        "ADMIN_ROLE_IDS": _entry("ADMIN_ROLE_IDS", [13]),
        "STAFF_ROLE_IDS": _entry("STAFF_ROLE_IDS", [14]),
        "LEAD_ROLE_IDS": _entry("LEAD_ROLE_IDS", [15]),
        "RECRUITER_ROLE_IDS": _entry("RECRUITER_ROLE_IDS", [16]),
        "NOTIFY_PING_ROLE_ID": _entry("NOTIFY_PING_ROLE_ID", 17),
        "CLAN_ROLE_IDS": _entry("CLAN_ROLE_IDS", [9999]),
        "RECRUITMENT_SHEET_ID": _entry("RECRUITMENT_SHEET_ID", "abc"),
        "ONBOARDING_TAB": _entry("ONBOARDING_TAB", "Onboarding"),
    }

    monkeypatch.setattr(coreops_cog, "get_feature_toggles", lambda: {})
    monkeypatch.setattr(
        type(cog._id_resolver),
        "resolve",
        lambda _self, _bot, snowflake: "(not found)" if snowflake == 9999 else "#channel",
    )

    embeds, warnings, keys = cog._build_env_embeds(
        bot_name="TestBot",
        env="dev",
        version="1.2.3",
        guild_name="Guild",
        entries=entries,
        sheet_sections=[],
        footer_text="footer",
        timestamp=dt.datetime.now(UTC),
    )

    warning_field = next(field for field in embeds[0].fields if field.name == "Warnings")
    assert "⚠" in warning_field.value
    assert "LOGGING_CHANNEL_ID" in warning_field.value
    assert warnings
    assert "CLAN_ROLE_IDS" in keys


def test_env_overview_splits_large_pages(monkeypatch, bot):
    cog = CoreOpsCog(bot)

    entries = {
        "BOT_NAME": _entry("BOT_NAME", "TestBot"),
        "BOT_VERSION": _entry("BOT_VERSION", "1.2.3"),
        "ENV_NAME": _entry("ENV_NAME", "dev"),
        "GUILD_IDS": _entry("GUILD_IDS", [1234]),
        "LOG_CHANNEL_ID": _entry("LOG_CHANNEL_ID", 2345),
        "WELCOME_CHANNEL_ID": _entry("WELCOME_CHANNEL_ID", 2346),
        "WELCOME_GENERAL_CHANNEL_ID": _entry("WELCOME_GENERAL_CHANNEL_ID", 2347),
        "NOTIFY_CHANNEL_ID": _entry("NOTIFY_CHANNEL_ID", 2348),
        "PROMO_CHANNEL_ID": _entry("PROMO_CHANNEL_ID", 2349),
        "RECRUITERS_CHANNEL_ID": _entry("RECRUITERS_CHANNEL_ID", 2350),
        "RECRUITERS_THREAD_ID": _entry("RECRUITERS_THREAD_ID", 3456),
        "REPORT_RECRUITERS_DEST_ID": _entry("REPORT_RECRUITERS_DEST_ID", 2351),
        "PANEL_FIXED_THREAD_ID": _entry("PANEL_FIXED_THREAD_ID", 2352),
        "PANEL_THREAD_MODE": _entry("PANEL_THREAD_MODE", "same"),
        "ROLEMAP_CHANNEL_ID": _entry("ROLEMAP_CHANNEL_ID", 4567),
        "SERVER_MAP_CHANNEL_ID": _entry("SERVER_MAP_CHANNEL_ID", 9876),
        "ADMIN_ROLE_IDS": _entry("ADMIN_ROLE_IDS", [1111]),
        "STAFF_ROLE_IDS": _entry("STAFF_ROLE_IDS", [2222]),
        "LEAD_ROLE_IDS": _entry("LEAD_ROLE_IDS", [3333]),
        "RECRUITER_ROLE_IDS": _entry("RECRUITER_ROLE_IDS", [4444]),
        "NOTIFY_PING_ROLE_ID": _entry("NOTIFY_PING_ROLE_ID", 5555),
        "RECRUITMENT_SHEET_ID": _entry("RECRUITMENT_SHEET_ID", "abc"),
        "ONBOARDING_TAB": _entry("ONBOARDING_TAB", "Onboarding"),
    }

    for idx in range(400):
        entries[f"DYNAMIC_ROLE_{idx}"] = _entry(
            f"DYNAMIC_ROLE_{idx}", 9000 + idx
        )

    monkeypatch.setattr(coreops_cog, "get_feature_toggles", lambda: {})
    monkeypatch.setattr(type(cog._id_resolver), "resolve", lambda *_: "#role")

    embeds, warnings, _ = cog._build_env_embeds(
        bot_name="TestBot",
        env="dev",
        version="1.2.3",
        guild_name="Guild",
        entries=entries,
        sheet_sections=[],
        footer_text="footer",
        timestamp=dt.datetime.now(UTC),
    )

    assert len(embeds) >= 4
    total = len(embeds)
    expected_titles = [
        f"TestBot · env: dev · Page {page}/{total}" for page in range(1, total + 1)
    ]
    assert [embed.title for embed in embeds] == expected_titles
    for index, embed in enumerate(embeds, start=1):
        assert _embed_length(embed) <= _MAX_EMBED_LENGTH
        assert f"Page {index}/{total}" in (embed.footer.text or "")
    assert warnings == []


def test_env_overview_no_split_renumbers(monkeypatch, bot):
    cog = CoreOpsCog(bot)

    entries = {
        "BOT_NAME": _entry("BOT_NAME", "TestBot"),
        "BOT_VERSION": _entry("BOT_VERSION", "1.2.3"),
        "ENV_NAME": _entry("ENV_NAME", "dev"),
        "GUILD_IDS": _entry("GUILD_IDS", [1234]),
        "LOG_CHANNEL_ID": _entry("LOG_CHANNEL_ID", 2345),
        "WELCOME_CHANNEL_ID": _entry("WELCOME_CHANNEL_ID", 2346),
        "WELCOME_GENERAL_CHANNEL_ID": _entry("WELCOME_GENERAL_CHANNEL_ID", 2347),
        "NOTIFY_CHANNEL_ID": _entry("NOTIFY_CHANNEL_ID", 2348),
        "PROMO_CHANNEL_ID": _entry("PROMO_CHANNEL_ID", 2349),
        "RECRUITERS_CHANNEL_ID": _entry("RECRUITERS_CHANNEL_ID", 2350),
        "RECRUITERS_THREAD_ID": _entry("RECRUITERS_THREAD_ID", 3456),
        "REPORT_RECRUITERS_DEST_ID": _entry("REPORT_RECRUITERS_DEST_ID", 2351),
        "PANEL_FIXED_THREAD_ID": _entry("PANEL_FIXED_THREAD_ID", 2352),
        "PANEL_THREAD_MODE": _entry("PANEL_THREAD_MODE", "same"),
        "ROLEMAP_CHANNEL_ID": _entry("ROLEMAP_CHANNEL_ID", 4567),
        "SERVER_MAP_CHANNEL_ID": _entry("SERVER_MAP_CHANNEL_ID", 9876),
        "ADMIN_ROLE_IDS": _entry("ADMIN_ROLE_IDS", [1111]),
        "STAFF_ROLE_IDS": _entry("STAFF_ROLE_IDS", [2222]),
        "LEAD_ROLE_IDS": _entry("LEAD_ROLE_IDS", [3333]),
        "RECRUITER_ROLE_IDS": _entry("RECRUITER_ROLE_IDS", [4444]),
        "NOTIFY_PING_ROLE_ID": _entry("NOTIFY_PING_ROLE_ID", 5555),
        "ROLEMAP_TAB": _entry("ROLEMAP_TAB", "WhoWeAre"),
        "RECRUITMENT_SHEET_ID": _entry("RECRUITMENT_SHEET_ID", "abc"),
        "ONBOARDING_TAB": _entry("ONBOARDING_TAB", "Onboarding"),
    }

    monkeypatch.setattr(coreops_cog, "get_feature_toggles", lambda: {})
    monkeypatch.setattr(type(cog._id_resolver), "resolve", lambda *_: "#channel")

    embeds, warnings, _ = cog._build_env_embeds(
        bot_name="TestBot",
        env="dev",
        version="1.2.3",
        guild_name="Guild",
        entries=entries,
        sheet_sections=[],
        footer_text="footer",
        timestamp=dt.datetime.now(UTC),
    )

    assert len(embeds) == 4
    expected_titles = [
        f"TestBot · env: dev · Page {page}/4" for page in range(1, len(embeds) + 1)
    ]
    assert [embed.title for embed in embeds] == expected_titles
    for index, embed in enumerate(embeds, start=1):
        assert _embed_length(embed) <= _MAX_EMBED_LENGTH
        assert f"Page {index}/4" in (embed.footer.text or "")
    assert warnings == []


def test_feature_defaults_enable_new_toggles():
    shared_config.update_feature_flags_snapshot({})

    assert shared_config.features.housekeeping_enabled is True
    assert shared_config.features.mirralith_overview_enabled is True
    assert shared_config.features.ops_permissions_enabled is True
    assert shared_config.features.promo_watcher_enabled is True
    assert shared_config.features.resume_command_enabled is True
    assert shared_config.features.welcome_watcher_enabled is True
    assert shared_config.features.shard_tracker_enabled is False

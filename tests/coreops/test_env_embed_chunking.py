import datetime as dt

import discord
from discord.ext import commands

from c1c_coreops.cog import (
    CoreOpsCog,
    _EnvEntry,
    _MAX_EMBED_LENGTH,
    _chunk_field_lines,
    _embed_length,
)


def test_chunk_field_lines_single_chunk():
    lines = ["alpha = 1", "beta = 2", "gamma = 3"]

    chunks = _chunk_field_lines("CHANNELS", lines)

    assert len(chunks) == 1
    name, value = chunks[0]
    assert name == "CHANNELS"
    for line in lines:
        assert line in value
    assert value == "\n".join(lines)


def test_chunk_field_lines_multiple_chunks():
    lines = [f"line-{idx} = value" for idx in range(20)]

    chunks = _chunk_field_lines("CHANNELS", lines, soft_limit=50)

    assert len(chunks) > 1
    total = len(chunks)
    expected_labels = [f"CHANNELS ({idx}/{total})" for idx in range(1, total + 1)]
    assert [label for label, _ in chunks] == expected_labels
    reconstructed = []
    for _, value in chunks:
        reconstructed.extend(value.split("\n"))
    assert reconstructed == lines


def test_env_embeds_chunk_large_fields(monkeypatch):
    intents = discord.Intents.none()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    cog = CoreOpsCog(bot)

    monkeypatch.setattr("c1c_coreops.cog.get_feature_toggles", lambda: {})

    def simple_line(key, entry, warnings, warning_keys, *, treat_ids=True):
        value = entry.display if entry else "—"
        return [f"**{key}**", f"  {value}"]

    monkeypatch.setattr(cog, "_format_entry_lines", simple_line)

    def make_entry(key: str, value: str) -> _EnvEntry:
        return _EnvEntry(key=key, normalized=value, display=value)

    entries = {
        "BOT_NAME": make_entry("BOT_NAME", "bot"),
        "BOT_VERSION": make_entry("BOT_VERSION", "v1"),
        "ENV_NAME": make_entry("ENV_NAME", "test"),
    }

    for key in [
        "GUILD_IDS",
        "LOG_CHANNEL_ID",
        "WELCOME_CHANNEL_ID",
        "WELCOME_GENERAL_CHANNEL_ID",
        "NOTIFY_CHANNEL_ID",
        "PROMO_CHANNEL_ID",
        "RECRUITERS_CHANNEL_ID",
        "RECRUITERS_THREAD_ID",
        "REPORT_RECRUITERS_DEST_ID",
        "PANEL_FIXED_THREAD_ID",
        "PANEL_THREAD_MODE",
        "ROLEMAP_CHANNEL_ID",
        "SERVER_MAP_CHANNEL_ID",
    ]:
        entries[key] = make_entry(key, f"{key.lower()}-value")

    for idx in range(80):
        entries[f"EXTRA_CHANNEL_{idx}"] = make_entry(
            f"EXTRA_CHANNEL_{idx}", f"channel-{idx}"
        )
    for idx in range(30):
        entries[f"EXTRA_THREAD_{idx}"] = make_entry(
            f"EXTRA_THREAD_{idx}", f"thread-{idx}"
        )

    for idx in range(60):
        entries[f"ROLE_{idx}"] = make_entry(f"ROLE_{idx}", str(idx))

    for idx in range(40):
        entries[f"SHEET_{idx}_SHEET_ID"] = make_entry(
            f"SHEET_{idx}_SHEET_ID", f"sheet-{idx}"
        )
    for idx in range(35):
        entries[f"TAB_{idx}_TAB"] = make_entry(f"TAB_{idx}_TAB", f"tab-{idx}")
    for idx in range(25):
        entries[f"CONFIG_{idx}"] = make_entry(f"CONFIG_{idx}", f"config-{idx}")

    timestamp = dt.datetime.now(dt.timezone.utc)

    embeds, warnings, warning_keys = cog._build_env_embeds(
        bot_name="Bot",
        env="test",
        version="v1",
        guild_name="Guild",
        entries=entries,
        sheet_sections=[],
        footer_text="footer",
        timestamp=timestamp,
    )

    assert not warnings
    assert not warning_keys
    assert len(embeds) >= 4
    assert any("Channels (" in field.name for embed in embeds for field in embed.fields)
    for page, embed in enumerate(embeds, start=1):
        assert f"Page {page}/{len(embeds)}" in embed.title
        assert _embed_length(embed) <= _MAX_EMBED_LENGTH
        for field in embed.fields:
            assert len(str(field.value)) <= 900

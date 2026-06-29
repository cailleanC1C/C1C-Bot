"""Sheet-driven Clan Ads service."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import discord

from modules.common import feature_flags
from modules.common import runtime as runtime_helpers
from modules.recruitment import emoji_pipeline
from shared.sheets import async_facade as sheets
from shared.sheets import recruitment

log = logging.getLogger("c1c.recruitment.clan_ads")
FEATURE_KEY = "clan_ads"
TRUE_VALUES = {"true", "1", "yes", "y", "on"}
STATUS_POSTED = "posted"
STATUS_DISABLED = "skipped_disabled"
STATUS_NOT_QUALIFIED = "skipped_not_qualified"
STATUS_MISSING_CLAN_ROW = "skipped_missing_clan_row"
STATUS_MISSING_RULE = "skipped_missing_rule"
STATUS_RULE_DISABLED = "skipped_rule_disabled"
STATUS_MISSING_DEFAULT = "skipped_missing_embed_template"
STATUS_ERROR_POST = "error_post_failed"

MESSAGE_HEADERS = {
    "clan_tag": "clan_tag",
    "enabled": "enabled",
    "embed_title": "embed_title",
    "embed_description": "embed_description",
    "embed_footer": "embed_footer",
    "last_ad_message_id": "last_ad_message_id",
    "last_posted_at_utc": "last_posted_at_utc",
    "last_open_spots": "last_open_spots",
    "last_status": "last_status",
    "last_error": "last_error",
}
RULE_HEADERS = {
    "bracket": "bracket",
    "min_openings_to_post": "min_openings_to_post",
    "enabled": "enabled",
}
CONFIG_KEYS = (
    "clan_ad_messages_tab",
    "clan_ad_rules_tab",
    "clan_ad_channel_id",
    "clan_ad_raid_role_id",
    "clan_ad_notification_message",
    "clan_ad_post_interval_hours",
    "clan_ad_last_posted_at_utc",
)
REQUIRED_CONFIG_KEYS = (
    "clan_ad_messages_tab",
    "clan_ad_rules_tab",
    "clan_ad_channel_id",
)
REQUIRED_CLAN_FIELDS = ("clan_tag", "clan_name", "bracket", "open_spots")


def norm(value: Any) -> str:
    return str(value or "").strip()


def key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(value).lower()).strip("_")


def is_true(value: Any) -> bool:
    return norm(value).lower() in TRUE_VALUES


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def tag_norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm(value).upper())


def build_header_map(
    headers: Sequence[Any], required: dict[str, str]
) -> dict[str, int]:
    lookup = {key(header): index for index, header in enumerate(headers)}
    resolved: dict[str, int] = {}
    for logical, label in required.items():
        normalized = key(label)
        if normalized not in lookup:
            raise MissingHeaderError(label)
        resolved[logical] = lookup[normalized]
    return resolved


def cell(row: Sequence[Any], index: int | None) -> str:
    return norm(row[index]) if index is not None and index < len(row) else ""


def a1(row: int, col0: int) -> str:
    label = ""
    col = col0 + 1
    while col:
        col, remainder = divmod(col - 1, 26)
        label = chr(65 + remainder) + label
    return f"{label}{row}"


class MissingHeaderError(RuntimeError):
    def __init__(self, header: str) -> None:
        super().__init__(f"missing required header {header}")
        self.header = header


class MissingClanFieldError(RuntimeError):
    def __init__(self, field: str) -> None:
        super().__init__(f"missing required clan field {field}")
        self.field = field


@dataclass
class RunReporter:
    bot: discord.Client | None
    sent: set[str] = field(default_factory=set)

    async def warn(self, message: str, *, dedupe_key: str | None = None) -> None:
        token = dedupe_key or message
        if token in self.sent:
            return
        self.sent.add(token)
        log.warning(message)
        try:
            await runtime_helpers.send_log_message(message)
        except Exception:
            log.exception("Clan Ads logging-channel warning failed")


@dataclass
class Config:
    messages_tab: str
    rules_tab: str
    channel_id: int
    raid_role_id: str
    notification: str
    interval_hours: float
    last_posted: str


@dataclass
class Rule:
    bracket: str
    min_openings: int | None
    enabled: bool


@dataclass
class MessageRow:
    row_number: int
    tag: str
    enabled: bool
    embed_title: str
    embed_description: str
    embed_footer: str
    last_message_id: str
    embed_color: str = ""


@dataclass
class ClanData:
    record: Any
    tag: str
    name: str
    bracket: str
    open_spots: int
    description: str = ""


@dataclass
class ClanCrestThumbnail:
    thumbnail_url: str | None = None
    file: discord.File | None = None


@dataclass
class Decision:
    tag: str
    clan: ClanData | None
    row: MessageRow | None
    status: str
    reason: str
    rule: Rule | None = None


class ClanAdButtonView(discord.ui.View):
    def __init__(self, clan_tag: str):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="View Clan Card",
                style=discord.ButtonStyle.primary,
                custom_id=f"clan_ads:view_card:{tag_norm(clan_tag)}",
            )
        )


async def build_clan_card(
    bot: discord.Client, clan_tag: str, guild: discord.Guild | None
):
    cog = bot.get_cog("ClanProfileCog") if hasattr(bot, "get_cog") else None
    if cog is None:
        log.error("Clan Ads could not find loaded ClanProfileCog for card rendering")
        return None, [], None
    if hasattr(cog, "build_profile_pages"):
        embeds, files, state = await cog.build_profile_pages(
            tag_norm(clan_tag), guild=guild
        )
        return embeds, files, state
    if hasattr(cog, "build_profile_payload"):
        embed, files, state = await cog.build_profile_payload(
            tag_norm(clan_tag), guild=guild
        )
        return ([embed] if embed is not None else []), files, state
    log.error("Clan Ads loaded ClanProfileCog has no supported card renderer")
    return None, [], None


async def resolve_clan_crest_thumbnail(
    bot: discord.Client, clan_tag: str, guild: discord.Guild | None
) -> ClanCrestThumbnail:
    cog = bot.get_cog("ClanProfileCog") if hasattr(bot, "get_cog") else None
    if cog is None:
        log.error("Clan Ads could not find loaded ClanProfileCog for crest rendering")
        return ClanCrestThumbnail()
    if not hasattr(cog, "build_clan_crest_thumbnail"):
        log.error("Clan Ads loaded ClanProfileCog has no crest thumbnail resolver")
        return ClanCrestThumbnail()
    asset = await cog.build_clan_crest_thumbnail(tag_norm(clan_tag), guild=guild)
    return ClanCrestThumbnail(
        thumbnail_url=getattr(asset, "thumbnail_url", None),
        file=getattr(asset, "file", None),
    )


async def load_config(
    reporter: RunReporter | None = None, *, force: bool = False
) -> Config | None:
    vals = {
        k: await recruitment.get_config_value_async(k, None, force=force)
        for k in CONFIG_KEYS
    }
    missing = [
        config_key for config_key in REQUIRED_CONFIG_KEYS if not vals.get(config_key)
    ]
    if missing:
        log.error("clan ads missing required config keys: %s", missing)
        if reporter:
            for config_key in missing:
                await reporter.warn(
                    f"⚠️ Clan Ads skipped: missing required Config key `{config_key}`.",
                    dedupe_key=f"missing_config:{config_key}",
                )
        return None

    try:
        channel_id = int(str(vals["clan_ad_channel_id"]).strip())
    except ValueError:
        log.error("invalid clan_ad_channel_id: %r", vals.get("clan_ad_channel_id"))
        if reporter:
            await reporter.warn(
                "⚠️ Clan Ads skipped: Config key `clan_ad_channel_id` must be a Discord channel ID.",
                dedupe_key="invalid_config:clan_ad_channel_id",
            )
        return None

    interval = 24.0
    raw_interval = vals.get("clan_ad_post_interval_hours")
    if raw_interval:
        try:
            interval = float(raw_interval)
        except ValueError:
            log.warning("invalid clan_ad_post_interval_hours: %r", raw_interval)
            if reporter:
                await reporter.warn(
                    "⚠️ Clan Ads warning: Config key `clan_ad_post_interval_hours` is invalid; using 24 hours.",
                    dedupe_key="invalid_config:clan_ad_post_interval_hours",
                )
    return Config(
        vals["clan_ad_messages_tab"] or "",
        vals["clan_ad_rules_tab"] or "",
        channel_id,
        vals.get("clan_ad_raid_role_id") or "",
        vals.get("clan_ad_notification_message") or "",
        interval,
        vals.get("clan_ad_last_posted_at_utc") or "",
    )


async def worksheet(tab: str):
    return await sheets.get_worksheet(recruitment.get_recruitment_sheet_id(), tab)


async def load_rules(config: Config, reporter: RunReporter) -> dict[str, Rule] | None:
    try:
        rows = await sheets.fetch_values(
            recruitment.get_recruitment_sheet_id(), config.rules_tab
        )
        if not rows:
            await reporter.warn(
                f"⚠️ Clan Ads skipped: `{config.rules_tab}` has no header row.",
                dedupe_key="rules:no_rows",
            )
            return None
        header_map = build_header_map(rows[0], RULE_HEADERS)
    except MissingHeaderError as exc:
        log.exception("clan ad rules tab missing header")
        await reporter.warn(
            f"⚠️ Clan Ads skipped: `{config.rules_tab}` is missing required header `{exc.header}`.",
            dedupe_key=f"rules:missing_header:{exc.header}",
        )
        return None
    except Exception:
        log.exception("failed to load clan ad rules tab")
        await reporter.warn(
            f"⚠️ Clan Ads skipped: could not read `{config.rules_tab}`. Check the tab name and bot Sheets access.",
            dedupe_key="rules:read_failed",
        )
        return None

    rules: dict[str, Rule] = {}
    for row in rows[1:]:
        bracket = cell(row, header_map["bracket"])
        if not bracket:
            continue
        raw_threshold = cell(row, header_map["min_openings_to_post"])
        threshold = int(raw_threshold) if raw_threshold.isdigit() else None
        rules[key(bracket)] = Rule(
            bracket=bracket,
            min_openings=threshold,
            enabled=is_true(cell(row, header_map["enabled"])),
        )
    return rules


async def load_messages(
    config: Config, reporter: RunReporter
) -> tuple[dict[str, MessageRow], MessageRow | None, dict[str, int]] | None:
    try:
        rows = await sheets.fetch_values(
            recruitment.get_recruitment_sheet_id(), config.messages_tab
        )
        if not rows:
            await reporter.warn(
                f"⚠️ Clan Ads skipped: `{config.messages_tab}` has no header row.",
                dedupe_key="messages:no_rows",
            )
            return None
        header_map = build_header_map(rows[0], MESSAGE_HEADERS)
        optional_lookup = {key(header): index for index, header in enumerate(rows[0])}
        if "embed_color" in optional_lookup:
            header_map["embed_color"] = optional_lookup["embed_color"]
    except MissingHeaderError as exc:
        log.exception("clan ad messages tab missing header")
        await reporter.warn(
            f"⚠️ Clan Ads skipped: `{config.messages_tab}` is missing required header `{exc.header}`.",
            dedupe_key=f"messages:missing_header:{exc.header}",
        )
        return None
    except Exception:
        log.exception("failed to load clan ad messages tab")
        await reporter.warn(
            f"⚠️ Clan Ads skipped: could not read `{config.messages_tab}`. Check the tab name and bot Sheets access.",
            dedupe_key="messages:read_failed",
        )
        return None

    items: dict[str, MessageRow] = {}
    default: MessageRow | None = None
    for row_number, row in enumerate(rows[1:], start=2):
        raw_tag = cell(row, header_map["clan_tag"])
        message_row = MessageRow(
            row_number=row_number,
            tag=tag_norm(raw_tag),
            enabled=is_true(cell(row, header_map["enabled"])),
            embed_title=cell(row, header_map["embed_title"]),
            embed_description=cell(row, header_map["embed_description"]),
            embed_footer=cell(row, header_map["embed_footer"]),
            last_message_id=cell(row, header_map["last_ad_message_id"]),
            embed_color=cell(row, header_map.get("embed_color")),
        )
        if raw_tag.lower() == "default":
            default = message_row
        elif message_row.tag:
            items[message_row.tag] = message_row
    return items, default, header_map


async def write_state(
    config: Config, header_map: dict[str, int], row: MessageRow, **values: str
) -> None:
    ws = await worksheet(config.messages_tab)
    updates = [
        {"range": a1(row.row_number, header_map[column]), "values": [[value]]}
        for column, value in values.items()
        if column in header_map
    ]
    if updates:
        await sheets.call_with_backoff(ws.batch_update, updates)


def record_field(record: Any, field_name: str) -> str:
    header_map = recruitment.get_clan_header_map()
    if field_name not in header_map:
        raise MissingClanFieldError(field_name)
    return cell(record.row, header_map[field_name])


def optional_record_field(record: Any, field_name: str) -> str | None:
    header_map = recruitment.get_clan_header_map()
    if field_name not in header_map:
        return None
    return cell(record.row, header_map[field_name])


def clan_data(record: Any) -> ClanData:
    raw_tag = record_field(record, "clan_tag")
    tag = tag_norm(raw_tag)
    if not tag:
        raise MissingClanFieldError("clan_tag")
    name = record_field(record, "clan_name")
    if not name:
        raise MissingClanFieldError("clan_name")
    bracket = optional_record_field(record, "bracket") or norm(
        getattr(record, "roster", "")
    )
    if not bracket:
        raise MissingClanFieldError("bracket")
    open_spots = getattr(record, "open_spots", None)
    if open_spots is None:
        raise MissingClanFieldError("open_spots")
    description = optional_record_field(record, "clan_description") or ""
    return ClanData(
        record=record,
        tag=tag,
        name=name,
        bracket=bracket,
        open_spots=int(open_spots),
        description=description,
    )


def parse_embed_color(value: Any) -> int | None:
    """Parse optional ClanAdMessages embed_color values into Discord RGB ints."""
    raw = norm(value)
    if not raw:
        return None
    if raw.startswith("#"):
        raw = raw[1:]
    elif raw.lower().startswith("0x"):
        raw = raw[2:]
    if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return None
    return int(raw, 16)


async def resolve_embed_color(
    row: MessageRow,
    default: MessageRow | None,
    clan_tag: str,
    reporter: RunReporter,
) -> int | None:
    raw_color = row.embed_color or (default.embed_color if default else "")
    if not raw_color:
        return None
    parsed = parse_embed_color(raw_color)
    if parsed is not None:
        return parsed
    await reporter.warn(
        f"⚠️ Clan Ads warning: invalid embed_color `{raw_color}` for `{clan_tag}`; using default embed color.",
        dedupe_key=f"invalid_embed_color:{raw_color}",
    )
    return None


def render(template: str, clan: ClanData, guild: discord.Guild | None) -> str:
    banner = ""
    emoji = emoji_pipeline.emoji_for_tag(guild, clan.tag) if guild else None
    if emoji:
        banner = str(emoji)
    values = {
        "clan_banner": banner,
        "clan_name": clan.name,
        "clan_tag": clan.tag,
        "bracket": clan.bracket,
        "open_spots": str(clan.open_spots),
        "clan_description": clan.description,
    }
    for placeholder, value in values.items():
        template = template.replace("{" + placeholder + "}", value)
    return template


async def decide(
    clan: ClanData,
    rows: dict[str, MessageRow],
    default: MessageRow | None,
    rules: dict[str, Rule],
    header_map: dict[str, int],
    config: Config,
    reporter: RunReporter,
) -> Decision:
    row = rows.get(clan.tag)
    if not row:
        await reporter.warn(
            f"⚠️ Clan Ads skipped `{clan.tag}`: no row exists for this clan in `{config.messages_tab}`.",
            dedupe_key=f"missing_clan_row:{clan.tag}",
        )
        return Decision(
            clan.tag,
            clan,
            None,
            STATUS_MISSING_CLAN_ROW,
            f"{clan.tag} missing ClanAdMessages row",
        )
    if not row.enabled:
        await write_state(
            config, header_map, row, last_status=STATUS_DISABLED, last_error=""
        )
        return Decision(
            clan.tag,
            clan,
            row,
            STATUS_DISABLED,
            f"{clan.tag} was not posted because clan ads are disabled for that clan.",
        )
    missing_fields = []
    if not (row.embed_title or (default and default.embed_title)):
        missing_fields.append("embed_title")
    if not (row.embed_description or (default and default.embed_description)):
        missing_fields.append("embed_description")
    if missing_fields:
        detail = "Missing required clan ad embed template field(s): " + ", ".join(
            missing_fields
        )
        await write_state(
            config,
            header_map,
            row,
            last_status=STATUS_MISSING_DEFAULT,
            last_error=detail,
        )
        await reporter.warn(
            f"⚠️ Clan Ads skipped `{clan.tag}`: `{config.messages_tab}` is missing required embed template field(s): {', '.join(missing_fields)}.",
            dedupe_key=f"missing_embed_template:{clan.tag}:{','.join(missing_fields)}",
        )
        return Decision(clan.tag, clan, row, STATUS_MISSING_DEFAULT, detail)

    rule = rules.get(key(clan.bracket))
    if not rule:
        await write_state(
            config,
            header_map,
            row,
            last_status=STATUS_MISSING_RULE,
            last_error=f"No enabled ClanAdRules row matches bracket: {clan.bracket}",
            last_open_spots=str(clan.open_spots),
        )
        await reporter.warn(
            f"⚠️ Clan Ads skipped `{clan.tag}`: no enabled ClanAdRules row matches bracket `{clan.bracket}`.",
            dedupe_key=f"missing_rule:{clan.tag}:{key(clan.bracket)}",
        )
        return Decision(
            clan.tag,
            clan,
            row,
            STATUS_MISSING_RULE,
            f"{clan.tag} was not posted because no enabled ClanAdRules row matches its bracket: {clan.bracket}.",
        )
    if not rule.enabled or rule.min_openings is None:
        await write_state(
            config,
            header_map,
            row,
            last_status=STATUS_RULE_DISABLED,
            last_error=f"Rule disabled for bracket: {rule.bracket}",
            last_open_spots=str(clan.open_spots),
        )
        await reporter.warn(
            f"⚠️ Clan Ads skipped `{clan.tag}`: ClanAdRules row for bracket `{rule.bracket}` is disabled.",
            dedupe_key=f"disabled_rule:{clan.tag}:{key(rule.bracket)}",
        )
        return Decision(
            clan.tag,
            clan,
            row,
            STATUS_RULE_DISABLED,
            f"{clan.tag} was not posted because no enabled ClanAdRules row matches its bracket: {rule.bracket}.",
            rule,
        )
    if clan.open_spots < rule.min_openings:
        await write_state(
            config,
            header_map,
            row,
            last_status=STATUS_NOT_QUALIFIED,
            last_open_spots=str(clan.open_spots),
            last_error="",
        )
        return Decision(
            clan.tag,
            clan,
            row,
            STATUS_NOT_QUALIFIED,
            f"{clan.tag} was not posted. It has {clan.open_spots} open spots, but its bracket requires {rule.min_openings}.",
            rule,
        )
    return Decision(clan.tag, clan, row, "qualified", "qualified", rule)


async def send_notification(channel: Any, config: Config, count: int) -> None:
    if not config.notification:
        return
    message = (
        config.notification.replace("{raid_role_id}", config.raid_role_id)
        .replace("{ad_count}", str(count))
        .replace("{clan_count}", str(count))
    )
    await channel.send(message)


async def post_decision(
    bot: discord.Client,
    channel: Any,
    config: Config,
    header_map: dict[str, int],
    default: MessageRow,
    decision: Decision,
    guild: discord.Guild | None,
    reporter: RunReporter,
) -> bool:
    row = decision.row
    clan = decision.clan
    if not row or not clan:
        return False
    delete_error = ""
    if row.last_message_id:
        try:
            old = await channel.fetch_message(int(row.last_message_id))
            await old.delete()
        except Exception as exc:
            delete_error = f"Old ad delete failed: {exc.__class__.__name__}"
            log.warning(
                "failed to delete old clan ad",
                exc_info=True,
                extra={"clan_tag": clan.tag},
            )

    try:
        color = await resolve_embed_color(row, default, clan.tag, reporter)
        embed = discord.Embed(
            title=render(
                row.embed_title or (default.embed_title if default else ""), clan, guild
            ),
            description=render(
                row.embed_description or (default.embed_description if default else ""),
                clan,
                guild,
            ),
            color=color,
        )
        footer = render(
            row.embed_footer or (default.embed_footer if default else ""), clan, guild
        )
        if footer:
            embed.set_footer(text=footer)
        crest_file = None
        try:
            crest = await resolve_clan_crest_thumbnail(bot, clan.tag, guild)
        except Exception:
            log.debug(
                "Clan Ads could not resolve clan crest thumbnail",
                exc_info=True,
                extra={"clan_tag": clan.tag},
            )
            crest = ClanCrestThumbnail()
        if crest.thumbnail_url:
            embed.set_thumbnail(url=crest.thumbnail_url)
            crest_file = crest.file
        else:
            log.debug(
                "Clan Ads could not resolve clan crest thumbnail",
                extra={"clan_tag": clan.tag},
            )
        send_kwargs = {"embed": embed, "view": ClanAdButtonView(clan.tag)}
        if crest_file is not None:
            send_kwargs["files"] = [crest_file]
        message = await channel.send(**send_kwargs)
    except Exception as exc:
        log.exception("failed to post clan ad", extra={"clan_tag": clan.tag})
        await write_state(
            config,
            header_map,
            row,
            last_status=STATUS_ERROR_POST,
            last_error=str(exc),
            last_open_spots=str(clan.open_spots),
        )
        await reporter.warn(
            f"⚠️ Clan Ads could not post in <#{config.channel_id}>: missing send message permission or invalid target.",
            dedupe_key=f"post_failed:{config.channel_id}",
        )
        return False

    if delete_error:
        await reporter.warn(
            f"⚠️ Clan Ads could not delete old ad for `{clan.tag}`, but posted the new ad. Check bot permissions or stale message ID.",
            dedupe_key=f"delete_failed:{clan.tag}",
        )
    await write_state(
        config,
        header_map,
        row,
        last_ad_message_id=str(message.id),
        last_posted_at_utc=now_iso(),
        last_open_spots=str(clan.open_spots),
        last_status=STATUS_POSTED,
        last_error=delete_error,
    )
    return True


async def _resolve_channel(bot: discord.Client, config: Config, reporter: RunReporter):
    try:
        return bot.get_channel(config.channel_id) or await bot.fetch_channel(
            config.channel_id
        )
    except Exception:
        log.exception(
            "failed to resolve clan ad channel", extra={"channel_id": config.channel_id}
        )
        await reporter.warn(
            f"⚠️ Clan Ads could not post in <#{config.channel_id}>: missing send message permission or invalid target.",
            dedupe_key=f"channel_failed:{config.channel_id}",
        )
        return None


def _manual_summary(posted: int, skipped_or_failed: int, fallback: str) -> str:
    if posted and skipped_or_failed:
        suffix = "clan was" if skipped_or_failed == 1 else "clans were"
        return (
            f"Posted {posted} clan ad(s). {skipped_or_failed} {suffix} skipped or failed. "
            "Check the bot logging channel for details."
        )
    if posted:
        return f"Posted {posted} clan ad(s)."
    return fallback


async def run(
    bot: discord.Client, *, clan_tag_filter: str | None = None, scheduled: bool = False
):
    reporter = RunReporter(bot)
    if not feature_flags.is_enabled(FEATURE_KEY):
        return {
            "posted": 0,
            "skipped": 0,
            "message": "Clan Ads are disabled by feature toggle.",
        }

    config = await load_config(reporter, force=True)
    if not config:
        return {"posted": 0, "skipped": 0, "message": "Clan Ads config is incomplete."}

    if scheduled and config.last_posted:
        try:
            last = datetime.fromisoformat(config.last_posted.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last < timedelta(
                hours=config.interval_hours
            ):
                return {"posted": 0, "skipped": 0, "message": "interval not elapsed"}
        except ValueError:
            log.warning("invalid clan_ad_last_posted_at_utc: %r", config.last_posted)
            await reporter.warn(
                "⚠️ Clan Ads warning: Config key `clan_ad_last_posted_at_utc` is invalid; scheduled run will continue.",
                dedupe_key="invalid_config:clan_ad_last_posted_at_utc",
            )

    channel = await _resolve_channel(bot, config, reporter)
    if channel is None:
        return {
            "posted": 0,
            "skipped": 0,
            "message": "Clan Ads channel is invalid or inaccessible.",
            "config": config,
        }

    rules = await load_rules(config, reporter)
    loaded_messages = await load_messages(config, reporter)
    if rules is None or loaded_messages is None:
        return {
            "posted": 0,
            "skipped": 0,
            "message": "Clan Ads sheet setup is incomplete.",
            "config": config,
        }
    rows, default, header_map = loaded_messages

    raw_records = await sheets.fetch_clan_records(force=True)
    clans: list[ClanData] = []
    clan_field_failures = 0
    for record in raw_records:
        try:
            data = clan_data(record)
        except MissingClanFieldError as exc:
            fallback_tag = tag_norm(
                optional_record_field(record, "clan_tag") or "unknown"
            )
            log.warning(
                "missing required clan field",
                extra={"field": exc.field, "clan_tag": fallback_tag},
            )
            await reporter.warn(
                f"⚠️ Clan Ads skipped `{fallback_tag}`: could not resolve required clan field `{exc.field}` from the recruitment sheet headers.",
                dedupe_key=f"missing_clan_field:{fallback_tag}:{exc.field}",
            )
            clan_field_failures += 1
            continue
        clans.append(data)

    if not clans and clan_field_failures:
        return {
            "posted": 0,
            "skipped": clan_field_failures,
            "message": "Clan ads could not evaluate any clans because required clan data fields are missing. Check the bot logging channel for details.",
            "config": config,
        }

    if clan_tag_filter:
        wanted = tag_norm(clan_tag_filter)
        clans = [clan for clan in clans if clan.tag == wanted]
        if not clans:
            return {
                "posted": 0,
                "skipped": 0,
                "message": f"{wanted} was not posted because the clan was not found.",
                "config": config,
            }

    templates = [
        template
        for row in rows.values()
        for template in (row.embed_title, row.embed_description, row.embed_footer)
        if template
    ]
    if default:
        templates.extend(
            template
            for template in (
                default.embed_title,
                default.embed_description,
                default.embed_footer,
            )
            if template
        )
    if any("{clan_description}" in template for template in templates):
        if "clan_description" not in recruitment.get_clan_header_map():
            await reporter.warn(
                "⚠️ Clan Ads warning: template uses `{clan_description}`, but no clan description field could be resolved from the existing clan data source.",
                dedupe_key="missing_optional:clan_description",
            )

    decisions = [
        await decide(clan, rows, default, rules, header_map, config, reporter)
        for clan in clans
    ]
    qualified = [decision for decision in decisions if decision.status == "qualified"]
    if not qualified:
        fallback = (
            decisions[0].reason
            if clan_tag_filter and decisions
            else "No clan ads were posted. No enabled clans currently meet their bracket posting rules."
        )
        if scheduled:
            log.info("Clan Ads scheduled run skipped: no qualifying clans")
        return {
            "posted": 0,
            "skipped": len(decisions),
            "message": fallback,
            "config": config,
        }

    posted = 0
    for decision in qualified:
        if await post_decision(
            bot,
            channel,
            config,
            header_map,
            default,  # type: ignore[arg-type]
            decision,
            getattr(channel, "guild", None),
            reporter,
        ):
            posted += 1

    if posted:
        try:
            await send_notification(channel, config, posted)
        except Exception:
            log.exception("failed to send clan ads notification")
            await reporter.warn(
                "⚠️ Clan Ads posted ads, but could not send the raid notification. Check bot channel permissions.",
                dedupe_key="notification_failed",
            )

    if scheduled and posted:
        ws = await worksheet(recruitment.get_config_tab_name())
        values = await sheets.fetch_values(
            recruitment.get_recruitment_sheet_id(), recruitment.get_config_tab_name()
        )
        header_map_config = (
            build_header_map(values[0], {"key": "key", "value": "value"})
            if values
            else {}
        )
        for row_number, row in enumerate(values[1:], start=2):
            if (
                cell(row, header_map_config["key"]).lower()
                == "clan_ad_last_posted_at_utc"
            ):
                await sheets.call_with_backoff(
                    ws.update, a1(row_number, header_map_config["value"]), now_iso()
                )
                break

    skipped_or_failed = len(decisions) - posted
    return {
        "posted": posted,
        "skipped": skipped_or_failed,
        "message": _manual_summary(
            posted,
            skipped_or_failed,
            "No clan ads were posted. No enabled clans currently meet their bracket posting rules.",
        ),
        "config": config,
    }


async def scheduled_tick(bot: discord.Client):
    try:
        return await run(bot, scheduled=True)
    except Exception:
        log.exception("scheduled clan ads failed")
        return {"posted": 0, "skipped": 0, "message": "Clan Ads scheduled run failed."}

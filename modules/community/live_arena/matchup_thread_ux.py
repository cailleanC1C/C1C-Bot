"""Final sheet-backed player matchup-thread UX for Live Arena.

The older Live Arena layers grew the matchup post incrementally: base Q1 copy,
full-set wording, weekly-availability copy, and extra result/scheduling buttons.
This final installer owns the visible player-facing matchup render so the live
message is deterministic and all copy changed by this UX pass comes from the
existing MESSAGES tab.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from string import Formatter

import discord

from shared.config import cfg
from shared.sheets.async_core import afetch_values
from shared.theme import colors

from modules.community.live_arena.messages import MESSAGE_HEADERS, load_pr5_config
from modules.community.live_arena.service import (
    LiveArenaConfigError,
    _enabled,
    _rows,
    _text,
)

log = logging.getLogger("c1c.community.live_arena.matchup_thread_ux")
_installed = False

_COPY_CONTRACTS: dict[str, set[str]] = {
    "match_thread_title": {"round_name", "match_number"},
    "match_thread_matchup_line": {"player_a_mention", "player_b_mention"},
    "match_thread_actions_heading": set(),
    "match_thread_action_1": set(),
    "match_thread_action_2": {"match_format"},
    "match_thread_action_3": set(),
    "match_thread_action_4": {"report_result_button_label"},
    "match_thread_deadline": {"round_deadline"},
    "match_thread_shared_availability": {"availability_windows"},
    "match_thread_no_shared_availability": {
        "update_availability_button_label",
        "scheduling_problem_button_label",
    },
    "match_thread_availability_note": {"update_availability_button_label"},
    "match_format_standard": set(),
    "match_format_final": set(),
    "button_dispute_result": set(),
    "button_scheduling_problem": set(),
    "button_update_availability": set(),
    "button_report_result": set(),
    "scheduling_problem_recorded": set(),
    "scheduling_problem_thread_notice": {"reporter_mention"},
    "screenshot_required": {"report_result_button_label"},
}

_MATCHUP_COPY: dict[str, dict[str, "CopyTemplate"]] = {}
_ACTIVE_SHEET_ID = ""


@dataclass(frozen=True)
class CopyTemplate:
    key: str
    title: str
    description: str
    color: int

    def render(self, **values: object) -> tuple[str, str]:
        expected = _COPY_CONTRACTS[self.key]
        fields = {
            name
            for _, name, _, _ in Formatter().parse(self.title + self.description)
            if name
        }
        if fields != expected:
            raise LiveArenaConfigError(
                f"MESSAGES.{self.key}: placeholders must be exactly "
                + ", ".join(sorted(expected))
            )
        missing = fields - values.keys()
        if missing:
            raise LiveArenaConfigError(
                f"MESSAGES.{self.key}: missing render value {', '.join(sorted(missing))}"
            )
        return self.title.format(**values), self.description.format(**values)

    def embed(self, **values: object) -> discord.Embed:
        title, description = self.render(**values)
        return discord.Embed(title=title, description=description, color=self.color)


async def refresh_matchup_copy(sheet_id: str) -> dict[str, CopyTemplate]:
    """Load and validate every matchup UX row from the configured MESSAGES tab."""
    global _ACTIVE_SHEET_ID

    sid = str(sheet_id or "").strip()
    if not sid:
        raise LiveArenaConfigError("Live Arena matchup copy requires a Sheet ID")
    config, _ = await load_pr5_config(sid)
    tab = config["MESSAGES_TAB"]
    rows = _rows(await afetch_values(sid, tab) or [], MESSAGE_HEADERS, tab)
    templates: dict[str, CopyTemplate] = {}

    for key, expected in _COPY_CONTRACTS.items():
        matches = [row for row in rows if _text(row["message_key"]) == key]
        if len(matches) != 1 or not _enabled(matches[0]["active"]):
            raise LiveArenaConfigError(
                f"MESSAGES: required active row missing or duplicated: {key}"
            )
        row = matches[0]
        color_text = _text(row["color_hex"])
        if len(color_text) != 7 or not color_text.startswith("#"):
            raise LiveArenaConfigError(f"MESSAGES.{key}: color_hex must be #RRGGBB")
        try:
            color = int(color_text[1:], 16)
        except ValueError as exc:
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: color_hex must be #RRGGBB"
            ) from exc
        template = CopyTemplate(
            key=key,
            title=_text(row["title"]),
            description=_text(row["description"]),
            color=color,
        )
        fields = {
            name
            for _, name, _, _ in Formatter().parse(template.title + template.description)
            if name
        }
        if fields != expected:
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: placeholders must be exactly "
                + ", ".join(sorted(expected))
            )
        template.render(**{name: "x" for name in expected})
        templates[key] = template

    _MATCHUP_COPY[sid] = templates
    _ACTIVE_SHEET_ID = sid
    return templates


def _templates(sheet_id: str | None = None) -> dict[str, CopyTemplate] | None:
    sid = str(sheet_id or _ACTIVE_SHEET_ID or "").strip()
    return _MATCHUP_COPY.get(sid)


def _template(key: str, sheet_id: str | None = None) -> CopyTemplate:
    templates = _templates(sheet_id)
    if templates is None or key not in templates:
        raise LiveArenaConfigError(
            "Live Arena matchup copy has not been loaded from MESSAGES"
        )
    return templates[key]


def _title(key: str, sheet_id: str | None = None, **values: object) -> str:
    return _template(key, sheet_id).render(**values)[0]


def _description(key: str, sheet_id: str | None = None, **values: object) -> str:
    return _template(key, sheet_id).render(**values)[1]


def _section(key: str, *, sheet_id: str | None = None, heading_level: int = 0, **values: object) -> str:
    title, description = _template(key, sheet_id).render(**values)
    parts: list[str] = []
    if title:
        if heading_level:
            parts.append(f"{'#' * heading_level} {title}")
        else:
            parts.append(f"**{title}**")
    if description:
        parts.append(description)
    return "\n".join(parts)


def _match_embed_from_sheet(tournament, round_row, match, slots):
    """Render the complete visible player matchup instructions from Sheet copy."""
    from modules.community.live_arena import qualification_panel

    # Tests and non-runtime construction can still exercise the older renderer.
    # Production registration loads MESSAGES before persistent views are created.
    if _templates() is None:
        return _match_embed_from_sheet._original(tournament, round_row, match, slots)

    deadline = qualification_panel._format_timestamp(
        _text(round_row["deadline_at_utc"]), "F"
    )
    shared_ids = [
        value for value in _text(match["shared_slot_ids_csv"]).split(",") if value
    ]
    by_id = {
        _text(row["slot_id"]): row
        for row in slots
        if _enabled(row["enabled"])
    }
    windows = [
        qualification_panel._render_slot(by_id[slot_id], round_row)
        for slot_id in shared_ids
        if slot_id in by_id
    ]

    report_label = _title("button_report_result")
    update_label = _title("button_update_availability")
    scheduling_label = _title("button_scheduling_problem")
    is_final = _text(round_row.get("round_stage")).lower() == "final"
    format_key = "match_format_final" if is_final else "match_format_standard"
    match_format = _title(format_key)

    title = _title(
        "match_thread_title",
        round_name=_text(round_row["round_name"]),
        match_number=_text(match["match_number"]),
    )
    matchup = _description(
        "match_thread_matchup_line",
        player_a_mention=f"<@{_text(match['player_a_discord_user_id'])}>",
        player_b_mention=f"<@{_text(match['player_b_discord_user_id'])}>",
    )

    sections = [
        matchup,
        _section("match_thread_actions_heading", heading_level=3),
        _section("match_thread_action_1"),
        _section("match_thread_action_2", match_format=match_format),
        _section("match_thread_action_3"),
        _section(
            "match_thread_action_4",
            report_result_button_label=report_label,
        ),
        _description("match_thread_deadline", round_deadline=deadline),
    ]

    if windows:
        sections.append(
            _section(
                "match_thread_shared_availability",
                heading_level=3,
                availability_windows="\n".join(f"• {window}" for window in windows),
            )
        )
    else:
        sections.append(
            _section(
                "match_thread_no_shared_availability",
                heading_level=3,
                update_availability_button_label=update_label,
                scheduling_problem_button_label=scheduling_label,
            )
        )

    sections.append(
        _section(
            "match_thread_availability_note",
            update_availability_button_label=update_label,
        )
    )
    description = "\n\n".join(section for section in sections if section)
    return discord.Embed(
        title=title,
        description=description[:4096],
        color=colors.c1c_blue,
    )


async def _rerender_open_match_threads(bot, qualification_service, snapshot) -> list[str]:
    """Refresh existing starter embeds so redeploy applies the new instructions."""
    from modules.community.live_arena import qualification_panel

    if _templates(qualification_service.sheet_id) is None or snapshot.round_row is None:
        return []
    try:
        _, (_, tournament), _, slots = await qualification_service.context()
    except Exception:
        log.exception("Live Arena matchup-copy context refresh failed")
        return ["matchup instructions"]

    warnings: list[str] = []
    for match in snapshot.matches:
        thread_id = _text(match.get("thread_id"))
        if not thread_id:
            continue
        try:
            thread = bot.get_channel(int(thread_id))
            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))
            get_partial = getattr(thread, "get_partial_message", None)
            starter = (
                get_partial(int(thread.id))
                if callable(get_partial)
                else await thread.fetch_message(int(thread.id))
            )
            await starter.edit(
                embed=qualification_panel.match_embed(
                    tournament,
                    snapshot.round_row,
                    match,
                    slots,
                )
            )
        except Exception:
            log.exception(
                "Live Arena matchup instruction rerender failed • match=%s",
                _text(match.get("match_id")),
            )
            warnings.append(
                f"Match {_text(match.get('match_number'))} matchup instructions"
            )
    return list(dict.fromkeys(warnings))


def _apply_match_button_labels(view, sheet_id: str) -> None:
    if _templates(sheet_id) is None:
        return
    labels = {
        "live_arena:match:dispute_result": _title("button_dispute_result", sheet_id),
        "live_arena:match:report_scheduling_problem": _title(
            "button_scheduling_problem", sheet_id
        ),
        "live_arena:availability:review_update": _title(
            "button_update_availability", sheet_id
        ),
        "live_arena:match:report_result": _title("button_report_result", sheet_id),
    }
    for item in getattr(view, "children", ()):
        custom_id = _text(getattr(item, "custom_id", ""))
        if custom_id in labels:
            item.label = labels[custom_id]


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        availability_reminder,
        competition_operations_runtime,
        panel,
        qualification_panel,
        result_views,
        runtime_hooks,
    )

    # Preload the configured copy before any persistent Live Arena views are
    # registered. A missing/invalid row fails registration rather than silently
    # exposing stale hardcoded player copy.
    original_register = panel.register_live_arena

    async def register_with_matchup_copy(bot):
        sheet_id = str(cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or "").strip()
        if sheet_id:
            await refresh_matchup_copy(sheet_id)
        return await original_register(bot)

    panel.register_live_arena = register_with_matchup_copy

    # Final visible embed renderer. This intentionally replaces the accumulated
    # base/full-set/availability wrappers with one Sheet-backed composition.
    original_match_embed = qualification_panel.match_embed
    _match_embed_from_sheet._original = original_match_embed
    qualification_panel.match_embed = _match_embed_from_sheet

    # Every state reconciliation already refreshes the result-control view. Add
    # an embed refresh after that so existing Q1/Q2/Q3/knockout threads receive
    # the clearer instructions on redeploy too.
    original_sync_round = runtime_hooks._sync_round_discord

    async def sync_round_with_matchup_copy(bot, qualification_service, snapshot):
        warnings = list(
            await original_sync_round(bot, qualification_service, snapshot)
        )
        warnings.extend(
            await _rerender_open_match_threads(bot, qualification_service, snapshot)
        )
        return list(dict.fromkeys(warnings))

    runtime_hooks._sync_round_discord = sync_round_with_matchup_copy

    # Relabel the final decorated match view by custom_id. This is resilient to
    # installer order and keeps callback wiring/custom IDs unchanged.
    original_match_result_init = result_views.MatchResultView.__init__

    def match_result_init_with_sheet_labels(self, sheet_id: str, **kwargs):
        original_match_result_init(self, sheet_id, **kwargs)
        _apply_match_button_labels(self, str(sheet_id))

    result_views.MatchResultView.__init__ = match_result_init_with_sheet_labels

    # The availability shortcut also appears outside MatchResultView. Keep its
    # standalone label sourced from the same MESSAGES row.
    original_availability_init = availability_reminder.WeeklyAvailabilityShortcutButton.__init__

    def availability_init_with_sheet_label(self, sheet_id: str):
        original_availability_init(self, sheet_id)
        if _templates(str(sheet_id)) is not None:
            self.label = _title("button_update_availability", str(sheet_id))

    availability_reminder.WeeklyAvailabilityShortcutButton.__init__ = (
        availability_init_with_sheet_label
    )

    # Use Sheet-backed confirmation/thread copy for the player scheduling-help
    # action while preserving its service/audit behavior.
    scheduling_button = competition_operations_runtime.ReportSchedulingProblemButton
    original_scheduling_callback = scheduling_button.callback

    async def scheduling_callback_with_sheet_copy(self, interaction: discord.Interaction):
        if _templates(str(self.sheet_id)) is None:
            await original_scheduling_callback(self, interaction)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = competition_operations_runtime.CompetitionOperationsService(
                self.sheet_id
            )
            await service.initialize()
            matches = await service.repository.matches()
            found = [
                row
                for row in matches
                if _text(row.get("thread_id")) == str(interaction.channel_id)
            ]
            if len(found) != 1:
                raise competition_operations_runtime.RegistrationError(
                    "This Duelling Deck thread could not be resolved uniquely"
                )
            await service.report_scheduling_problem(
                str(interaction.user.id), _text(found[0].get("match_id"))
            )
            await interaction.followup.send(
                embed=_template(
                    "scheduling_problem_recorded", str(self.sheet_id)
                ).embed(),
                ephemeral=True,
            )
            try:
                await interaction.channel.send(
                    embed=_template(
                        "scheduling_problem_thread_notice", str(self.sheet_id)
                    ).embed(reporter_mention=f"<@{interaction.user.id}>"),
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )
            except Exception:
                log.exception("Live Arena scheduling-problem thread notice failed")
        except Exception as exc:
            log.exception("Live Arena scheduling-problem report failed")
            await interaction.followup.send(
                embed=competition_operations_runtime.error_embed(exc),
                ephemeral=True,
            )

    scheduling_button.callback = scheduling_callback_with_sheet_copy

"""Discord/runtime integration for Live Arena Top 8 and knockout stages."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord

from shared.theme import colors

from modules.community.live_arena.knockout import KNOCKOUT, KnockoutService
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.qualification import QualificationService, QualificationSnapshot
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.knockout_runtime")
_installed = False

_PUBLIC = {"open", "active", "published", "published/open", "ready_to_close", "closed", "correction_in_progress"}


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel
    from modules.community.live_arena import tournament_lifecycle

    original_install = qualification_panel.install_qualification
    original_execute_lifecycle = tournament_lifecycle._execute_lifecycle

    def install_with_knockout(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_knockout_runtime_installed", False):
            return True
        manager._knockout_runtime_installed = True

        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if not callable(add_item):
                return result
            disabled = status is not None and status != "active"
            add_item(FreezeTop8Button(manager, disabled=disabled))
            add_item(OpenKnockoutButton(manager, disabled=disabled))
            return result

        manager.view = view

        base_sync = getattr(manager, "_competition_sync", None)

        async def competition_sync():
            warnings: list[str] = []
            if callable(base_sync):
                warnings.extend(await base_sync())
            service = KnockoutService(manager.sheet_id)
            await service.initialize()
            try:
                warnings.extend(await _reconcile_knockout(manager, service))
            except Exception:
                log.exception("Live Arena knockout reconciliation failed")
                warnings.append("knockout Discord state")
            return list(dict.fromkeys(warnings))

        manager._competition_sync = competition_sync

        # Result views keep a callback reference; the last installed competition sync
        # must be the one invoked after knockout result mutations.
        from modules.community.live_arena.result_views import set_post_mutation_sync
        set_post_mutation_sync(manager.sheet_id, competition_sync)
        return True

    async def execute_lifecycle_with_knockout_guard(interaction, manager, action):
        if action == "complete":
            try:
                service = KnockoutService(manager.sheet_id)
                await service.initialize()
                summary = await service.complete_tournament(str(interaction.user.id))
            except Exception as exc:
                log.exception("Live Arena completion blocked by knockout state")
                await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
                return
            await original_execute_lifecycle(interaction, manager, action)
            try:
                _, tournament, *_ = await manager.data(interaction.guild)
                if _text(getattr(tournament, "status", "")) == "completed":
                    await _sync_final_recap(manager, service, summary)
            except Exception:
                log.exception("Live Arena final recap synchronization failed")
            return
        await original_execute_lifecycle(interaction, manager, action)

    qualification_panel.install_qualification = install_with_knockout
    tournament_lifecycle._execute_lifecycle = execute_lifecycle_with_knockout_guard


class FreezeTop8Button(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Freeze Top 8",
            custom_id="live_arena:organizer:knockout:freeze_top8",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = KnockoutService(self.manager.sheet_id)
            await service.initialize()
            seeds = await service.freeze_top8(str(interaction.user.id))
            try:
                preview = await service.generate_quarterfinal_preview(str(interaction.user.id))
            except RegistrationError as exc:
                if "already exists" not in str(exc):
                    raise
                preview = await _snapshot(service, "quarterfinal")
            await _sync_preview_message(self.manager, service, preview)
            lines = [f"**#{seed['seed']}** <@{seed['discord_user_id']}>" for seed in seeds]
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Top 8 frozen",
                    description=(
                        "Qualification order is now immutable. The Quarterfinal preview has been generated in Captain's Table.\n\n"
                        + "\n".join(lines)
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
            await self.manager.sync()
        except Exception as exc:
            log.exception("Live Arena Top 8 freeze failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class OpenKnockoutButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Approve & Open Knockout",
            custom_id="live_arena:organizer:knockout:open",
            style=discord.ButtonStyle.success,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = KnockoutService(self.manager.sheet_id)
            await service.initialize()
            stage = await _current_preview_stage(service)
            if stage is None:
                raise RegistrationError("There is no knockout preview waiting for approval")
            opened = await service.approve_and_open(str(interaction.user.id), stage)
            warnings = await KnockoutPublisher(self.manager.bot, service).reconcile(opened)
            await _retire_preview_message(self.manager, service, stage)
            try:
                await self.manager.sync()
            except Exception:
                log.exception("Live Arena organizer panel refresh after knockout publication failed")
                warnings.append("organizer panel")
            embed = discord.Embed(
                title=f"{KNOCKOUT[stage]['name']} opened",
                description=(
                    f"**{len(opened.matches)}** matchup{'s are' if len(opened.matches) != 1 else ' is'} now official. "
                    "The six-day round window starts now."
                ),
                color=colors.c1c_blue,
            )
            if stage == "final":
                embed.add_field(
                    name="Final confirmation",
                    value="The Final is BO5. A reported result does not become final until an organizer explicitly confirms it.",
                    inline=False,
                )
            if warnings:
                embed.add_field(
                    name="Sync warning",
                    value=("Sheet state is saved, but these Discord items need repair:\n" + "\n".join(f"• {item}" for item in dict.fromkeys(warnings)))[:1024],
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            log.exception("Live Arena knockout approval/publication failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class KnockoutPublisher:
    """Create only official knockout Discord resources after organizer approval."""

    def __init__(self, bot, service: KnockoutService):
        self.bot = bot
        self.service = service

    async def reconcile(self, snapshot: QualificationSnapshot) -> list[str]:
        from modules.community.live_arena import qualification_panel, runtime_hooks

        if snapshot.round_row is None or snapshot.status not in _PUBLIC:
            return []
        stage = _text(snapshot.round_row.get("round_stage")).lower()
        if stage not in KNOCKOUT:
            return []
        config = self.service.repository.config
        organizer = OrganizerService(
            self.service.sheet_id,
            repository=self.service.registration_repository,
            clock=self.service.clock,
        )
        await organizer.initialize()
        _, (_, tournament), _, slots = await organizer.context()
        warnings: list[str] = []
        matches = [dict(row) for row in snapshot.matches]
        try:
            forum = await qualification_panel._resolve_channel(self.bot, int(config["MATCH_FORUM_CHANNEL_ID"]))
        except Exception:
            log.exception("Live Arena knockout forum resolution failed")
            forum = None
            warnings.append("duelling-decks forum")

        if forum is not None:
            for match in matches:
                label = f"{KNOCKOUT[stage]['name']} match {_text(match.get('match_number'))} forum post"
                try:
                    existing = await qualification_panel._resolve_existing_thread(self.bot, _text(match.get("thread_id")))
                    if existing is not None:
                        continue
                    created = await forum.create_thread(
                        name=_thread_name(stage, match),
                        content=(f"<@{_text(match['player_a_discord_user_id'])}> <@{_text(match['player_b_discord_user_id'])}>"),
                        embed=qualification_panel.match_embed(tournament, snapshot.round_row, match, slots),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
                    thread = getattr(created, "thread", None)
                    if thread is None and isinstance(created, tuple):
                        thread = created[0]
                    if thread is None:
                        thread = created
                    try:
                        await _record_thread_id(self.service, _text(match["match_id"]), str(thread.id))
                    except Exception:
                        try:
                            await thread.delete(reason="Live Arena knockout thread ID persistence failed")
                        except Exception:
                            log.exception("Live Arena knockout untracked thread cleanup failed")
                        raise
                except Exception:
                    log.exception("Live Arena knockout matchup publication failed • match=%s", _text(match.get("match_id")))
                    warnings.append(label)

        refreshed = await _snapshot(self.service, stage)
        try:
            warnings.extend(await runtime_hooks._sync_round_discord(self.bot, self.service, refreshed))
        except Exception:
            log.exception("Live Arena knockout Victory Ledger synchronization failed")
            warnings.append("Victory Ledger overview")
        return list(dict.fromkeys(warnings))


async def _reconcile_knockout(manager, service: KnockoutService) -> list[str]:
    warnings: list[str] = []
    config = await load_config(manager.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    rounds = [row for row in await service.repository.rounds() if _text(row.get("tournament_id")) == tid]

    # Reconcile whichever knockout round is public now.
    for stage in ("final", "semifinal", "quarterfinal"):
        row = _round_for_stage(rounds, tid, stage)
        if row is not None and _text(row.get("status")) in _PUBLIC:
            warnings.extend(await KnockoutPublisher(manager.bot, service).reconcile(await _snapshot(service, stage)))
            break

    # After a round closes, generate exactly one organizer-only next preview.
    qf = _round_for_stage(rounds, tid, "quarterfinal")
    sf = _round_for_stage(rounds, tid, "semifinal")
    final = _round_for_stage(rounds, tid, "final")
    try:
        if qf is not None and _text(qf.get("status")) == "closed" and sf is None:
            preview = await service.generate_next_preview("system", "semifinal")
            await _sync_preview_message(manager, service, preview)
        elif sf is not None and _text(sf.get("status")) == "closed" and final is None:
            preview = await service.generate_next_preview("system", "final")
            await _sync_preview_message(manager, service, preview)
        else:
            for stage in ("final", "semifinal", "quarterfinal"):
                row = _round_for_stage(rounds, tid, stage)
                if row is not None and _text(row.get("status")) == "preview":
                    await _sync_preview_message(manager, service, await _snapshot(service, stage))
                    break
    except Exception:
        log.exception("Live Arena knockout preview reconciliation failed")
        warnings.append("Captain's Table knockout preview")
    return list(dict.fromkeys(warnings))


async def _sync_preview_message(manager, service, snapshot) -> None:
    from modules.community.live_arena.messages import load_pr5_config

    stage = _text(snapshot.round_row.get("round_stage")).lower()
    config, _ = await load_pr5_config(manager.sheet_id)
    channel_id = _text(config.get("ORGANIZER_CHANNEL_ID"))
    channel = manager.bot.get_channel(int(channel_id))
    if channel is None:
        channel = await manager.bot.fetch_channel(int(channel_id))
    tid = _text(snapshot.round_row.get("tournament_id"))
    resource = await service.registration_repository.discord_resource(tid, "knockout_preview", stage)
    message = None
    if resource and _text(resource.get("state")) == "active" and _text(resource.get("message_id")):
        try:
            message = await channel.fetch_message(int(_text(resource["message_id"])))
        except discord.NotFound:
            message = None
    embed = _preview_embed(snapshot)
    if message is None:
        message = await channel.send(embed=embed)
    else:
        await message.edit(embed=embed)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    await service.registration_repository.upsert_discord_resource(
        tournament_id=tid,
        resource_type="knockout_preview",
        resource_key=stage,
        channel_id=channel_id,
        message_id=str(message.id),
        created_at_utc=_text(resource.get("created_at_utc")) if resource else now,
        updated_at_utc=now,
        state="active",
        notes="Organizer-only knockout preview; no player-facing resources until approval",
    )


async def _retire_preview_message(manager, service, stage: str) -> None:
    from modules.community.live_arena.messages import load_pr5_config

    config = await load_config(manager.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    resource = await service.registration_repository.discord_resource(tid, "knockout_preview", stage)
    if not resource or _text(resource.get("state")) != "active":
        return
    message_id = _text(resource.get("message_id"))
    channel_id = _text(resource.get("channel_id"))
    if message_id and channel_id:
        try:
            channel = manager.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await manager.bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            pass
        except Exception:
            log.exception("Live Arena knockout preview deletion failed")
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    await service.registration_repository.upsert_discord_resource(
        tournament_id=tid,
        resource_type="knockout_preview",
        resource_key=stage,
        channel_id=channel_id,
        message_id=message_id,
        created_at_utc=_text(resource.get("created_at_utc")),
        updated_at_utc=now,
        state="retired",
        notes="Knockout preview retired after official publication",
    )


async def _sync_final_recap(manager, service: KnockoutService, summary) -> None:
    from modules.community.live_arena import qualification_panel

    config = service.repository.config
    channel = await qualification_panel._resolve_channel(manager.bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"]))
    seeds = await service.seed_snapshot()
    rounds = await service.repository.rounds()
    matches = await service.repository.matches()
    tid = summary["tournament_id"]
    qf = [row for row in matches if _text(row.get("tournament_id")) == tid and _text(row.get("round_id")) == f"{tid}-QF"]
    sf = [row for row in matches if _text(row.get("tournament_id")) == tid and _text(row.get("round_id")) == f"{tid}-SF"]
    final = [row for row in matches if _text(row.get("tournament_id")) == tid and _text(row.get("round_id")) == f"{tid}-F"]
    champion = summary["champion_discord_user_id"]
    runner = summary["runner_up_discord_user_id"]
    semifinalists = []
    for row in sf:
        winner = _text(row.get("final_winner_discord_user_id"))
        for uid in (_text(row.get("player_a_discord_user_id")), _text(row.get("player_b_discord_user_id"))):
            if uid and uid != winner:
                semifinalists.append(uid)
    embed = discord.Embed(
        title="Tournament complete",
        description=f"🏆 **Champion:** <@{champion}>\n🥈 **Runner-up:** <@{runner}>",
        color=colors.c1c_blue,
    )
    if semifinalists:
        embed.add_field(name="Semifinalists", value="\n".join(f"<@{uid}>" for uid in semifinalists), inline=False)
    embed.add_field(name="Top 8", value="\n".join(f"**#{seed['seed']}** <@{seed['discord_user_id']}>" for seed in seeds)[:1024], inline=False)
    participants = await service.registration_repository.participants()
    participated = []
    qualification_ids = {f"{tid}-Q1", f"{tid}-Q2", f"{tid}-Q3"}
    played_ids = set()
    for row in matches:
        if _text(row.get("tournament_id")) != tid or _text(row.get("round_id")) not in qualification_ids:
            continue
        if _text(row.get("status")) not in {"finalized", "forfeit", "double_forfeit", "bye"}:
            continue
        played_ids.update(filter(None, (_text(row.get("player_a_discord_user_id")), _text(row.get("player_b_discord_user_id")))))
    participated = [
        _text(row.get("discord_user_id"))
        for row in participants
        if _text(row.get("tournament_id")) == tid and _text(row.get("discord_user_id")) in played_ids
    ]
    if participated:
        embed.add_field(name="Crew who took the field", value=" ".join(f"<@{uid}>" for uid in participated)[:1024], inline=False)
    await channel.send(embed=embed)


async def _record_thread_id(service, match_id: str, thread_id: str) -> None:
    helper = QualificationService(
        service.sheet_id,
        registration_repository=service.registration_repository,
        qualification_repository=service.repository,
        clock=service.clock,
    )
    await helper.record_thread_id(match_id, thread_id)


async def _snapshot(service: KnockoutService, stage: str) -> QualificationSnapshot:
    meta = KNOCKOUT[stage]
    config = await load_config(service.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    round_id = f"{tid}-{meta['suffix']}"
    rounds = await service.repository.rounds()
    matches = await service.repository.matches()
    row = _round_by_id(rounds, tid, round_id)
    qmatches = tuple(sorted(
        [dict(item) for item in matches if _text(item.get("tournament_id")) == tid and _text(item.get("round_id")) == round_id],
        key=lambda item: int(_text(item.get("match_number")) or 0),
    ))
    return QualificationSnapshot(dict(row) if row else None, qmatches)


async def _current_preview_stage(service: KnockoutService) -> str | None:
    config = await load_config(service.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    rounds = await service.repository.rounds()
    for stage in ("final", "semifinal", "quarterfinal"):
        row = _round_for_stage(rounds, tid, stage)
        if row is not None and _text(row.get("status")) == "preview":
            return stage
    return None


def _preview_embed(snapshot) -> discord.Embed:
    stage = _text(snapshot.round_row.get("round_stage")).lower()
    name = KNOCKOUT[stage]["name"]
    embed = discord.Embed(
        title=f"{name} · Organizer Preview",
        description="This draw is **not official**. No Duelling Deck threads or player notifications exist until approval.",
        color=colors.c1c_blue,
    )
    for row in snapshot.matches:
        embed.add_field(
            name=f"Match {_text(row.get('match_number'))}",
            value=(f"**{_text(row.get('player_a_display_name'))}** vs **{_text(row.get('player_b_display_name'))}**"),
            inline=False,
        )
    if stage == "final":
        embed.add_field(name="Format", value="BO5 · organizer confirmation required for the final result", inline=False)
    else:
        embed.add_field(name="Format", value="BO3", inline=False)
    return embed


def _thread_name(stage: str, match) -> str:
    label = {"quarterfinal": "QF", "semifinal": "SF", "final": "Final"}[stage]
    raw = f"{label} • M{int(_text(match.get('match_number')) or 0):02d} • {_text(match.get('player_a_display_name'))} vs {_text(match.get('player_b_display_name'))}"
    return raw[:100]


def _round_for_stage(rounds, tid, stage):
    return _round_by_id(rounds, tid, f"{tid}-{KNOCKOUT[stage]['suffix']}")


def _round_by_id(rounds, tid, round_id):
    found = [row for row in rounds if _text(row.get("tournament_id")) == tid and _text(row.get("round_id")) == round_id]
    if len(found) > 1:
        raise RegistrationError(f"ROUNDS contains duplicate {round_id}")
    return found[0] if found else None

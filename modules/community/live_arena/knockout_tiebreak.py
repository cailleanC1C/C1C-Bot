"""Organizer-recorded qualification tiebreak outcomes for Live Arena Top 8."""

from __future__ import annotations

import json
from datetime import UTC

import discord

from shared.theme import colors

from modules.community.live_arena import knockout
from modules.community.live_arena.competition import calculate_qualification_standings
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.qualification import ROUND_HEADERS
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.views import error_embed

_installed = False
_TB_SUFFIX = "TB"


def install() -> None:
    """Add a manual, auditable BO3 tiebreak outcome path without inventing tie logic."""
    global _installed
    if _installed:
        return
    _installed = True

    original_freeze = knockout.KnockoutService.freeze_top8

    async def freeze_top8_with_recorded_tiebreaks(self, actor_id: str):
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        rounds = await self.repository.rounds()
        matches = await self.repository.matches()
        standings = calculate_qualification_standings(matches, tid)
        affected = knockout._competitive_ties(standings[:8], standings)
        if not affected:
            return await original_freeze(self, actor_id)

        resolutions = _read_resolutions(_tiebreak_row(rounds, tid))
        ordered = _apply_resolutions(standings, affected, resolutions)
        if ordered is None:
            names = ", ".join(entry.display_name for entry in affected)
            raise RegistrationError(
                "Organizer Tiebreak Required. Record the BO3 tiebreak outcome before freezing Top 8: "
                + names
            )

        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            existing = knockout._seed_row(old_rounds, tid)
            if existing is not None:
                if _text(existing.get("status")) == "frozen":
                    return knockout._read_seeds(existing)
                raise RegistrationError("Top 8 seed state already exists and requires organizer review")
            q3 = knockout._round_by_id(old_rounds, tid, f"{tid}-Q3")
            if q3 is None or _text(q3.get("status")) != "closed":
                raise RegistrationError("Qualification Round 3 must be closed before Top 8 can be frozen")

            top8 = ordered[:8]
            seeds = [
                {
                    "seed": index,
                    "discord_user_id": entry.discord_user_id,
                    "display_name": entry.display_name,
                    "qualification_rank": entry.rank,
                    "record": entry.match_record,
                }
                for index, entry in enumerate(top8, 1)
            ]
            now = utc_iso(self.clock().astimezone(UTC))
            row = {header: "" for header in ROUND_HEADERS}
            row.update(
                tournament_id=tid,
                round_id=f"{tid}-{knockout.SEED_ROUND_SUFFIX}",
                round_name="Top 8 Seeding",
                round_stage="top8_seeding",
                round_number="3",
                status="frozen",
                approved_at_utc=now,
                approved_by_discord_user_id=str(actor_id),
                completed_at_utc=now,
                notes=json.dumps({"seeds": seeds}, sort_keys=True, separators=(",", ":")),
            )
            updated_rounds = [dict(item) for item in old_rounds] + [row]
            await self.repository.persist_rounds(updated_rounds, previous_rounds=old_rounds)
            await self._audit(
                tid,
                actor_id,
                "top8_seeds_frozen",
                {"seeds": seeds, "tiebreak_resolutions_applied": True},
                now,
            )
            return seeds

    knockout.KnockoutService.freeze_top8 = freeze_top8_with_recorded_tiebreaks

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_with_tiebreak(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_knockout_tiebreak_installed", False):
            return True
        manager._knockout_tiebreak_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if callable(add_item):
                add_item(
                    RecordTiebreakButton(
                        manager,
                        disabled=status is not None and status != "active",
                    )
                )
            return result

        manager.view = view
        return True

    qualification_panel.install_qualification = install_with_tiebreak


class RecordTiebreakButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Record BO3 Tiebreak",
            custom_id="live_arena:organizer:knockout:tiebreak",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.send_modal(RecordTiebreakModal(self.manager))


class RecordTiebreakModal(discord.ui.Modal, title="Record Qualification Tiebreak"):
    order = discord.ui.TextInput(
        label="Finishing order (Discord IDs)",
        placeholder="123, 456, 789",
        min_length=3,
        max_length=500,
    )
    reason = discord.ui.TextInput(
        label="BO3 tiebreak result / reason",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=500,
    )

    def __init__(self, manager):
        super().__init__(timeout=600)
        self.manager = manager

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            ids = [part.strip() for part in str(self.order.value).replace("\n", ",").split(",") if part.strip()]
            if len(ids) < 2 or any(not value.isdigit() for value in ids):
                raise RegistrationError("Enter at least two Discord user IDs separated by commas")
            service = knockout.KnockoutService(self.manager.sheet_id)
            await service.initialize()
            await record_tiebreak_resolution(
                service,
                str(interaction.user.id),
                ids,
                str(self.reason.value),
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Tiebreak outcome recorded",
                    description=(
                        "The order was saved only for the exact unresolved tied group. "
                        "Top 8 seeding can now use that BO3 outcome; no unrelated seeds can be reordered."
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
            await self.manager.sync()
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def record_tiebreak_resolution(service, actor_id: str, ordered_ids: list[str], reason: str) -> None:
    reason = str(reason or "").strip()
    if not reason:
        raise RegistrationError("A BO3 tiebreak result / reason is required")
    config = await load_config(service.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    async with _locks[(service.sheet_id, tid)]:
        old_rounds = await service.repository.rounds()
        matches = await service.repository.matches()
        q3 = knockout._round_by_id(old_rounds, tid, f"{tid}-Q3")
        if q3 is None or _text(q3.get("status")) != "closed":
            raise RegistrationError("Qualification Round 3 must be closed before recording a tiebreak")
        if knockout._seed_row(old_rounds, tid) is not None:
            raise RegistrationError("Top 8 seeds are already frozen")

        standings = calculate_qualification_standings(matches, tid)
        affected = knockout._competitive_ties(standings[:8], standings)
        groups = _affected_groups(affected)
        candidate = set(ordered_ids)
        matching = [group for group in groups if set(group) == candidate]
        if len(matching) != 1 or len(candidate) != len(ordered_ids):
            raise RegistrationError(
                "The entered IDs must match exactly one current Organizer Tiebreak Required group"
            )

        now = utc_iso(service.clock().astimezone(UTC))
        row = _tiebreak_row(old_rounds, tid)
        resolutions = _read_resolutions(row)
        key = sorted(matching[0])
        resolutions = [item for item in resolutions if sorted(item.get("group", [])) != key]
        resolutions.append(
            {
                "group": key,
                "order": ordered_ids,
                "reason": reason,
                "resolved_by_discord_user_id": str(actor_id),
                "resolved_at_utc": now,
            }
        )
        updated = {header: "" for header in ROUND_HEADERS}
        if row is not None:
            updated.update(dict(row))
        updated.update(
            tournament_id=tid,
            round_id=f"{tid}-{_TB_SUFFIX}",
            round_name="Qualification Tiebreaks",
            round_stage="qualification_tiebreak",
            round_number="3",
            status="resolved",
            completed_at_utc=now,
            approved_at_utc=now,
            approved_by_discord_user_id=str(actor_id),
            notes=json.dumps({"resolutions": resolutions}, sort_keys=True, separators=(",", ":")),
        )
        rounds = [
            dict(item)
            for item in old_rounds
            if not (
                _text(item.get("tournament_id")) == tid
                and _text(item.get("round_id")) == f"{tid}-{_TB_SUFFIX}"
            )
        ] + [updated]
        await service.repository.persist_rounds(rounds, previous_rounds=old_rounds)
        await service._audit(
            tid,
            actor_id,
            "qualification_tiebreak_recorded",
            {"group": key, "order": ordered_ids, "reason": reason},
            now,
        )


def _affected_groups(affected) -> list[list[str]]:
    by_rank: dict[int, list[str]] = {}
    for entry in affected:
        by_rank.setdefault(int(entry.rank), []).append(entry.discord_user_id)
    return [sorted(ids) for ids in by_rank.values() if len(ids) >= 2]


def _apply_resolutions(standings, affected, resolutions):
    groups = _affected_groups(affected)
    resolved = {tuple(sorted(item.get("group", []))): item.get("order", []) for item in resolutions}
    order_index: dict[str, tuple[int, int]] = {}
    for group in groups:
        order = resolved.get(tuple(sorted(group)))
        if not order or set(order) != set(group) or len(order) != len(group):
            return None
        for offset, uid in enumerate(order):
            order_index[uid] = (next(entry.rank for entry in standings if entry.discord_user_id == uid), offset)

    indexed = list(enumerate(standings))
    indexed.sort(
        key=lambda pair: (
            order_index.get(pair[1].discord_user_id, (pair[1].rank, pair[0]))[0],
            order_index.get(pair[1].discord_user_id, (pair[1].rank, pair[0]))[1],
            pair[0],
        )
    )
    return [entry for _, entry in indexed]


def _tiebreak_row(rounds, tid):
    return knockout._round_by_id(rounds, tid, f"{tid}-{_TB_SUFFIX}")


def _read_resolutions(row):
    if row is None:
        return []
    try:
        payload = json.loads(_text(row.get("notes")) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RegistrationError("Qualification tiebreak metadata is invalid") from exc
    resolutions = payload.get("resolutions", [])
    return resolutions if isinstance(resolutions, list) else []

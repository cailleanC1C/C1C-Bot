"""Qualification bye support for Live Arena PR 6B-4."""

from __future__ import annotations

from datetime import UTC, timedelta

from modules.community.live_arena import qualification, qualification_panel, runtime_hooks, swiss, swiss_panel
from modules.community.live_arena.competition import MATCH_TERMINAL_STATUSES
from modules.community.live_arena.qualification import QualificationSnapshot
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import _text

_installed = False
_BYE_NOTE = "QUALIFICATION_BYE"


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    _install_q1()
    _install_swiss()
    _install_publishers()
    _install_q1_ui()


def _install_q1() -> None:
    original_generate = qualification.QualificationService._generate
    original_approve = qualification.QualificationService.approve_draw

    async def generate_with_random_bye(self, actor_id: str, *, regenerate: bool):
        config = await qualification.load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        participants = await self.registration_repository.participants()
        roster = [
            dict(row)
            for row in participants
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("status")) == "confirmed"
        ]
        if len(roster) % 2 == 0:
            return await original_generate(self, actor_id, regenerate=regenerate)

        _, (_, tournament), _, _ = await self.context()
        if _text(tournament.get("status")) != "signup_closed":
            raise RegistrationError("Q1 can only be generated after registration is closed")
        minimum = int(_text(tournament.get("min_participants")) or 0)
        if len(roster) < minimum:
            raise RegistrationError(
                f"Q1 requires at least {minimum} confirmed participants; currently {len(roster)}"
            )

        bye_player = self.rng.choice(roster)
        bye_id = _text(bye_player.get("discord_user_id"))
        original_participants = self.registration_repository.participants
        original_context = self.context

        async def even_participants():
            rows = await original_participants()
            return [
                row
                for row in rows
                if not (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("discord_user_id")) == bye_id
                )
            ]

        async def even_context():
            base, (row_number, current_tournament), clans, slots = await original_context()
            adjusted = dict(current_tournament)
            adjusted["min_participants"] = str(
                min(
                    int(_text(current_tournament.get("min_participants")) or 0),
                    len(roster) - 1,
                )
            )
            return base, (row_number, adjusted), clans, slots

        self.registration_repository.participants = even_participants
        self.context = even_context
        try:
            snapshot = await original_generate(self, actor_id, regenerate=regenerate)
        finally:
            self.registration_repository.participants = original_participants
            self.context = original_context

        old_matches = await self.repository.matches()
        round_id = f"{tid}-Q1"
        round_matches = [
            row
            for row in old_matches
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        ]
        bye = _bye_row(
            tid,
            round_id,
            len(round_matches) + 1,
            bye_player,
            status="proposed",
        )
        matches = [dict(row) for row in old_matches] + [bye]
        await self.repository.persist_matches(matches, previous_matches=old_matches)
        await self._audit(
            tid,
            str(actor_id),
            "qualification_bye_drawn",
            {"round_number": 1, "player_id": bye_id, "selection": "random"},
            utc_iso(self.clock().astimezone(UTC)),
        )
        return QualificationSnapshot(snapshot.round_row, tuple(snapshot.matches) + (bye,))

    async def approve_with_bye(self, actor_id: str):
        config = await qualification.load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        rows = await self.repository.matches()
        bye_rows = [
            dict(row)
            for row in rows
            if _is_bye(row)
            and _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == f"{tid}-Q1"
        ]
        if not bye_rows:
            return await original_approve(self, actor_id)
        if len(bye_rows) != 1:
            raise RegistrationError("Q1 contains more than one bye row")

        bye = bye_rows[0]
        bye_id = _text(bye.get("player_a_discord_user_id"))
        original_participants = self.registration_repository.participants
        original_matches = self.repository.matches

        async def even_participants():
            source = await original_participants()
            return [
                row
                for row in source
                if not (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("discord_user_id")) == bye_id
                )
            ]

        async def matches_without_bye():
            source = await original_matches()
            return [
                row
                for row in source
                if not (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("match_id")) == _text(bye.get("match_id"))
                )
            ]

        self.registration_repository.participants = even_participants
        self.repository.matches = matches_without_bye
        try:
            snapshot = await original_approve(self, actor_id)
        finally:
            self.registration_repository.participants = original_participants
            self.repository.matches = original_matches

        current = await self.repository.matches()
        now = utc_iso(self.clock().astimezone(UTC))
        bye.update(
            status="bye",
            published_at_utc=now,
            deadline_at_utc=_text(snapshot.round_row.get("deadline_at_utc")),
            final_result_type="bye",
            final_score_a="",
            final_score_b="",
            final_winner_discord_user_id=bye_id,
            finalized_by_discord_user_id=str(actor_id),
            finalized_at_utc=now,
            confirmed_at_utc=now,
        )
        updated = [dict(row) for row in current] + [bye]
        await self.repository.persist_matches(updated, previous_matches=current)
        return QualificationSnapshot(snapshot.round_row, tuple(snapshot.matches) + (bye,))

    qualification.QualificationService._generate = generate_with_random_bye
    qualification.QualificationService.approve_draw = approve_with_bye


def _install_swiss() -> None:
    original_generate = swiss.SwissQualificationService.generate_preview
    original_publish = swiss.SwissQualificationService.publish_approved
    original_validate = swiss._validate_persisted_draw

    async def generate_with_ranked_bye(self, actor_id: str, round_number: int, *, regenerate: bool = False):
        config = await swiss.load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        participants = await self.registration_repository.participants()
        roster = [
            dict(row)
            for row in participants
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("status")) == "confirmed"
        ]
        if len(roster) % 2 == 0:
            return await original_generate(self, actor_id, round_number, regenerate=regenerate)

        prior_matches = await self.repository.matches()
        standings = swiss.calculate_qualification_standings(prior_matches, tid)
        players = swiss._players_from_roster(roster, standings)
        bye_player = choose_ranked_bye(players, prior_matches, tid)
        bye_id = bye_player.user_id
        original_participants = self.registration_repository.participants

        async def even_participants():
            rows = await original_participants()
            return [
                row
                for row in rows
                if not (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("discord_user_id")) == bye_id
                )
            ]

        self.registration_repository.participants = even_participants
        try:
            snapshot = await original_generate(
                self, actor_id, round_number, regenerate=regenerate
            )
        finally:
            self.registration_repository.participants = original_participants

        current = await self.repository.matches()
        round_id = f"{tid}-Q{round_number}"
        round_matches = [
            row
            for row in current
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        ]
        roster_by_id = {
            _text(row.get("discord_user_id")): row
            for row in roster
        }
        bye = _bye_row(
            tid,
            round_id,
            len(round_matches) + 1,
            roster_by_id[bye_id],
            status="preview",
        )
        bye["notes"] = (
            f"{_BYE_NOTE} · lowest-ranked eligible player · "
            f"previous_bye={'yes' if bye_id in previous_bye_users(prior_matches, tid) else 'no'}"
        )
        updated = [dict(row) for row in current] + [bye]
        await self.repository.persist_matches(updated, previous_matches=current)
        await self._audit(
            tid,
            str(actor_id),
            "qualification_bye_drawn",
            {
                "round_number": int(round_number),
                "player_id": bye_id,
                "selection": "lowest_ranked_without_previous_bye",
            },
            utc_iso(self.clock().astimezone(UTC)),
        )
        return QualificationSnapshot(snapshot.round_row, tuple(snapshot.matches) + (bye,))

    async def publish_with_bye(self, actor_id: str, round_number: int):
        snapshot = await original_publish(self, actor_id, round_number)
        bye_rows = [dict(row) for row in snapshot.matches if _is_bye(row)]
        if not bye_rows:
            return snapshot
        if len(bye_rows) != 1:
            raise RegistrationError(f"Q{round_number} contains more than one bye row")
        old_matches = await self.repository.matches()
        matches = [dict(row) for row in old_matches]
        now = utc_iso(self.clock().astimezone(UTC))
        target = next(
            row
            for row in matches
            if _text(row.get("match_id")) == _text(bye_rows[0].get("match_id"))
        )
        player_id = _text(target.get("player_a_discord_user_id"))
        target.update(
            status="bye",
            final_result_type="bye",
            final_score_a="",
            final_score_b="",
            final_winner_discord_user_id=player_id,
            finalized_by_discord_user_id=str(actor_id),
            finalized_at_utc=now,
            confirmed_at_utc=now,
        )
        await self.repository.persist_matches(matches, previous_matches=old_matches)
        refreshed = await self.snapshot(round_number)
        return refreshed

    def validate_with_bye(matches, tid: str, round_number: int) -> None:
        current = [
            row
            for row in matches
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == f"{tid}-Q{round_number}"
        ]
        byes = [row for row in current if _is_bye(row)]
        if len(byes) > 1:
            raise RegistrationError("Swiss draw contains more than one bye")
        normal = [row for row in matches if row not in byes]
        original_validate(normal, tid, round_number)
        if byes:
            bye_id = _text(byes[0].get("player_a_discord_user_id"))
            if not bye_id or _text(byes[0].get("player_b_discord_user_id")):
                raise RegistrationError("Swiss bye row is invalid")
            seen = {
                _text(row.get(key))
                for row in current
                if not _is_bye(row)
                for key in ("player_a_discord_user_id", "player_b_discord_user_id")
            }
            if bye_id in seen:
                raise RegistrationError("Swiss bye player also appears in a matchup")

    swiss.SwissQualificationService.generate_preview = generate_with_ranked_bye
    swiss.SwissQualificationService.publish_approved = publish_with_bye
    swiss._validate_persisted_draw = validate_with_bye


def _install_publishers() -> None:
    original_q1 = qualification_panel.QualificationPublisher.reconcile
    original_swiss = swiss_panel.SwissPublisher.reconcile

    async def q1_reconcile_without_bye_thread(self):
        real_service = self.service
        real_snapshot = real_service.snapshot
        full_snapshot = await real_snapshot()
        has_bye = any(_is_bye(row) for row in full_snapshot.matches)

        async def filtered_snapshot():
            snap = await real_snapshot()
            return QualificationSnapshot(
                snap.round_row,
                tuple(row for row in snap.matches if not _is_bye(row)),
            )

        real_service.snapshot = filtered_snapshot
        try:
            warnings = list(await original_q1(self))
        finally:
            real_service.snapshot = real_snapshot
        if not has_bye:
            return list(dict.fromkeys(warnings))
        try:
            snap = await real_snapshot()
            warnings.extend(
                await runtime_hooks._sync_round_discord(self.bot, real_service, snap)
            )
        except Exception:
            warnings.append("Victory Ledger overview")
        return list(dict.fromkeys(warnings))

    async def swiss_reconcile_without_bye_thread(self, snapshot=None):
        snapshot = snapshot or await self._current_open_snapshot()
        if snapshot is not None:
            snapshot = QualificationSnapshot(
                snapshot.round_row,
                tuple(row for row in snapshot.matches if not _is_bye(row)),
            )
        return await original_swiss(self, snapshot)

    qualification_panel.QualificationPublisher.reconcile = q1_reconcile_without_bye_thread
    swiss_panel.SwissPublisher.reconcile = swiss_reconcile_without_bye_thread


def _install_q1_ui() -> None:
    original_view_init = qualification_panel.SwapPlayersView.__init__
    original_proposal = qualification_panel.proposal_embed

    def swap_view_without_bye(self, manager, snapshot):
        filtered = QualificationSnapshot(
            snapshot.round_row,
            tuple(row for row in snapshot.matches if not _is_bye(row)),
        )
        original_view_init(self, manager, filtered)

    def proposal_with_bye(snapshot):
        normal = QualificationSnapshot(
            snapshot.round_row,
            tuple(row for row in snapshot.matches if not _is_bye(row)),
        )
        embed = original_proposal(normal)
        bye = next((row for row in snapshot.matches if _is_bye(row)), None)
        if bye is not None:
            embed.add_field(
                name="Q1 Bye",
                value=(
                    f"**{_text(bye.get('player_a_display_name'))}** receives the random Q1 bye. "
                    "It counts as one match win and +2 game differential when the round opens."
                ),
                inline=False,
            )
        return embed

    qualification_panel.SwapPlayersView.__init__ = swap_view_without_bye
    qualification_panel.proposal_embed = proposal_with_bye


def choose_ranked_bye(players, matches, tid: str):
    if not players:
        raise RegistrationError("No eligible player exists for a qualification bye")
    previous = previous_bye_users(matches, tid)
    fresh = [player for player in players if player.user_id not in previous]
    pool = fresh or list(players)
    return max(pool, key=lambda player: (player.ranking_index, player.user_id))


def previous_bye_users(matches, tid: str) -> set[str]:
    return {
        _text(row.get("player_a_discord_user_id"))
        for row in matches
        if _text(row.get("tournament_id")) == str(tid)
        and (
            _text(row.get("status")) == "bye"
            or _text(row.get("final_result_type")) == "bye"
        )
        and _text(row.get("player_a_discord_user_id"))
    }


def _bye_row(tid: str, round_id: str, number: int, player, *, status: str):
    row = qualification._blank(qualification.MATCH_HEADERS)
    row.update(
        tournament_id=tid,
        round_id=round_id,
        match_id=f"{round_id}-M{number:02d}",
        match_number=str(number),
        player_a_discord_user_id=_text(player.get("discord_user_id")),
        player_a_display_name=_text(player.get("display_name_at_signup")),
        player_b_discord_user_id="",
        player_b_display_name="",
        status=status,
        shared_slot_ids_csv="",
        has_scheduling_conflict="FALSE",
        scheduling_conflict_notes="",
        extension_count="0",
        notes=_BYE_NOTE,
    )
    return row


def _is_bye(row) -> bool:
    return (
        _text(row.get("final_result_type")) == "bye"
        or _text(row.get("status")) == "bye"
        or _BYE_NOTE in _text(row.get("notes"))
    )

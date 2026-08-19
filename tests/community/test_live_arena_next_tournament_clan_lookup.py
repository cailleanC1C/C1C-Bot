from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import next_tournament
from modules.community.live_arena import next_tournament_clan_lookup as lookup
from modules.community.live_arena import next_tournament_wizard_ux as wizard


class _Response:
    def __init__(self):
        self.done = False

    def is_done(self):
        return self.done

    async def defer(self, **_kwargs):
        self.done = True

    async def send_message(self, **_kwargs):
        self.done = True


class _Template:
    def __init__(self):
        self.calls = []

    def embed(self, **values):
        self.calls.append(values)
        return ("embed", values)


class _Role:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class _Guild:
    def __init__(self, roles):
        self.roles = list(roles)
        self._by_id = {role.id: role for role in roles}

    def get_role(self, role_id):
        return self._by_id.get(role_id)


class _Interaction:
    def __init__(self, guild=None):
        self.response = _Response()
        self.followup = SimpleNamespace(send=AsyncMock())
        self.edit_original_response = AsyncMock()
        self.user = SimpleNamespace(id=42)
        self.guild = guild


@pytest.mark.asyncio
async def test_clan_lookup_loader_uses_config_driven_tab_and_header_contract(monkeypatch):
    calls = []

    async def fetch(_sheet_id, tab):
        calls.append(tab)
        if tab == "CONFIG":
            return [
                ["Key", "Value", "Notes / clear name"],
                ["CLAN_TAB", "clan_lookup", "master clans"],
            ]
        if tab == "clan_lookup":
            return [
                ["clan_name", "clan_tag"],
                ["Elders", "C1CE"],
                ["Martyrs", "C1CM"],
            ]
        raise AssertionError(tab)

    monkeypatch.setattr(next_tournament, "afetch_values", fetch)

    assert await lookup._load_clan_lookup("sheet-1") == [
        ("Elders", "C1CE"),
        ("Martyrs", "C1CM"),
    ]
    assert calls == ["CONFIG", "clan_lookup"]


@pytest.mark.asyncio
async def test_clan_lookup_loader_rejects_duplicate_tags(monkeypatch):
    async def fetch(_sheet_id, tab):
        if tab == "CONFIG":
            return [
                ["Key", "Value", "Notes / clear name"],
                ["CLAN_TAB", "clan_lookup", ""],
            ]
        return [
            ["clan_name", "clan_tag"],
            ["Martyrs", "C1CM"],
            ["Martyrs Again", "c1cm"],
        ]

    monkeypatch.setattr(next_tournament, "afetch_values", fetch)

    with pytest.raises(next_tournament.LiveArenaConfigError, match="duplicate clan_tag"):
        await lookup._load_clan_lookup("sheet-1")


@pytest.mark.asyncio
async def test_clan_options_come_from_master_lookup_without_inherited_defaults(monkeypatch):
    async def load(_sheet_id):
        return [
            ("Elders", "C1CE"),
            ("Martyrs", "C1CM"),
            ("Druids", "C1CD"),
        ]

    monkeypatch.setattr(lookup, "_load_clan_lookup", load)

    service = next_tournament.NextTournamentService("sheet-1")
    options = await service.clan_options()

    assert [item.label for item in options] == [
        "C1CE · Elders",
        "C1CM · Martyrs",
        "C1CD · Druids",
    ]
    assert [item.discord_role_id for item in options] == ["C1CE", "C1CM", "C1CD"]
    assert not any(item.active_current for item in options)

    view = wizard.TournamentEligibilityView(SimpleNamespace(sheet_id="sheet-1"), object(), options)
    select = view.children[0]
    assert [option.value for option in select.options] == ["C1CE", "C1CM", "C1CD"]
    assert not any(option.default for option in select.options)


def test_role_resolver_matches_stylized_discord_clan_role(monkeypatch):
    role = _Role(706438713332465694, "﹝𝖢1𝖢𝖬﹞martyrs")
    guild = _Guild([role])
    option = next_tournament.ClanOption("C1CM", "Martyrs", "C1CM")
    monkeypatch.setattr(
        lookup.runtime_config,
        "get_clan_role_ids",
        lambda: {706438713332465694},
    )

    assert lookup._resolve_discord_role_id(guild, option) == "706438713332465694"


def test_role_resolver_rejects_unconfigured_matching_role(monkeypatch):
    guild = _Guild([_Role(123, "C1CM Martyrs")])
    option = next_tournament.ClanOption("C1CM", "Martyrs", "C1CM")
    monkeypatch.setattr(lookup.runtime_config, "get_clan_role_ids", lambda: {999})

    with pytest.raises(RuntimeError, match="configured CLAN_ROLE_IDS"):
        lookup._resolve_discord_role_id(guild, option)


@pytest.mark.asyncio
async def test_clan_selection_resolves_tag_to_role_and_advances_to_review(monkeypatch):
    role_id = 706438713332465694
    guild = _Guild([_Role(role_id, "﹝𝖢1𝖢𝖬﹞martyrs")])
    monkeypatch.setattr(lookup.runtime_config, "get_clan_role_ids", lambda: {role_id})

    manager = SimpleNamespace(sheet_id="sheet-1")
    draft = next_tournament.NextTournamentDraft(
        tournament_name="Rum, Cookies & Carnage",
        short_name="Carnage",
        min_participants=8,
        max_participants=16,
        timezone="UTC",
        signup_opens_at_utc="2099-09-01T18:00:00Z",
        signup_closes_at_utc="2099-09-06T18:00:00Z",
        tournament_motto="Fueled by rum. Bribed with cookies. Bound for trouble.",
    )
    options = [next_tournament.ClanOption("C1CM", "Martyrs", "C1CM")]
    view = wizard.TournamentEligibilityView(manager, draft, options)
    select = view.children[0]
    select._values = ["C1CM"]
    interaction = _Interaction(guild)
    template = _Template()

    async def load_messages(_sheet_id, keys):
        assert keys == {"next_tournament_wizard_review"}
        return {"next_tournament_wizard_review": template}

    monkeypatch.setattr(next_tournament, "_load_next_messages", load_messages)

    await select.callback(interaction)

    kwargs = interaction.edit_original_response.await_args.kwargs
    assert isinstance(kwargs["view"], next_tournament.ConfirmCreateNextTournamentView)
    final_draft = kwargs["view"].draft
    assert final_draft.eligible_role_ids == (str(role_id),)
    assert final_draft.eligible_clan_tags == ("C1CM",)
    assert template.calls[0]["eligible_clans"] == "C1CM · Martyrs"


@pytest.mark.asyncio
async def test_legacy_numeric_clan_option_still_works_without_guild(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet-1")
    draft = next_tournament.NextTournamentDraft(
        tournament_name="Legacy",
        short_name="Legacy",
        min_participants=8,
        max_participants=16,
        timezone="UTC",
        signup_opens_at_utc="2099-09-01T18:00:00Z",
        signup_closes_at_utc="2099-09-06T18:00:00Z",
        tournament_motto="",
    )
    options = [next_tournament.ClanOption("C1C", "Pirate Crew", "111")]
    view = wizard.TournamentEligibilityView(manager, draft, options)
    select = view.children[0]
    select._values = ["111"]
    interaction = _Interaction()
    template = _Template()

    async def load_messages(_sheet_id, _keys):
        return {"next_tournament_wizard_review": template}

    monkeypatch.setattr(next_tournament, "_load_next_messages", load_messages)

    await select.callback(interaction)

    final_draft = interaction.edit_original_response.await_args.kwargs["view"].draft
    assert final_draft.eligible_role_ids == ("111",)
    assert final_draft.eligible_clan_tags == ("C1C",)


@pytest.mark.asyncio
async def test_create_boundary_injects_lookup_clan_for_existing_transaction(monkeypatch):
    captured = {}

    async def load(_sheet_id):
        return [("Martyrs", "C1CM")]

    async def original_create(self, actor_id, draft):
        matrix = [
            list(next_tournament.ELIGIBLE_CLAN_HEADERS),
            ["OLD", "OLD", "Old", "999", "TRUE", "history"],
        ]
        rows = next_tournament._rows(
            matrix,
            next_tournament.ELIGIBLE_CLAN_HEADERS,
            "ELIGIBLE_CLANS",
        )
        captured["rows"] = rows
        captured["actor"] = actor_id
        return "LA-NEW"

    monkeypatch.setattr(lookup, "_load_clan_lookup", load)
    monkeypatch.setattr(
        next_tournament.NextTournamentService,
        "_clan_lookup_original_create",
        original_create,
    )

    draft = next_tournament.NextTournamentDraft(
        eligible_role_ids=("706438713332465694",),
        eligible_clan_tags=("C1CM",),
    )
    service = next_tournament.NextTournamentService("sheet-1")

    assert await lookup._create_with_lookup(service, "42", draft) == "LA-NEW"
    assert captured["actor"] == "42"
    assert captured["rows"][-1] == {
        "tournament_id": "__lookup__",
        "clan_tag": "C1CM",
        "clan_name": "Martyrs",
        "discord_role_id": "706438713332465694",
        "active": "TRUE",
        "notes": "Resolved from CLAN_TAB for Create Next Tournament.",
    }


@pytest.mark.asyncio
async def test_create_boundary_leaves_legacy_draft_path_unchanged(monkeypatch):
    calls = []

    async def original_create(self, actor_id, draft):
        calls.append((self.sheet_id, actor_id, draft))
        return "legacy-ok"

    monkeypatch.setattr(
        next_tournament.NextTournamentService,
        "_clan_lookup_original_create",
        original_create,
    )
    draft = next_tournament.NextTournamentDraft(eligible_role_ids=("111",))
    service = next_tournament.NextTournamentService("sheet-1")

    assert await lookup._create_with_lookup(service, "42", draft) == "legacy-ok"
    assert calls == [("sheet-1", "42", draft)]

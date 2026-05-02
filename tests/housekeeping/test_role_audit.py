from types import SimpleNamespace
from datetime import datetime, timezone

from modules.housekeeping import role_audit


class DummyMember(SimpleNamespace):
    @property
    def mention(self) -> str:  # pragma: no cover - property used in formatting
        return f"<@{self.id}>"


class DummyRole(SimpleNamespace):
    pass


def test_classify_roles_covers_stray_and_wander_cases():
    clan_roles = {10, 11}

    assert (
        role_audit._classify_roles({1}, raid_role_id=1, wanderer_role_id=2, clan_role_ids=clan_roles)
        == "stray"
    )
    assert (
        role_audit._classify_roles(
            {1, 2}, raid_role_id=1, wanderer_role_id=2, clan_role_ids=clan_roles
        )
        == "drop_raid"
    )
    assert (
        role_audit._classify_roles(
            {2, 10}, raid_role_id=1, wanderer_role_id=2, clan_role_ids=clan_roles
        )
        == "wander_with_clan"
    )
    assert (
        role_audit._classify_roles(
            {1, 10}, raid_role_id=1, wanderer_role_id=2, clan_role_ids=clan_roles
        )
        == "ok"
    )


def test_render_report_formats_all_sections():
    member = DummyMember(id=1, name="tester", roles=[], joined_at=datetime(2026, 4, 18, tzinfo=timezone.utc))
    clan_role = DummyRole(id=10, name="ClanTag")
    ticket = SimpleNamespace(name="W0001-test", url="https://discord.com/channels/1/2")

    summary = role_audit.AuditResult(
        checked=3,
        auto_fixed_strays=[member],
        auto_fixed_wanderers=[member],
        wanderers_with_clans=[(member, [clan_role])],
        visitors_no_ticket=[member],
        visitors_closed_only=[(member, [ticket])],
        visitors_extra_roles=[(member, [clan_role], [ticket])],
    )

    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )

    assert isinstance(embed, role_audit.discord.Embed)
    description = embed.description or ""

    assert "1) Stray members" in description
    assert "Manual review" in description
    assert "Visitors without any ticket" in description
    assert "joined 2026-04-18" in description
    assert "Visitors with only closed tickets" in description
    assert "Visitors with extra roles" in description


def test_render_report_uses_unknown_join_date_when_missing():
    member = DummyMember(id=1, name="tester", roles=[], joined_at=None)
    summary = role_audit.AuditResult(checked=1, visitors_no_ticket=[member])

    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )

    description = embed.description or ""
    assert "joined unknown" in description

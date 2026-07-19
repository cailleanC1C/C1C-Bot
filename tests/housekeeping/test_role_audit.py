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
        role_audit._classify_roles(
            {1}, raid_role_id=1, wanderer_role_id=2, clan_role_ids=clan_roles
        )
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
    member = DummyMember(
        id=1,
        name="tester",
        roles=[],
        joined_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
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

    assert "DETECTED ISSUES" in description
    assert "1) Stray members" in description
    assert "Manual review" in description
    assert "Visitors without any ticket" in description
    assert "joined 2026-04-18" in description
    assert "Visitors with only closed tickets" in description
    assert "Visitors with extra roles" in description
    assert "ACTIONS PERFORMED" not in description
    assert "• None" not in description


def test_render_report_uses_unknown_join_date_when_missing():
    member = DummyMember(id=1, name="tester", roles=[], joined_at=None)
    summary = role_audit.AuditResult(checked=1, visitors_no_ticket=[member])

    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )

    description = embed.description or ""
    assert "joined unknown" in description


def test_render_report_apply_mode_includes_actions_and_failures():
    member = DummyMember(
        id=1,
        name="tester",
        roles=[],
        joined_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    summary = role_audit.AuditResult(
        checked=2,
        auto_fixed_strays=[member],
        action_roles_removed=["• <@1> – removed `Raid`"],
        action_roles_added=["• <@1> – added `Wandering Souls`"],
        action_users_kicked=["• <@1> – kicked: visitor expired / no valid ticket"],
        action_failed_or_skipped=["• <@1> – could not kick: missing permission"],
    )
    embed = role_audit._render_report(
        summary=summary,
        raid_role_name="Raid",
        wanderer_role_name="Wandering Souls",
        dry_run=False,
    )
    description = embed.description or ""
    assert "ACTIONS PERFORMED" in description
    assert "6) Roles removed" in description
    assert "7) Roles added" in description
    assert "8) Users kicked" in description
    assert "9) Failed / skipped actions" in description
    assert "removed `Raid`" in description
    assert "added `Wandering Souls`" in description
    assert "kicked:" in description


def test_render_report_dry_run_wording_and_footer():
    member = DummyMember(
        id=1,
        name="tester",
        roles=[],
        joined_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    summary = role_audit.AuditResult(
        checked=9, auto_fixed_strays=[member], auto_fixed_wanderers=[member]
    )
    embed = role_audit._render_report(
        summary=summary,
        raid_role_name="raid",
        wanderer_role_name="Wandering Souls",
        dry_run=True,
    )
    description = embed.description or ""
    assert "Would remove" in description
    assert "would add" in description
    assert embed.footer.text is not None
    assert "Date:" in embed.footer.text
    assert "Checked: 9 members" in embed.footer.text


def test_only_everyone_section_includes_non_bot_member():
    guild = SimpleNamespace(id=100)
    everyone = DummyRole(id=100, name="@everyone")
    member = DummyMember(
        id=1,
        name="roleless",
        display_name="Role Less",
        guild=guild,
        roles=[everyone],
        bot=False,
        joined_at=datetime.now(timezone.utc),
    )
    summary = role_audit.AuditResult(checked=1, members_only_everyone=[member])

    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )

    description = embed.description or ""
    assert "Members with only @everyone" in description
    assert "<@1>" in description
    assert "Role Less" in description
    assert (
        datetime.now(timezone.utc).strftime("joined %Y-%m-%d (0d ago)") in description
    )


def test_only_everyone_section_unknown_join_date():
    member = DummyMember(
        id=1, name="roleless", display_name="Role Less", joined_at=None
    )
    summary = role_audit.AuditResult(checked=1, members_only_everyone=[member])
    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )
    assert "<@1> – Role Less – joined unknown" in (embed.description or "")


def test_only_everyone_helper_excludes_members_with_other_roles_and_bots():
    guild = SimpleNamespace(id=100)
    everyone = DummyRole(id=100, name="@everyone")
    other = DummyRole(id=200, name="Member")
    human_only_everyone = DummyMember(id=1, guild=guild, roles=[everyone], bot=False)
    human_with_role = DummyMember(id=2, guild=guild, roles=[everyone, other], bot=False)
    bot_only_everyone = DummyMember(id=3, guild=guild, roles=[everyone], bot=True)

    assert role_audit._is_only_everyone_member(human_only_everyone) is True
    assert role_audit._is_only_everyone_member(human_with_role) is False
    assert role_audit._is_only_everyone_member(bot_only_everyone) is False


def test_render_report_large_roleless_set_splits_without_omitting_members():
    guild = SimpleNamespace(id=100)
    everyone = DummyRole(id=100, name="@everyone")
    members = [
        DummyMember(
            id=idx,
            name=f"roleless-{idx}",
            display_name=f"Roleless Member {idx}",
            guild=guild,
            roles=[everyone],
            bot=False,
        )
        for idx in range(1, 220)
    ]
    summary = role_audit.AuditResult(
        checked=len(members), members_only_everyone=members
    )

    embeds = role_audit._render_report_embeds(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )
    combined = "\n".join(embed.description or "" for embed in embeds)

    assert len(embeds) > 1
    for member in members:
        assert f"<@{member.id}>" in combined
    assert all(len(embed.description or "") <= 4096 for embed in embeds)


def test_scheduled_role_audit_apply_changes_remains_report_only():
    member = DummyMember(id=1, roles=[])
    role = DummyRole(id=2, name="Raid")
    called = False

    async def _remove_roles(*_args, **_kwargs):
        nonlocal called
        called = True

    member.remove_roles = _remove_roles

    import asyncio

    changed, error = asyncio.run(
        role_audit._apply_role_changes(
            member,
            actor="scheduled",
            dry_run=False,
            remove=(role,),
        )
    )

    assert changed is True
    assert error is None
    assert called is False


def test_fusion_cleanup_section_renders_summary_counts():
    summary = role_audit.AuditResult(
        checked=0,
        fusion_role_cleanup=[
            role_audit.fusion_role_cleanup.FusionRoleCleanupSummary(
                fusion_id="f-ended",
                fusion_name="Old Fusion",
                role_id=777,
                role_name="Fusion Ping",
                members_found=3,
                removed_count=2,
                failed_count=1,
                skipped_count=0,
                status="partial_failure",
                failure_reasons=["member 1: permission/hierarchy/API failure"],
            )
        ],
    )

    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )
    description = embed.description or ""

    assert "Fusion role cleanup" in description
    assert "Old Fusion" in description
    assert "found=3, removed=2, failed=1, skipped=0" in description
    assert "permission/hierarchy/API failure" in description

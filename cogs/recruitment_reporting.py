from __future__ import annotations

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only

from modules.recruitment.reporting.daily_recruiter_update import (
    feature_enabled,
    log_manual_result,
    post_daily_recruiter_update,
    run_full_recruiter_reports,
)
from modules.housekeeping.role_audit import preview_role_audit_mutations, run_role_and_visitor_audit


class RecruitmentReporting(commands.Cog):
    """Admin commands for recruitment reporting."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("admin")
    @help_metadata(
        function_group="operational",
        section="utilities",
        access_tier="admin",
    )
    @commands.command(
        name="report",
        help="Posts the Daily Recruiter Update to the configured channel immediately.",
        brief="Posts the Daily Recruiter Update immediately.",
    )
    @admin_only()
    async def report_group(self, ctx: commands.Context, *args: str) -> None:
        recruiters = len(args) >= 1 and args[0].lower() == "recruiters"
        run_all = len(args) == 2 and args[1].lower() == "all" if recruiters else False

        if not recruiters:
            await ctx.reply("Usage: !report recruiters [all]", mention_author=False)
            return

        if not feature_enabled():
            await ctx.reply("Daily Recruiter Update is disabled.", mention_author=False)
            await log_manual_result(
                bot=self.bot,
                user_id=getattr(ctx.author, "id", 0),
                result="blocked",
                error="feature-off",
            )
            return

        ok: bool
        error: str
        manual_results = None
        try:
            if run_all:
                manual_results = await run_full_recruiter_reports(
                    self.bot, actor="manual", user_id=getattr(ctx.author, "id", None)
                )
                ok, error = manual_results.get("report", (False, "missing"))
            else:
                ok, error = await post_daily_recruiter_update(self.bot)
        except Exception as exc:  # pragma: no cover - defensive guard
            ok = False
            error = f"{type(exc).__name__}:{exc}"

        result = "ok" if ok else "fail"
        await log_manual_result(
            bot=self.bot,
            user_id=getattr(ctx.author, "id", 0),
            result=result,
            error=error,
        )

        if run_all and manual_results:
            report_ok, report_error = manual_results.get("report", (False, "missing"))
            audit_ok, audit_error = manual_results.get("audit", (False, "missing"))
            tickets_ok, tickets_error = manual_results.get("open_tickets", (False, "missing"))
            summary_lines = [
                f"Recruiter report: {'ok' if report_ok else 'fail'} ({report_error})",
                f"Role/visitor audit: {'ok' if audit_ok else 'fail'} ({audit_error})",
                f"Open tickets: {'ok' if tickets_ok else 'fail'} ({tickets_error})",
            ]
            await ctx.reply("\n".join(summary_lines), mention_author=False)
            return

        if not ok:
            await ctx.reply("Failed to post report. Check log channel.", mention_author=False)

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @commands.command(
        name="roleaudit",
        help="Preview or apply role-audit role mutations.",
        brief="Preview or apply role-audit role mutations.",
    )
    @admin_only()
    async def roleaudit(self, ctx: commands.Context, action: str = "preview", *args: str) -> None:
        cmd = (action or "").strip().lower()
        if cmd == "preview":
            ok, error, preview = await preview_role_audit_mutations(self.bot, actor="manual")
            if not ok or preview is None:
                await ctx.reply(f"Role audit preview failed: {error}", mention_author=False)
                return
            mutations = preview.proposed_role_mutations or []
            lines = ["Role-audit preview (dry-run):"]
            for member, remove_roles, add_roles in mutations[:20]:
                remove_txt = ", ".join(f"`{r.name}`" for r in remove_roles) or "-"
                add_txt = ", ".join(f"`{r.name}`" for r in add_roles) or "-"
                lines.append(f"• {member.mention}: remove {remove_txt}; add {add_txt}")
            lines.append(f"Total members affected: {len(mutations)}")
            lines.append(f"Total mutations: {sum(len(rm)+len(ad) for _, rm, ad in mutations)}")
            if len(mutations) > 20:
                lines.append(f"(showing first 20 of {len(mutations)})")
            await ctx.reply("\n".join(lines), mention_author=False)
            return

        if cmd == "apply":
            confirm = len(args) >= 1 and args[0] == "CONFIRM"
            allow_over_cap = any(a.lower() == "override" for a in args[1:]) if confirm else False
            if not confirm:
                await ctx.reply(
                    "Apply requires explicit confirmation: `!roleaudit apply CONFIRM [override]`",
                    mention_author=False,
                )
                return
            ok, error = await run_role_and_visitor_audit(
                self.bot,
                actor="manual",
                dry_run=False,
                max_mutations=10,
                allow_over_cap=allow_over_cap,
            )
            if not ok:
                await ctx.reply(f"Role audit apply failed: {error}", mention_author=False)
                return
            await ctx.reply("Role audit apply completed.", mention_author=False)
            return

        await ctx.reply("Usage: !roleaudit preview | !roleaudit apply CONFIRM [override]", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecruitmentReporting(bot))

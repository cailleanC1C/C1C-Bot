from __future__ import annotations

import discord
from discord.ext import commands
from c1c_coreops.helpers import help_metadata
from shared import config as cfg

ADMIN_ROLE_IDS = set()  # resolved by existing CoreOps RBAC


def _redact_tail(text: str) -> str:
    if not text:
        return ""
    tail = text[-6:] if len(text) >= 6 else text
    return f"…{tail}" if len(text) > len(tail) else tail


class ConfigCmd(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @help_metadata(
        function_group="operational",
        section="config_health",
        access_tier="admin",
        usage="!cfg [KEY]",
    )
    @commands.command(
        name="cfg",
        help=(
            "Admin-only merged config lookup. With KEY, replies in-channel with the current cached value, redacted source sheet tail, and merged key count. Without KEY, defaults to ONBOARDING_TAB. Reads runtime config only and does not write Sheets."
        ),
    )
    @commands.has_permissions(administrator=True)
    async def cfg_cmd(self, ctx: commands.Context, key: str | None = None):
        target = (key or "ONBOARDING_TAB").strip().upper()
        merged_keys = len(cfg._CONFIG.keys()) if hasattr(cfg, "_CONFIG") else 0
        value = cfg._CONFIG.get(target) if hasattr(cfg, "_CONFIG") else None
        # try to show where it came from in a safe way
        sheet_id = cfg._CONFIG.get("ONBOARDING_SHEET_ID", "")
        sheet_tail = _redact_tail(sheet_id)

        if value is None:
            msg = f"🧊 Config — key={target} • missing (not merged yet)"
            await ctx.send(msg)
            return

        msg = (
            f"🧩 Config — key={target} • value={value} • source=sheet:{sheet_tail} • merged={merged_keys} keys"
        )
        await ctx.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCmd(bot))

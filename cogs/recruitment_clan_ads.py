from __future__ import annotations
import logging
from discord.ext import commands
from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.recruitment import clan_ads

log = logging.getLogger("c1c.recruitment.clan_ads.cog")


class ClanAdsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @tier("admin")
    @help_metadata(
        function_group="operational", section="recruitment", access_tier="admin", usage="!clanads post <all|clantag>"
    )
    @commands.group(name="clanads", invoke_without_command=True, help="Admin clan-ad publishing tools. Use post all to evaluate every configured clan, or post <clantag> to publish one clan ad; posts to the configured clan-ad channel and replies with a run summary.", brief="Publish configured clan recruitment ads.")
    @admin_only()
    async def clanads(self, ctx: commands.Context):
        await ctx.send("Usage: `!clanads post all` or `!clanads post <clantag>`")

    @clanads.group(name="post", invoke_without_command=True, help="Posts clan ads for all configured clans or one clan tag from Sheets-backed clan data; replies with success/failure counts and may delete the command response in the ad channel.", brief="Post all clan ads or one clan ad.")
    @admin_only()
    async def post(self, ctx: commands.Context, clantag: str | None = None):
        if not clantag:
            await ctx.send("Usage: `!clanads post all` or `!clanads post <clantag>`")
            return
        try:
            result = await clan_ads.run(
                self.bot,
                clan_tag_filter=None if clantag.lower() == "all" else clantag,
                scheduled=False,
            )
        except Exception:
            log.exception("manual clan ads post failed")
            await ctx.send(
                "Clan ads post failed. Please check the bot logs for details."
            )
            return
        delete_after = None
        config = getattr(result, "get", lambda *_: None)("config")
        target_channel_id = getattr(config, "channel_id", None)
        if (
            target_channel_id is not None
            and getattr(getattr(ctx, "channel", None), "id", None) == target_channel_id
        ):
            delete_after = 20
        await ctx.send(
            result.get("message") or "Clan ads command completed.",
            delete_after=delete_after,
        )

    # This listener intentionally handles clan ad buttons by stable custom_id prefix
    # instead of relying on in-memory View state, so existing ad buttons keep working
    # after bot restart. Do not replace this with a timeout-bound view-only handler.
    @commands.Cog.listener("on_interaction")
    async def clan_ad_card_interaction(self, interaction):
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not custom_id or not str(custom_id).startswith("clan_ads:view_card:"):
            return
        tag = str(custom_id).rsplit(":", 1)[-1]
        embeds, files, _state = await clan_ads.build_clan_card(
            self.bot, tag, interaction.guild
        )
        if not embeds:
            await interaction.response.send_message(
                f"Unknown clan tag `{tag}`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embeds=embeds, files=files, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ClanAdsCog(bot))

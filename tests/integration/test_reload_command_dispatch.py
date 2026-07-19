import asyncio

import discord
from discord.ext import commands

from c1c_coreops.commands import reload


def test_reload_command_dispatches_reboot(monkeypatch):
    intents = discord.Intents.none()
    bot = commands.Bot(command_prefix="!", intents=intents)

    monkeypatch.setattr("shared.config.reload_config", lambda: None)

    reboot_called = {}

    async def fake_reboot() -> None:
        reboot_called["called"] = True

    monkeypatch.setattr("c1c_coreops.commands.reload._handle_reboot", fake_reboot)

    async def runner() -> None:
        await reload.setup(bot)

        command = bot.get_command("reload")
        assert command is not None

        class DummyContext:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def send(self, message: str) -> None:
                self.messages.append(message)

        ctx = DummyContext()
        try:
            await command.callback(command.cog, ctx, "--reboot")
        finally:
            await bot.close()
            reload.bot = None

        assert reboot_called == {"called": True}
        assert ctx.messages[-1] == "✅ Reloaded configuration and rebooted runtime."

    asyncio.run(runner())

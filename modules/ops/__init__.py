"""Operational tooling modules."""

from __future__ import annotations

from modules.ops import permissions_ui as permissions_ui

_ORIGINAL_PERMISSIONS_SETUP = permissions_ui.setup


async def _setup_permissions_and_server_rules(bot):
    await _ORIGINAL_PERMISSIONS_SETUP(bot)
    from cogs import server_rules as server_rules_cog

    await server_rules_cog.setup(bot)


permissions_ui.setup = _setup_permissions_and_server_rules

__all__ = [
    "permissions_ui",
    "server_map",
    "cluster_role_map",
    "server_rules",
]

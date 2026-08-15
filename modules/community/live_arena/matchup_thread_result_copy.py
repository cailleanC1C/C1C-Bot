"""Sheet-backed copy for matchup result preflight responses."""

from __future__ import annotations

import logging

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.matchup_thread_result_copy")
_installed = False


class _FollowupProxy:
    def __init__(self, followup, sheet_id: str):
        self._followup = followup
        self._sheet_id = str(sheet_id)

    async def send(self, *args, **kwargs):
        embed = kwargs.get("embed")
        if _text(getattr(embed, "title", "")) == "Screenshot required":
            try:
                from modules.community.live_arena.matchup_thread_ux import (
                    _template,
                    _templates,
                    _title,
                )

                if _templates(self._sheet_id) is not None:
                    kwargs["embed"] = _template(
                        "screenshot_required", self._sheet_id
                    ).embed(
                        report_result_button_label=_title(
                            "button_report_result", self._sheet_id
                        )
                    )
            except Exception:
                log.exception(
                    "Live Arena screenshot-required Sheet copy render failed"
                )
        return await self._followup.send(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._followup, name)


class _InteractionProxy:
    def __init__(self, interaction, sheet_id: str):
        self._interaction = interaction
        self.followup = _FollowupProxy(interaction.followup, sheet_id)

    def __getattr__(self, name):
        return getattr(self._interaction, name)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import simulation_ux_hardening

    original_submit = simulation_ux_hardening.HardenedReportResultModal.on_submit

    async def submit_with_sheet_preflight_copy(self, interaction):
        from modules.community.live_arena.matchup_thread_ux import _templates

        if _templates(str(self.sheet_id)) is None:
            return await original_submit(self, interaction)
        return await original_submit(
            self,
            _InteractionProxy(interaction, str(self.sheet_id)),
        )

    simulation_ux_hardening.HardenedReportResultModal.on_submit = (
        submit_with_sheet_preflight_copy
    )

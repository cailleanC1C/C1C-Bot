"""Live Arena tournament automation."""

from modules.community.live_arena.runtime_hooks import install as _install_runtime_hooks
from modules.community.live_arena.competition_repair import install as _install_repair
from modules.community.live_arena.competition_followup import install as _install_followup
from modules.community.live_arena.swiss_panel import install as _install_swiss

_install_runtime_hooks()
_install_repair()
_install_followup()
_install_swiss()

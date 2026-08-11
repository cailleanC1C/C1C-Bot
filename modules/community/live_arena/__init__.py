"""Live Arena tournament automation."""

from modules.community.live_arena.runtime_hooks import install as _install_runtime_hooks
from modules.community.live_arena.competition_repair import install as _install_repair

_install_runtime_hooks()
_install_repair()

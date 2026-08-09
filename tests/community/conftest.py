import datetime as dt

import pytest

from modules.community.fusion import progress_share


class _FrozenTraditionalProgressDateTime(dt.datetime):
    """Stable clock for the one historical traditional-fusion denominator fixture."""

    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 8, 8, 12, 0, tzinfo=dt.timezone.utc)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)


@pytest.fixture(autouse=True)
def _freeze_traditional_denominator_test_clock(request, monkeypatch):
    """Keep fixed 8-9 Aug event fixtures from aging into `missed` status.

    This applies only to the denominator/source-count regression. Other Fusion tests
    continue to use their normal real or explicitly supplied clocks.
    """
    if (
        request.node.name
        != "test_traditional_my_progress_uses_required_rare_denominator_not_source_count"
    ):
        return

    monkeypatch.setattr(
        progress_share.dt,
        "datetime",
        _FrozenTraditionalProgressDateTime,
    )

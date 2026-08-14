"""Deterministic clocks for placement tests with historical reservation fixtures."""

from __future__ import annotations

import datetime as datetime_module

import pytest


@pytest.fixture(autouse=True)
def _freeze_reservation_command_fixture_date(request, monkeypatch):
    """Keep historical reserve-flow fixtures inside their intended date window.

    ``test_reserve_command.py`` contains interactive date inputs from 13–14 August
    2026. Those tests exercise reservation cleanup/recompute behavior rather than
    the passage of wall-clock time. Once the real date moved beyond the fixture
    window, the production date validator correctly rejected the fixture and the
    tests stopped reaching the behavior they were written to verify.
    """
    if request.node.path.name != "test_reserve_command.py":
        return

    from modules.placement import reservations as reserve_module

    real_date = datetime_module.date

    class FixedDate(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 12)

    monkeypatch.setattr(reserve_module.dt, "date", FixedDate)

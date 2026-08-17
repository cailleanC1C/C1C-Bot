from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_shared_config_import_does_not_read_google_sheets():
    script = textwrap.dedent(
        """
        import sys
        import types

        calls = []
        fake_onboarding = types.ModuleType("shared.sheets.onboarding")

        def _read_onboarding_config(_sheet_id):
            calls.append("sync-sheet-read")
            return {}

        fake_onboarding._read_onboarding_config = _read_onboarding_config
        sys.modules["shared.sheets.onboarding"] = fake_onboarding

        import shared.config  # noqa: F401

        assert calls == [], f"shared.config performed import-time Sheet I/O: {calls}"
        """
    )
    env = os.environ.copy()
    env.update(
        {
            "DISCORD_TOKEN": "test-token",
            "GSPREAD_CREDENTIALS": "{}",
            "RECRUITMENT_SHEET_ID": "recruitment-sheet",
            "ONBOARDING_SHEET_ID": "onboarding-sheet",
            "MILESTONES_SHEET_ID": "",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout

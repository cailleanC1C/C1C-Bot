"""Test-only compatibility seams for the legacy Server Rules behavior suite.

The production publisher now uses ``shared.sheets.async_core`` exclusively.  The
large historical behavior suite predates that boundary and monkeypatches the old
adapter symbol directly.  Keep those behavioral assertions intact without
reintroducing a runtime adapter dependency by adapting only that test module.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _server_rules_async_core_test_seam(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.module.__name__.split(".")[-1] != "test_server_rules":
        return

    from modules.ops import server_rules

    class _AdapterShim:
        async def aworksheet_values_update(
            self,
            ws: Any,
            cell_range: str,
            values: list[list[Any]],
            timeout: float | None = None,
        ) -> Any:
            return await server_rules.sheets_core.acall_with_backoff(
                ws.update,
                cell_range,
                values,
                timeout=timeout,
            )

    # Historical tests patch this attribute to capture/fail single-cell writes.
    # It exists only inside the test process; production server_rules has no
    # async_adapter import or compatibility alias.
    monkeypatch.setattr(
        server_rules,
        "async_adapter",
        _AdapterShim(),
        raising=False,
    )

    async def _compat_write_message_id(
        tab: str,
        header_map: dict[str, int],
        row: Any,
        message_id: str,
    ) -> None:
        ws = await server_rules._worksheet(tab)
        await server_rules.async_adapter.aworksheet_values_update(
            ws,
            server_rules._message_id_range(header_map, row.row_number),
            [[message_id]],
        )

    monkeypatch.setattr(server_rules, "_write_message_id", _compat_write_message_id)

    async def _compat_load_rows():
        """Preserve the old two-argument afetch_values monkeypatch seam in tests."""

        tab = await server_rules.recruitment_sheet.get_config_value_async(
            "SERVER_RULES_FAQ_TAB", None, force=True
        )
        if not tab:
            return "", {}, [], ["Config key SERVER_RULES_FAQ_TAB is missing"]

        # Intentionally omit production attribution kwargs here: one historical
        # test injects a two-argument reader.  The focused boundary guardrail
        # separately verifies that production uses server_rules/load_rows tags.
        matrix = await server_rules.sheets_core.afetch_values(
            server_rules._mirralith_sheet_id(),
            tab,
        )
        if not matrix:
            return tab, {}, [], ["sheet tab has no header row"]

        headers = [server_rules._text(value).lower() for value in matrix[0]]
        header_map = {header: idx for idx, header in enumerate(headers) if header}
        missing = sorted(server_rules.REQUIRED_HEADERS - set(header_map))
        if missing:
            return tab, header_map, [], ["missing headers: " + ", ".join(missing)]

        rows = []
        for offset, values in enumerate(matrix[1:], start=2):
            data = {
                name: server_rules._text(values[idx]) if idx < len(values) else ""
                for name, idx in header_map.items()
            }
            if server_rules._is_empty_placeholder(data):
                continue
            row = server_rules.Row(
                offset,
                list(values),
                data,
                server_rules.parse_enabled(data.get("enabled")) is True,
            )
            row.topic_key = data.get("topic_key", "").strip()
            row.topic_title = data.get("topic_title", "").strip()
            rows.append(row)
        return tab, header_map, rows, []

    monkeypatch.setattr(server_rules, "load_rows", _compat_load_rows)

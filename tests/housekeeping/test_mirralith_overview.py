import pytest

from modules.housekeeping import mirralith_overview as mirralith
from shared.sheets.export_utils import ImageExportError


@pytest.mark.asyncio
async def test_export_spec_retries_retryable_failure_then_succeeds(monkeypatch):
    attempts = []
    sleeps = []

    async def export(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) == 1:
            raise ImageExportError("pdf_export_status_429", retry_after_seconds=0.25)
        return b"png"

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(mirralith, "export_pdf_as_png", export)
    monkeypatch.setattr(mirralith.asyncio, "sleep", sleep)

    result = await mirralith._export_spec_with_retry(
        "sheet123",
        "456",
        "A65:F69",
        label="[MIRRALITH_CLUSTER_BEGINNER]",
        tab_name="cluster_structure",
    )

    assert result == b"png"
    assert len(attempts) == 2
    assert sleeps == [0.25]
    assert attempts[0][1]["raise_on_failure"] is True


@pytest.mark.asyncio
async def test_export_spec_reports_final_retryable_failure(monkeypatch):
    attempts = 0
    sleeps = []

    async def export(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ImageExportError("empty_pdf_response")

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(mirralith, "export_pdf_as_png", export)
    monkeypatch.setattr(mirralith.asyncio, "sleep", sleep)

    with pytest.raises(ImageExportError, match="empty_pdf_response"):
        await mirralith._export_spec_with_retry(
            "sheet123",
            "456",
            "A65:F69",
            label="[MIRRALITH_CLUSTER_BEGINNER]",
            tab_name="cluster_structure",
        )

    assert attempts == mirralith._EXPORT_MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0]


@pytest.mark.asyncio
async def test_export_spec_does_not_retry_nonretryable_failure(monkeypatch):
    attempts = 0

    async def export(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ImageExportError("auth_failure:bad credentials")

    monkeypatch.setattr(mirralith, "export_pdf_as_png", export)

    with pytest.raises(ImageExportError, match="auth_failure"):
        await mirralith._export_spec_with_retry(
            "sheet123",
            "456",
            "A65:F69",
            label="[MIRRALITH_CLUSTER_BEGINNER]",
            tab_name="cluster_structure",
        )

    assert attempts == 1

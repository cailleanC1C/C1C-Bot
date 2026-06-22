from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from typing import Any, Dict

import importlib.util
import requests
from PIL import Image, ImageChops
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

from shared.sheets import core as sheets_core

log = logging.getLogger("c1c.sheets.export")

convert_from_bytes = None
_HAS_PDF2IMAGE = False
_pdf2image_spec = importlib.util.find_spec("pdf2image")
if _pdf2image_spec is not None:
    import importlib

    pdf2image = importlib.import_module("pdf2image")
    convert_from_bytes = getattr(pdf2image, "convert_from_bytes", None)
    _HAS_PDF2IMAGE = callable(convert_from_bytes)

GOOGLE_EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/export"


def _trim_outer_whitespace(image: Image.Image) -> Image.Image:
    """Trim only the outer near-white page frame from a rasterized PDF page.

    Google Sheets PDF exports render the selected range onto a white PDF page.
    Comparing against pure white is too brittle because PDF rasterization can leave
    anti-aliased near-white pixels around sheet content, while aggressive cropping
    risks shaving off footer rows or side content.  Treat only pixels that are very
    close to white as page whitespace, and keep a tiny safety pad around the
    detected content bbox.
    """

    rgb_image = image.convert("RGB")
    white = Image.new("RGB", rgb_image.size, (255, 255, 255))
    diff = ImageChops.difference(rgb_image, white)

    # Ignore tiny rasterization noise in the PDF page background, but consider any
    # visible sheet fill, borders, text, or image pixels to be content.
    threshold = 8
    mask = diff.convert("L").point(lambda pixel: 255 if pixel > threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image

    safety_pad_px = 2
    left, top, right, bottom = bbox
    left = max(0, left - safety_pad_px)
    top = max(0, top - safety_pad_px)
    right = min(image.width, right + safety_pad_px)
    bottom = min(image.height, bottom + safety_pad_px)
    return image.crop((left, top, right, bottom))


class ImageExportError(RuntimeError):
    """Raised when a sheet image export would be unsafe to post."""


def _export_delay_seconds() -> float:
    """
    Read SHEETS_EXPORT_DELAY_MS from the environment and return the delay in seconds.

    Empty / missing / invalid / <= 0 -> 0.0 (no delay)
    """

    raw = os.getenv("SHEETS_EXPORT_DELAY_MS", "").strip()
    if not raw:
        return 0.0
    try:
        ms = int(raw)
    except ValueError:
        return 0.0
    if ms <= 0:
        return 0.0
    return ms / 1000.0


_EXPORT_DELAY_SECONDS: float = _export_delay_seconds()


async def _sleep_after_export(label: str | None) -> None:
    """
    Optional pacing hook for all exports.

    This keeps Mirralith / Leagues export jobs from hammering the Google API when they
    render many boards in sequence.
    """

    if _EXPORT_DELAY_SECONDS <= 0:
        return

    await asyncio.sleep(_EXPORT_DELAY_SECONDS)


def _service_account_info() -> Dict[str, Any]:
    raw = (
        os.getenv("GSPREAD_CREDENTIALS")
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or ""
    )
    if not raw:
        raise RuntimeError("GSPREAD_CREDENTIALS/GOOGLE_SERVICE_ACCOUNT_JSON not set")
    return json.loads(raw)


def _get_service_account_headers() -> dict[str, str]:
    info = _service_account_info()
    creds = Credentials.from_service_account_info(info, scopes=sheets_core.SCOPES)
    creds.refresh(Request())
    if not creds.token:
        raise RuntimeError("service account token unavailable")
    return {"Authorization": f"Bearer {creds.token}"}


def _log_error(reason: str, *, log_context: dict[str, Any]) -> None:
    label = log_context.get("label", "")
    tab = log_context.get("tab", "")
    cell_range = log_context.get("range", "")
    log.error(
        "❌ error — mirralith_export • label=%s • tab=%s • range=%s • reason=%s",
        label,
        tab,
        cell_range,
        reason,
        extra={k: v for k, v in log_context.items() if k not in {"label", "tab", "range"}},
    )


def get_tab_gid(sheet_id: str, tab_name: str) -> str | None:
    worksheet = sheets_core.get_worksheet(sheet_id, tab_name)
    gid = getattr(worksheet, "id", None)
    return str(gid) if gid is not None else None


def _convert_pdf_to_png(
    pdf_bytes: bytes,
    *,
    fail_on_multi_page: bool = True,
    crop_to_content: bool = True,
) -> bytes | None:
    if not _HAS_PDF2IMAGE:
        log.warning("export_pdf_as_png: pdf2image not installed; skipping PDF rasterization")
        return None

    try:
        images = convert_from_bytes(
            pdf_bytes,
            fmt="png",
            dpi=150,
            first_page=1,
            last_page=2,
        )
        if not images:
            log.error("export_pdf_as_png: pdf2image returned no pages")
            return None
        if len(images) > 1 and fail_on_multi_page:
            log.error("export_pdf_as_png: PDF export produced multiple pages")
            raise ImageExportError("image export produced multiple pages")
        image = images[0]

        if crop_to_content:
            image = _trim_outer_whitespace(image)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except ImageExportError:
        raise
    except Exception as exc:
        log.exception(
            "export_pdf_as_png: PDF rasterization failed",
            extra={"error": str(exc)},
        )
        return None


def _build_pdf_export_params(
    gid: str | int,
    cell_range: str,
    *,
    fit_range_to_one_page: bool,
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "format": "pdf",
        "gid": gid,
        "range": cell_range,
        "portrait": "false",
        "fitw": "true",
        "sheetnames": "false",
        "printtitle": "false",
        "pagenumbers": "false",
        "gridlines": "false",
        "fzr": "false",
    }
    if fit_range_to_one_page:
        # Google Sheets scale=4 is "fit to page". Keep it opt-in so existing
        # callers can preserve prior framing unless they request strict range fitting.
        params.update(
            {
                "scale": "4",
                "size": "7",
                "top_margin": "0.25",
                "bottom_margin": "0.25",
                "left_margin": "0.25",
                "right_margin": "0.25",
            }
        )
    return params


def _export_pdf_as_png_sync(
    sheet_id: str,
    gid: str | int | None,
    cell_range: str,
    *,
    log_context: dict[str, Any] | None = None,
    fit_range_to_one_page: bool = False,
    fail_on_multi_page: bool = True,
    crop_to_content: bool = True,
) -> bytes | None:
    context = {"range": cell_range}
    context.update(log_context or {})

    try:
        headers = _get_service_account_headers()
    except Exception as exc:  # pragma: no cover - network/auth failure
        _log_error(f"auth_failure:{exc}", log_context=context)
        return None

    if not gid and gid != 0:
        _log_error("missing_gid", log_context=context)
        return None

    try:
        response = requests.get(
            GOOGLE_EXPORT_URL.format(sheet_id=sheet_id),
            headers=headers,
            params=_build_pdf_export_params(
                gid,
                cell_range,
                fit_range_to_one_page=fit_range_to_one_page,
            ),
            timeout=20,
        )
    except Exception as exc:  # pragma: no cover - network failure
        _log_error(f"pdf_request_failed:{exc}", log_context=context)
        return None

    if response.status_code != 200:
        _log_error(
            f"pdf_export_status_{response.status_code}",
            log_context={**context, "status": response.status_code},
        )
        return None

    pdf_content = response.content or b""
    if not pdf_content:
        _log_error("empty_pdf_response", log_context=context)
        return None

    return _convert_pdf_to_png(
        pdf_content,
        fail_on_multi_page=fail_on_multi_page,
        crop_to_content=crop_to_content,
    )


async def export_pdf_as_png(
    sheet_id: str,
    gid: str | int | None,
    cell_range: str,
    *,
    log_context: dict[str, Any] | None = None,
    fit_range_to_one_page: bool = False,
    fail_on_multi_page: bool = True,
    crop_to_content: bool = True,
) -> bytes | None:
    label = ""
    if log_context:
        label = str(log_context.get("label", ""))

    try:
        return await asyncio.to_thread(
            _export_pdf_as_png_sync,
            sheet_id,
            gid,
            cell_range,
            log_context=log_context,
            fit_range_to_one_page=fit_range_to_one_page,
            fail_on_multi_page=fail_on_multi_page,
            crop_to_content=crop_to_content,
        )
    finally:
        await _sleep_after_export(label)

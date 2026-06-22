from io import BytesIO

import pytest
from PIL import Image

from shared.sheets import export_utils


def _png_image(color=(255, 255, 255)):
    return Image.new("RGB", (8, 8), color)


def test_convert_pdf_to_png_rejects_multiple_pages(monkeypatch):
    monkeypatch.setattr(export_utils, "_HAS_PDF2IMAGE", True)
    monkeypatch.setattr(
        export_utils,
        "convert_from_bytes",
        lambda *args, **kwargs: [_png_image(), _png_image()],
    )

    with pytest.raises(export_utils.ImageExportError) as exc:
        export_utils._convert_pdf_to_png(b"%PDF")

    assert str(exc.value) == "image export produced multiple pages"


def test_convert_pdf_to_png_renders_single_page(monkeypatch):
    captured = {}

    def convert(pdf_bytes, **kwargs):
        captured.update(kwargs)
        return [_png_image((0, 0, 0))]

    monkeypatch.setattr(export_utils, "_HAS_PDF2IMAGE", True)
    monkeypatch.setattr(export_utils, "convert_from_bytes", convert)

    png = export_utils._convert_pdf_to_png(b"%PDF")

    assert png is not None
    assert captured["first_page"] == 1
    assert captured["last_page"] == 2
    rendered = Image.open(BytesIO(png))
    assert rendered.size == (8, 8)


def test_export_pdf_request_uses_single_page_fit_options(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        content = b"%PDF"

    def get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(export_utils, "_get_service_account_headers", lambda: {"Authorization": "Bearer token"})
    monkeypatch.setattr(export_utils.requests, "get", get)
    monkeypatch.setattr(export_utils, "_convert_pdf_to_png", lambda pdf: b"png")

    assert export_utils._export_pdf_as_png_sync("sheet123", "456", "A1:V42") == b"png"

    params = captured["params"]
    assert params["scale"] == "4"
    assert params["portrait"] == "false"
    assert params["gridlines"] == "false"
    assert params["range"] == "A1:V42"

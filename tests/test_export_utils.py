from io import BytesIO

import pytest
from PIL import Image

from shared.sheets import export_utils


def _png_image(color=(255, 255, 255), size=(8, 8)):
    return Image.new("RGB", size, color)


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


def test_convert_pdf_to_png_can_allow_first_page_when_requested(monkeypatch):
    monkeypatch.setattr(export_utils, "_HAS_PDF2IMAGE", True)
    monkeypatch.setattr(
        export_utils,
        "convert_from_bytes",
        lambda *args, **kwargs: [_png_image((0, 0, 0)), _png_image()],
    )

    png = export_utils._convert_pdf_to_png(b"%PDF", fail_on_multi_page=False)

    assert png is not None
    rendered = Image.open(BytesIO(png))
    assert rendered.size == (8, 8)


def test_convert_pdf_to_png_crops_to_content_by_default(monkeypatch):
    captured = {}
    image = _png_image(size=(10, 10))
    image.putpixel((4, 4), (0, 0, 0))

    def convert(pdf_bytes, **kwargs):
        captured.update(kwargs)
        return [image]

    monkeypatch.setattr(export_utils, "_HAS_PDF2IMAGE", True)
    monkeypatch.setattr(export_utils, "convert_from_bytes", convert)

    png = export_utils._convert_pdf_to_png(b"%PDF")

    assert png is not None
    assert captured["first_page"] == 1
    assert captured["last_page"] == 2
    rendered = Image.open(BytesIO(png))
    assert rendered.size == (1, 1)


def test_convert_pdf_to_png_can_preserve_page_framing(monkeypatch):
    image = _png_image(size=(10, 10))
    image.putpixel((4, 4), (0, 0, 0))

    monkeypatch.setattr(export_utils, "_HAS_PDF2IMAGE", True)
    monkeypatch.setattr(export_utils, "convert_from_bytes", lambda *args, **kwargs: [image])

    png = export_utils._convert_pdf_to_png(b"%PDF", crop_to_content=False)

    assert png is not None
    rendered = Image.open(BytesIO(png))
    assert rendered.size == (10, 10)


def test_default_pdf_request_preserves_prior_non_scale_options():
    params = export_utils._build_pdf_export_params(
        "456", "A1:AE26", fit_range_to_one_page=False
    )

    assert params["range"] == "A1:AE26"
    assert params["portrait"] == "false"
    assert params["fitw"] == "true"
    assert params["gridlines"] == "false"
    assert "scale" not in params
    assert "top_margin" not in params


def test_fit_to_one_page_pdf_request_adds_strict_fit_options(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        content = b"%PDF"

    def get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(
        export_utils,
        "_get_service_account_headers",
        lambda: {"Authorization": "Bearer token"},
    )
    monkeypatch.setattr(export_utils.requests, "get", get)
    monkeypatch.setattr(
        export_utils,
        "_convert_pdf_to_png",
        lambda pdf, **kwargs: b"png",
    )

    assert (
        export_utils._export_pdf_as_png_sync(
            "sheet123", "456", "A1:V42", fit_range_to_one_page=True
        )
        == b"png"
    )

    params = captured["params"]
    assert params["scale"] == "4"
    assert params["size"] == "7"
    assert params["portrait"] == "false"
    assert params["gridlines"] == "false"
    assert params["fzr"] == "false"
    assert params["top_margin"] == "0.25"
    assert params["range"] == "A1:V42"

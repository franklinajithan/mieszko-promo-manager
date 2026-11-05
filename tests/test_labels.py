import io
import subprocess

import pytest
import zxingcpp
from PIL import Image
from pypdf import PdfReader

from app.labels import render_pdf


def row(name="Ser żółty Edamski plastry", price="1.99"):
    return {
        "start_date": "05-11-2026",
        "end_date": "15-11-2026",
        "price": price,
        "previous_price": "2.49",
        "product": {
            "name": name,
            "barcode": "5900820005535",
            "brand": "Mlekpol",
            "weight": "150G",
            "unit": "EACH",
        },
    }


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_pdf_visual_regions_and_barcode_scan(tmp_path, orientation):
    pdf = tmp_path / "label.pdf"
    pdf.write_bytes(render_pdf([row()], {"orientation": orientation}))
    subprocess.run(
        ["pdftoppm", "-singlefile", "-r", "150", "-png", str(pdf), str(tmp_path / "label")],
        check=True,
        capture_output=True,
    )
    img = Image.open(tmp_path / "label.png").convert("RGB")
    found = zxingcpp.read_barcodes(img)
    assert any(x.text == "5900820005535" and x.format == zxingcpp.BarcodeFormat.EAN13 for x in found), found
    w, h = img.size

    # Stable colour blocks guard the reference geometry without font/platform pixel noise.
    def sample(x, y):
        return img.getpixel((int(w * x), int(h * y)))

    yellow = sample(0.76, 0.48)
    assert yellow[0] > 230 and yellow[1] > 200 and yellow[2] < 100
    header = sample(0.56, 0.08)
    assert header[0] > 120 and header[1] < 100 and header[2] < 100
    assert sample(0.01, 0.01) == (255, 255, 255)  # safe paper margin
    reader = PdfReader(io.BytesIO(pdf.read_bytes()))
    text = reader.pages[0].extract_text()
    for expected in (
        "POLSKIE SUPERMARKETY",
        "MIESZKO",
        "Promotion Period",
        "1.99",
        "Ser żółty Edamski plastry",
    ):
        assert expected in text


def test_large_price_and_long_description_fit(tmp_path):
    output = render_pdf([row("Bardzo długa nazwa produktu " * 12, "9999999.99")])
    reader = PdfReader(io.BytesIO(output))
    assert "9999999.99" in reader.pages[0].extract_text()
    assert len(reader.pages) == 1
    assert output == render_pdf([row("Bardzo długa nazwa produktu " * 12, "9999999.99")])


def test_many_products_one_page_each():
    output = render_pdf([row(name=f"Product {i}") for i in range(200)])
    reader = PdfReader(io.BytesIO(output))
    assert len(reader.pages) == 200
    assert "Product 199" in reader.pages[-1].extract_text()


def test_invalid_gtin_uses_code128(tmp_path):
    data = row()
    data["product"]["barcode"] = "5900820005536"
    pdf = tmp_path / "code128.pdf"
    pdf.write_bytes(render_pdf([data]))
    subprocess.run(
        ["pdftoppm", "-singlefile", "-r", "180", "-png", str(pdf), str(tmp_path / "barcode")],
        check=True,
        capture_output=True,
    )
    found = zxingcpp.read_barcodes(Image.open(tmp_path / "barcode.png"))
    assert any(x.text == "5900820005536" and x.format == zxingcpp.BarcodeFormat.Code128 for x in found)

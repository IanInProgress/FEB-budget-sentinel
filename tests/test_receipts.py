import io

from PIL import Image

from receipts import build_receipt_pdf


def _image_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(output, format="PNG")
    return output.getvalue()


def test_build_receipt_pdf_combines_all_images():
    pdf_bytes = build_receipt_pdf([_image_bytes("red"), _image_bytes("blue")])

    assert pdf_bytes.startswith(b"%PDF")
    assert pdf_bytes.count(b"/Type /Page") >= 2

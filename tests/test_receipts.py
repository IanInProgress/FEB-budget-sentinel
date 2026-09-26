import io

from PIL import Image
from pypdf import PdfReader

from receipts import build_receipt_pdf


def _image_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(output, format="PNG")
    return output.getvalue()


def _pdf_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "green").save(output, format="PDF")
    return output.getvalue()


def test_build_receipt_pdf_combines_all_images():
    pdf_bytes = build_receipt_pdf([_image_bytes("red"), _image_bytes("blue")])

    assert pdf_bytes.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) == 2


def test_build_receipt_pdf_combines_mixed_images_and_pdfs():
    pdf_bytes = build_receipt_pdf([_image_bytes("red"), _pdf_bytes(), _image_bytes("blue")])

    assert pdf_bytes.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) == 3

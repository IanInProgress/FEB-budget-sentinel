import io
import ssl

from PIL import Image
from pypdf import PdfReader

from receipts import ReceiptDriveStorage, ReceiptStorageError, build_receipt_pdf


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


def test_upload_receipt_pdf_rebuilds_client_and_retries_transient_connection_errors(monkeypatch):
    class UploadCall:
        def __init__(self, result):
            self.result = result

        def execute(self):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class Files:
        def __init__(self, result):
            self.result = result

        def create(self, **kwargs):
            return UploadCall(self.result)

    class Drive:
        def __init__(self, result):
            self.result = result

        def files(self):
            return Files(self.result)

    storage = ReceiptDriveStorage.__new__(ReceiptDriveStorage)
    storage._credentials = object()
    storage._root_folder_id = "receipts-folder"
    storage._drive = Drive(BrokenPipeError("broken pipe"))
    replacement_drives = [
        Drive(ssl.SSLError("unexpected EOF")),
        Drive({"id": "file-123", "webViewLink": "https://drive.google.com/file/d/file-123/view"}),
    ]
    sleeps = []
    monkeypatch.setattr("receipts.build", lambda *args, **kwargs: replacement_drives.pop(0))
    monkeypatch.setattr("receipts.time.sleep", sleeps.append)

    link = storage.upload_receipt_pdf(pdf_bytes=b"%PDF-test", request_id="REQ-1")

    assert link == "https://drive.google.com/file/d/file-123/view"
    assert sleeps == [1.0, 2.0]
    assert replacement_drives == []


def test_upload_receipt_pdf_stops_after_transient_attempt_limit(monkeypatch):
    class UploadCall:
        def execute(self):
            raise BrokenPipeError("broken pipe")

    class Files:
        def create(self, **kwargs):
            return UploadCall()

    class Drive:
        def files(self):
            return Files()

    storage = ReceiptDriveStorage.__new__(ReceiptDriveStorage)
    storage._credentials = object()
    storage._root_folder_id = "receipts-folder"
    storage._drive = Drive()
    replacement_drives = [Drive(), Drive()]
    sleeps = []
    monkeypatch.setattr("receipts.build", lambda *args, **kwargs: replacement_drives.pop(0))
    monkeypatch.setattr("receipts.time.sleep", sleeps.append)

    try:
        storage.upload_receipt_pdf(pdf_bytes=b"%PDF-test", request_id="REQ-1")
    except ReceiptStorageError as error:
        assert isinstance(error.__cause__, BrokenPipeError)
    else:
        raise AssertionError("Expected the upload to fail after exhausting retries")

    assert sleeps == [1.0, 2.0]
    assert replacement_drives == []

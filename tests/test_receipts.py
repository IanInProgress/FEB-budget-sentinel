import io

from PIL import Image

from receipts import build_receipt_pdf


def _image_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(output, format="PNG")
    return output.getvalue()


def test_build_receipt_pdf_preserves_image_order():
    pdf_bytes = build_receipt_pdf([_image_bytes("red"), _image_bytes("blue")])

    assert pdf_bytes.startswith(b"%PDF-")
    assert pdf_bytes.count(b"/Type /Page") >= 2


def test_drive_oauth_token_is_preferred_over_service_account(monkeypatch):
    from receipts import ReceiptDriveStorage

    captured = {}

    def fake_build(_service, _version, *, credentials, cache_discovery):
        captured["credentials"] = credentials
        return object()

    monkeypatch.setattr("receipts.build", fake_build)
    ReceiptDriveStorage(
        folder_id="folder",
        service_account_json='{"type":"service_account"}',
        oauth_token_json='{"client_id":"id","client_secret":"secret","refresh_token":"token","token":"access","scopes":["https://www.googleapis.com/auth/drive"]}',
    )

    assert captured["credentials"].refresh_token == "token"
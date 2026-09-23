from __future__ import annotations

import io
import json
import logging
import urllib.request
from pathlib import Path

from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_receipts_upload_folder_id() -> str:
    return "1NV1_4CdbSGxh7eqFzHhdWAFXQ0N-NLyU"


class ReceiptStorageError(RuntimeError):
    pass


def build_receipt_pdf(image_payloads: list[bytes]) -> bytes:
    if not image_payloads:
        raise ReceiptStorageError("No receipt images were provided")

    pages: list[Image.Image] = []
    try:
        for payload in image_payloads:
            with Image.open(io.BytesIO(payload)) as source:
                pages.append(ImageOps.exif_transpose(source).convert("RGB").copy())

        output = io.BytesIO()
        pages[0].save(output, format="PDF", save_all=True, append_images=pages[1:])
        return output.getvalue()
    except Exception as error:
        raise ReceiptStorageError("Could not convert receipt images to PDF") from error
    finally:
        for page in pages:
            page.close()


def download_slack_images(image_urls: list[str], slack_bot_token: str) -> list[bytes]:
    if not image_urls:
        raise ReceiptStorageError("No Slack receipt image URLs were provided")

    payloads: list[bytes] = []
    for image_url in image_urls:
        request = urllib.request.Request(
            image_url,
            headers={"Authorization": f"Bearer {slack_bot_token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payloads.append(response.read())
        except Exception as error:
            raise ReceiptStorageError("Could not download a receipt image from Slack") from error
    return payloads


class ReceiptDriveStorage:
    def __init__(
        self,
        *,
        folder_id: str,
        service_account_file: str | None = None,
        service_account_json: str | None = None,
        oauth_client_file: str | None = None,
        oauth_client_json: str | None = None,
        oauth_token_file: str | None = None,
        oauth_token_json: str | None = None,
    ) -> None:
        try:
            if oauth_token_file or oauth_token_json:
                token_info = (
                    json.loads(oauth_token_json)
                    if oauth_token_json
                    else json.loads(Path(oauth_token_file).read_text())
                )
                credentials = OAuthCredentials.from_authorized_user_info(token_info, DRIVE_SCOPES)
            elif oauth_client_file or oauth_client_json:
                raise ReceiptStorageError(
                    "Google Drive OAuth client credentials are configured, but no authorized token is available. "
                    "Run authorize_drive.py first."
                )
            elif service_account_file:
                credentials = Credentials.from_service_account_file(service_account_file, scopes=DRIVE_SCOPES)
            elif service_account_json:
                credentials = Credentials.from_service_account_info(
                    json.loads(service_account_json), scopes=DRIVE_SCOPES
                )
            else:
                raise ReceiptStorageError("Missing Google service-account credentials")
            self._drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        except ReceiptStorageError:
            raise
        except Exception as error:
            raise ReceiptStorageError("Could not initialize Google Drive access") from error

        self._root_folder_id = folder_id

    def upload_receipt_pdf(self, *, pdf_bytes: bytes, request_id: str) -> str:
        media = MediaIoBaseUpload(
            io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False
        )
        try:
            created = self._drive.files().create(
                body={"name": f"{request_id}_receipts.pdf", "parents": [self._root_folder_id]},
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            ).execute()
        except HttpError as error:
            if "storageQuotaExceeded" in str(error):
                raise ReceiptStorageError(
                    "Google Drive rejected the upload because service accounts have no My Drive storage quota."
                ) from error
            raise ReceiptStorageError("Google Drive rejected the receipt PDF upload") from error
        file_id = str(created["id"])
        return str(created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view")

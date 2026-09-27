from __future__ import annotations

import io
import json
import logging
import time
import urllib.request
from pathlib import Path

from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
RECEIPT_UPLOAD_MAX_ATTEMPTS = 3
RECEIPT_UPLOAD_INITIAL_BACKOFF_SECONDS = 1.0
TRANSIENT_DRIVE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


def get_receipts_upload_folder_id() -> str:
    return "1NV1_4CdbSGxh7eqFzHhdWAFXQ0N-NLyU"


class ReceiptStorageError(RuntimeError):
    pass


def _is_pdf_payload(payload: bytes) -> bool:
    return payload.lstrip()[:5] == b"%PDF-"


def build_receipt_pdf(receipt_payloads: list[bytes]) -> bytes:
    """
    Combine receipt uploads (images and/or PDFs, in any mix and order) into one PDF.
    """
    if not receipt_payloads:
        raise ReceiptStorageError("No receipt files were provided")

    writer = PdfWriter()
    opened_images: list[Image.Image] = []
    try:
        for payload in receipt_payloads:
            if _is_pdf_payload(payload):
                reader = PdfReader(io.BytesIO(payload))
                for page in reader.pages:
                    writer.add_page(page)
                continue

            with Image.open(io.BytesIO(payload)) as source:
                page = ImageOps.exif_transpose(source).convert("RGB").copy()
            opened_images.append(page)

            page_pdf = io.BytesIO()
            page.save(page_pdf, format="PDF")
            page_pdf.seek(0)
            for page_obj in PdfReader(page_pdf).pages:
                writer.add_page(page_obj)

        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as error:
        raise ReceiptStorageError("Could not combine receipt files into a PDF") from error
    finally:
        for image in opened_images:
            image.close()


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
            self._credentials = credentials
            self._drive = self._build_drive_client()
        except ReceiptStorageError:
            raise
        except Exception as error:
            raise ReceiptStorageError("Could not initialize Google Drive access") from error

        self._root_folder_id = folder_id

    def _build_drive_client(self):
        return build("drive", "v3", credentials=self._credentials, cache_discovery=False)

    def upload_receipt_pdf(self, *, pdf_bytes: bytes, request_id: str) -> str:
        # Transient connection drops (e.g. BrokenPipeError or SSL EOF) happen against Google's API.
        # Rebuild the client between attempts so a broken pooled connection is not reused.
        last_error: Exception | None = None
        for attempt in range(1, RECEIPT_UPLOAD_MAX_ATTEMPTS + 1):
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
                status_code = getattr(getattr(error, "resp", None), "status", None)
                if status_code not in TRANSIENT_DRIVE_HTTP_STATUS_CODES:
                    raise ReceiptStorageError("Google Drive rejected the receipt PDF upload") from error
                last_error = error
            except (BrokenPipeError, ConnectionError, TimeoutError, OSError) as error:
                last_error = error
            else:
                file_id = str(created["id"])
                return str(created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view")

            if attempt == RECEIPT_UPLOAD_MAX_ATTEMPTS:
                break

            backoff_seconds = RECEIPT_UPLOAD_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Transient error uploading receipt PDF for %s (attempt %d/%d): %s; retrying in %.1fs",
                request_id,
                attempt,
                RECEIPT_UPLOAD_MAX_ATTEMPTS,
                last_error,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
            try:
                self._drive = self._build_drive_client()
            except Exception as error:
                raise ReceiptStorageError("Could not reconnect to Google Drive for receipt upload") from error

        raise ReceiptStorageError("Could not reach Google Drive to upload the receipt PDF") from last_error

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

from receipts import DRIVE_SCOPES


def main() -> None:
    load_dotenv()
    client_file = os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_FILE", "oauth-client.json").strip()
    token_file = os.getenv("GOOGLE_DRIVE_OAUTH_TOKEN_FILE", "drive-token.json").strip()
    if not Path(client_file).exists():
        raise SystemExit(
            f"OAuth client file not found: {client_file}. "
            "Download a Desktop OAuth client JSON from Google Cloud Console first."
        )

    flow = InstalledAppFlow.from_client_secrets_file(client_file, DRIVE_SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    Path(token_file).write_text(credentials.to_json())
    print(f"Saved Google Drive OAuth token to {token_file}")
    print("Use this token JSON as GOOGLE_DRIVE_OAUTH_TOKEN_JSON on Railway.")


if __name__ == "__main__":
    main()

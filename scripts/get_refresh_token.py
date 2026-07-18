#!/usr/bin/env python3
"""Run a local OAuth consent flow and print values for GitHub Secrets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secrets", type=Path, default=Path("client_secret.json"))
    args = parser.parse_args()
    if not args.client_secrets.is_file():
        raise SystemExit(f"ERROR: file not found: {args.client_secrets}")
    payload = json.loads(args.client_secrets.read_text(encoding="utf-8"))
    client = payload.get("installed")
    if not isinstance(client, dict):
        raise SystemExit("ERROR: client_secret.json must contain a Google OAuth Desktop client")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(args.client_secrets), scopes=[YOUTUBE_UPLOAD_SCOPE]
    )
    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        authorization_prompt_message="Open this URL to authorize YouTube uploads:\n{url}",
        success_message="Authorization complete. You may close this tab.",
        open_browser=True,
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        raise SystemExit(
            "ERROR: Google did not return a refresh token. Revoke the app grant and run again."
        )
    print("\nAdd these exact values to GitHub Actions secrets:")
    print(f"YOUTUBE_CLIENT_ID={client.get('client_id', '')}")
    print(f"YOUTUBE_CLIENT_SECRET={client.get('client_secret', '')}")
    print(f"YOUTUBE_REFRESH_TOKEN={credentials.refresh_token}")
    print("\nThese values were not written to disk. Close this terminal when finished.")


if __name__ == "__main__":
    main()


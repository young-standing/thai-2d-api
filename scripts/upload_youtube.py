#!/usr/bin/env python3
"""Upload the rendered Short through YouTube Data API v3."""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import time
from pathlib import Path
from typing import Any

import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
RETRIABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error,
    OSError,
    IOError,
    socket.timeout,
)
MAX_RETRIES = 10


def required_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub secret: {name}")
    return value


def load_metadata(path: Path) -> dict[str, Any]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read metadata file {path}: {exc}") from exc
    required = {"title", "description", "tags", "category_id", "made_for_kids", "privacy_status"}
    missing = sorted(required - set(metadata))
    if missing:
        raise RuntimeError("Metadata is missing: " + ", ".join(missing))
    if metadata["privacy_status"] not in {"private", "unlisted", "public"}:
        raise RuntimeError("Metadata contains an invalid privacy status")
    if len(metadata["title"]) > 100:
        raise RuntimeError("YouTube title exceeds 100 characters")
    return metadata


def youtube_client():
    credentials = Credentials(
        token=None,
        refresh_token=required_secret("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=required_secret("YOUTUBE_CLIENT_ID"),
        client_secret=required_secret("YOUTUBE_CLIENT_SECRET"),
        scopes=[YOUTUBE_UPLOAD_SCOPE],
    )
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def resumable_upload(request) -> dict[str, Any]:
    response = None
    retry = 0
    while response is None:
        error: Exception | None = None
        try:
            status, response = request.next_chunk()
            if status:
                print(f"Upload progress: {int(status.progress() * 100)}%")
        except HttpError as exc:
            if exc.resp.status in RETRIABLE_STATUS_CODES:
                error = exc
            else:
                raise
        except RETRIABLE_EXCEPTIONS as exc:
            error = exc
        if error is not None:
            retry += 1
            if retry > MAX_RETRIES:
                raise RuntimeError("YouTube upload failed after maximum retries") from error
            sleep_seconds = random.uniform(0, min(2**retry, 64))
            print(
                f"Temporary upload error ({type(error).__name__}); "
                f"retry {retry}/{MAX_RETRIES} in {sleep_seconds:.1f}s"
            )
            time.sleep(sleep_seconds)
    if not response or "id" not in response:
        raise RuntimeError("YouTube returned an unexpected upload response")
    return response


def upload(video_path: Path, metadata_path: Path) -> str:
    if not video_path.is_file() or video_path.stat().st_size < 10_000:
        raise RuntimeError(f"Rendered video is missing or invalid: {video_path}")
    metadata = load_metadata(metadata_path)
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": str(metadata.get("category_id", "22")),
        },
        "status": {
            "privacyStatus": metadata.get("privacy_status", "private"),
            "selfDeclaredMadeForKids": bool(metadata.get("made_for_kids", False)),
        },
    }
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    request = youtube_client().videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )
    response = resumable_upload(request)
    video_id = response["id"]
    print(f"Upload complete: https://youtu.be/{video_id}")
    return video_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, default=ROOT / "output" / "short.mp4")
    parser.add_argument("--metadata", type=Path, default=ROOT / "output" / "metadata.json")
    args = parser.parse_args()
    upload(args.video, args.metadata)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


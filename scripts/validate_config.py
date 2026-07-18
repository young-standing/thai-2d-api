#!/usr/bin/env python3
"""Validate GitHub Shorts configuration before spending time on rendering."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

try:
    from scripts.render_short import (
        MUSIC_DIR,
        OUTPUT_DIR,
        ShortsError,
        find_music_files,
        load_quotes,
        load_state,
        validate_channel_name,
    )
except ModuleNotFoundError:  # Support `python scripts/validate_config.py` locally.
    from render_short import (  # type: ignore[no-redef]
        MUSIC_DIR,
        OUTPUT_DIR,
        ShortsError,
        find_music_files,
        load_quotes,
        load_state,
        validate_channel_name,
    )


def require_environment(names: list[str]) -> list[str]:
    return [name for name in names if not os.environ.get(name, "").strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-youtube", action="store_true")
    args = parser.parse_args()

    required = ["PEXELS_API_KEY", "CHANNEL_NAME"]
    if args.require_youtube:
        required.extend(
            ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"]
        )
    missing = require_environment(required)
    if missing:
        raise ShortsError("Missing required configuration: " + ", ".join(missing))
    validate_channel_name(os.environ.get("CHANNEL_NAME", ""))
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise ShortsError("ffmpeg and ffprobe must both be installed")
    load_quotes()
    load_state()
    music = find_music_files(MUSIC_DIR)
    if not music:
        raise ShortsError("No MP3 files found in music/. See music/README.md")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    probe = OUTPUT_DIR / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    print(f"Configuration is valid; {len(music)} music track(s) available.")


if __name__ == "__main__":
    try:
        main()
    except ShortsError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

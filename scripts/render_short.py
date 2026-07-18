#!/usr/bin/env python3
"""Create a 20-second motivational YouTube Short with Pexels footage and music."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
QUOTES_PATH = ROOT / "data" / "quotes.json"
STATE_PATH = ROOT / "data" / "state.json"
MUSIC_DIR = ROOT / "music"
OUTPUT_DIR = ROOT / "output"
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
FALLBACK_SEARCH = "calm sunrise nature"
VIDEO_SECONDS = 20
WIDTH = 1080
HEIGHT = 1920
FPS = 30
HASHTAGS = "#motivation #discipline #success #mindset #shorts"
REQUIRED_CATEGORIES = {
    "discipline",
    "success",
    "healing",
    "confidence",
    "growth",
    "consistency",
}


class ShortsError(RuntimeError):
    """A user-actionable automation error."""


def http_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "github-youtube-shorts/1.0"})
    return session


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ShortsError(f"Required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ShortsError(f"Invalid JSON in {path}: {exc}") from exc


def load_quotes(path: Path = QUOTES_PATH) -> list[dict[str, str]]:
    payload = load_json(path)
    if not isinstance(payload, list) or len(payload) < 100:
        raise ShortsError("data/quotes.json must contain at least 100 entries")
    seen: set[str] = set()
    quotes: list[dict[str, str]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ShortsError(f"Quote entry {index} must be an object")
        required = {"id", "quote", "pexels_search", "category"}
        if set(item) != required:
            raise ShortsError(f"Quote entry {index} must contain exactly {sorted(required)}")
        normalized = {key: str(item[key]).strip() for key in required}
        if not all(normalized.values()):
            raise ShortsError(f"Quote entry {index} contains an empty value")
        if normalized["id"] in seen:
            raise ShortsError(f"Duplicate quote id: {normalized['id']}")
        if normalized["category"] not in REQUIRED_CATEGORIES:
            raise ShortsError(f"Unsupported quote category: {normalized['category']}")
        seen.add(normalized["id"])
        quotes.append(normalized)
    if not REQUIRED_CATEGORIES.issubset({item["category"] for item in quotes}):
        raise ShortsError("The quote catalog does not cover every required category")
    return quotes


def default_state() -> dict[str, Any]:
    return {"version": 1, "cycle": 1, "used_ids": [], "history": []}


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    payload = load_json(path)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ShortsError("data/state.json must be a version 1 state object")
    used_ids = payload.get("used_ids", [])
    history = payload.get("history", [])
    cycle = payload.get("cycle", 1)
    if not isinstance(cycle, int) or cycle < 1:
        raise ShortsError("state.cycle must be a positive integer")
    if not isinstance(used_ids, list) or not all(isinstance(value, str) for value in used_ids):
        raise ShortsError("state.used_ids must be a list of strings")
    if not isinstance(history, list) or not all(isinstance(value, dict) for value in history):
        raise ShortsError("state.history must be a list of objects")
    return {"version": 1, "cycle": cycle, "used_ids": used_ids, "history": history}


def select_quote(
    quotes: list[dict[str, str]],
    state: dict[str, Any],
    *,
    rng: random.Random | random.SystemRandom | None = None,
) -> dict[str, str]:
    """Choose an unused quote; reset only after the complete catalog is used."""
    if not quotes:
        raise ShortsError("The quote catalog is empty")
    chooser = rng or random.SystemRandom()
    known_ids = {entry["id"] for entry in quotes}
    used = {value for value in state.get("used_ids", []) if value in known_ids}
    candidates = [entry for entry in quotes if entry["id"] not in used]
    if not candidates:
        state["used_ids"] = []
        state["cycle"] = int(state.get("cycle", 1)) + 1
        candidates = list(quotes)
    selected = chooser.choice(candidates)
    state.setdefault("used_ids", []).append(selected["id"])
    state.setdefault("history", []).append(
        {
            "id": selected["id"],
            "used_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    state["history"] = state["history"][-200:]
    state["version"] = 1
    state.setdefault("cycle", 1)
    return selected


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def wrap_quote(text: str, *, width: int = 26, max_lines: int = 7) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        raise ShortsError("Quote text cannot be empty")
    lines = textwrap.wrap(
        cleaned,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(lines) > max_lines:
        raise ShortsError(
            f"Quote needs {len(lines)} lines and exceeds the {max_lines}-line safe area"
        )
    if any(len(line) > width + 8 for line in lines):
        raise ShortsError("Quote contains a word too long for the text safe area")
    return "\n".join(lines)


def validate_channel_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        raise ShortsError("CHANNEL_NAME is required")
    if len(cleaned) > 40:
        raise ShortsError("CHANNEL_NAME must be 40 characters or fewer to stay in the safe area")
    return cleaned


def make_title(quote: str) -> str:
    suffix = " #shorts"
    cleaned = re.sub(r"\s+", " ", quote).strip().strip('"')
    limit = 100 - len(suffix)
    if len(cleaned) > limit:
        shortened = cleaned[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:!?")
        cleaned = (shortened or cleaned[: limit - 1]).rstrip() + "…"
    return cleaned + suffix


def build_metadata(
    entry: dict[str, str],
    *,
    creator_name: str,
    source_url: str,
    channel_name: str,
    privacy_status: str,
    music_name: str,
) -> dict[str, Any]:
    if privacy_status not in {"private", "unlisted", "public"}:
        raise ShortsError(f"Invalid privacy status: {privacy_status}")
    quote = entry["quote"].strip()
    return {
        "quote_id": entry["id"],
        "quote": quote,
        "category": entry["category"],
        "pexels_search": entry["pexels_search"],
        "pexels_creator": creator_name,
        "pexels_source_url": source_url,
        "music_file": music_name,
        "channel_name": channel_name,
        "title": make_title(quote),
        "description": (
            f"{quote}\n\n"
            f"Footage by {creator_name} on Pexels: {source_url}\n\n"
            f"{HASHTAGS}"
        ),
        "tags": [
            "motivation",
            "discipline",
            "success",
            "mindset",
            "self improvement",
            entry["category"],
            "shorts",
        ],
        "category_id": "22",
        "made_for_kids": False,
        "privacy_status": privacy_status,
        "duration_seconds": VIDEO_SECONDS,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def valid_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.netloc)
    except ValueError:
        return False


def video_file_score(item: dict[str, Any]) -> tuple[float, float]:
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    ratio_penalty = abs((width / height if height else 99) - (9 / 16))
    size_penalty = abs(width - WIDTH) + abs(height - HEIGHT)
    landscape_penalty = 10 if width >= height else 0
    return (landscape_penalty + ratio_penalty, size_penalty)


def suitable_video_files(videos: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for video in videos:
        creator = video.get("user") or {}
        for media in video.get("video_files") or []:
            link = str(media.get("link") or "")
            file_type = str(media.get("file_type") or "")
            width = int(media.get("width") or 0)
            height = int(media.get("height") or 0)
            if (
                not valid_https_url(link)
                or file_type != "video/mp4"
                or width < 540
                or height < 960
            ):
                continue
            candidates.append(
                {
                    "download_url": link,
                    "width": width,
                    "height": height,
                    "creator_name": str(creator.get("name") or "Pexels contributor"),
                    "source_url": str(video.get("url") or "https://www.pexels.com/videos/"),
                    "score": video_file_score(media),
                }
            )
    return sorted(candidates, key=lambda value: value["score"])


def search_pexels(
    phrase: str,
    *,
    api_key: str,
    rng: random.Random | random.SystemRandom | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    chooser = rng or random.SystemRandom()
    client = session or http_session()
    errors: list[str] = []
    phrases = [phrase]
    if phrase.casefold() != FALLBACK_SEARCH.casefold():
        phrases.append(FALLBACK_SEARCH)
    for query in phrases:
        try:
            response = client.get(
                PEXELS_SEARCH_URL,
                headers={"Authorization": api_key},
                params={
                    "query": query,
                    "orientation": "portrait",
                    "size": "medium",
                    "locale": "en-US",
                    "per_page": 40,
                },
                timeout=(10, 45),
            )
            response.raise_for_status()
            payload = response.json()
            candidates = suitable_video_files(payload.get("videos") or [])
            if candidates:
                top = candidates[: min(8, len(candidates))]
                selected = chooser.choice(top)
                selected["search_used"] = query
                return selected
            errors.append(f"no suitable portrait MP4 results for {query!r}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{query!r}: {exc}")
    raise ShortsError("Pexels search failed; " + "; ".join(errors))


def download_file(url: str, destination: Path, *, session: requests.Session | None = None) -> None:
    if not valid_https_url(url):
        raise ShortsError("Pexels returned an invalid download URL")
    client = session or http_session()
    try:
        with client.get(url, stream=True, timeout=(10, 120)) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "video" not in content_type and "octet-stream" not in content_type:
                raise ShortsError(f"Pexels download returned unexpected content type: {content_type}")
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
    except requests.RequestException as exc:
        raise ShortsError(f"Unable to download Pexels footage: {exc}") from exc
    if not destination.exists() or destination.stat().st_size < 1024:
        raise ShortsError("Downloaded Pexels footage is empty or invalid")


def find_music_files(directory: Path = MUSIC_DIR) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() == ".mp3"
    )


def locate_font() -> Path:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return path
    raise ShortsError("A bold DejaVu Sans or Arial font could not be found")


def ffmpeg_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def run_ffmpeg(
    footage: Path,
    music: Path,
    quote_file: Path,
    channel_file: Path,
    output: Path,
) -> None:
    font = locate_font()
    quote_path = ffmpeg_filter_path(quote_file)
    channel_path = ffmpeg_filter_path(channel_file)
    font_path = ffmpeg_filter_path(font)
    video_filter = (
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS},eq=brightness=-0.12:saturation=0.92,"
        "format=yuv420p,"
        f"drawtext=fontfile='{font_path}':textfile='{quote_path}':expansion=none:"
        "fontcolor=white:fontsize=76:line_spacing=18:"
        "x=(w-text_w)/2:y=(h-text_h)/2:"
        "box=1:boxcolor=black@0.48:boxborderw=38,"
        f"drawtext=fontfile='{font_path}':textfile='{channel_path}':expansion=none:"
        "fontcolor=white@0.90:fontsize=38:"
        "x=(w-text_w)/2:y=h-190,"
        f"trim=duration={VIDEO_SECONDS},setpts=PTS-STARTPTS[v];"
        f"[1:a]volume=0.16,afade=t=in:st=0:d=1,"
        f"afade=t=out:st={VIDEO_SECONDS - 2}:d=2,"
        f"atrim=duration={VIDEO_SECONDS},asetpts=PTS-STARTPTS[a]"
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(footage),
        "-stream_loop",
        "-1",
        "-i",
        str(music),
        "-filter_complex",
        video_filter,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        str(VIDEO_SECONDS),
        "-r",
        str(FPS),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "21",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-40:])
        raise ShortsError(f"FFmpeg rendering failed:\n{tail}")
    if not output.exists() or output.stat().st_size < 10_000:
        raise ShortsError("FFmpeg completed without producing a valid MP4")


def write_metadata(metadata: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quote", default=os.environ.get("INPUT_QUOTE", ""))
    parser.add_argument("--search", default=os.environ.get("INPUT_PEXELS_SEARCH", ""))
    parser.add_argument(
        "--privacy",
        default=os.environ.get("INPUT_PRIVACY_STATUS", "private"),
        choices=("private", "unlisted", "public"),
    )
    parser.add_argument("--footage-file", type=Path, help="Local footage for offline smoke tests")
    parser.add_argument("--music-file", type=Path, help="Specific music for offline smoke tests")
    parser.add_argument("--seed", type=int, help="Deterministic selection for tests")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rng: random.Random | random.SystemRandom = (
        random.Random(args.seed) if args.seed is not None else random.SystemRandom()
    )
    channel_name = validate_channel_name(os.environ.get("CHANNEL_NAME", ""))
    if not shutil.which("ffmpeg"):
        raise ShortsError("ffmpeg is not installed or not on PATH")

    quotes = load_quotes()
    state = load_state()
    quote_override = re.sub(r"\s+", " ", args.quote).strip()
    if quote_override:
        selected = {
            "id": "manual",
            "quote": quote_override,
            "pexels_search": args.search.strip() or FALLBACK_SEARCH,
            "category": "growth",
        }
        state_changed = False
    else:
        selected = select_quote(quotes, state, rng=rng)
        if args.search.strip():
            selected = {**selected, "pexels_search": args.search.strip()}
        state_changed = True

    wrapped = wrap_quote(selected["quote"])
    music_files = find_music_files()
    if args.music_file:
        if not args.music_file.is_file():
            raise ShortsError(f"Music file does not exist: {args.music_file}")
        music = args.music_file
    else:
        if not music_files:
            raise ShortsError("No MP3 files found in music/. See music/README.md")
        music = rng.choice(music_files)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_video = OUTPUT_DIR / "short.mp4"
    output_metadata = OUTPUT_DIR / "metadata.json"

    with tempfile.TemporaryDirectory(prefix="youtube-short-") as temp_name:
        temporary = Path(temp_name)
        if args.footage_file:
            if not args.footage_file.is_file():
                raise ShortsError(f"Footage file does not exist: {args.footage_file}")
            footage = args.footage_file
            asset = {
                "creator_name": "Local smoke test",
                "source_url": "https://www.pexels.com/",
                "search_used": selected["pexels_search"],
            }
        else:
            api_key = os.environ.get("PEXELS_API_KEY", "").strip()
            if not api_key:
                raise ShortsError("PEXELS_API_KEY is required")
            asset = search_pexels(selected["pexels_search"], api_key=api_key, rng=rng)
            footage = temporary / "pexels.mp4"
            download_file(asset["download_url"], footage)

        quote_file = temporary / "quote.txt"
        channel_file = temporary / "channel.txt"
        quote_file.write_text(wrapped, encoding="utf-8")
        channel_file.write_text(channel_name, encoding="utf-8")
        run_ffmpeg(footage, music, quote_file, channel_file, output_video)

    metadata = build_metadata(
        selected,
        creator_name=asset["creator_name"],
        source_url=asset["source_url"],
        channel_name=channel_name,
        privacy_status=args.privacy,
        music_name=music.name,
    )
    metadata["pexels_search_used"] = asset["search_used"]
    write_metadata(metadata, output_metadata)
    if state_changed:
        save_state(state)
    print(f"Rendered {output_video.relative_to(ROOT)}")
    print(f"Metadata {output_metadata.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except ShortsError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

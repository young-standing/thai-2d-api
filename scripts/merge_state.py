#!/usr/bin/env python3
"""Merge a rendered quote-state snapshot with the latest remote state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_state(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise RuntimeError(f"Unsupported state version in {path}")
    return payload


def merge_states(current: dict[str, Any], rendered: dict[str, Any]) -> dict[str, Any]:
    current_cycle = int(current.get("cycle", 1))
    rendered_cycle = int(rendered.get("cycle", 1))
    if rendered_cycle > current_cycle:
        used_ids = list(dict.fromkeys(rendered.get("used_ids", [])))
    elif current_cycle > rendered_cycle:
        used_ids = list(dict.fromkeys(current.get("used_ids", [])))
    else:
        used_ids = list(
            dict.fromkeys([*current.get("used_ids", []), *rendered.get("used_ids", [])])
        )
    history_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for event in [*current.get("history", []), *rendered.get("history", [])]:
        if isinstance(event, dict) and event.get("id") and event.get("used_at"):
            history_by_key[(str(event["id"]), str(event["used_at"]))] = {
                "id": str(event["id"]),
                "used_at": str(event["used_at"]),
            }
    history = sorted(history_by_key.values(), key=lambda item: item["used_at"])[-200:]
    return {
        "version": 1,
        "cycle": max(current_cycle, rendered_cycle),
        "used_ids": used_ids,
        "history": history,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current", type=Path)
    parser.add_argument("rendered", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    merged = merge_states(read_state(args.current), read_state(args.rendered))
    args.output.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

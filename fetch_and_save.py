"""Fetch one unified SET sample and persist it atomically to SQLite."""

from __future__ import annotations

import asyncio
import json
import os

from market_repository import MarketRepository
from unified_set_client import UnifiedSetClient


async def main() -> None:
    repository = MarketRepository(os.getenv("DATABASE_PATH", "thai_2d.sqlite3"))
    repository.initialize()
    sample = await UnifiedSetClient().fetch()
    repository.save_sample(sample)
    latest = repository.get_latest()
    if latest is None:
        raise RuntimeError("Latest SET sample was not available after save")
    print(json.dumps(latest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())

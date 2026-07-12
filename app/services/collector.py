import asyncio
from datetime import datetime, timezone

import structlog

from app.config import Settings
from app.database import SessionLocal
from app.repositories.market_repository import MarketRepository
from app.services.set_client import SetClient

log = structlog.get_logger(__name__)


class MarketCollector:
    def __init__(self, settings: Settings, client: SetClient):
        self.settings = settings
        self.client = client
        self._task: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.settings.collector_enabled and not self.running:
            self._task = asyncio.create_task(self._run(), name="set-market-collector")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                log.error("collector_refresh_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(self.settings.collector_interval_seconds)

    async def refresh(self):
        async with self._refresh_lock:
            quote = await self.client.fetch()
            with SessionLocal() as session:
                snapshot, inserted = MarketRepository(session).add_if_new(
                    market="SET",
                    index=quote.index,
                    value=quote.value,
                    source_timestamp=quote.source_timestamp,
                    source=quote.source,
                )
            log.info(
                "market_snapshot_collected",
                inserted=inserted,
                index=quote.index,
                value=quote.value,
                source=quote.source,
                collected_at=datetime.now(timezone.utc).isoformat(),
            )
            return snapshot, inserted

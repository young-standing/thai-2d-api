import logging
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.database import create_tables
from app.services.collector import MarketCollector
from app.services.set_client import SetClient


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(stream=sys.stdout, level=settings.log_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    collector = MarketCollector(get_settings(), SetClient(get_settings()))
    app.state.collector = collector
    collector.start()
    yield
    await collector.stop()


app = FastAPI(title="Thai 2D API", version="1.0.0", lifespan=lifespan)
app.include_router(router)

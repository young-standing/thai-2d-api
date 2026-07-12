"""Read-only FastAPI application backed exclusively by stored SQLite data."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from market_repository import MarketRepository
from two_d_service import MyanmarTwoDStrategy, TwoDCalculationError

logger = logging.getLogger("thai_2d_api")


def _log(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str, sort_keys=True))


class MarketDataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    last: str
    value: str
    market_datetime: str
    market_status: str
    change: str
    percent_change: str
    source_client: str
    fetched_at: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]
    latest_available: bool
    latest_fetched_at: str | None
    stale: bool


class TwoDLatestResponse(BaseModel):
    number: str
    index_digit: str
    value_digit: str
    set_index: str
    value_raw: str
    value_million: str
    market_datetime: str
    market_status: str
    fetched_at: str
    source_client: str
    strategy: str
    stale: bool


def stale_after_seconds() -> int:
    raw = os.getenv("STALE_AFTER_SECONDS", "86400")
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        raise ValueError("STALE_AFTER_SECONDS must be a positive integer")
    return int(raw)


def allowed_origins() -> list[str]:
    return [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()]


def parse_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored fetched_at must be timezone-aware")
    return parsed


def get_repository(request: Request) -> MarketRepository:
    return request.app.state.repository


RepositoryDependency = Annotated[MarketRepository, Depends(get_repository)]


def create_app(
    repository: MarketRepository | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    market_repository = repository or MarketRepository(os.getenv("DATABASE_PATH", "thai_2d.sqlite3"))
    clock = now or (lambda: datetime.now(timezone.utc))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _log("api_started")
        yield
        _log("api_stopped")

    application = FastAPI(title="Thai 2D API", version="1.0.0", lifespan=lifespan)
    application.state.repository = market_repository
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
    )

    def is_stale(row: dict | None) -> bool:
        if row is None:
            return True
        current = clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("API clock must return a timezone-aware datetime")
        fetched = parse_aware_datetime(row["fetched_at"])
        age = current.astimezone(timezone.utc) - fetched.astimezone(timezone.utc)
        return age.total_seconds() > stale_after_seconds()

    def latest_or_404(repo: MarketRepository) -> dict:
        latest = repo.get_latest()
        if latest is None:
            raise HTTPException(status_code=404, detail="No stored market data is available")
        return latest

    @application.exception_handler(sqlite3.Error)
    async def database_error_handler(_: Request, exc: sqlite3.Error) -> JSONResponse:
        _log("api_error", error_type=type(exc).__name__, component="database")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @application.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        _log("api_error", error_type=type(exc).__name__, component="application")
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @application.get("/health", response_model=HealthResponse)
    def health(repo: RepositoryDependency) -> HealthResponse:
        try:
            latest = repo.get_latest()
            return HealthResponse(
                status="ok",
                database="ok",
                latest_available=latest is not None,
                latest_fetched_at=latest["fetched_at"] if latest else None,
                stale=is_stale(latest),
            )
        except sqlite3.Error as exc:
            _log("api_error", error_type=type(exc).__name__, component="database_health")
            return HealthResponse(
                status="degraded",
                database="error",
                latest_available=False,
                latest_fetched_at=None,
                stale=True,
            )

    @application.get("/api/market/latest", response_model=MarketDataResponse)
    def latest_market(repo: RepositoryDependency) -> dict:
        return latest_or_404(repo)

    @application.get("/api/market/history", response_model=list[MarketDataResponse])
    def market_history(
        repo: RepositoryDependency,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict]:
        return repo.get_history(limit)

    @application.get("/api/2d/latest", response_model=TwoDLatestResponse)
    def latest_two_d(repo: RepositoryDependency) -> TwoDLatestResponse:
        latest = latest_or_404(repo)
        try:
            calculated = MyanmarTwoDStrategy().calculate(
                last=latest["last"], value=latest["value"]
            )
        except TwoDCalculationError as exc:
            _log("api_error", error_type=type(exc).__name__, component="two_d")
            raise HTTPException(status_code=500, detail="Internal server error") from exc
        return TwoDLatestResponse(
            **calculated,
            market_datetime=latest["market_datetime"],
            market_status=latest["market_status"],
            fetched_at=latest["fetched_at"],
            source_client=latest["source_client"],
            stale=is_stale(latest),
        )

    return application


app = create_app()

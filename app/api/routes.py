import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.repositories.market_repository import MarketRepository
from app.schemas import HealthOut, MarketSnapshotOut, RefreshOut, TwoDOut
from app.services.two_d_service import get_two_d_strategy

router = APIRouter()


def _is_stale(collected_at: datetime, settings: Settings) -> bool:
    reference = collected_at if collected_at.tzinfo else collected_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - reference).total_seconds() > settings.stale_after_seconds


def _snapshot_out(snapshot, settings: Settings) -> MarketSnapshotOut:
    result = MarketSnapshotOut.model_validate(snapshot)
    return result.model_copy(update={"is_stale": _is_stale(snapshot.collected_at, settings)})


def _require_latest(db: Session):
    snapshot = MarketRepository(db).latest()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No market data has been collected yet")
    return snapshot


@router.get("/health", response_model=HealthOut)
def health(request: Request, db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "error"
    latest = MarketRepository(db).latest()
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        database=database,
        collector_running=request.app.state.collector.running,
        latest_data_at=latest.collected_at if latest else None,
    )


@router.get("/api/market/latest", response_model=MarketSnapshotOut)
def market_latest(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return _snapshot_out(_require_latest(db), settings)


@router.get("/api/market/history", response_model=list[MarketSnapshotOut])
def market_history(
    limit: int = Query(default=50, ge=1, le=1000),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return [_snapshot_out(item, settings) for item in MarketRepository(db).history(limit)]


@router.get("/api/2d/latest", response_model=TwoDOut)
def two_d_latest(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    snapshot = _require_latest(db)
    strategy = get_two_d_strategy(settings.two_d_strategy)
    result = strategy.calculate(snapshot)
    return TwoDOut(
        market=snapshot.market,
        set_index=snapshot.index,
        set_value=snapshot.value,
        two_d=result,
        strategy=strategy.name,
        calculation_status="not_configured" if result is None else "calculated",
        source_timestamp=snapshot.source_timestamp,
        collected_at=snapshot.collected_at,
        is_stale=_is_stale(snapshot.collected_at, settings),
    )


@router.post("/api/admin/refresh", response_model=RefreshOut)
async def admin_refresh(
    request: Request,
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
):
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    try:
        snapshot, inserted = await request.app.state.collector.refresh()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="SET collection failed") from exc
    return RefreshOut(status="ok", inserted=inserted, snapshot=_snapshot_out(snapshot, settings))

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MarketSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    market: str
    index: str
    value: str
    source_timestamp: datetime
    collected_at: datetime
    source: str
    is_stale: bool = False


class TwoDOut(BaseModel):
    market: str
    set_index: str
    set_value: str
    two_d: str | None
    strategy: str
    calculation_status: str
    source_timestamp: datetime
    collected_at: datetime
    is_stale: bool


class HealthOut(BaseModel):
    status: str
    database: str
    collector_running: bool
    latest_data_at: datetime | None


class RefreshOut(BaseModel):
    status: str
    inserted: bool
    snapshot: MarketSnapshotOut

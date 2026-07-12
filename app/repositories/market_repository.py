from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import MarketSnapshot


class MarketRepository:
    def __init__(self, session: Session):
        self.session = session

    def latest(self) -> MarketSnapshot | None:
        return self.session.scalar(select(MarketSnapshot).order_by(desc(MarketSnapshot.source_timestamp), desc(MarketSnapshot.id)).limit(1))

    def history(self, limit: int) -> list[MarketSnapshot]:
        return list(self.session.scalars(select(MarketSnapshot).order_by(desc(MarketSnapshot.source_timestamp), desc(MarketSnapshot.id)).limit(limit)))

    def add_if_new(
        self, *, market: str, index: str, value: str, source_timestamp: datetime, source: str
    ) -> tuple[MarketSnapshot, bool]:
        snapshot = MarketSnapshot(
            market=market, index=index, value=value, source_timestamp=source_timestamp, source=source
        )
        self.session.add(snapshot)
        try:
            self.session.commit()
            self.session.refresh(snapshot)
            return snapshot, True
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(MarketSnapshot).where(
                    MarketSnapshot.market == market,
                    MarketSnapshot.index == index,
                    MarketSnapshot.value == value,
                    MarketSnapshot.source_timestamp == source_timestamp,
                )
            )
            if existing is None:
                raise
            return existing, False

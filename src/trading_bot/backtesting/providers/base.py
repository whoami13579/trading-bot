from datetime import datetime
from typing import Protocol
from ..models import Dataset


class HistoricalProvider(Protocol):
    def fetch(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> Dataset: ...

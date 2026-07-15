"""Strategy interface. Strategies are pure functions of an indicator-enriched
OHLCV DataFrame -> Signal, with no knowledge of orders, accounts or IB."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

import pandas as pd


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Strategy(ABC):
    @property
    @abstractmethod
    def min_bars(self) -> int:
        """Minimum number of bars required (indicator warmup) before signals are valid."""

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """df must already contain indicator columns (see data.indicators.add_indicators)."""

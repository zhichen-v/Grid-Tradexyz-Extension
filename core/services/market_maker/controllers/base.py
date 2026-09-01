from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class MarketFeatureSnapshotLike(Protocol):
    health: object
    reason: str
    sample_count: int
    mid: Decimal | None
    return_5s_ticks: Decimal | None
    return_15s_ticks: Decimal | None
    rms_1s_move_15s_ticks: Decimal | None
    microprice_shift_ticks: Decimal | None


@dataclass(frozen=True)
class SideQuoteAdjustment:
    extra_spread_ticks: int = 0
    blocked: bool = False
    toxicity_score_ticks: Decimal = Decimal("0")
    directional_confirmations: int = 0
    reason: str = "none"


@dataclass(frozen=True)
class QuoteControllerDecision:
    mode: str
    controller: str
    ready: bool
    reason: str
    decision_id: int
    bid: SideQuoteAdjustment
    ask: SideQuoteAdjustment
    features: MarketFeatureSnapshotLike | None
    applies_to_entry_only: bool = True


@dataclass(frozen=True)
class QuoteControllerContext:
    now_monotonic: float
    features: MarketFeatureSnapshotLike | None
    market: object | None = None
    metadata: object | None = None
    position: object | None = None
    risk: object | None = None
    live_orders: tuple[object, ...] = ()
    base_quotes: object | None = None
    entry_markout_feedback: object | None = None
    economic_stop_pending: bool = False
    entry_admission_allowed: bool = True
    inventory_unwind_active: bool = False
    active_unwind_pending: bool = False
    active_unwind_ready: bool = False


class EntryQuoteController(Protocol):
    def evaluate(self, context: QuoteControllerContext) -> QuoteControllerDecision:
        ...

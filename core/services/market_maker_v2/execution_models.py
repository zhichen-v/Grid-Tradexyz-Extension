"""Execution-only DTOs shared by the V2 order manager and execution port."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ...adapters.exchanges.models import OrderSide


class RuntimeState(Enum):
    STARTING = "starting"
    SYNCING = "syncing"
    ACTIVE = "active"
    RISK_REDUCTION = "risk_reduction"
    PAUSED_MARKET = "paused_market"
    PAUSED_DATA = "paused_data"
    PAUSED_POSITION = "paused_position"
    PAUSED_EXCHANGE = "paused_exchange"
    PAUSED_ORDER_STATE = "paused_order_state"
    PAUSED_ERROR = "paused_error"
    STOPPING = "stopping"
    STOPPED = "stopped"


class OrderSlotState(Enum):
    EMPTY = "empty"
    SUBMITTING = "submitting"
    LIVE = "live"
    PARTIALLY_FILLED = "partially_filled"
    CANCELING = "canceling"
    UNCERTAIN_SUBMISSION = "uncertain_submission"
    UNCERTAIN_CANCELLATION = "uncertain_cancellation"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class MarketMetadata:
    symbol: str
    price_decimals: int
    size_decimals: int
    price_tick: Decimal
    quantity_step: Decimal
    min_base_amount: Decimal
    min_quote_amount: Decimal


@dataclass(frozen=True)
class DesiredOrder:
    side: OrderSide
    price: Decimal
    amount: Decimal
    reduce_only: bool
    reason: str


@dataclass(frozen=True)
class DesiredQuotes:
    bid: DesiredOrder | None
    ask: DesiredOrder | None
    reference_price: Decimal
    reservation_price: Decimal
    half_spread: Decimal
    inventory_ratio: Decimal
    runtime_state: RuntimeState
    reason: str


@dataclass
class ManagedOrder:
    side: OrderSide
    state: OrderSlotState
    order_id: str | None
    client_id: str | None
    price: Decimal
    amount: Decimal
    remaining: Decimal
    reduce_only: bool
    created_monotonic: float
    updated_monotonic: float
    submission_uncertain: bool = False
    cancellation_uncertain: bool = False
    simulated: bool = False


@dataclass(frozen=True)
class RiskDecision:
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    buy_reduce_only: bool
    sell_reduce_only: bool
    buy_capacity: Decimal
    sell_capacity: Decimal
    worst_long: Decimal
    worst_short: Decimal
    inventory_ratio: Decimal
    runtime_state: RuntimeState
    reason: str
    safe: bool
    soft_exit_latched: bool = False

    @property
    def allow_buy(self) -> bool:
        return self.safe and self.buy_amount is not None and self.buy_amount > 0

    @property
    def allow_sell(self) -> bool:
        return self.safe and self.sell_amount is not None and self.sell_amount > 0

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from ...adapters.exchanges.models import OrderBookLevel, OrderSide


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


class OrderIntentKind(Enum):
    BASE_ENTRY = "base_entry"
    CONTROLLER_ENTRY = "controller_entry"
    PASSIVE_EXIT = "passive_exit"
    ACTIVE_EXIT = "active_exit"


class InventoryExitStage(Enum):
    STRICT_PROFIT = "strict_profit"
    SURPLUS_FUNDED_PASSIVE = "surplus_funded_passive"
    BOUNDED_PASSIVE_LOSS = "bounded_passive_loss"
    INVENTORY_HOLD = "inventory_hold"
    ACTIVE_IOC = "active_ioc"
    FLAT_PENDING_AUDIT = "flat_pending_audit"
    COMPLETED = "completed"


class ExitBindingConstraint(Enum):
    NORMAL_PASSIVE = "normal_passive"
    EPISODE_CAP = "episode_cap"
    SESSION_SURPLUS = "session_surplus"
    SESSION_LOSS_CAP = "session_loss_cap"
    DRAWDOWN_CAP = "drawdown_cap"
    ACTIVE_SLIPPAGE = "active_slippage"
    ATTEMPT_CAP = "attempt_cap"
    DATA_UNTRUSTED = "data_untrusted"


@dataclass(frozen=True)
class OrderIntentMetadata:
    kind: OrderIntentKind
    revision: int
    controller_decision_id: int | None = None
    controller_outward_only: bool = False
    controller_extra_spread_ticks: int | None = None
    inventory_episode_id: int | None = None
    authenticated_episode_sequence: int | None = None
    exit_stage: InventoryExitStage | None = None
    policy_decision_id: int | None = None
    binding_constraint: ExitBindingConstraint | None = None
    available_completed_surplus: Decimal | None = None
    surplus_reserve: Decimal | None = None
    unlocked_episode_loss: Decimal | None = None
    allowed_passive_loss: Decimal | None = None
    entered_inventory_hold: bool = False
    active_attempts: int = 0


@dataclass(frozen=True)
class EpisodePolicyObservation:
    authenticated_episode_sequence: int
    entered_inventory_hold: bool
    active_attempts: int
    max_unlocked_episode_loss: Decimal


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
class MarketSnapshot:
    symbol: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    best_bid: Decimal
    best_ask: Decimal
    exchange_timestamp: datetime | None
    received_monotonic: float


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    signed_size: Decimal
    entry_price: Decimal | None
    unrealized_pnl: Decimal | None
    received_monotonic: float


@dataclass(frozen=True)
class DesiredOrder:
    side: OrderSide
    price: Decimal
    amount: Decimal
    reduce_only: bool
    reason: str
    intent: OrderIntentMetadata | None = None


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
    controller_blocked_sides: frozenset[OrderSide] = frozenset()


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
    intent: OrderIntentMetadata | None = None
    simulated: bool = False

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Iterable, Mapping
from uuid import uuid4

from ...adapters.exchanges.models import OrderSide
from .config import MarketMakerConfig
from .models import (
    EpisodePolicyObservation,
    OrderIntentKind,
    OrderIntentMetadata,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_TEN_THOUSAND = Decimal("10000")
_READ_ATTEMPTS = 6
_READ_RETRY_SECONDS = 1.0
_COMPLETED_EPISODE_LEDGER_LIMIT = 100
_AUTHENTICATED_FILL_LEDGER_LIMIT = 500
# The authenticated API returns at most 100 recent trades.  Keep a much larger
# exact horizon and evict only proofs outside the current page/open episode.
_SESSION_ATTRIBUTION_LIMIT = 8192
_ORDER_ROLE_BINDING_LIMIT = _SESSION_ATTRIBUTION_LIMIT
_SEEN_TRADE_ID_LIMIT = _SESSION_ATTRIBUTION_LIMIT
_SECONDS_TIMESTAMP_LIMIT = 10_000_000_000


class AccountAuditError(RuntimeError):
    """The authenticated account state is unsafe or cannot be trusted."""


class _SnapshotMismatch(RuntimeError):
    pass


def _decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountAuditError(f"invalid {name}") from exc
    if not parsed.is_finite():
        raise AccountAuditError(f"invalid {name}")
    return parsed


def _position_state(
    positions: Iterable[Any], symbol: str
) -> tuple[Decimal, Decimal]:
    signed = _ZERO
    unrealized_pnl = _ZERO
    for position in positions:
        size = _decimal(getattr(position, "size", None), "position size")
        if size < 0:
            raise AccountAuditError("position size cannot be negative")
        if size == 0:
            continue
        position_symbol = getattr(position, "symbol", None)
        if position_symbol != symbol:
            raise AccountAuditError(
                f"non-{symbol} position is present: {position_symbol}"
            )
        raw_data = getattr(position, "raw_data", None)
        raw_position = (
            raw_data.get("position_info") if isinstance(raw_data, dict) else None
        )
        margin_fraction = _decimal(
            getattr(raw_position, "initial_margin_fraction", None),
            "position initial margin fraction",
        )
        if margin_fraction != 100:
            raise AccountAuditError(f"{symbol} position leverage is not 1x")
        if _decimal(getattr(position, "leverage", None), "position leverage") != 1:
            raise AccountAuditError(f"{symbol} position leverage is not 1x")
        margin_mode = str(
            getattr(getattr(position, "margin_mode", None), "value", "")
        ).lower()
        if margin_mode != "cross":
            raise AccountAuditError(f"{symbol} position is not cross margin")
        unrealized_pnl += _decimal(
            getattr(position, "unrealized_pnl", None),
            "position unrealized pnl",
        )
        side = str(getattr(getattr(position, "side", None), "value", "")).lower()
        if side == "long":
            signed += size
        elif side == "short":
            signed -= size
        else:
            raise AccountAuditError("position side is invalid")
    return signed, unrealized_pnl


def _collateral_total(balances: Iterable[Any]) -> Decimal:
    candidates = [
        balance
        for balance in balances
        if str(getattr(balance, "currency", "")).upper() in {"USDG", "USDC"}
    ]
    if len(candidates) != 1:
        raise AccountAuditError("exactly one USDG/USDC collateral balance is required")
    total = _decimal(getattr(candidates[0], "total", None), "collateral total")
    if total < 0:
        raise AccountAuditError("collateral total cannot be negative")
    return total


def _trade_ids(trades: Iterable[Any]) -> set[str]:
    ids = [str(getattr(trade, "id", "") or "") for trade in trades]
    if any(not trade_id for trade_id in ids) or len(ids) != len(set(ids)):
        raise _SnapshotMismatch("account trade page has invalid/duplicate ids")
    return set(ids)


def _order_ids(orders: Iterable[Any]) -> set[str]:
    ids = [str(getattr(order, "id", "") or "") for order in orders]
    if any(not order_id for order_id in ids) or len(ids) != len(set(ids)):
        raise _SnapshotMismatch("account open order page has invalid/duplicate ids")
    return set(ids)


def _has_zero_integrator_fee_signing_proof(adapter: Any) -> bool:
    value = getattr(adapter, "managed_order_integrator_fee_tick", None)
    return type(value) is int and value == 0


@dataclass(frozen=True)
class _Fill:
    trade_id: str
    order_id: str
    side: OrderSide
    amount: Decimal
    turnover: Decimal
    fee: Decimal
    gross: Decimal
    sort_key: tuple[int, int]
    active_unwind: bool = False

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.side is OrderSide.BUY else -self.amount


def _trade_sort_key(trade: Any) -> tuple[int, int]:
    trade_id = str(getattr(trade, "id", "") or "")
    raw_data = getattr(trade, "raw_data", None)
    if not isinstance(raw_data, dict):
        raise AccountAuditError("account trade metadata is unavailable")
    timestamp = raw_data.get("timestamp")
    if type(timestamp) is not int or timestamp < 0:
        raise AccountAuditError("account trade timestamp is invalid")
    trade_sequence = raw_data.get("trade_sequence", trade_id)
    try:
        trade_sequence = int(str(trade_sequence))
    except (TypeError, ValueError) as exc:
        raise AccountAuditError("account trade sequence is invalid") from exc
    if trade_sequence < 0:
        raise AccountAuditError("account trade sequence is invalid")
    return timestamp, trade_sequence


def _inventory_duration_seconds(opened_at: int, closed_at: int) -> Decimal:
    opened_seconds = Decimal(opened_at) / (
        Decimal("1000")
        if opened_at >= _SECONDS_TIMESTAMP_LIMIT
        else Decimal("1")
    )
    closed_seconds = Decimal(closed_at) / (
        Decimal("1000")
        if closed_at >= _SECONDS_TIMESTAMP_LIMIT
        else Decimal("1")
    )
    return max(_ZERO, closed_seconds - opened_seconds)


def _fill_from_trade(
    trade: Any,
    expected_fee_rate: Decimal,
    *,
    taker_fee_rate: Decimal = _ZERO,
    active_unwind_order_ids: set[str] | frozenset[str] = frozenset(),
    allow_unreported_zero_integrator_fee: bool = False,
) -> _Fill:
    trade_id = str(getattr(trade, "id", "") or "")
    order_id = str(getattr(trade, "order_id", "") or "")
    if not trade_id or not order_id:
        raise AccountAuditError("account trade is missing a stable trade/order id")
    side = getattr(trade, "side", None)
    if side not in {OrderSide.BUY, OrderSide.SELL}:
        raise AccountAuditError("account trade side is invalid")
    amount = _decimal(getattr(trade, "amount", None), "trade amount")
    turnover = abs(_decimal(getattr(trade, "cost", None), "trade turnover"))
    if amount <= 0 or turnover <= 0:
        raise AccountAuditError("account trade amount/turnover must be positive")

    fee_data = getattr(trade, "fee", None)
    raw_data = getattr(trade, "raw_data", None)
    if not isinstance(fee_data, dict) or not isinstance(raw_data, dict):
        raise AccountAuditError("account trade fee metadata is unavailable")
    active_unwind = order_id in active_unwind_order_ids
    expected_role = "taker" if active_unwind else "maker"
    if fee_data.get("role") != expected_role:
        raise AccountAuditError(
            "active unwind trade is not taker"
            if active_unwind
            else "non-maker account trade detected"
        )
    expected_rate = taker_fee_rate if active_unwind else expected_fee_rate
    fee_rate = _decimal(fee_data.get("rate"), "trade fee rate")
    if fee_rate != expected_rate:
        raise AccountAuditError(
            f"{'taker' if active_unwind else 'maker'} fee changed: "
            f"expected {expected_rate}, observed {fee_rate}"
        )
    fee = _decimal(fee_data.get("cost"), "trade fee")
    gross = _decimal(raw_data.get("realized_pnl"), "trade realized pnl")
    integrator_fee_tick = raw_data.get("integrator_fee_tick")
    if integrator_fee_tick is None:
        if not allow_unreported_zero_integrator_fee:
            raise AccountAuditError("account trade integrator fee is unverified")
    elif _decimal(integrator_fee_tick, "trade integrator fee") != 0:
        raise AccountAuditError("account trade contains an unaccounted integrator fee")
    if fee < 0:
        raise AccountAuditError("trade fee cannot be negative")
    if raw_data.get("trade_type") != "trade":
        raise AccountAuditError("non-standard account trade detected")
    return _Fill(
        trade_id=trade_id,
        order_id=order_id,
        side=side,
        amount=amount,
        turnover=turnover,
        fee=fee,
        gross=gross,
        sort_key=_trade_sort_key(trade),
        active_unwind=active_unwind,
    )


@dataclass
class SessionEconomics:
    config: MarketMakerConfig
    baseline_equity: Decimal
    session_id: str = field(default_factory=lambda: uuid4().hex)
    seen_trade_ids: set[str] = field(default_factory=set)
    ledger_position: Decimal = _ZERO
    unique_maker_fills: int = 0
    unique_taker_fills: int = 0
    maker_turnover: Decimal = _ZERO
    turnover: Decimal = _ZERO
    exact_fee: Decimal = _ZERO
    gross: Decimal = _ZERO
    completed_round_trips: int = 0
    completed_fills: int = 0
    completed_turnover: Decimal = _ZERO
    completed_exact_fee: Decimal = _ZERO
    completed_gross: Decimal = _ZERO
    completed_episode_ledger: list[dict[str, Any]] = field(default_factory=list)
    authenticated_fill_attributions: list[dict[str, Any]] = field(
        default_factory=list
    )
    _order_role_bindings: dict[
        str, tuple[str, str, int, bool]
    ] = field(default_factory=dict, repr=False)
    _order_intent_bindings: dict[str, OrderIntentMetadata] = field(
        default_factory=dict, repr=False
    )
    _seen_trade_id_order: dict[str, None] = field(
        default_factory=dict, repr=False
    )
    _episode_order_ids: set[str] = field(default_factory=set, repr=False)
    seen_trade_id_evictions: int = 0
    order_role_binding_evictions: int = 0
    _last_applied_fill_sort_key: tuple[int, int] | None = field(
        default=None, repr=False
    )
    episode_active_unwind_flat: int = 0
    active_unwind_turnover: Decimal = _ZERO
    active_unwind_exact_fee: Decimal = _ZERO
    active_unwind_gross: Decimal = _ZERO
    _episode_fills: int = 0
    _episode_turnover: Decimal = _ZERO
    _episode_fee: Decimal = _ZERO
    _episode_taker_fee: Decimal = _ZERO
    _episode_gross: Decimal = _ZERO
    _episode_entry_turnover: Decimal = _ZERO
    _episode_entry_quantity: Decimal = _ZERO
    _episode_entry_fee: Decimal = _ZERO
    _episode_exit_turnover: Decimal = _ZERO
    _episode_exit_quantity: Decimal = _ZERO
    _episode_entry_side: str | None = None
    _episode_opened_at: int | None = None
    _episode_active_unwind_used: bool = False
    _episode_active_attempt_order_ids: set[str] = field(
        default_factory=set, repr=False
    )
    _episode_vwap_complete: bool = True
    _episode_close_policy_coverage: bool = True
    _episode_final_exit_stage: str | None = None
    _episode_final_binding_constraint: str | None = None
    _episode_entered_inventory_hold: bool | None = None
    _episode_max_unlocked_loss: Decimal = _ZERO
    _episode_passive_exit_net: Decimal = _ZERO
    _episode_passive_exit_quantity: Decimal = _ZERO
    _episode_surplus_funded_passive_net: Decimal = _ZERO
    _episode_surplus_funded_passive_quantity: Decimal = _ZERO
    _episode_active_attempt_count: int = 0
    _episode_policy_observation_seen: bool = False
    _episode_inventory_episode_id: int | None = None
    _episode_sequence: int = 0
    policy_context_missing_count: int = 0
    _pending_economic_stop_reason: str | None = None
    current_equity: Decimal | None = None
    last_flat_equity_change: Decimal = _ZERO
    last_flat_completed_fills: int = 0
    economic_state: str = "collecting"
    economic_reason: str | None = None

    def _project_seen_trade_registry(
        self,
        page_trade_ids: set[str],
        new_fills: list[_Fill],
    ) -> tuple[set[str], dict[str, None], int]:
        projected = set(self.seen_trade_ids)
        order = {
            trade_id: None
            for trade_id in self._seen_trade_id_order
            if trade_id in projected
        }
        for trade_id in sorted(projected - set(order)):
            order[trade_id] = None
        for trade_id in page_trade_ids:
            if trade_id in projected:
                order.pop(trade_id, None)
                order[trade_id] = None

        evictions = 0
        while len(projected) + len(new_fills) > _SEEN_TRADE_ID_LIMIT:
            candidate = next(
                (
                    trade_id
                    for trade_id in order
                    if trade_id not in page_trade_ids
                ),
                None,
            )
            if candidate is None:
                raise AccountAuditError(
                    "account trade identity registry exhausted"
                )
            order.pop(candidate)
            projected.discard(candidate)
            evictions += 1
        for fill in new_fills:
            projected.add(fill.trade_id)
            order.pop(fill.trade_id, None)
            order[fill.trade_id] = None
        return projected, order, evictions

    def seed(self, trades: Iterable[Any]) -> None:
        trade_ids: list[str] = []
        sort_keys: list[tuple[int, int]] = []
        for trade in trades:
            trade_id = str(getattr(trade, "id", "") or "")
            if not trade_id:
                raise AccountAuditError("baseline account trade is missing an id")
            trade_ids.append(trade_id)
            try:
                sort_keys.append(_trade_sort_key(trade))
            except AccountAuditError:
                # Legacy baseline records are identity-only. They remain replay
                # protected by seen_trade_ids; ordering starts at the first
                # authenticated runtime fill with a valid sort key.
                continue
        unique_trade_ids = set(trade_ids)
        if len(unique_trade_ids) != len(trade_ids):
            raise AccountAuditError("baseline account trades contain duplicate ids")
        if (
            len(self.seen_trade_ids | unique_trade_ids)
            > _SEEN_TRADE_ID_LIMIT
        ):
            raise AccountAuditError("account trade identity registry exhausted")
        self.seen_trade_ids.update(unique_trade_ids)
        for trade_id in trade_ids:
            self._seen_trade_id_order.pop(trade_id, None)
            self._seen_trade_id_order[trade_id] = None
        if sort_keys:
            latest_sort_key = max(sort_keys)
            if (
                self._last_applied_fill_sort_key is None
                or latest_sort_key > self._last_applied_fill_sort_key
            ):
                self._last_applied_fill_sort_key = latest_sort_key

    def apply(
        self,
        trades: Iterable[Any],
        *,
        current_position: Decimal,
        current_equity: Decimal,
        managed_order_ids: set[str],
        active_unwind_order_ids: set[str] | frozenset[str] = frozenset(),
        open_order_ids: set[str] | frozenset[str] = frozenset(),
        terminal_order_ids: set[str] | frozenset[str] = frozenset(),
        order_intent_contexts: Mapping[str, OrderIntentMetadata] | None = None,
        episode_policy_observation: EpisodePolicyObservation | None = None,
        allow_unreported_zero_integrator_fee: bool = False,
    ) -> None:
        trades = tuple(trades)
        if order_intent_contexts is None:
            order_intent_contexts = {}
        if episode_policy_observation is not None:
            if not isinstance(
                episode_policy_observation, EpisodePolicyObservation
            ):
                raise AccountAuditError(
                    "account episode policy observation is invalid"
                )
            observation_sequence = (
                episode_policy_observation.authenticated_episode_sequence
            )
            if type(observation_sequence) is not int or observation_sequence <= 0:
                raise AccountAuditError(
                    "account episode policy observation sequence is invalid"
                )
            if type(episode_policy_observation.entered_inventory_hold) is not bool:
                raise AccountAuditError(
                    "account episode policy hold observation is invalid"
                )
            if (
                type(episode_policy_observation.active_attempts) is not int
                or episode_policy_observation.active_attempts < 0
            ):
                raise AccountAuditError(
                    "account episode policy active attempts are invalid"
                )
            observed_unlocked = (
                episode_policy_observation.max_unlocked_episode_loss
            )
            if (
                not isinstance(observed_unlocked, Decimal)
                or not observed_unlocked.is_finite()
                or observed_unlocked < 0
            ):
                raise AccountAuditError(
                    "account episode policy unlocked loss is invalid"
                )
            if (
                self.ledger_position != 0
                and self._episode_sequence > 0
                and observation_sequence != self._episode_sequence
            ):
                raise AccountAuditError(
                    "account episode policy observation conflicts with open episode"
                )
        active_attempt_contexts: dict[int, set[str]] = {}
        active_attempt_counts: dict[int, int] = {}
        inventory_hold_contexts: set[int] = set()
        max_unlocked_contexts: dict[int, Decimal] = {}
        for order_id, intent_context in order_intent_contexts.items():
            if not isinstance(intent_context, OrderIntentMetadata):
                continue
            context_sequence = intent_context.authenticated_episode_sequence
            if type(context_sequence) is not int:
                continue
            if (
                type(intent_context.active_attempts) is not int
                or intent_context.active_attempts < 0
            ):
                raise AccountAuditError(
                    "account order intent active_attempts is invalid"
                )
            if type(intent_context.entered_inventory_hold) is not bool:
                raise AccountAuditError(
                    "account order intent entered_inventory_hold is invalid"
                )
            context_attempts = intent_context.active_attempts + int(
                intent_context.kind is OrderIntentKind.ACTIVE_EXIT
            )
            active_attempt_counts[context_sequence] = max(
                active_attempt_counts.get(context_sequence, 0),
                context_attempts,
            )
            unlocked = intent_context.unlocked_episode_loss
            if unlocked is not None:
                if (
                    not isinstance(unlocked, Decimal)
                    or not unlocked.is_finite()
                    or unlocked < 0
                ):
                    raise AccountAuditError(
                        "account order intent unlocked_episode_loss is invalid"
                    )
                max_unlocked_contexts[context_sequence] = max(
                    max_unlocked_contexts.get(context_sequence, _ZERO),
                    unlocked,
                )
            if intent_context.kind is OrderIntentKind.ACTIVE_EXIT:
                active_attempt_contexts.setdefault(
                    context_sequence, set()
                ).add(order_id)
            if intent_context.entered_inventory_hold or (
                intent_context.exit_stage is not None
                and intent_context.exit_stage.value == "inventory_hold"
            ):
                inventory_hold_contexts.add(context_sequence)
        page_trade_ids = {
            str(getattr(trade, "id", "") or "") for trade in trades
        }
        page_order_ids = {
            str(getattr(trade, "order_id", "") or "") for trade in trades
        }
        new_fills = sorted(
            (
                _fill_from_trade(
                    trade,
                    self.config.maker_fee_rate,
                    taker_fee_rate=self.config.taker_fee_rate,
                    active_unwind_order_ids=active_unwind_order_ids,
                    allow_unreported_zero_integrator_fee=(
                        allow_unreported_zero_integrator_fee
                    ),
                )
                for trade in trades
                if str(getattr(trade, "id", "") or "")
                not in self.seen_trade_ids
            ),
            key=lambda fill: fill.sort_key,
        )
        if len(new_fills) >= 100:
            raise AccountAuditError("account trade audit window exhausted")
        new_trade_ids = {fill.trade_id for fill in new_fills}
        if len(new_trade_ids) != len(new_fills):
            raise AccountAuditError("account trade response contains duplicate ids")
        previous_sort_key = self._last_applied_fill_sort_key
        for fill in new_fills:
            if previous_sort_key is not None and fill.sort_key <= previous_sort_key:
                raise AccountAuditError(
                    "account trade arrived out of authenticated ledger order"
                )
            previous_sort_key = fill.sort_key
        attributable_order_ids = (
            managed_order_ids
            | set(active_unwind_order_ids)
            | set(self._order_role_bindings)
        )
        if any(
            fill.order_id not in attributable_order_ids for fill in new_fills
        ):
            raise AccountAuditError("account trade is not attributable to this runtime")
        expected_position = self.ledger_position + sum(
            (fill.signed_amount for fill in new_fills), _ZERO
        )
        if expected_position != current_position:
            raise _SnapshotMismatch(
                "account trades and position are from inconsistent snapshots"
            )

        projected_seen_ids, projected_seen_order, trade_id_evictions = (
            self._project_seen_trade_registry(page_trade_ids, new_fills)
        )

        projected_position = self.ledger_position
        episode_sequence = self._episode_sequence
        known_orders = dict(self._order_role_bindings)
        known_intents = dict(self._order_intent_bindings)
        episode_inventory_id = self._episode_inventory_episode_id
        episode_order_ids = set(self._episode_order_ids)
        pinned_order_ids = (
            page_order_ids | set(open_order_ids) | episode_order_ids
        )
        role_binding_evictions = 0
        classified_fills: list[
            tuple[_Fill, dict[str, Any], OrderIntentMetadata | None, bool]
        ] = []
        for fill in new_fills:
            prior_position = projected_position
            next_position = prior_position + fill.signed_amount
            position_flip = prior_position * next_position < 0
            if prior_position == 0:
                episode_inventory_id = None
            if fill.active_unwind:
                if prior_position == 0:
                    raise AccountAuditError(
                        "active unwind requires nonzero inventory"
                    )
                if prior_position * fill.signed_amount >= 0:
                    raise AccountAuditError(
                        "active unwind direction does not reduce inventory"
                    )
                if position_flip:
                    raise AccountAuditError(
                        "active unwind must not flip inventory"
                    )
                if abs(next_position) >= abs(prior_position):
                    raise AccountAuditError(
                        "active unwind must strictly reduce inventory"
                    )

            known = known_orders.pop(fill.order_id, None)
            if known is not None:
                known_side, role, fill_episode_sequence, known_active = known
                if prior_position == 0 or (
                    episode_sequence > 0
                    and fill_episode_sequence != episode_sequence
                ):
                    raise AccountAuditError(
                        "account order id was reused across inventory episodes"
                    )
                if known_side != fill.side.value or known_active != fill.active_unwind:
                    raise AccountAuditError(
                        "account order attribution changed across partial fills"
                    )
                known_orders[fill.order_id] = known
            else:
                while len(known_orders) >= _ORDER_ROLE_BINDING_LIMIT:
                    candidate = next(
                        (
                            order_id
                            for order_id in known_orders
                            if order_id in terminal_order_ids
                            and order_id not in pinned_order_ids
                        ),
                        None,
                    )
                    if candidate is None:
                        raise AccountAuditError(
                            "account order attribution registry exhausted"
                        )
                    known_orders.pop(candidate)
                    known_intents.pop(candidate, None)
                    role_binding_evictions += 1
                if prior_position == 0:
                    episode_sequence += 1
                    role = "entry"
                else:
                    if episode_sequence == 0:
                        episode_sequence = 1
                    if position_flip or prior_position * fill.signed_amount > 0:
                        role = "risk_increasing"
                    else:
                        role = (
                            "active_exit"
                            if fill.active_unwind
                            else "passive_exit"
                        )
                fill_episode_sequence = episode_sequence
                known_orders[fill.order_id] = (
                    fill.side.value,
                    role,
                    fill_episode_sequence,
                    fill.active_unwind,
                )

            provided_intent = None
            if fill.order_id in order_intent_contexts:
                provided_intent = order_intent_contexts[fill.order_id]
                if not isinstance(provided_intent, OrderIntentMetadata):
                    raise AccountAuditError(
                        "account order intent context is invalid"
                    )
            bound_intent = known_intents.get(fill.order_id)
            if (
                bound_intent is not None
                and provided_intent is not None
                and bound_intent != provided_intent
            ):
                raise AccountAuditError(
                    "account order intent changed across partial fills"
                )
            intent = bound_intent or provided_intent
            if intent is not None:
                known_intents[fill.order_id] = intent
                expected_intents = (
                    {
                        OrderIntentKind.BASE_ENTRY,
                        OrderIntentKind.CONTROLLER_ENTRY,
                    }
                    if role in {"entry", "risk_increasing"}
                    else {
                        OrderIntentKind.ACTIVE_EXIT
                        if role == "active_exit"
                        else OrderIntentKind.PASSIVE_EXIT
                    }
                )
                if intent.kind not in expected_intents:
                    raise AccountAuditError(
                        "account order intent conflicts with authenticated fill role"
                    )
                if fill.active_unwind != (
                    intent.kind is OrderIntentKind.ACTIVE_EXIT
                ):
                    raise AccountAuditError(
                        "account order intent conflicts with active unwind lane"
                    )
                authenticated_sequence = (
                    intent.authenticated_episode_sequence
                )
                if authenticated_sequence is not None and (
                    type(authenticated_sequence) is not int
                    or authenticated_sequence != fill_episode_sequence
                ):
                    raise AccountAuditError(
                        "account order intent conflicts with authenticated episode"
                    )
                inventory_episode_id = intent.inventory_episode_id
                if inventory_episode_id is not None:
                    if type(inventory_episode_id) is not int:
                        raise AccountAuditError(
                            "account order intent inventory episode is invalid"
                        )
                    if (
                        episode_inventory_id is not None
                        and inventory_episode_id != episode_inventory_id
                    ):
                        raise AccountAuditError(
                            "account order intent changed inventory episode"
                        )
                    episode_inventory_id = inventory_episode_id

                for field_name in (
                    "available_completed_surplus",
                    "surplus_reserve",
                    "unlocked_episode_loss",
                    "allowed_passive_loss",
                ):
                    policy_value = getattr(intent, field_name)
                    if policy_value is not None and (
                        not isinstance(policy_value, Decimal)
                        or not policy_value.is_finite()
                        or policy_value < 0
                    ):
                        raise AccountAuditError(
                            f"account order intent {field_name} is invalid"
                        )

            context_complete = intent is not None
            if intent is not None and role in {"passive_exit", "active_exit"}:
                context_complete &= (
                    intent.authenticated_episode_sequence
                    == fill_episode_sequence
                    and intent.inventory_episode_id is not None
                    and intent.policy_decision_id is not None
                    and intent.exit_stage is not None
                    and intent.binding_constraint is not None
                )
            if prior_position == 0:
                episode_order_ids.clear()
            episode_order_ids.add(fill.order_id)
            pinned_order_ids.add(fill.order_id)
            attribution = {
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "side": fill.side.value,
                "role": role,
                "episode_sequence": fill_episode_sequence,
                "prior_position": prior_position,
                "next_position": next_position,
                "exchange_timestamp": fill.sort_key[0],
                "active_unwind": fill.active_unwind,
                "position_flip": position_flip,
                "intent_kind": (
                    intent.kind.value if intent is not None else None
                ),
                "policy_decision_id": (
                    intent.policy_decision_id if intent is not None else None
                ),
                "exit_stage": (
                    intent.exit_stage.value
                    if intent is not None and intent.exit_stage is not None
                    else None
                ),
                "binding_constraint": (
                    intent.binding_constraint.value
                    if intent is not None
                    and intent.binding_constraint is not None
                    else None
                ),
                "controller_decision_id": (
                    intent.controller_decision_id
                    if intent is not None
                    else None
                ),
            }
            classified_fills.append(
                (fill, attribution, intent, context_complete)
            )
            projected_position = next_position
            if next_position == 0:
                episode_order_ids.clear()
                episode_inventory_id = None

        def merge_policy_observation() -> None:
            if episode_policy_observation is None:
                return
            self._episode_entered_inventory_hold = bool(
                self._episode_entered_inventory_hold
                or episode_policy_observation.entered_inventory_hold
            )
            self._episode_active_attempt_count = max(
                self._episode_active_attempt_count,
                episode_policy_observation.active_attempts,
            )
            self._episode_max_unlocked_loss = max(
                self._episode_max_unlocked_loss,
                episode_policy_observation.max_unlocked_episode_loss,
            )
            self._episode_policy_observation_seen = True

        if (
            episode_policy_observation is not None
            and self._episode_sequence
            == episode_policy_observation.authenticated_episode_sequence
        ):
            merge_policy_observation()

        if self._episode_sequence in max_unlocked_contexts:
            self._episode_max_unlocked_loss = max(
                self._episode_max_unlocked_loss,
                max_unlocked_contexts[self._episode_sequence],
            )
        for fill, attribution, intent, context_complete in classified_fills:
            prior_position = attribution["prior_position"]
            next_position = attribution["next_position"]
            if prior_position == 0:
                self._episode_entry_side = fill.side.value
                self._episode_opened_at = fill.sort_key[0]
                self._episode_entry_turnover = _ZERO
                self._episode_entry_quantity = _ZERO
                self._episode_entry_fee = _ZERO
                self._episode_exit_turnover = _ZERO
                self._episode_exit_quantity = _ZERO
                self._episode_active_attempt_order_ids.clear()
                self._episode_vwap_complete = True
                self._episode_close_policy_coverage = True
                self._episode_final_exit_stage = None
                self._episode_final_binding_constraint = None
                self._episode_entered_inventory_hold = None
                self._episode_max_unlocked_loss = _ZERO
                self._episode_passive_exit_net = _ZERO
                self._episode_passive_exit_quantity = _ZERO
                self._episode_surplus_funded_passive_net = _ZERO
                self._episode_surplus_funded_passive_quantity = _ZERO
                self._episode_active_attempt_count = 0
                self._episode_policy_observation_seen = False
            self.ledger_position = next_position
            self._episode_sequence = attribution["episode_sequence"]
            if (
                episode_policy_observation is not None
                and self._episode_sequence
                == episode_policy_observation.authenticated_episode_sequence
            ):
                merge_policy_observation()
            self._episode_active_attempt_order_ids.update(
                active_attempt_contexts.get(self._episode_sequence, ())
            )
            self._episode_active_attempt_count = max(
                self._episode_active_attempt_count,
                active_attempt_counts.get(self._episode_sequence, 0),
            )
            if self._episode_sequence in inventory_hold_contexts:
                self._episode_entered_inventory_hold = True
            if self._episode_sequence in max_unlocked_contexts:
                self._episode_max_unlocked_loss = max(
                    self._episode_max_unlocked_loss,
                    max_unlocked_contexts[self._episode_sequence],
                )
            if intent is None:
                self.policy_context_missing_count += 1
            self._episode_close_policy_coverage &= context_complete
            if attribution["position_flip"]:
                self._episode_vwap_complete = False
            if attribution["role"] in {"entry", "risk_increasing"}:
                self._episode_entry_turnover += fill.turnover
                self._episode_entry_quantity += fill.amount
                self._episode_entry_fee += fill.fee
            else:
                self._episode_exit_turnover += fill.turnover
                self._episode_exit_quantity += fill.amount
                self._episode_final_exit_stage = attribution["exit_stage"]
                self._episode_final_binding_constraint = attribution[
                    "binding_constraint"
                ]
                if attribution["role"] == "passive_exit":
                    passive_net = fill.gross - fill.fee
                    self._episode_passive_exit_net += passive_net
                    self._episode_passive_exit_quantity += fill.amount
                    if (
                        attribution["exit_stage"]
                        == "surplus_funded_passive"
                    ):
                        self._episode_surplus_funded_passive_net += passive_net
                        self._episode_surplus_funded_passive_quantity += (
                            fill.amount
                        )
            if fill.active_unwind:
                self._episode_active_attempt_order_ids.add(fill.order_id)
            if attribution["exit_stage"] == "inventory_hold":
                self._episode_entered_inventory_hold = True
            if (
                intent is not None
                and intent.unlocked_episode_loss is not None
            ):
                self._episode_max_unlocked_loss = max(
                    self._episode_max_unlocked_loss,
                    intent.unlocked_episode_loss,
                )
            self.authenticated_fill_attributions.append(attribution)
            del self.authenticated_fill_attributions[
                :-_AUTHENTICATED_FILL_LEDGER_LIMIT
            ]
            if fill.active_unwind:
                self.unique_taker_fills += 1
                self.active_unwind_turnover += fill.turnover
                self.active_unwind_exact_fee += fill.fee
                self.active_unwind_gross += fill.gross
                self._episode_taker_fee += fill.fee
            else:
                self.unique_maker_fills += 1
                self.maker_turnover += fill.turnover
            self.turnover += fill.turnover
            self.exact_fee += fill.fee
            self.gross += fill.gross
            self._episode_fills += int(not fill.active_unwind)
            self._episode_turnover += fill.turnover
            self._episode_fee += fill.fee
            self._episode_gross += fill.gross
            self._episode_active_unwind_used |= fill.active_unwind
            if next_position == 0:
                vwap_complete = (
                    self._episode_vwap_complete
                    and self._episode_entry_quantity > 0
                    and self._episode_entry_quantity
                    == self._episode_exit_quantity
                )
                entry_vwap = (
                    self._episode_entry_turnover
                    / self._episode_entry_quantity
                    if vwap_complete
                    else None
                )
                exit_vwap = (
                    self._episode_exit_turnover
                    / self._episode_exit_quantity
                    if vwap_complete
                    else None
                )
                quantity = (
                    self._episode_entry_quantity if vwap_complete else None
                )
                inventory_duration_seconds = (
                    _inventory_duration_seconds(
                        self._episode_opened_at, fill.sort_key[0]
                    )
                    if self._episode_opened_at is not None
                    else None
                )
                self.completed_round_trips += 1
                self.completed_fills += self._episode_fills
                self.completed_turnover += self._episode_turnover
                self.completed_exact_fee += self._episode_fee
                self.completed_gross += self._episode_gross
                entry_quantity = self._episode_entry_quantity
                passive_entry_fee = (
                    self._episode_entry_fee
                    * min(
                        _ONE,
                        self._episode_passive_exit_quantity
                        / entry_quantity,
                    )
                    if entry_quantity > 0
                    else _ZERO
                )
                surplus_entry_fee = (
                    self._episode_entry_fee
                    * min(
                        _ONE,
                        self._episode_surplus_funded_passive_quantity
                        / entry_quantity,
                    )
                    if entry_quantity > 0
                    else _ZERO
                )
                passive_loss_used = max(
                    _ZERO,
                    -(self._episode_passive_exit_net - passive_entry_fee),
                )
                policy_coverage = (
                    self._episode_close_policy_coverage
                    and self._episode_policy_observation_seen
                )
                self.completed_episode_ledger.append(
                    {
                        "session_id": self.session_id,
                        "episode_sequence": attribution["episode_sequence"],
                        "opened_at": self._episode_opened_at,
                        "closed_at": fill.sort_key[0],
                        "maker_fills": self._episode_fills,
                        "entry_side": self._episode_entry_side,
                        "turnover": self._episode_turnover,
                        "gross": self._episode_gross,
                        "exact_fee": self._episode_fee,
                        "maker_fee": self._episode_fee - self._episode_taker_fee,
                        "taker_fee": self._episode_taker_fee,
                        "net_ex_funding": self._episode_gross - self._episode_fee,
                        "active_unwind_used": self._episode_active_unwind_used,
                        "close_type": (
                            "active_unwind_flat"
                            if fill.active_unwind
                            else "maker_flat"
                        ),
                        "entry_vwap": entry_vwap,
                        "exit_vwap": exit_vwap,
                        "quantity": quantity,
                        "inventory_duration_seconds": (
                            inventory_duration_seconds
                        ),
                        "final_exit_stage": self._episode_final_exit_stage,
                        "final_binding_constraint": (
                            self._episode_final_binding_constraint
                        ),
                        "surplus_spent": (
                            max(
                                _ZERO,
                                -(
                                    self._episode_surplus_funded_passive_net
                                    - surplus_entry_fee
                                ),
                            )
                            if policy_coverage
                            else None
                        ),
                        "passive_loss_used": (
                            passive_loss_used if policy_coverage else None
                        ),
                        "max_unlocked_episode_loss": (
                            self._episode_max_unlocked_loss
                            if policy_coverage
                            else None
                        ),
                        "entered_inventory_hold": (
                            self._episode_entered_inventory_hold
                        ),
                        "active_attempts": max(
                            self._episode_active_attempt_count,
                            len(self._episode_active_attempt_order_ids),
                        ),
                        "close_policy_coverage": (
                            policy_coverage
                        ),
                    }
                )
                del self.completed_episode_ledger[
                    :-_COMPLETED_EPISODE_LEDGER_LIMIT
                ]
                self.episode_active_unwind_flat += int(fill.active_unwind)
                self._episode_fills = 0
                self._episode_turnover = _ZERO
                self._episode_fee = _ZERO
                self._episode_taker_fee = _ZERO
                self._episode_gross = _ZERO
                self._episode_entry_turnover = _ZERO
                self._episode_entry_quantity = _ZERO
                self._episode_entry_fee = _ZERO
                self._episode_exit_turnover = _ZERO
                self._episode_exit_quantity = _ZERO
                self._episode_entry_side = None
                self._episode_opened_at = None
                self._episode_active_unwind_used = False
                self._episode_active_attempt_order_ids.clear()
                self._episode_vwap_complete = True
                self._episode_close_policy_coverage = True
                self._episode_final_exit_stage = None
                self._episode_final_binding_constraint = None
                self._episode_entered_inventory_hold = None
                self._episode_max_unlocked_loss = _ZERO
                self._episode_passive_exit_net = _ZERO
                self._episode_passive_exit_quantity = _ZERO
                self._episode_surplus_funded_passive_net = _ZERO
                self._episode_surplus_funded_passive_quantity = _ZERO
                self._episode_active_attempt_count = 0
                self._episode_policy_observation_seen = False

        self.seen_trade_ids = projected_seen_ids
        self._seen_trade_id_order = projected_seen_order
        self.seen_trade_id_evictions += trade_id_evictions
        self._order_role_bindings = known_orders
        self._order_intent_bindings = known_intents
        self._episode_inventory_episode_id = episode_inventory_id
        self._episode_order_ids = episode_order_ids
        self.order_role_binding_evictions += role_binding_evictions
        if new_fills:
            self._last_applied_fill_sort_key = new_fills[-1].sort_key

        self.current_equity = current_equity
        if self.ledger_position == 0:
            self.last_flat_equity_change = (
                current_equity - self.baseline_equity
            )
            self.last_flat_completed_fills = self.completed_fills
        self._evaluate()

    def _evaluate(self) -> None:
        loss_budget = (
            self.config.max_session_loss_for_unwind
            if self.config.active_unwind_enabled
            else self.config.max_session_loss_for_maker_exit
        )
        if self._pending_economic_stop_reason is not None:
            if self.ledger_position != 0:
                self.economic_state = "economic_stop_pending_flat"
                self.economic_reason = (
                    f"{self._pending_economic_stop_reason}; "
                    "waiting for authenticated flat"
                )
                return
            self.economic_state = "no_go"
            self.economic_reason = self._pending_economic_stop_reason
            raise AccountAuditError(self.economic_reason)

        session_loss = self.session_loss_for_maker_exit
        if loss_budget > 0 and session_loss > loss_budget:
            failure_reason = "bounded maker-exit session loss exceeded"
            if not self.config.dry_run and self.ledger_position != 0:
                self._pending_economic_stop_reason = failure_reason
                self.economic_state = "economic_stop_pending_flat"
                self.economic_reason = (
                    f"{failure_reason}; waiting for authenticated flat"
                )
                return
            self.economic_state = "no_go"
            self.economic_reason = failure_reason
            raise AccountAuditError(failure_reason)

        if self.completed_fills < self.config.economic_min_fills:
            if self.ledger_position != 0:
                self.economic_state = "incomplete_nonflat"
                self.economic_reason = "inventory has not returned to flat"
            else:
                self.economic_state = "collecting"
                self.economic_reason = (
                    f"need {self.config.economic_min_fills} completed maker fills"
                )
            return
        net_bps = self.completed_net_turnover_bps
        failure_reason: str | None = None
        if self.completed_gross < self.completed_exact_fee:
            failure_reason = "completed gross does not cover exact fees"
        elif (
            net_bps is None
            or net_bps < self.config.min_completed_net_turnover_bps
        ):
            failure_reason = (
                "completed net/turnover is below the configured threshold"
            )
        elif self.ledger_position != 0:
            self.economic_state = "fee_gate_pass_equity_pending_flat"
            self.economic_reason = (
                "completed fee/net gate passed; waiting for flat equity reconciliation"
            )
            return
        elif (
            self.flat_equity_turnover_bps is None
            or self.flat_equity_turnover_bps
            < self.config.min_completed_net_turnover_bps
        ):
            failure_reason = (
                "flat account-value change/turnover is below the configured threshold"
            )
        if failure_reason is not None:
            if not self.config.dry_run:
                if self.ledger_position != 0:
                    self._pending_economic_stop_reason = failure_reason
                    self.economic_state = "economic_stop_pending_flat"
                    self.economic_reason = (
                        f"{failure_reason}; waiting for authenticated flat"
                    )
                    return
                self.economic_state = "no_go"
                self.economic_reason = failure_reason
                raise AccountAuditError(failure_reason)
            if loss_budget > 0:
                self.economic_state = "bounded_economic_recovery"
                self.economic_reason = (
                    f"{failure_reason}; maker-exit session loss remains bounded"
                )
                return
            self.economic_state = "no_go"
            self.economic_reason = failure_reason
            raise AccountAuditError(failure_reason)

        self.economic_state = "fee_and_equity_gate_go"
        self.economic_reason = None

    @property
    def completed_net(self) -> Decimal:
        return self.completed_gross - self.completed_exact_fee

    @property
    def completed_net_turnover_bps(self) -> Decimal | None:
        if self.completed_turnover <= 0:
            return None
        return self.completed_net / self.completed_turnover * _TEN_THOUSAND

    @property
    def flat_equity_turnover_bps(self) -> Decimal | None:
        if (
            self.current_equity is None
            or self.ledger_position != 0
            or self.completed_turnover <= 0
        ):
            return None
        return (
            (self.current_equity - self.baseline_equity)
            / self.completed_turnover
            * _TEN_THOUSAND
        )

    @property
    def session_loss_for_maker_exit(self) -> Decimal:
        return max(
            _ZERO,
            -self.completed_net,
            -self.last_flat_equity_change,
        )

    def snapshot(self) -> dict[str, Any]:
        fee_cover_ratio = (
            self.completed_gross / self.completed_exact_fee
            if self.completed_exact_fee > 0
            else None
        )
        flat_equity_change = (
            self.current_equity - self.baseline_equity
            if self.current_equity is not None and self.ledger_position == 0
            else None
        )
        return {
            "economic_state": self.economic_state,
            "economic_reason": self.economic_reason,
            "ledger_position": self.ledger_position,
            "baseline_equity": self.baseline_equity,
            "current_equity": self.current_equity,
            "unique_maker_fills": self.unique_maker_fills,
            "unique_taker_fills": self.unique_taker_fills,
            "maker_turnover": self.maker_turnover,
            "turnover": self.turnover,
            "exact_fee": self.exact_fee,
            "gross": self.gross,
            "completed_round_trips": self.completed_round_trips,
            "completed_fills": self.completed_fills,
            "completed_turnover": self.completed_turnover,
            "completed_exact_fee": self.completed_exact_fee,
            "completed_gross": self.completed_gross,
            "completed_episode_ledger": [
                dict(episode) for episode in self.completed_episode_ledger
            ],
            "authenticated_fill_attributions": [
                dict(attribution)
                for attribution in self.authenticated_fill_attributions
            ],
            "policy_context_missing_count": self.policy_context_missing_count,
            "episode_sequence": self._episode_sequence,
            "seen_trade_id_registry_size": len(self.seen_trade_ids),
            "seen_trade_id_evictions": self.seen_trade_id_evictions,
            "order_role_binding_registry_size": len(
                self._order_role_bindings
            ),
            "order_role_binding_evictions": (
                self.order_role_binding_evictions
            ),
            "episode_flat_success": self.completed_round_trips,
            "episode_active_unwind_flat": self.episode_active_unwind_flat,
            "active_unwind_turnover": self.active_unwind_turnover,
            "active_unwind_exact_fee": self.active_unwind_exact_fee,
            "active_unwind_gross": self.active_unwind_gross,
            "completed_net_ex_funding": self.completed_net,
            "open_episode_turnover": self._episode_turnover,
            "open_episode_net_ex_funding": (
                self._episode_gross - self._episode_fee
            ),
            "completed_net_turnover_bps": self.completed_net_turnover_bps,
            "flat_equity_turnover_bps": self.flat_equity_turnover_bps,
            "completed_fee_cover_ratio": fee_cover_ratio,
            "flat_equity_change": flat_equity_change,
            "last_flat_equity_change": self.last_flat_equity_change,
            "last_flat_completed_fills": self.last_flat_completed_fills,
            "max_session_loss_for_maker_exit": (
                self.config.max_session_loss_for_maker_exit
            ),
            "session_loss_for_maker_exit": self.session_loss_for_maker_exit,
            "remaining_session_loss_for_maker_exit": (
                max(
                    _ZERO,
                    self.config.max_session_loss_for_maker_exit
                    - self.session_loss_for_maker_exit,
                )
                if self.config.max_session_loss_for_maker_exit > 0
                else None
            ),
            "max_session_loss_for_unwind": (
                self.config.max_session_loss_for_unwind
            ),
            "remaining_session_loss_for_unwind": (
                max(
                    _ZERO,
                    self.config.max_session_loss_for_unwind
                    - self.session_loss_for_maker_exit,
                )
                if self.config.max_session_loss_for_unwind > 0
                else None
            ),
            "unattributed_flat_cashflow": (
                flat_equity_change - self.completed_net
                if flat_equity_change is not None
                else None
            ),
            "min_completed_net_turnover_bps": (
                self.config.min_completed_net_turnover_bps
            ),
        }


class MarketMakerAccountMonitor:
    """Authenticated in-process safety and economics monitor."""

    def __init__(
        self,
        adapter: Any,
        config: MarketMakerConfig,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self.adapter = adapter
        self.config = config
        self._monotonic = monotonic
        self._sleep = sleep
        self.economics: SessionEconomics | None = None
        self.started_monotonic: float | None = None
        self.last_audit_monotonic: float | None = None
        self.total_read_failures = 0
        self.current_drawdown = _ZERO
        self.max_observed_drawdown = _ZERO
        self.audited_position: Decimal | None = None
        self.audited_unrealized_pnl: Decimal | None = None
        self.audited_open_order_count: int | None = None
        self.last_audit_authenticated = False
        self.state = "starting"
        self.reason: str | None = None

    async def initialize(self) -> None:
        try:
            snapshot = await self._read_consistent(set(), baseline=True)
        except AccountAuditError as exc:
            self.mark_hard_stop(str(exc))
            raise
        if self.config.require_flat_start and snapshot["position"] != 0:
            reason = "account audit requires a flat starting position"
            self.mark_hard_stop(reason)
            raise AccountAuditError(reason)
        self.economics = SessionEconomics(
            self.config, baseline_equity=snapshot["equity"]
        )
        self.economics.ledger_position = snapshot["position"]
        self.economics.seed(snapshot["trades"])
        self.economics.current_equity = snapshot["equity"]
        self.audited_position = snapshot["position"]
        self.audited_unrealized_pnl = snapshot["unrealized_pnl"]
        self.audited_open_order_count = len(snapshot["open_order_ids"])
        self.started_monotonic = self._monotonic()
        self.last_audit_monotonic = self.started_monotonic
        self.state = "healthy"

    async def audit(
        self,
        managed_order_ids: set[str],
        *,
        confirm_open_orders: bool = False,
        active_unwind_order_ids: set[str] | frozenset[str] = frozenset(),
        terminal_order_ids: set[str] | frozenset[str] = frozenset(),
        order_intent_contexts: Mapping[str, OrderIntentMetadata] | None = None,
        episode_policy_observation: EpisodePolicyObservation | None = None,
    ) -> None:
        if self.economics is None:
            raise AccountAuditError("account monitor is not initialized")
        snapshot: dict[str, Any] | None = None
        economics_applied = False
        self.last_audit_authenticated = False
        self.audited_open_order_count = None
        try:
            snapshot = await self._read_consistent(
                managed_order_ids,
                baseline=False,
                confirm_open_orders=confirm_open_orders,
                active_unwind_order_ids=active_unwind_order_ids,
            )
            self.economics.apply(
                snapshot["trades"],
                current_position=snapshot["position"],
                current_equity=snapshot["equity"],
                managed_order_ids=managed_order_ids,
                active_unwind_order_ids=active_unwind_order_ids,
                open_order_ids=snapshot["open_order_ids"],
                terminal_order_ids=terminal_order_ids,
                order_intent_contexts=order_intent_contexts,
                episode_policy_observation=episode_policy_observation,
                allow_unreported_zero_integrator_fee=(
                    _has_zero_integrator_fee_signing_proof(self.adapter)
                ),
            )
            economics_applied = True
            self._update_drawdown(snapshot["equity"])
        except AccountAuditError as exc:
            if (
                snapshot is not None
                and self.economics.ledger_position == snapshot["position"]
                and (
                    economics_applied
                    or self.economics.economic_state == "no_go"
                )
            ):
                self.audited_position = snapshot["position"]
                self.audited_unrealized_pnl = snapshot["unrealized_pnl"]
                self.audited_open_order_count = len(
                    snapshot["open_order_ids"]
                )
                self.last_audit_monotonic = self._monotonic()
                self.last_audit_authenticated = True
                if not economics_applied:
                    try:
                        self._update_drawdown(snapshot["equity"])
                    except AccountAuditError:
                        pass
            self.mark_hard_stop(str(exc))
            raise
        self.audited_position = snapshot["position"]
        self.audited_unrealized_pnl = snapshot["unrealized_pnl"]
        self.audited_open_order_count = len(snapshot["open_order_ids"])
        self.last_audit_monotonic = self._monotonic()
        self.last_audit_authenticated = True
        self.state = "healthy"
        self.reason = None

    def mark_hard_stop(self, reason: str) -> None:
        self.state = "hard_stop"
        self.reason = reason

    async def _read_consistent(
        self,
        managed_order_ids: set[str],
        *,
        baseline: bool,
        confirm_open_orders: bool = False,
        active_unwind_order_ids: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        last_error: BaseException | None = None
        for attempt in range(_READ_ATTEMPTS):
            try:
                orders = list(await self.adapter.get_open_orders())
                self._validate_orders(orders, managed_order_ids)
                trades = list(
                    await self.adapter.get_account_trades(
                        self.config.symbol, limit=100
                    )
                )
                positions = list(await self.adapter.get_positions())
                position, unrealized_pnl = _position_state(
                    positions, self.config.symbol
                )
                if abs(position) > self.config.max_position:
                    raise AccountAuditError("position exceeds max_position")
                balances = list(await self.adapter.get_balances())
                collateral = _collateral_total(balances)
                equity = collateral + unrealized_pnl
                confirmed_trades = list(
                    await self.adapter.get_account_trades(
                        self.config.symbol, limit=100
                    )
                )
                if _trade_ids(trades) != _trade_ids(confirmed_trades):
                    raise _SnapshotMismatch(
                        "account trades changed during the audit read"
                    )
                trades = confirmed_trades
                if confirm_open_orders:
                    confirmed_orders = list(
                        await self.adapter.get_open_orders()
                    )
                    self._validate_orders(
                        confirmed_orders, managed_order_ids
                    )
                    if _order_ids(orders) != _order_ids(confirmed_orders):
                        raise _SnapshotMismatch(
                            "account open orders changed during the audit read"
                        )
                    orders = confirmed_orders
                if not baseline and self.economics is not None:
                    fills = [
                        _fill_from_trade(
                            trade,
                            self.config.maker_fee_rate,
                            taker_fee_rate=self.config.taker_fee_rate,
                            active_unwind_order_ids=active_unwind_order_ids,
                            allow_unreported_zero_integrator_fee=(
                                _has_zero_integrator_fee_signing_proof(
                                    self.adapter
                                )
                            ),
                        )
                        for trade in trades
                        if str(getattr(trade, "id", "") or "")
                        not in self.economics.seen_trade_ids
                    ]
                    if len(fills) >= 100:
                        raise AccountAuditError(
                            "account trade audit window exhausted"
                        )
                    expected = self.economics.ledger_position + sum(
                        (fill.signed_amount for fill in fills), _ZERO
                    )
                    if expected != position:
                        raise _SnapshotMismatch(
                            "account trades and position are inconsistent"
                        )
                return {
                    "equity": equity,
                    "collateral": collateral,
                    "position": position,
                    "unrealized_pnl": unrealized_pnl,
                    "trades": trades,
                    "open_order_ids": {
                        str(getattr(order, "id", "") or "")
                        for order in orders
                    },
                }
            except AccountAuditError:
                raise
            except Exception as exc:
                last_error = exc
                self.total_read_failures += 1
                if attempt + 1 < _READ_ATTEMPTS:
                    await self._sleep(_READ_RETRY_SECONDS)
        self.state = "hard_stop"
        detail = (
            f"{type(last_error).__name__}: {last_error}"
            if last_error is not None
            else "unknown read failure"
        )
        self.reason = (
            "account state remained untrusted after bounded retries: " + detail
        )
        raise AccountAuditError(self.reason) from last_error

    def _validate_orders(
        self, orders: Iterable[Any], managed_order_ids: set[str]
    ) -> None:
        for order in orders:
            symbol = getattr(order, "symbol", None)
            if symbol != self.config.symbol:
                raise AccountAuditError(f"non-{self.config.symbol} open order detected")
            if str(getattr(order, "id", "") or "") not in managed_order_ids:
                raise AccountAuditError("unmanaged open order detected")

    def _update_drawdown(self, equity: Decimal) -> None:
        if self.economics is None:
            return
        self.current_drawdown = max(
            _ZERO, self.economics.baseline_equity - equity
        )
        self.max_observed_drawdown = max(
            self.max_observed_drawdown, self.current_drawdown
        )
        if (
            self.config.max_session_drawdown > 0
            and self.current_drawdown >= self.config.max_session_drawdown
        ):
            self.mark_hard_stop("max_session_drawdown reached")
            raise AccountAuditError(self.reason)

    def snapshot(self, now: float) -> dict[str, Any]:
        economics = self.economics.snapshot() if self.economics else {}
        session_age = (
            max(0.0, now - self.started_monotonic)
            if self.started_monotonic is not None
            else None
        )
        hours = (
            Decimal(str(session_age)) / Decimal("3600")
            if session_age is not None and session_age > 0
            else None
        )
        turnover = economics.get("maker_turnover")
        fills = economics.get("unique_maker_fills")
        return {
            "state": self.state,
            "reason": self.reason,
            "session_age_seconds": session_age,
            "age_seconds": (
                max(0.0, now - self.last_audit_monotonic)
                if self.last_audit_monotonic is not None
                else None
            ),
            "total_read_failures": self.total_read_failures,
            "current_drawdown": self.current_drawdown,
            "max_observed_drawdown": self.max_observed_drawdown,
            "audited_position": self.audited_position,
            "audited_unrealized_pnl": self.audited_unrealized_pnl,
            "audited_open_order_count": self.audited_open_order_count,
            "last_audit_authenticated": self.last_audit_authenticated,
            "maker_turnover_per_wall_hour": (
                turnover / hours
                if hours is not None and isinstance(turnover, Decimal)
                else None
            ),
            "maker_fills_per_wall_hour": (
                Decimal(fills) / hours
                if hours is not None and type(fills) is int
                else None
            ),
            **economics,
        }

"""Order health checker with order sync, gap repair, and position validation."""

from __future__ import annotations

import logging
import asyncio
import time
import traceback
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ....adapters.exchanges import OrderSide as ExchangeOrderSide
from ....adapters.exchanges import OrderType, PositionSide
from ....adapters.exchanges.models import OrderData, PositionData
from ....logging import get_logger
from ..models import GridConfig, GridOrder, GridOrderSide, GridOrderStatus, GridType


@dataclass
class PositionHealthResult:
    expected_position: Decimal
    actual_position: Decimal
    tolerance: Decimal
    needs_adjustment: bool


class OrderHealthChecker:
    """Keep exchange orders, local cache, and exposure aligned."""

    RECENT_FILL_COOLDOWN_SECONDS = 15
    TP_ONLY_MISMATCH_WARNING_CYCLES = 4
    TP_ONLY_MISMATCH_WARNING_SECONDS = 60.0

    def __init__(self, config: GridConfig, engine, reserve_manager=None):
        self.config = config
        self.engine = engine
        self.reserve_manager = reserve_manager
        self.logger = get_logger(__name__)
        self._missing_order_seen_at: Dict[str, float] = {}
        self._missing_grid_seen_at: Dict[Tuple[str, str], float] = {}
        self._missing_order_resolution_timeout = float(
            getattr(self.engine, "_missing_order_resolution_timeout", 20.0)
        )
        self._last_unresolved_order_price_keys: set[Tuple[str, str]] = set()
        self._restored_missing_orders_in_sync = 0
        self._runtime_consistency_log_key: Optional[Tuple[str, str]] = None
        self._runtime_consistency_log_time = 0.0
        self._health_summary_log_key: Optional[str] = None
        self._health_summary_log_time = 0.0
        self._position_repair_skip_logged = False
        self._tp_only_mismatch_cycles = 0
        self._tp_only_mismatch_started_at = 0.0

        # Reuse the shared line-limited handler and keep file output at INFO.
        self.logger.logger.setLevel(logging.INFO)
        for handler in self.logger.logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(logging.INFO)

    async def perform_health_check(self) -> bool:
        """Run the full order health check flow."""
        repair_count = 0
        consistency_deferred = False

        try:
            self._restored_missing_orders_in_sync = 0
            repair_block_reason = self._get_health_repair_suspend_reason()
            exchange_orders, positions = await self._fetch_orders_and_positions()

            unresolved_orders = await self._sync_orders_into_engine(
                exchange_orders,
                allow_restore=repair_block_reason is None,
            )
            if repair_block_reason:
                consistency_deferred = True
                self.logger.info(
                    "Skip duplicate cleanup, gap repair, and position repair because "
                    f"health-check repairs are suspended: reason={repair_block_reason}"
                )
            else:
                current_price = await self.engine.get_current_price()
                cleanup_count = await self._cleanup_duplicate_orders(exchange_orders)
                repair_count += cleanup_count
                if cleanup_count:
                    exchange_orders, positions = await self._fetch_orders_and_positions()
                    unresolved_orders = await self._sync_orders_into_engine(exchange_orders)

                if self._restored_missing_orders_in_sync:
                    consistency_deferred = True
                    self.logger.info(
                        "Skip gap repair and position repair because missing orders were "
                        f"already restored in this health-check cycle: restored={self._restored_missing_orders_in_sync}"
                    )
                else:
                    missing_orders = self._build_missing_orders(exchange_orders, current_price)
                    blocked_missing_orders, repairable_missing_orders = (
                        self._split_missing_orders_by_unresolved(missing_orders)
                    )
                    if blocked_missing_orders:
                        consistency_deferred = True
                        self.logger.info(
                            "Defer overlapping gap repairs because some missing orders still "
                            "have unresolved final status: "
                            f"blocked={len(blocked_missing_orders)}, unresolved={unresolved_orders}, "
                            f"keys={self._summarize_order_price_keys(blocked_missing_orders)}"
                        )
                    if repairable_missing_orders:
                        if self._should_delay_gap_repair_for_recent_fill(
                            repairable_missing_orders,
                            cleanup_count,
                        ):
                            self.logger.info(
                                f"Skip gap repair because a fill happened within the last "
                                f"{self.RECENT_FILL_COOLDOWN_SECONDS} seconds"
                            )
                        else:
                            if cleanup_count and self._has_recent_fill():
                                self.logger.info(
                                    "Bypass recent-fill cooldown for gap repair because this "
                                    f"cycle already removed duplicate orders: repaired={len(repairable_missing_orders)}"
                                )
                            elif self._has_persistent_missing_orders(repairable_missing_orders):
                                self.logger.info(
                                    "Bypass recent-fill cooldown for persistent missing grid orders: "
                                    f"repaired={len(repairable_missing_orders)}, "
                                    f"keys={self._summarize_order_price_keys(repairable_missing_orders)}"
                                )

                            placed_count = await self._place_missing_orders(repairable_missing_orders)
                            repair_count += placed_count
                            if placed_count:
                                exchange_orders, positions = await self._fetch_orders_and_positions()
                                unresolved_orders = await self._sync_orders_into_engine(exchange_orders)
                    elif unresolved_orders:
                        consistency_deferred = True
                        self.logger.info(
                            "Skip gap repair and position repair because some missing orders "
                            f"still have unresolved final status: count={unresolved_orders}"
                        )

                    if not unresolved_orders and self._should_repair_position():
                        position_result = self._check_position_health(exchange_orders, positions)
                        if position_result.needs_adjustment:
                            adjusted = await self._adjust_position(position_result)
                            if adjusted:
                                repair_count += 1

            self._log_runtime_consistency(
                exchange_orders=exchange_orders,
                positions=positions,
                deferred=consistency_deferred,
                unresolved_orders=unresolved_orders,
            )

            local_count = len(self.engine.get_pending_orders())
            self._log_health_summary(
                f"Health check complete: exchange_orders={len(exchange_orders)}, "
                f"local_orders={local_count}, repairs={repair_count}"
            )

            self.engine._last_health_repair_count = repair_count
            self.engine._last_health_repair_time = time.time()
            return True
        except Exception as exc:
            self.logger.error(f"Order health check failed: {exc}")
            self.logger.error(traceback.format_exc())
            self.engine._last_health_repair_count = repair_count
            self.engine._last_health_repair_time = time.time()
            return False

    def _get_health_repair_suspend_reason(self) -> Optional[str]:
        """Return the active reason for suspending health-check repairs."""
        getter = getattr(self.engine, "get_health_repair_suspend_reason", None)
        if callable(getter):
            try:
                reason = getter()
                if reason:
                    return reason
            except Exception:
                pass

        reason = getattr(self.engine, "_health_repairs_suspended_reason", None)
        if reason:
            return reason

        if getattr(self.engine, "_shutting_down", False):
            return "engine shutdown in progress"
        if not getattr(self.engine, "_running", True):
            return "engine not running"

        coordinator = getattr(self.engine, "coordinator", None)
        if coordinator is None:
            return None

        if not getattr(coordinator, "_running", True):
            return "coordinator stopped"
        if getattr(coordinator, "_paused", False):
            return "coordinator paused"
        public_pause_state = getattr(coordinator, "is_paused", False)
        if isinstance(public_pause_state, bool) and public_pause_state:
            return "coordinator order operations paused"
        if callable(public_pause_state):
            try:
                if public_pause_state():
                    return "coordinator order operations paused"
            except Exception:
                pass
        if getattr(coordinator, "_resetting", False) or getattr(coordinator, "_is_resetting", False):
            return "coordinator reset in progress"

        return None

    async def _fetch_orders_and_positions(self) -> Tuple[List[OrderData], List[PositionData]]:
        """Fetch open orders and positions from the exchange."""
        orders: List[OrderData] = []
        positions: List[PositionData] = []

        try:
            orders = await self.engine.exchange.get_open_orders(self.config.symbol)
        except Exception as exc:
            self.logger.error(f"Failed to fetch open orders: {exc}")
            self.logger.error(traceback.format_exc())

        try:
            positions = await self.engine.exchange.get_positions([self.config.symbol])
        except Exception as exc:
            self.logger.debug(f"Failed to fetch positions during health check: {exc}")

        return orders, positions

    async def _sync_orders_into_engine(
        self,
        exchange_orders: List[OrderData],
        allow_restore: bool = True,
    ) -> int:
        """Sync remote open orders into the engine pending-order cache."""
        exchange_order_ids = {order.id for order in exchange_orders if getattr(order, "id", None)}
        filled_orders: List[GridOrder] = []
        unresolved_orders = 0
        unresolved_price_keys: set[Tuple[str, str]] = set()

        for grid_order in list(self.engine.get_pending_orders()):
            alias_keys = self._pending_keys_for_order(grid_order)
            if any(alias in exchange_order_ids for alias in alias_keys):
                self._clear_missing_order_tracking(alias_keys)
                continue

            if any(alias in self.engine._expected_cancellations for alias in alias_keys):
                grid_order.mark_cancelled()
                self._consume_expected_cancellations(alias_keys)
                self._clear_missing_order_tracking(alias_keys)
                self._clear_pending_order_refs(grid_order, alias_keys)
                continue

            order_age = max(
                0.0,
                time.time() - grid_order.created_at.timestamp()
            ) if grid_order.created_at else self.engine._exchange_sync_grace_period
            if order_age < self.engine._exchange_sync_grace_period:
                continue

            try:
                exchange_order = await self.engine.exchange.get_order(
                    grid_order.order_id,
                    self.config.symbol,
                )
            except Exception:
                if allow_restore and await self._restore_missing_order_if_timed_out(grid_order, alias_keys):
                    continue
                unresolved_orders += 1
                unresolved_price_keys.add(self._order_price_key(grid_order))
                continue

            status = exchange_order.status.value.lower() if exchange_order.status else "unknown"
            filled_price = exchange_order.average or exchange_order.price or grid_order.price
            filled_amount = self._decimal_or_zero(getattr(exchange_order, "filled", Decimal("0")))
            order_amount = self._get_logical_order_amount(grid_order)
            tolerance = self._get_order_fill_tolerance()

            if self._is_user_fill_snapshot(exchange_order):
                fills = []
                raw_data = getattr(exchange_order, "raw_data", {}) or {}
                if isinstance(raw_data, dict):
                    raw_fills = raw_data.get("fills")
                    if isinstance(raw_fills, list):
                        fills = [fill for fill in raw_fills if isinstance(fill, dict)]

                if filled_amount > 0 or fills:
                    self._record_partial_fill_snapshot(
                        grid_order,
                        filled_price,
                        filled_amount,
                        fills=fills,
                    )
                    effective_fill = self._get_cumulative_fill_amount(grid_order)
                    if order_amount > 0:
                        effective_fill = min(effective_fill, order_amount)
                    if order_amount > 0 and effective_fill < (order_amount - tolerance):
                        self._clear_missing_order_tracking(alias_keys)
                        continue

                    if order_amount > 0 and abs(order_amount - effective_fill) <= tolerance:
                        self._clear_missing_order_tracking(alias_keys)
                        self._note_finalized_order(*alias_keys, grid_order.order_id)
                        grid_order.mark_filled(filled_price, order_amount)
                        filled_orders.append(grid_order)
                        self._clear_pending_order_refs(grid_order, alias_keys)
                        continue

            if status == "filled" and not self._is_inferred_order(exchange_order):
                self._clear_missing_order_tracking(alias_keys)
                finalized_amount = self._get_finalized_fill_amount(
                    grid_order,
                    filled_amount or grid_order.amount,
                )
                self._note_finalized_order(*alias_keys, grid_order.order_id)
                grid_order.mark_filled(filled_price, finalized_amount)
                filled_orders.append(grid_order)
                self._clear_pending_order_refs(grid_order, alias_keys)
            elif status in {"canceled", "cancelled", "rejected", "expired"}:
                self._clear_missing_order_tracking(alias_keys)
                self._note_finalized_order(*alias_keys, grid_order.order_id)
                grid_order.mark_cancelled()
                self._clear_pending_order_refs(grid_order, alias_keys)
            else:
                if allow_restore and await self._restore_missing_order_if_timed_out(grid_order, alias_keys):
                    continue
                unresolved_orders += 1
                unresolved_price_keys.add(self._order_price_key(grid_order))

        for grid_order in filled_orders:
            for callback in self.engine._order_callbacks:
                if asyncio.iscoroutinefunction(callback):
                    await callback(grid_order)
                else:
                    result = callback(grid_order)
                    if asyncio.iscoroutine(result):
                        await result

        for ex_order in exchange_orders:
            order_id = getattr(ex_order, "id", None)
            if not order_id or order_id in self.engine._pending_orders:
                continue
            if self._was_recently_finalized_order(order_id):
                continue

            try:
                mirrored_order = self._build_grid_order_from_exchange_order(ex_order)
                if mirrored_order is None:
                    continue
                self._register_engine_pending_order(mirrored_order, order_id, getattr(ex_order, "client_id", None))
            except Exception as exc:
                self.logger.warning(f"Failed to mirror exchange order into local cache: {exc}")

        self._last_unresolved_order_price_keys = unresolved_price_keys
        return unresolved_orders

    async def _cleanup_duplicate_orders(self, exchange_orders: List[OrderData]) -> int:
        """Cancel duplicate orders that map to the same grid and side."""
        duplicates = self._find_duplicate_orders(exchange_orders)
        if not duplicates:
            return 0

        if self._has_recent_fill():
            self.logger.info(
                f"Skip duplicate cleanup because a fill happened within the last "
                f"{self.RECENT_FILL_COOLDOWN_SECONDS} seconds"
            )
            return 0

        cleaned = 0
        for order in duplicates:
            order_id = getattr(order, "id", None)
            if not order_id:
                continue

            try:
                if await self.engine.cancel_order(order_id):
                    cleaned += 1
                    self.logger.info(
                        f"Canceled duplicate order: order_id={order_id}, "
                        f"price={order.price}, side={order.side.value}"
                    )
            except Exception as exc:
                self.logger.warning(f"Failed to cancel duplicate order {order_id}: {exc}")

        return cleaned

    def _find_duplicate_orders(self, exchange_orders: List[OrderData]) -> List[OrderData]:
        """Return duplicate orders after grouping by exact price and side."""
        grouped: Dict[Tuple[str, str], List[OrderData]] = {}
        local_order_ids_by_key = self._get_local_pending_order_ids_by_price_key()
        duplicates: List[OrderData] = []

        for order in exchange_orders:
            if getattr(order, "price", None) is None:
                continue

            key = (
                self._normalize_price_key(order.price),
                order.side.value.lower(),
            )
            grouped.setdefault(key, []).append(order)

        for key, orders in grouped.items():
            local_order_ids = local_order_ids_by_key.get(key, [])
            exchange_order_ids = {
                str(order_id)
                for order in orders
                for order_id in [getattr(order, "id", None)]
                if order_id
            }
            tracked_exchange_order_ids = [
                order_id for order_id in local_order_ids if order_id in exchange_order_ids
            ]
            if len(orders) <= 1:
                continue

            keep_ids: set[str] = set()
            tracked_id_set = set(tracked_exchange_order_ids)
            for order in orders:
                raw_order_id = getattr(order, "id", None)
                order_id = str(raw_order_id) if raw_order_id else None
                if (
                    order_id
                    and order_id in tracked_id_set
                    and not keep_ids
                ):
                    keep_ids.add(order_id)

            for order in orders:
                raw_order_id = getattr(order, "id", None)
                order_id = str(raw_order_id) if raw_order_id else None
                if order_id and not keep_ids:
                    keep_ids.add(order_id)

            for order in orders:
                raw_order_id = getattr(order, "id", None)
                order_id = str(raw_order_id) if raw_order_id else None
                if order_id and order_id not in keep_ids:
                    duplicates.append(order)

        return duplicates

    def _build_missing_orders(
        self,
        exchange_orders: List[OrderData],
        current_price: Decimal,
    ) -> List[GridOrder]:
        """Build missing orders from tracked orders plus the live base-side ladder."""
        expected_orders = self._get_expected_open_orders()
        existing_keys = {
            (self._normalize_price_key(order.price), order.side.value.lower())
            for order in exchange_orders
            if getattr(order, "price", None) is not None
        }
        base_side = self._base_side_for_grid_type()
        existing_grid_ids = {
            grid_id
            for order in exchange_orders
            if (
                base_side is not None
                and getattr(order, "side", None) is not None
                and order.side.value.lower() == base_side.value.lower()
            )
            for grid_id in [self._grid_id_from_price(order.price)]
            if grid_id is not None
        }
        protected_grid_ids = self._get_local_protected_base_grid_ids()
        protected_grid_ids.update(
            self._get_exchange_protected_base_grid_ids(exchange_orders)
        )

        missing: List[GridOrder] = []
        missing_keys: set[Tuple[str, str]] = set()

        for expected in expected_orders:
            key = (
                self._normalize_price_key(expected.price),
                expected.side.value.lower(),
            )
            if key in existing_keys:
                continue
            if key in missing_keys:
                continue
            repair_amount = self._get_remaining_repair_amount(expected)
            if repair_amount <= self._get_order_fill_tolerance():
                continue
            missing.append(
                GridOrder(
                    order_id=expected.order_id,
                    grid_id=expected.grid_id,
                    side=expected.side,
                    price=expected.price,
                    amount=repair_amount,
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now(),
                    parent_order_id=expected.parent_order_id,
                    exchange_data=self._build_continuation_exchange_data(
                        expected,
                        repair_amount,
                    ),
                )
            )
            missing_keys.add(key)

        supplemental_orders = self._build_missing_base_grid_orders(
            current_price=current_price,
            existing_grid_ids=existing_grid_ids,
            protected_grid_ids=protected_grid_ids,
            existing_keys=existing_keys,
        )
        for expected in supplemental_orders:
            key = (
                self._normalize_price_key(expected.price),
                expected.side.value.lower(),
            )
            if key in missing_keys:
                continue
            missing.append(expected)
            missing_keys.add(key)

        self._update_missing_grid_tracking(missing)

        if not expected_orders and not supplemental_orders:
            self.logger.info(
                "Skip gap repair because there is no tracked or inferred base grid order set to repair"
            )
            return []

        if missing:
            self.logger.info(
                f"Detected missing grid orders: count={len(missing)}, "
                f"current_price={current_price}"
            )

        return missing

    def _build_missing_base_grid_orders(
        self,
        current_price: Decimal,
        existing_grid_ids: set[int],
        protected_grid_ids: set[int],
        existing_keys: set[Tuple[str, str]],
    ) -> List[GridOrder]:
        """Infer missing base-side maker orders from the current price and grid range."""
        base_side = self._base_side_for_grid_type()
        if base_side is None:
            return []

        missing: List[GridOrder] = []
        for grid_id in range(1, self.config.grid_count + 1):
            price = self.config.get_grid_price(grid_id)
            if not self._should_have_base_order(grid_price=price, current_price=current_price):
                continue
            if grid_id in existing_grid_ids:
                continue
            if grid_id in protected_grid_ids:
                continue
            if self._is_grid_locked(grid_id):
                continue

            key = (
                self._normalize_price_key(price),
                base_side.value.lower(),
            )
            if key in existing_keys:
                continue

            missing.append(
                GridOrder(
                    order_id="",
                    grid_id=grid_id,
                    side=base_side,
                    price=price,
                    amount=self.config.get_formatted_grid_order_amount(grid_id),
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now(),
                )
            )

        return missing

    def _get_expected_open_orders(self) -> List[GridOrder]:
        """Return all tracked open orders that should still exist on the exchange."""
        expected_orders: List[GridOrder] = []
        seen_order_ids: set[str] = set()
        seen_price_keys: set[Tuple[str, str]] = set()

        def append_order(order: GridOrder) -> None:
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                return

            order_id = getattr(order, "order_id", None)
            if order_id:
                normalized_id = str(order_id)
                if normalized_id in seen_order_ids:
                    return
                seen_order_ids.add(normalized_id)

            key = self._order_price_key(order)
            if key in seen_price_keys:
                return
            seen_price_keys.add(key)
            expected_orders.append(order)

        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None

        if state and getattr(state, "active_orders", None):
            for order in state.active_orders.values():
                append_order(order)

        for order in self.engine.get_pending_orders():
            append_order(order)

        return expected_orders

    def _get_local_open_order_keys(self) -> set[Tuple[int, str]]:
        """Return tracked local pending orders grouped by grid id and side."""
        keys: set[Tuple[int, str]] = set()

        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if state and getattr(state, "active_orders", None):
            for order in state.active_orders.values():
                if getattr(order, "status", None) != GridOrderStatus.PENDING:
                    continue
                keys.add((order.grid_id, order.side.value.lower()))

        for order in self.engine.get_pending_orders():
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                continue
            keys.add((order.grid_id, order.side.value.lower()))

        return keys

    def _iter_local_pending_orders(self) -> Iterable[GridOrder]:
        """Yield unique pending orders from state and engine."""
        seen_ids: set[str] = set()
        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None

        state_orders = state.active_orders.values() if state and getattr(state, "active_orders", None) else []
        for order in state_orders:
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                continue
            order_id = getattr(order, "order_id", None) or f"object:{id(order)}"
            if order_id in seen_ids:
                continue
            seen_ids.add(order_id)
            yield order

        for order in self.engine.get_pending_orders():
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                continue
            order_id = getattr(order, "order_id", None) or f"object:{id(order)}"
            if order_id in seen_ids:
                continue
            seen_ids.add(order_id)
            yield order

    def _get_local_pending_order_ids_by_price_key(self) -> Dict[Tuple[str, str], List[str]]:
        """Return locally tracked pending order ids grouped by normalized price and side."""
        grouped: Dict[Tuple[str, str], List[str]] = {}
        for order in self._iter_local_pending_orders():
            order_id = getattr(order, "order_id", None)
            if not order_id:
                continue
            normalized_order_id = str(order_id)
            key = self._order_price_key(order)
            grouped.setdefault(key, [])
            if normalized_order_id not in grouped[key]:
                grouped[key].append(normalized_order_id)
        return grouped

    def _get_local_protected_base_grid_ids(self) -> set[int]:
        """Return local TP/reverse grid ids that should block base-side fallback repair."""
        protected: set[int] = set()
        base_side = self._base_side_for_grid_type()
        if base_side is None:
            return protected

        for order in self._iter_local_pending_orders():
            if getattr(order, "parent_order_id", None) or order.side != base_side:
                protected.add(order.grid_id)

        return protected

    def _get_exchange_protected_base_grid_ids(
        self,
        exchange_orders: List[OrderData],
    ) -> set[int]:
        """Infer base grid ids protected by live exchange-side reverse orders."""
        protected: set[int] = set()
        base_side = self._base_side_for_grid_type()
        if base_side is None:
            return protected

        distance = getattr(self.config, "reverse_order_grid_distance", 1) or 1
        try:
            reverse_offset = self.config.grid_interval * Decimal(str(distance))
        except Exception:
            reverse_offset = self.config.grid_interval

        for order in exchange_orders:
            price = getattr(order, "price", None)
            side = getattr(order, "side", None)
            if price is None or side is None:
                continue

            order_side = (
                GridOrderSide.BUY
                if side.value.lower() == "buy"
                else GridOrderSide.SELL
            )
            if order_side == base_side:
                continue

            order_price = Decimal(str(price))
            if base_side == GridOrderSide.BUY:
                source_price = order_price - reverse_offset
            else:
                source_price = order_price + reverse_offset

            grid_id = self._grid_id_from_price(source_price)
            if grid_id is not None:
                protected.add(grid_id)

        return protected

    def _get_state_open_order_keys(self) -> set[Tuple[int, str]]:
        """Return coordinator-state pending orders grouped by grid id and side."""
        keys: set[Tuple[int, str]] = set()
        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if not state or not getattr(state, "active_orders", None):
            return keys

        for order in state.active_orders.values():
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                continue
            keys.add((order.grid_id, order.side.value.lower()))

        return keys

    def _base_side_for_grid_type(self) -> Optional[GridOrderSide]:
        """Return the base maker side used to open each grid level."""
        if self.config.grid_type in {
            GridType.LONG,
            GridType.FOLLOW_LONG,
            GridType.MARTINGALE_LONG,
        }:
            return GridOrderSide.BUY
        if self.config.grid_type in {
            GridType.SHORT,
            GridType.FOLLOW_SHORT,
            GridType.MARTINGALE_SHORT,
        }:
            return GridOrderSide.SELL
        return None

    def _should_have_base_order(self, grid_price: Decimal, current_price: Decimal) -> bool:
        """Return whether a grid slot should currently host a base-side maker order."""
        if self.config.grid_type in {
            GridType.LONG,
            GridType.FOLLOW_LONG,
            GridType.MARTINGALE_LONG,
        }:
            return grid_price < current_price
        if self.config.grid_type in {
            GridType.SHORT,
            GridType.FOLLOW_SHORT,
            GridType.MARTINGALE_SHORT,
        }:
            return grid_price > current_price
        return False

    def _is_grid_locked(self, grid_id: int) -> bool:
        """Return whether coordinator-level logic currently blocks new base orders on this grid."""
        coordinator = getattr(self.engine, "coordinator", None)
        locks = getattr(coordinator, "_grid_level_locks", None) if coordinator else None
        return bool(locks and grid_id in locks)

    def _log_runtime_consistency(
        self,
        exchange_orders: List[OrderData],
        positions: List[PositionData],
        deferred: bool,
        unresolved_orders: int,
    ) -> None:
        """Log whether orders and position sources agree on the current runtime state."""
        exchange_keys = self._get_exchange_open_order_keys(exchange_orders)
        engine_keys = self._get_local_open_order_keys()
        state_keys = self._get_state_open_order_keys()
        exchange_price_keys = self._get_exchange_open_order_price_keys(exchange_orders)
        engine_price_keys = self._get_engine_open_order_price_keys()
        state_price_keys = self._get_state_open_order_price_keys()

        actual_position = self._extract_actual_position(positions)
        tracker_position = self._get_tracker_position()
        state_position = self._get_state_position()
        tolerance = self._position_tolerance()

        if deferred:
            self._reset_tp_only_mismatch_tracking()
            self._log_runtime_consistency_message(
                "info",
                "Runtime consistency check deferred: "
                f"unresolved_orders={unresolved_orders}, restored_missing_orders={self._restored_missing_orders_in_sync}, "
                f"exchange_orders={len(exchange_keys)}, engine_orders={len(engine_keys)}, state_orders={len(state_keys)}, "
                f"exchange_position={actual_position}, tracker_position={tracker_position}, state_position={state_position}",
            )
            return

        structural_issues: List[str] = []
        if exchange_price_keys != engine_price_keys:
            structural_issues.append(
                self._describe_key_diff(
                    label="engine_vs_exchange",
                    left=engine_price_keys,
                    right=exchange_price_keys,
                )
            )
        if exchange_price_keys != state_price_keys:
            structural_issues.append(
                self._describe_key_diff(
                    label="state_vs_exchange",
                    left=state_price_keys,
                    right=exchange_price_keys,
                )
            )
        if abs(tracker_position - actual_position) > tolerance:
            structural_issues.append(
                f"tracker_position={tracker_position} != exchange_position={actual_position}"
            )
        if abs(state_position - actual_position) > tolerance:
            structural_issues.append(
                f"state_position={state_position} != exchange_position={actual_position}"
            )

        tp_open_amount = self._get_open_take_profit_amount(exchange_orders)
        expected_tp_amount = self._get_expected_take_profit_amount(actual_position)
        partial_tp_gap_allowance = self._get_pending_partial_base_fill_exposure()
        tp_gap = abs(tp_open_amount - expected_tp_amount)
        tp_issue: Optional[str] = None
        if tp_gap > (tolerance + partial_tp_gap_allowance):
            tp_issue = (
                f"take_profit_coverage={tp_open_amount} != expected_take_profit={expected_tp_amount} "
                f"(partial_fill_allowance={partial_tp_gap_allowance})"
            )

        if structural_issues:
            self._reset_tp_only_mismatch_tracking()
            if tp_issue:
                structural_issues.append(tp_issue)
            self._log_runtime_consistency_message(
                "warning",
                "Runtime consistency issue detected: " + "; ".join(structural_issues),
            )
            return

        if tp_issue:
            level, prefix = self._classify_tp_only_mismatch_log_level()
            self._log_runtime_consistency_message(
                level,
                prefix
                + f"exchange_orders={len(exchange_keys)}, engine_orders={len(engine_keys)}, "
                f"state_orders={len(state_keys)}, exchange_position={actual_position}, "
                f"tracker_position={tracker_position}, state_position={state_position}; "
                + tp_issue,
            )
            return

        self._reset_tp_only_mismatch_tracking()
        self._log_runtime_consistency_message(
            "info",
            "Runtime consistency verified: "
            f"exchange_orders={len(exchange_keys)}, engine_orders={len(engine_keys)}, state_orders={len(state_keys)}, "
            f"exchange_position={actual_position}, tracker_position={tracker_position}, state_position={state_position}, "
            f"take_profit_coverage={tp_open_amount}, partial_fill_allowance={partial_tp_gap_allowance}",
        )

    def _get_exchange_open_order_keys(self, exchange_orders: List[OrderData]) -> set[Tuple[int, str]]:
        """Return exchange open orders grouped by grid id and side."""
        keys: set[Tuple[int, str]] = set()
        for order in exchange_orders:
            grid_id = self._grid_id_from_price(order.price)
            if grid_id is None:
                continue
            keys.add((grid_id, order.side.value.lower()))
        return keys

    def _get_exchange_open_order_price_keys(self, exchange_orders: List[OrderData]) -> set[Tuple[str, str]]:
        """Return exchange open orders grouped by normalized price and side."""
        keys: set[Tuple[str, str]] = set()
        for order in exchange_orders:
            keys.add((self._normalize_price_key(order.price), order.side.value.lower()))
        return keys

    def _get_engine_open_order_price_keys(self) -> set[Tuple[str, str]]:
        """Return engine pending orders grouped by normalized price and side."""
        keys: set[Tuple[str, str]] = set()
        for order in self.engine.get_pending_orders():
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                continue
            keys.add((self._normalize_price_key(order.price), order.side.value.lower()))
        return keys

    def _get_state_open_order_price_keys(self) -> set[Tuple[str, str]]:
        """Return state pending orders grouped by normalized price and side."""
        keys: set[Tuple[str, str]] = set()
        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if not state or not getattr(state, "active_orders", None):
            return keys

        for order in state.active_orders.values():
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                continue
            keys.add((self._normalize_price_key(order.price), order.side.value.lower()))
        return keys

    def _get_tracker_position(self) -> Decimal:
        """Return the tracker position when available."""
        coordinator = getattr(self.engine, "coordinator", None)
        tracker = getattr(coordinator, "tracker", None) if coordinator else None
        if tracker and hasattr(tracker, "get_current_position"):
            try:
                return Decimal(str(tracker.get_current_position()))
            except Exception:
                return Decimal("0")
        return Decimal("0")

    def _get_state_position(self) -> Decimal:
        """Return the coordinator-state position when available."""
        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if not state:
            return Decimal("0")
        return Decimal(str(getattr(state, "current_position", Decimal("0"))))

    def _get_open_take_profit_amount(self, exchange_orders: List[OrderData]) -> Decimal:
        """Return the total open take-profit amount implied by exchange orders."""
        total = Decimal("0")
        tracked_tp_keys = self._get_tracked_take_profit_price_keys()
        if not tracked_tp_keys:
            return total

        for order in exchange_orders:
            key = (self._normalize_price_key(order.price), order.side.value.lower())
            if key not in tracked_tp_keys:
                continue
            total += Decimal(str(order.amount or 0))
        return total

    def _get_expected_take_profit_amount(self, actual_position: Decimal) -> Decimal:
        """Return the amount that should currently be covered by reverse take-profit orders."""
        if self.config.grid_type in {
            GridType.LONG,
            GridType.FOLLOW_LONG,
            GridType.MARTINGALE_LONG,
        }:
            return max(actual_position, Decimal("0"))
        if self.config.grid_type in {
            GridType.SHORT,
            GridType.FOLLOW_SHORT,
            GridType.MARTINGALE_SHORT,
        }:
            return abs(min(actual_position, Decimal("0")))
        return Decimal("0")

    def _is_user_fill_snapshot(self, exchange_order: OrderData) -> bool:
        """Return whether get_order() only has a user-fill snapshot, not a final order status."""
        params = getattr(exchange_order, "params", {}) or {}
        return bool(params.get("user_fill_snapshot_only"))

    def _get_order_fill_tolerance(self) -> Decimal:
        """Return the quantity tolerance used when comparing cumulative fills to order size."""
        precision = getattr(self.config, "quantity_precision", None)
        if precision is None:
            return Decimal("0.00000001")

        try:
            quantizer = Decimal("0.1") ** int(precision)
        except Exception:
            return Decimal("0.00000001")
        return quantizer / Decimal("2")

    def _record_partial_fill_snapshot(
        self,
        grid_order: GridOrder,
        filled_price: Decimal,
        filled_amount: Decimal,
        fills: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Store a conservative partial-fill snapshot so health checks do not over-repair it."""
        order_amount = self._get_logical_order_amount(grid_order)
        tracking = self._get_tradexyz_fill_tracking(grid_order)
        previous_amount = self._decimal_or_zero(tracking.get("cumulative_filled"))
        recorded_from_snapshot = False
        snapshot_fill_count = 0
        if fills:
            for fill in fills:
                fill_price = self._decimal_or_zero(
                    fill.get("px") or fill.get("avgPx") or fill.get("price")
                )
                fill_size = self._decimal_or_zero(
                    fill.get("sz") or fill.get("filledSz") or fill.get("filled")
                )
                if fill_size <= 0:
                    continue
                event_key = self._build_tradexyz_fill_event_key(
                    fill,
                    grid_order,
                    fill_price if fill_price > 0 else filled_price,
                    fill_size,
                )
                if self._record_tradexyz_fill_entry(
                    tracking,
                    event_key,
                    fill_size,
                    fill_price if fill_price > 0 else filled_price,
                ):
                    recorded_from_snapshot = True
                snapshot_fill_count += 1

        normalized_fill = self._decimal_or_zero(tracking.get("cumulative_filled"))
        carry_amount = self._decimal_or_zero(tracking.get("carry_filled_amount"))
        if order_amount > 0:
            snapshot_fill = min(filled_amount, max(order_amount - carry_amount, Decimal("0")))
        else:
            snapshot_fill = filled_amount
        snapshot_cumulative = carry_amount + snapshot_fill
        if snapshot_cumulative > normalized_fill:
            tracking["snapshot_cumulative_filled"] = str(snapshot_fill)
            tracking["cumulative_filled"] = str(snapshot_cumulative)
            tracking["last_fill_price"] = str(filled_price)
            normalized_fill = snapshot_cumulative
            recorded_from_snapshot = True

        if normalized_fill <= previous_amount:
            return

        tracking["remaining_amount"] = str(max(order_amount - normalized_fill, Decimal("0")))
        self.logger.info(
            "Recorded partial fill snapshot from health-check: "
            f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
            f"cumulative={normalized_fill}, target_amount={order_amount}, "
            f"snapshot_fills={snapshot_fill_count}, ledger_updated={recorded_from_snapshot}, "
            f"order_id={grid_order.order_id}"
        )

    def _get_tradexyz_fill_tracking(self, grid_order: GridOrder) -> Dict[str, Any]:
        """Return mutable TradeXYZ fill tracking shared with the engine."""
        if not isinstance(getattr(grid_order, "exchange_data", None), dict):
            grid_order.exchange_data = {}

        tracking = grid_order.exchange_data.get("tradexyz_fill_tracking")
        if not isinstance(tracking, dict):
            tracking = {}
            grid_order.exchange_data["tradexyz_fill_tracking"] = tracking

        if not isinstance(tracking.get("seen_fill_ids"), list):
            tracking["seen_fill_ids"] = []
        if not isinstance(tracking.get("fills_by_id"), dict):
            tracking["fills_by_id"] = {}
        return tracking

    def _record_tradexyz_fill_entry(
        self,
        tracking: Dict[str, Any],
        event_key: str,
        fill_amount: Decimal,
        filled_price: Decimal,
    ) -> bool:
        """Record one unique fill into the shared TradeXYZ ledger."""
        fills_by_id = tracking.setdefault("fills_by_id", {})
        seen_fill_ids = tracking.setdefault("seen_fill_ids", [])
        if event_key in fills_by_id:
            return False

        fills_by_id[event_key] = {
            "amount": format(fill_amount.normalize(), "f"),
            "price": format(filled_price.normalize(), "f"),
        }
        seen_fill_ids.append(event_key)
        if len(seen_fill_ids) > 200:
            stale_keys = seen_fill_ids[:-200]
            del seen_fill_ids[:-200]
            for stale_key in stale_keys:
                if stale_key not in seen_fill_ids:
                    fills_by_id.pop(stale_key, None)

        snapshot_floor = self._decimal_or_zero(tracking.get("snapshot_cumulative_filled"))
        carry_amount = self._decimal_or_zero(tracking.get("carry_filled_amount"))
        ledger_total = Decimal("0")
        for fill_info in fills_by_id.values():
            ledger_total += self._decimal_or_zero(fill_info.get("amount"))
        current_order_filled = (
            ledger_total
            if ledger_total >= snapshot_floor
            else snapshot_floor + ledger_total
        )
        cumulative_filled = carry_amount + current_order_filled
        tracking["cumulative_filled"] = str(cumulative_filled)
        tracking["last_fill_price"] = str(filled_price)
        return True

    def _get_logical_order_amount(self, grid_order: GridOrder) -> Decimal:
        """Return the logical full amount for a possibly restored partial order."""
        tracking = self._get_tradexyz_fill_tracking(grid_order)
        target_amount = self._decimal_or_zero(tracking.get("target_amount"))
        order_amount = self._decimal_or_zero(getattr(grid_order, "amount", Decimal("0")))
        if target_amount <= 0:
            target_amount = order_amount
            if target_amount > 0:
                tracking["target_amount"] = str(target_amount)
        return target_amount

    def _get_cumulative_fill_amount(self, grid_order: GridOrder) -> Decimal:
        """Return the cumulative fill amount recorded for the logical order."""
        tracking = self._get_tradexyz_fill_tracking(grid_order)
        return self._decimal_or_zero(tracking.get("cumulative_filled"))

    def _get_remaining_repair_amount(self, grid_order: GridOrder) -> Decimal:
        """Return the amount that should be submitted to complete a missing order."""
        target_amount = self._get_logical_order_amount(grid_order)
        cumulative_filled = self._get_cumulative_fill_amount(grid_order)
        if target_amount <= 0:
            return self._decimal_or_zero(getattr(grid_order, "amount", Decimal("0")))
        return max(target_amount - min(cumulative_filled, target_amount), Decimal("0"))

    def _get_finalized_fill_amount(
        self,
        grid_order: GridOrder,
        reported_fill_amount: Decimal,
    ) -> Decimal:
        """Return the logical amount to use when a restored partial order completes."""
        target_amount = self._get_logical_order_amount(grid_order)
        cumulative_filled = self._get_cumulative_fill_amount(grid_order)
        if target_amount > 0 and cumulative_filled > 0:
            return target_amount
        return reported_fill_amount

    def _build_continuation_exchange_data(
        self,
        grid_order: GridOrder,
        remaining_amount: Decimal,
    ) -> Dict[str, Any]:
        """Carry partial-fill ledger data onto a replacement missing order."""
        data = deepcopy(grid_order.exchange_data) if isinstance(grid_order.exchange_data, dict) else {}
        tracking = data.get("tradexyz_fill_tracking")
        if not isinstance(tracking, dict):
            tracking = {}
            data["tradexyz_fill_tracking"] = tracking

        target_amount = self._get_logical_order_amount(grid_order)
        cumulative_filled = self._get_cumulative_fill_amount(grid_order)
        prior_fills = tracking.get("fills_by_id")
        if prior_fills and "prior_fills_by_id" not in tracking:
            tracking["prior_fills_by_id"] = deepcopy(prior_fills)
        tracking["fills_by_id"] = {}
        tracking["seen_fill_ids"] = []
        tracking["carry_filled_amount"] = str(cumulative_filled)
        tracking["snapshot_cumulative_filled"] = "0"
        tracking["target_amount"] = str(target_amount)
        tracking["cumulative_filled"] = str(cumulative_filled)
        tracking["remaining_amount"] = str(remaining_amount)
        tracking["continuation_source_order_id"] = str(getattr(grid_order, "order_id", "") or "")
        return data

    def _build_tradexyz_fill_event_key(
        self,
        item: Dict[str, Any],
        grid_order: GridOrder,
        filled_price: Decimal,
        fill_amount: Decimal,
    ) -> str:
        """Build the shared fill key used by websocket and health-check paths."""
        explicit_id = (
            item.get("tid")
            or item.get("fillId")
            or item.get("fill_id")
            or item.get("tradeId")
            or item.get("hash")
            or item.get("txHash")
        )
        if explicit_id is not None:
            return str(explicit_id)

        event_time = item.get("time") or item.get("timestamp") or ""
        side = item.get("side") or grid_order.side.value
        start_position = item.get("startPosition") or item.get("start_pos") or ""
        direction = item.get("dir") or item.get("direction") or ""
        fee = item.get("fee") or item.get("commission") or ""
        return (
            f"{grid_order.order_id}:{event_time}:{side}:{start_position}:{direction}:{fee}:"
            f"{format(filled_price.normalize(), 'f')}:{format(fill_amount.normalize(), 'f')}"
        )

    def _describe_key_diff(
        self,
        label: str,
        left: set[Tuple[object, str]],
        right: set[Tuple[object, str]],
    ) -> str:
        """Return a compact summary of missing and extra grid-side pairs."""
        missing = sorted(right - left)
        extra = sorted(left - right)
        missing_preview = ",".join(f"{key}:{side}" for key, side in missing[:4]) or "-"
        extra_preview = ",".join(f"{key}:{side}" for key, side in extra[:4]) or "-"
        return (
            f"{label}(missing={len(missing)}[{missing_preview}], "
            f"extra={len(extra)}[{extra_preview}])"
        )

    def _normalize_price_key(self, price: Optional[Decimal]) -> str:
        """Return a stable string key for comparing order prices across sources."""
        if price is None:
            return "none"
        return format(Decimal(str(price)).normalize(), "f")

    def _is_inferred_order(self, exchange_order: OrderData) -> bool:
        """Return whether an adapter flagged the order status as inferred."""
        params = getattr(exchange_order, "params", {}) or {}
        raw_data = getattr(exchange_order, "raw_data", {}) or {}
        return bool(params.get("inferred") or raw_data.get("inferred"))

    def _pending_keys_for_order(self, grid_order: GridOrder) -> List[str]:
        """Return every pending-cache alias for one local order."""
        if hasattr(self.engine, "_pending_keys_for_order"):
            return list(self.engine._pending_keys_for_order(grid_order))
        return [grid_order.order_id] if grid_order.order_id else []

    def _clear_pending_order_refs(self, grid_order: GridOrder, alias_keys: List[str]) -> None:
        """Remove every pending-cache alias for one local order."""
        if hasattr(self.engine, "_clear_pending_order_refs"):
            self.engine._clear_pending_order_refs(*(alias_keys or [grid_order.order_id]))
            return

        for key in alias_keys:
            self.engine._pending_orders.pop(key, None)

    def _consume_expected_cancellations(self, alias_keys: List[str]) -> None:
        """Clear expected-cancel markers for every alias of one order."""
        for key in alias_keys:
            if key in self.engine._expected_cancellations:
                self.engine._expected_cancellations.remove(key)

    def _register_engine_pending_order(
        self,
        grid_order: GridOrder,
        *keys: Optional[str],
    ) -> None:
        """Register a mirrored exchange order in the engine cache."""
        register = getattr(self.engine, "_register_pending_order", None)
        if callable(register):
            register(grid_order, *keys)
            return

        for key in keys:
            if key:
                self.engine._pending_orders[str(key)] = grid_order

    def _build_grid_order_from_exchange_order(self, exchange_order: OrderData) -> Optional[GridOrder]:
        """Convert an exchange open order into a local grid order without losing reverse-grid source."""
        builder = getattr(self.engine, "_build_grid_order_from_exchange_order", None)
        if callable(builder):
            return builder(exchange_order)

        order_id = getattr(exchange_order, "id", None)
        price = getattr(exchange_order, "price", None)
        amount = getattr(exchange_order, "amount", None)
        side = getattr(exchange_order, "side", None)
        if not order_id or price is None or amount is None or side is None:
            return None

        grid_side = (
            GridOrderSide.BUY
            if side.value.lower() == "buy"
            else GridOrderSide.SELL
        )
        price = Decimal(str(price))
        grid_id = self._infer_grid_id_for_exchange_order(price, grid_side)
        if grid_id is None:
            return None

        parent_order_id = None
        if self._is_reverse_side(grid_side):
            parent_order_id = f"mirrored:{order_id}"

        return GridOrder(
            order_id=str(order_id),
            grid_id=grid_id,
            side=grid_side,
            price=price,
            amount=Decimal(str(amount)),
            status=GridOrderStatus.PENDING,
            created_at=datetime.now(),
            parent_order_id=parent_order_id,
            exchange_data={"mirrored_exchange_order": True},
        )

    def _infer_grid_id_for_exchange_order(
        self,
        price: Decimal,
        side: GridOrderSide,
    ) -> Optional[int]:
        """Infer the logical source grid for imported base and reverse orders."""
        if not self._is_reverse_side(side):
            return self._grid_id_from_price(price)

        source_price = self._source_price_for_reverse_order(price, side)
        if source_price is None:
            return self._grid_id_from_price(price)
        return self._grid_id_from_price(source_price)

    def _source_price_for_reverse_order(
        self,
        price: Decimal,
        side: GridOrderSide,
    ) -> Optional[Decimal]:
        """Return the source grid price for an opposite-side reverse order."""
        base_side = self._base_side_for_grid_type()
        if base_side is None:
            return None

        distance = getattr(self.config, "reverse_order_grid_distance", 1) or 1
        try:
            offset = self.config.grid_interval * Decimal(str(distance))
        except Exception:
            return None

        if base_side == GridOrderSide.BUY and side == GridOrderSide.SELL:
            return price - offset
        if base_side == GridOrderSide.SELL and side == GridOrderSide.BUY:
            return price + offset
        return None

    def _is_reverse_side(self, side: GridOrderSide) -> bool:
        """Return whether an order side is opposite to the configured base side."""
        base_side = self._base_side_for_grid_type()
        return base_side is not None and side != base_side

    def _note_finalized_order(self, *keys: Optional[str]) -> None:
        """Ask the engine to remember finalized order ids for stale-snapshot filtering."""
        notifier = getattr(self.engine, "_note_finalized_order", None)
        if callable(notifier):
            notifier(*keys)

    def _was_recently_finalized_order(self, key: Optional[str]) -> bool:
        """Return whether the engine recently finalized this order id."""
        checker = getattr(self.engine, "_was_recently_finalized_order", None)
        if callable(checker):
            try:
                return bool(checker(key))
            except Exception:
                return False
        return False

    async def _restore_missing_order_if_timed_out(
        self,
        grid_order: GridOrder,
        alias_keys: List[str],
    ) -> bool:
        """Restore a missing order after it has been unresolved for long enough."""
        first_seen = self._note_missing_order(alias_keys)
        if (time.time() - first_seen) < self._missing_order_resolution_timeout:
            return False

        if self._has_recent_fill():
            return False

        self.logger.warning(
            "Treat unresolved missing order as unexpected cancellation after timeout: "
            f"grid_id={grid_order.grid_id}, side={grid_order.side.value}, "
            f"price={grid_order.price}, order_id={grid_order.order_id}, "
            f"timeout={self._missing_order_resolution_timeout:.0f}s"
        )
        self._clear_missing_order_tracking(alias_keys)
        self._clear_pending_order_refs(grid_order, alias_keys)
        grid_order.mark_cancelled()
        self._remove_order_from_state(grid_order.order_id)

        if hasattr(self.engine, "_restore_cancelled_grid_order"):
            await self.engine._restore_cancelled_grid_order(grid_order, grid_order.order_id)
            self._restored_missing_orders_in_sync += 1
            return True

        return False

    def _note_missing_order(self, alias_keys: List[str]) -> float:
        """Track when a pending order first disappeared from exchange open orders."""
        now = time.time()
        normalized = [key for key in alias_keys if key]
        first_seen = min(
            (self._missing_order_seen_at[key] for key in normalized if key in self._missing_order_seen_at),
            default=now,
        )
        for key in normalized:
            self._missing_order_seen_at.setdefault(key, first_seen)
        return first_seen

    def _clear_missing_order_tracking(self, alias_keys: List[str]) -> None:
        """Clear disappearance tracking for one order and all of its aliases."""
        for key in alias_keys:
            if key in self._missing_order_seen_at:
                del self._missing_order_seen_at[key]

    def _update_missing_grid_tracking(self, missing_orders: List[GridOrder]) -> None:
        """Track which inferred grid gaps persist across health-check cycles."""
        current_keys = {self._order_price_key(order) for order in missing_orders}
        now = time.time()
        for key in current_keys:
            self._missing_grid_seen_at.setdefault(key, now)

        stale_keys = set(self._missing_grid_seen_at.keys()) - current_keys
        for key in stale_keys:
            del self._missing_grid_seen_at[key]

    def _has_persistent_missing_orders(self, missing_orders: List[GridOrder]) -> bool:
        """Return whether any missing grid gap has persisted for at least one cooldown window."""
        now = time.time()
        for order in missing_orders:
            key = self._order_price_key(order)
            first_seen = self._missing_grid_seen_at.get(key)
            if first_seen is not None and (now - first_seen) >= self.RECENT_FILL_COOLDOWN_SECONDS:
                return True
        return False

    def _should_delay_gap_repair_for_recent_fill(
        self,
        missing_orders: List[GridOrder],
        cleanup_count: int,
    ) -> bool:
        """Return whether gap repair should still respect the recent-fill cooldown."""
        if not missing_orders or not self._has_recent_fill():
            return False
        if cleanup_count:
            return False
        if self._has_persistent_missing_orders(missing_orders):
            return False
        return True

    def _split_missing_orders_by_unresolved(
        self,
        missing_orders: List[GridOrder],
    ) -> Tuple[List[GridOrder], List[GridOrder]]:
        """Split missing orders into unresolved-overlapping and safe-to-repair subsets."""
        if not self._last_unresolved_order_price_keys:
            return [], missing_orders

        blocked: List[GridOrder] = []
        repairable: List[GridOrder] = []
        for order in missing_orders:
            if self._order_price_key(order) in self._last_unresolved_order_price_keys:
                blocked.append(order)
            else:
                repairable.append(order)
        return blocked, repairable

    def _order_price_key(self, order: GridOrder) -> Tuple[str, str]:
        """Return one normalized price/side key for a grid order."""
        return (
            self._normalize_price_key(order.price),
            order.side.value.lower(),
        )

    def _summarize_order_price_keys(self, orders: List[GridOrder], limit: int = 5) -> str:
        """Return a compact summary of missing order price/side keys for logs."""
        keys = [
            f"{order.price}:{order.side.value.lower()}"
            for order in orders[:limit]
        ]
        if len(orders) > limit:
            keys.append(f"+{len(orders) - limit} more")
        return "[" + ", ".join(keys) + "]"

    def _remove_order_from_state(self, order_id: str) -> None:
        """Remove one stale order from coordinator state if available."""
        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if state and hasattr(state, "active_orders"):
            stale_order = state.active_orders.pop(order_id, None)
            if stale_order is None:
                return
            if stale_order.side == GridOrderSide.BUY and getattr(state, "pending_buy_orders", 0) > 0:
                state.pending_buy_orders -= 1
            elif stale_order.side == GridOrderSide.SELL and getattr(state, "pending_sell_orders", 0) > 0:
                state.pending_sell_orders -= 1

    def _build_expected_orders(self, current_price: Decimal) -> List[GridOrder]:
        """Build the expected live order layout from the configured grid."""
        expected_orders: List[GridOrder] = []

        for grid_id in range(1, self.config.grid_count + 1):
            price = self.config.get_grid_price(grid_id)
            amount = self.config.get_formatted_grid_order_amount(grid_id)
            side = self._expected_side_for_grid(price, current_price)

            expected_orders.append(
                GridOrder(
                    order_id="",
                    grid_id=grid_id,
                    side=side,
                    price=price,
                    amount=amount,
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now(),
                )
            )

        return expected_orders

    def _expected_side_for_grid(self, grid_price: Decimal, current_price: Decimal) -> GridOrderSide:
        """Infer the expected maker side for a grid slot from the current price."""
        if grid_price <= current_price:
            return GridOrderSide.BUY
        return GridOrderSide.SELL

    def _check_position_health(
        self,
        exchange_orders: List[OrderData],
        positions: List[PositionData],
    ) -> PositionHealthResult:
        """Compare expected exposure against actual exchange exposure."""
        expected_position = self._calculate_expected_position(exchange_orders)
        actual_position = self._extract_actual_position(positions)
        tolerance = self._position_tolerance()
        needs_adjustment = abs(expected_position - actual_position) > tolerance

        if needs_adjustment:
            self.logger.warning(
                f"Position drift detected: expected={expected_position}, "
                f"actual={actual_position}, tolerance={tolerance}"
            )

        return PositionHealthResult(
            expected_position=expected_position,
            actual_position=actual_position,
            tolerance=tolerance,
            needs_adjustment=needs_adjustment,
        )

    def _should_repair_position(self) -> bool:
        """Return whether automatic position repair should run for this exchange."""
        exchange_name = str(getattr(self.config, "exchange", "")).lower()
        if exchange_name == "tradexyz":
            if not self._position_repair_skip_logged:
                self.logger.info(
                    "Skip position repair for TradeXYZ because open-order based exposure "
                    "estimation is not reliable enough for safe market repair"
                )
                self._position_repair_skip_logged = True
            return False
        return True

    def _log_runtime_consistency_message(
        self,
        level: str,
        message: str,
        throttle_seconds: float = 300.0,
    ) -> None:
        """Avoid writing the same runtime-consistency message every health-check cycle."""
        now = time.time()
        log_key = (level, message)
        if (
            log_key == self._runtime_consistency_log_key
            and (now - self._runtime_consistency_log_time) < throttle_seconds
        ):
            return

        getattr(self.logger, level)(message)
        self._runtime_consistency_log_key = log_key
        self._runtime_consistency_log_time = now

    def _log_health_summary(self, message: str, throttle_seconds: float = 300.0) -> None:
        """Avoid writing identical health-check summaries every cycle."""
        now = time.time()
        if (
            message == self._health_summary_log_key
            and (now - self._health_summary_log_time) < throttle_seconds
        ):
            return

        self.logger.info(message)
        self._health_summary_log_key = message
        self._health_summary_log_time = now

    def _iter_unique_local_pending_orders(self) -> Iterable[GridOrder]:
        """Yield unique pending-order objects across coordinator state and engine cache."""
        seen: set[int] = set()

        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if state and getattr(state, "active_orders", None):
            for order in state.active_orders.values():
                if getattr(order, "status", None) != GridOrderStatus.PENDING:
                    continue
                order_ref = id(order)
                if order_ref in seen:
                    continue
                seen.add(order_ref)
                yield order

        for order in self.engine.get_pending_orders():
            if getattr(order, "status", None) != GridOrderStatus.PENDING:
                continue
            order_ref = id(order)
            if order_ref in seen:
                continue
            seen.add(order_ref)
            yield order

    def _get_tracked_take_profit_price_keys(self) -> set[Tuple[str, str]]:
        """Return normalized price-side keys for locally tracked reverse/TP orders."""
        tp_keys: set[Tuple[str, str]] = set()

        for order in self._iter_unique_local_pending_orders():
            if self._is_tracked_take_profit_order(order):
                tp_keys.add((self._normalize_price_key(order.price), order.side.value.lower()))

        return tp_keys

    def _is_tracked_take_profit_order(self, order: GridOrder) -> bool:
        """Return whether a local pending order is an exit-side TP order."""
        if not self._is_reverse_side(order.side):
            return False

        scalping_tp_id = self._get_scalping_take_profit_order_id()
        if scalping_tp_id and getattr(order, "order_id", None) == scalping_tp_id:
            return True

        return bool(getattr(order, "parent_order_id", None))

    def _get_scalping_take_profit_order_id(self) -> Optional[str]:
        """Return the active scalping TP order id when scalping mode is enabled."""
        coordinator = getattr(self.engine, "coordinator", None)
        scalping_manager = getattr(coordinator, "scalping_manager", None) if coordinator else None
        if not scalping_manager:
            return None

        try:
            tp_order = scalping_manager.get_current_take_profit_order()
        except Exception:
            return None

        if not tp_order:
            return None
        return getattr(tp_order, "order_id", None)

    def _classify_tp_only_mismatch_log_level(self) -> Tuple[str, str]:
        """Downgrade isolated TP-coverage mismatches unless they persist for multiple cycles."""
        now = time.time()
        if self._tp_only_mismatch_cycles == 0:
            self._tp_only_mismatch_started_at = now
        self._tp_only_mismatch_cycles += 1
        mismatch_age = max(0.0, now - self._tp_only_mismatch_started_at)

        if (
            self._tp_only_mismatch_cycles >= self.TP_ONLY_MISMATCH_WARNING_CYCLES
            and mismatch_age >= self.TP_ONLY_MISMATCH_WARNING_SECONDS
        ):
            return (
                "warning",
                "Runtime consistency issue detected: persistent isolated TP-coverage mismatch "
                f"(cycles={self._tp_only_mismatch_cycles}, age={int(mismatch_age)}s): ",
            )

        return (
            "info",
            "Runtime consistency note: isolated TP-coverage mismatch while order and position "
            f"sources remain aligned (cycles={self._tp_only_mismatch_cycles}, age={int(mismatch_age)}s): ",
        )

    def _reset_tp_only_mismatch_tracking(self) -> None:
        """Clear isolated TP-mismatch tracking after the runtime state converges or changes class."""
        self._tp_only_mismatch_cycles = 0
        self._tp_only_mismatch_started_at = 0.0

    def _get_pending_partial_base_fill_exposure(self) -> Decimal:
        """Return live exposure caused by partial base fills that should not yet have TP coverage."""
        base_side = self._base_side_for_grid_type()
        if base_side is None:
            return Decimal("0")

        total = Decimal("0")
        for order in self._iter_unique_local_pending_orders():
            if order.side != base_side:
                continue
            if getattr(order, "parent_order_id", None):
                continue

            tracking = getattr(order, "exchange_data", {}) or {}
            fill_tracking = tracking.get("tradexyz_fill_tracking", {})
            if not isinstance(fill_tracking, dict):
                continue

            cumulative_filled = self._decimal_or_zero(fill_tracking.get("cumulative_filled"))
            if cumulative_filled <= 0:
                continue

            target_amount = self._get_logical_order_amount(order)
            if target_amount > 0:
                cumulative_filled = min(cumulative_filled, target_amount)
            total += cumulative_filled

        return total

    @staticmethod
    def _decimal_or_zero(value: object) -> Decimal:
        """Convert an arbitrary numeric value to Decimal, defaulting invalid values to zero."""
        try:
            if value is None:
                return Decimal("0")
            return Decimal(str(value))
        except Exception:
            return Decimal("0")

    def _calculate_expected_position(self, exchange_orders: List[OrderData]) -> Decimal:
        """Estimate target exposure from current open buy/sell grid counts."""
        buy_count = sum(1 for order in exchange_orders if order.side.value.lower() == "buy")
        sell_count = sum(1 for order in exchange_orders if order.side.value.lower() == "sell")

        if self.config.grid_type in {
            GridType.LONG,
            GridType.FOLLOW_LONG,
            GridType.MARTINGALE_LONG,
        }:
            filled_buy_count = sell_count
            if self.config.is_martingale_mode():
                start_grid_id = self.config.grid_count - filled_buy_count + 1
                return sum(
                    (
                        self.config.get_formatted_grid_order_amount(grid_id)
                        for grid_id in range(start_grid_id, self.config.grid_count + 1)
                    ),
                    Decimal("0"),
                )
            return Decimal(str(filled_buy_count)) * self.config.order_amount

        if self.config.grid_type in {
            GridType.SHORT,
            GridType.FOLLOW_SHORT,
            GridType.MARTINGALE_SHORT,
        }:
            filled_sell_count = buy_count
            if self.config.is_martingale_mode():
                amount = sum(
                    (
                        self.config.get_formatted_grid_order_amount(grid_id)
                        for grid_id in range(1, filled_sell_count + 1)
                    ),
                    Decimal("0"),
                )
                return -amount
            return -Decimal(str(filled_sell_count)) * self.config.order_amount

        return Decimal("0")

    def _extract_actual_position(self, positions: Iterable[PositionData]) -> Decimal:
        """Return the signed live position size for the configured symbol."""
        target_symbol = self.config.symbol
        target_base = target_symbol.split("/")[0]

        for position in positions:
            symbol = getattr(position, "symbol", "")
            base = symbol.split("/")[0] if "/" in symbol else symbol
            if symbol != target_symbol and base != target_base:
                continue

            side = position.side
            size = Decimal(str(position.size or 0))
            if side == PositionSide.SHORT:
                return -abs(size)
            if side == PositionSide.LONG:
                return abs(size)
            return size

        return Decimal("0")

    async def _adjust_position(self, result: PositionHealthResult) -> bool:
        """Adjust live exposure toward the expected exposure."""
        target = result.expected_position
        actual = result.actual_position
        delta = target - actual

        if abs(delta) <= result.tolerance:
            return False

        current_price = await self.engine.get_current_price()

        # Flip side first when the position sign is wrong.
        if actual > 0 and target < 0:
            if not await self._close_position(PositionSide.LONG, abs(actual), current_price):
                return False
            actual = Decimal("0")
            delta = target
        elif actual < 0 and target > 0:
            if not await self._close_position(PositionSide.SHORT, abs(actual), current_price):
                return False
            actual = Decimal("0")
            delta = target

        if abs(delta) <= result.tolerance:
            return True

        if delta > 0:
            return await self._open_position(PositionSide.LONG, abs(delta), current_price)

        if actual > 0:
            return await self._close_position(PositionSide.LONG, abs(delta), current_price)

        return await self._open_position(PositionSide.SHORT, abs(delta), current_price)

    async def _close_position(self, side: PositionSide, amount: Decimal, current_price: Decimal) -> bool:
        """Reduce an existing position with a market order."""
        if amount <= 0:
            return True

        close_side = ExchangeOrderSide.SELL if side == PositionSide.LONG else ExchangeOrderSide.BUY
        return await self._submit_market_order(close_side, amount, current_price, "close_position")

    async def _open_position(self, side: PositionSide, amount: Decimal, current_price: Decimal) -> bool:
        """Open additional exposure with a market order."""
        if amount <= 0:
            return True

        open_side = ExchangeOrderSide.BUY if side == PositionSide.LONG else ExchangeOrderSide.SELL
        return await self._submit_market_order(open_side, amount, current_price, "open_position")

    async def _submit_market_order(
        self,
        side: ExchangeOrderSide,
        amount: Decimal,
        current_price: Decimal,
        reason: str,
    ) -> bool:
        """Submit a market order through the exchange adapter."""
        try:
            await self.engine.exchange.create_order(
                symbol=self.config.symbol,
                side=side,
                order_type=OrderType.MARKET,
                amount=amount,
                price=current_price,
                params=None,
            )
            self.logger.info(
                f"Submitted market order for health repair: reason={reason}, "
                f"side={side.value}, amount={amount}, reference_price={current_price}"
            )
            return True
        except Exception as exc:
            self.logger.error(
                f"Failed to submit market order for health repair: "
                f"reason={reason}, side={side.value}, amount={amount}, error={exc}"
            )
            self.logger.error(traceback.format_exc())
            return False

    async def _place_missing_orders(self, orders: List[GridOrder]) -> int:
        """Place missing grid orders back on the exchange."""
        if not orders:
            return 0

        source_order_ids = {
            id(order): getattr(order, "order_id", None)
            for order in orders
        }

        if len(orders) == 1:
            placed = await self.engine.place_order(orders[0])
            if placed:
                self._sync_placed_missing_order_to_state(
                    placed,
                    source_order_ids.get(id(orders[0])),
                )
                self.logger.info(
                    f"Placed missing grid order: grid_id={placed.grid_id}, "
                    f"side={placed.side.value}, price={placed.price}, amount={placed.amount}"
                )
                return 1
            return 0

        placed_orders = await self.engine.place_batch_orders(orders)
        for order in placed_orders:
            self._sync_placed_missing_order_to_state(
                order,
                source_order_ids.get(id(order)),
            )
            self.logger.info(
                f"Placed missing grid order: grid_id={order.grid_id}, "
                f"side={order.side.value}, price={order.price}, amount={order.amount}"
            )
        return len(placed_orders)

    def _sync_placed_missing_order_to_state(
        self,
        placed_order: GridOrder,
        source_order_id: Optional[str],
    ) -> None:
        """Replace stale coordinator-state entries with a repaired exchange order."""
        coordinator = getattr(self.engine, "coordinator", None)
        state = getattr(coordinator, "state", None) if coordinator else None
        if not state or not hasattr(state, "active_orders"):
            return

        active_orders = state.active_orders
        remove_ids: List[str] = []
        normalized_source_id = str(source_order_id) if source_order_id else ""
        new_order_id = getattr(placed_order, "order_id", "")

        for order_id, order in list(active_orders.items()):
            if order_id == new_order_id:
                continue
            if normalized_source_id and order_id == normalized_source_id:
                remove_ids.append(order_id)
                continue
            if (
                getattr(order, "status", None) == GridOrderStatus.PENDING
                and order.grid_id == placed_order.grid_id
                and order.side == placed_order.side
                and self._normalize_price_key(order.price)
                == self._normalize_price_key(placed_order.price)
            ):
                remove_ids.append(order_id)

        for order_id in remove_ids:
            if hasattr(state, "remove_order"):
                state.remove_order(order_id)
            else:
                active_orders.pop(order_id, None)

        if new_order_id not in active_orders:
            if hasattr(state, "add_order"):
                state.add_order(placed_order)
            else:
                active_orders[new_order_id] = placed_order

    def _has_recent_fill(self) -> bool:
        """Avoid immediate repairs right after a genuine fill event."""
        coordinator = getattr(self.engine, "coordinator", None)
        last_fill = getattr(coordinator, "_last_fill_time", 0) if coordinator else 0
        return (time.time() - last_fill) < self.RECENT_FILL_COOLDOWN_SECONDS

    def _position_tolerance(self) -> Decimal:
        """Return the allowed drift before a position repair is triggered."""
        tolerance_cfg = self.config.position_tolerance or {}
        multiplier = Decimal(str(tolerance_cfg.get("tolerance_multiplier", "0.25")))
        base_amount = getattr(self.config, "order_amount", Decimal("0"))
        tolerance = base_amount * multiplier
        return max(tolerance, Decimal("0"))

    def _grid_id_from_price(self, price: Optional[Decimal]) -> Optional[int]:
        """Safely map an order price back to a grid id."""
        if price is None:
            return None
        try:
            return self.config.get_grid_index_by_price(Decimal(str(price)))
        except Exception:
            return None

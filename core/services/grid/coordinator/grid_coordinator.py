"""
Grid coordinator module.

Coordinates the grid runtime, including initialization, fill handling,
reverse-order placement, and runtime recovery logic.

Note: some exchanges such as Lighter may expose alternate order identifiers,
so the coordinator keeps compatibility with those exchange-specific order-id
behaviors during sync and verification.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
from decimal import Decimal
from datetime import datetime

from ....adapters.exchanges.models import OrderSide, OrderType, PositionSide
from ....logging import get_logger
from ..interfaces import IGridStrategy, IGridEngine, IPositionTracker
from ..models import (
    GridConfig, GridState, GridOrder, GridOrderSide,
    GridOrderStatus, GridStatus, GridStatistics
)
from ..scalping import ScalpingManager
from ..capital_protection import CapitalProtectionManager
from ..take_profit import TakeProfitManager
from ..price_lock import PriceLockManager

#  导入新模块
from .grid_reset_manager import GridResetManager
from .position_monitor import PositionMonitor
from .balance_monitor import BalanceMonitor
from .scalping_operations import ScalpingOperations


class GridCoordinator:
    """
    Grid runtime coordinator.

    Responsibilities:
    1. Initialize the strategy, engine, tracker, and shared state.
    2. Handle order-fill callbacks and reverse-order logic.
    3. Process batch fill paths.
    4. Keep runtime state coherent.
    5. Coordinate recovery and error handling.
    """

    def __init__(
        self,
        config: GridConfig,
        strategy: IGridStrategy,
        engine: IGridEngine,
        tracker: IPositionTracker,
        grid_state: GridState,
        reserve_manager=None  #  Optional Reserved Manager (In Stock Only)
    ):
        """
        Initialize the grid coordinator.

        Args:
            config: Grid configuration.
            strategy: Grid strategy implementation.
            engine: Execution engine.
            tracker: Position tracker.
            grid_state: Shared grid-state instance.
            reserve_manager: Optional spot reserve manager.
        """
        self.logger = get_logger(__name__)
        self.config = config
        self.strategy = strategy
        self.engine = engine
        self.tracker = tracker
        self.reserve_manager = reserve_manager  #  Save reserved manager references

        # Expose the coordinator on the engine for health checker and helper access.
        if hasattr(engine, 'coordinator'):
            engine.coordinator = self

        # Mesh state (using the passed-in shared instance)
        self.state = grid_state

        # Log: Reserved for management status
        if self.reserve_manager:
            self.logger.info("Spot reserve manager enabled")

            # Pass the reserved manager to the health checker (to be set later after engine initialization).
            # Note: _health_checker is created in engine.initialize(), this is just a record.

        # Operation control
        self._running = False
        self._paused = False
        self._manual_pause_owned = False
        self._resetting = False  # Reset-in-progress flag used by protection and scalping flows.
        self._emergency_stop_requested = False
        self._emergency_stop_task: Optional[asyncio.Task] = None
        self._stop_lock = asyncio.Lock()
        self._shutdown_completed = False
        self._shutdown_order_cancellation_failed = False
        self._shutdown_cleanup_error: Optional[str] = None
        self._unsafe_shutdown_incident: Optional[str] = None
        self._fatal_stop_reason: Optional[str] = None

        #  Transaction deduplication mechanism: Prevents the same transaction from being processed repeatedly by multiple detection mechanisms.
        # key = 'grid_id:side:price', value = timestamp
        self._recent_fills: Dict[str, float] = {}
        self._fill_dedup_window: float = 10.0
        self._last_fill_time: float = 0
        self._active_fill_callbacks: int = 0
        self._deferred_fills: Dict[str, GridOrder] = {}
        self._deferred_fill_drain_task: Optional[asyncio.Task] = None

        #  Grid-level locking: Tracks the status of pending take-profit orders at each grid level.
        # key = grid_id, value = {'tp_side': str, 'tp_price': Decimal}
        self._grid_level_locks: Dict[int, Dict] = {}

        # 系统状态管理（REST失败保护）
        self.is_emergency_stopped = False  # 持仓异常时紧急停止

        # 异常计数
        self._error_count = 0
        self._max_errors = 5  # 最大错误次数，超过则暂停

        # 触发次数统计（仅标记次数，无实质性功能）
        self._scalping_trigger_count = 0  # Scalping-mode trigger count.
        self._price_escape_trigger_count = 0  # 价格朝有利方向脱离触发次数
        self._take_profit_trigger_count = 0  # 止盈模式触发次数
        self._capital_protection_trigger_count = 0  # 本金保护模式触发次数
        self._stop_loss_trigger_count = 0
        self._stop_loss_triggered = False
        self._stop_loss_monitor_task: Optional[asyncio.Task] = None

        #  价格移动网格专用
        self._price_escape_start_time: Optional[float] = None  # 价格脱离开始时间
        self._last_escape_check_time: float = 0  # 上次检查时间
        self._escape_check_interval: int = 10  # 检查间隔（秒）
        self._is_resetting: bool = False  # 是否正在重置网格

        # Scalping manager.
        self.scalping_manager: Optional[ScalpingManager] = None
        self._scalping_position_monitor_task: Optional[asyncio.Task] = None
        self._scalping_position_check_interval: int = 1  # Scalping-mode position check interval in seconds (REST polling).
        self._last_ws_position_size = Decimal('0')  # 用于WebSocket事件驱动
        self._last_ws_position_price = Decimal('0')
        #  持仓监控状态（类似订单统计的混合模式）
        self._position_ws_enabled: bool = False  # WebSocket持仓监控是否启用
        self._last_position_ws_time: float = 0  # 最后一次收到WebSocket持仓更新的时间
        self._last_order_filled_time: float = 0  # 最后一次订单成交的时间（用于判断WS是否失效）
        self._position_ws_response_timeout: int = 5  # 订单成交后WebSocket响应超时（秒）
        self._position_ws_check_interval: int = 5  # 尝试恢复WebSocket的间隔（秒）
        self._last_position_ws_check_time: float = 0  # 上次检查WebSocket的时间
        #  定期REST校验（心跳检测）
        self._position_rest_verify_interval: int = 60  # 每分钟用REST校验WebSocket持仓（秒）
        self._last_position_rest_verify_time: float = 0  # 上次REST校验的时间
        if config.is_scalping_enabled():
            self.scalping_manager = ScalpingManager(config)
            self.logger.info("Scalping manager enabled")

        # ️ 本金保护管理器
        self.capital_protection_manager: Optional[CapitalProtectionManager] = None
        if config.is_capital_protection_enabled():
            self.capital_protection_manager = CapitalProtectionManager(config)
            self.logger.info("Capital protection manager enabled")

        #  止盈管理器
        self.take_profit_manager: Optional[TakeProfitManager] = None
        if config.take_profit_enabled:
            self.take_profit_manager = TakeProfitManager(config)
            self.logger.info("Take-profit manager enabled")

        #  价格锁定管理器
        self.price_lock_manager: Optional[PriceLockManager] = None
        if config.price_lock_enabled:
            self.price_lock_manager = PriceLockManager(config)
            self.logger.info("Price lock manager enabled")

        #  账户余额（由BalanceMonitor管理）
        self._spot_balance: Decimal = Decimal('0')  # 现货余额（未用作保证金）
        self._collateral_balance: Decimal = Decimal('0')  # 抵押品余额（用作保证金）
        self._order_locked_balance: Decimal = Decimal('0')  # 订单冻结余额
        self._symbol_initial_capital: Decimal = Decimal('0')
        self._symbol_reference_price: Decimal = Decimal('0')

        #  新增：模块化组件初始化
        self.reset_manager = GridResetManager(
            self, config, grid_state, engine, tracker, strategy
        )
        self.position_monitor = PositionMonitor(
            engine, tracker, config, self
        )
        self.balance_monitor = BalanceMonitor(
            engine, config, self, update_interval=10
        )

        # Optional scalping-operations helper.
        self.scalping_ops: Optional[ScalpingOperations] = None
        if config.is_scalping_enabled() and self.scalping_manager:
            self.scalping_ops = ScalpingOperations(
                self, self.scalping_manager, engine, grid_state,
                tracker, strategy, config
            )

        self.logger.info(f"Grid coordinator initialized: {config}")

    async def initialize(self):
        """初始化网格系统"""
        try:
            self.logger.info("Starting grid system initialization")

            # 1. Initialize the execution engine first (sets engine.config).
            await self.engine.initialize(self.config)
            self.logger.info("Engine initialization completed")

            # 获取当前市价（所有模式都需要，用于过滤 taker 订单）
            current_price = await self.engine.get_current_price()
            self.logger.info(f"Current market price: ${current_price:,.2f}")

            if self.config.check_stop_loss(current_price):
                raise ValueError(
                    f"Startup price ${current_price:,.4f} already breaches "
                    f"stop_loss_price ${self.config.stop_loss_price:,.4f}; "
                    "refusing to place grid orders"
                )

            #  价格移动网格：根据当前价格设置价格区间
            if self.config.is_follow_mode():
                self.config.update_price_range_for_follow_mode(current_price)
                self.logger.info(
                    f"Follow-mode price range updated from market price ${current_price:,.2f}: "
                    f"[${self.config.lower_price:,.2f}, ${self.config.upper_price:,.2f}]"
                )

            self.ensure_symbol_isolated_capital(current_price=current_price)

            # 2. 初始化网格状态
            self.state.initialize_grid_levels(
                self.config.grid_count,
                self.config.get_grid_price
            )
            self.logger.info(
                f"Grid state initialized with {self.config.grid_count} levels"
            )

            # 3. 初始化策略，生成初始订单（传入市价，过滤高于市价的买单/低于市价的卖单）
            initial_orders = self.strategy.initialize(self.config, current_price)
            initial_orders = self.filter_orders_within_max_position(
                initial_orders,
                "startup grid",
            )
            if not initial_orders:
                raise RuntimeError(
                    "No initial grid orders are eligible at the current price and max_position"
                )

            #  价格移动网格：价格区间在初始化后才设置
            if self.config.is_follow_mode():
                self.logger.info(
                    f"Strategy initialized with {len(initial_orders)} initial orders "
                    f"across [${self.config.lower_price:,.2f}, ${self.config.upper_price:,.2f}]"
                )
            else:
                self.logger.info(
                    f"Strategy initialized with {len(initial_orders)} initial orders "
                    f"across ${self.config.lower_price:,.2f} - ${self.config.upper_price:,.2f}"
                )

            # 4. 订阅订单更新
            self.engine.subscribe_order_updates(self._on_order_filled)
            self.logger.info("Order update subscription completed")
            if hasattr(self.engine, "suspend_health_repairs"):
                self.engine.suspend_health_repairs("startup initial grid placement")

            #  提前设置_running标志，确保监控任务能正常运行
            self._running = True
            if not self.engine.is_running():
                await self.engine.start()

            #  4.5. 启动持仓监控（使用新模块 PositionMonitor）
            await self.position_monitor.start_monitoring()

            # 5. 批量下所有初始订单（关键修改）
            self.logger.info(
                f"Starting initial batch placement for {len(initial_orders)} orders"
            )
            placed_orders = await self.engine.place_batch_orders(initial_orders)

            placement_error: Optional[RuntimeError] = None
            if len(placed_orders) != len(initial_orders):
                placement_error = RuntimeError(
                    "Partial initial grid placement: "
                    f"submitted={len(placed_orders)}/{len(initial_orders)}; "
                    "startup is unsafe"
                )
            else:
                try:
                    await self._verify_initial_orders_live(placed_orders)
                except Exception as verification_error:
                    placement_error = RuntimeError(
                        f"Initial grid verification failed: {verification_error}"
                    )

            if placement_error is not None:
                self.logger.critical(str(placement_error))
                try:
                    await self.stop()
                except Exception as cleanup_error:
                    raise RuntimeError(
                        f"{placement_error}; cleanup failed: {cleanup_error}"
                    ) from cleanup_error
                raise placement_error

            # 6. 批量添加到状态追踪（只添加未成交的订单）
            self.logger.info(
                f"Adding {len(placed_orders)} placed orders to state tracking"
            )
            added_count = 0
            skipped_count = 0
            for order in placed_orders:
                #  检查订单是否已经在状态中（可能已经通过WebSocket成交回调处理）
                if order.order_id in self.state.active_orders:
                    skipped_count += 1
                    self.logger.debug(
                        f"Skipping existing order in state: {order.order_id} "
                        f"(Grid {order.grid_id}, {order.side.value})"
                    )
                    continue

                #  检查订单是否已经成交（状态为FILLED）
                if order.status == GridOrderStatus.FILLED:
                    skipped_count += 1
                    self.logger.debug(
                        f"Skipping already-filled order: {order.order_id} "
                        f"(Grid {order.grid_id}, {order.side.value})"
                    )
                    continue

                self.state.add_order(order)
                added_count += 1
                self.logger.debug(
                    f"Added order to state: {order.order_id} "
                    f"(Grid {order.grid_id}, {order.side.value})"
                )

            self.logger.info(
                f"Initial placement completed: {len(placed_orders)}/{len(initial_orders)} "
                f"orders submitted"
            )
            self.logger.info(
                f"State add summary: added={added_count}, skipped={skipped_count} "
                f"(already present or already filled)"
            )
            self.logger.info(
                f"State summary: "
                f"buy_orders={self.state.pending_buy_orders}, "
                f"sell_orders={self.state.pending_sell_orders}, "
                f"active_orders={len(self.state.active_orders)}"
            )

            # 7. 启动系统
            self.state.start()
            # self._running = True  # 已在启动监控任务前设置

            self.logger.info(
                "Grid system initialization completed; waiting for fills"
            )
            if hasattr(self.engine, "resume_health_repairs"):
                self.engine.resume_health_repairs("startup initial grid placement")

        except Exception as e:
            self.logger.error(f"Grid system initialization failed: {e}")
            if hasattr(self.engine, "resume_health_repairs"):
                self.engine.resume_health_repairs("startup initial grid placement")
            self.state.set_error()
            raise

    async def _verify_initial_orders_live(
        self,
        placed_orders: List[GridOrder],
    ) -> None:
        """Require each accepted startup order to be live or already filled."""
        exchange_orders = await self.engine.exchange.get_open_orders(
            self.config.symbol
        )
        exchange_keys = {
            str(key)
            for exchange_order in exchange_orders
            for key in (
                getattr(exchange_order, "id", None),
                getattr(exchange_order, "client_id", None),
            )
            if key is not None
        }
        pending_key_getter = getattr(self.engine, "_pending_keys_for_order", None)
        unverified_orders = []

        for order in placed_orders:
            if order.status == GridOrderStatus.FILLED:
                continue

            order_keys = {str(order.order_id)} if order.order_id else set()
            if callable(pending_key_getter):
                order_keys.update(
                    str(key)
                    for key in pending_key_getter(order)
                    if key is not None
                )

            if order.status != GridOrderStatus.PENDING or not (
                order_keys & exchange_keys
            ):
                unverified_orders.append(order)

        if unverified_orders:
            summary = ", ".join(
                f"grid={order.grid_id}/{order.side.value}@{order.price}/"
                f"status={order.status.value}"
                for order in unverified_orders[:5]
            )
            if len(unverified_orders) > 5:
                summary += f", +{len(unverified_orders) - 5} more"
            raise RuntimeError(
                f"{len(unverified_orders)}/{len(placed_orders)} accepted orders "
                f"are not live: {summary}"
            )

        self.logger.info(
            f"Initial exchange verification passed: {len(placed_orders)} orders covered"
        )

    async def _on_order_filled(self, filled_order: GridOrder):
        """
        订单成交回调 - 核心逻辑

        当订单成交时：
        1. 记录成交信息
        2. Check scalping mode
        3. 计算反向订单参数
        4. 立即挂反向订单

        Args:
            filled_order: 已成交订单
        """
        self._active_fill_callbacks = getattr(self, "_active_fill_callbacks", 0) + 1
        fill_committed = False
        reverse_secured = False
        try:
            # Critical: do not process fills after the system has stopped; this prevents new orders during shutdown.
            if not self._running:
                self.logger.debug("System is stopped; skipping order handling")
                return

            if getattr(self, "_emergency_stop_requested", False):
                self.logger.warning("Fatal stop is pending; skipping order handling")
                return

            #  关键检查：防止在重置期间处理订单
            if self._paused:
                if self._defer_fill_during_rest_pause(filled_order):
                    return
                self.logger.warning("System is paused; skipping order handling")
                return

            if self._resetting:
                self.logger.warning("System reset in progress; skipping order handling")
                return

            #  成交去重：防止同一笔成交被 REST 轮询和健康检查同步重复处理
            import time as _time
            fill_key = self._fill_key(filled_order)
            current_time = _time.time()
            # 清理过期条目
            self._recent_fills = {
                k: v for k, v in self._recent_fills.items()
                if current_time - v < self._fill_dedup_window
            }
            if fill_key in self._recent_fills:
                elapsed = current_time - self._recent_fills[fill_key]
                self.logger.info(
                    f"Skipping duplicate fill handling: Grid {filled_order.grid_id} "
                    f"{filled_order.side.value}@{filled_order.price} "
                    f"(last handled {elapsed:.1f}s ago, fill_key={fill_key}, "
                    f"reverse_order_id={filled_order.reverse_order_id})"
                )
                return
            self._recent_fills[fill_key] = current_time
            self._last_fill_time = current_time  #  记录最近成交时间（供 health checker 冷却）

            self.logger.info(
                f"Order filled: {filled_order.side.value} "
                f"{filled_order.filled_amount}@{filled_order.filled_price} "
                f"(Grid {filled_order.grid_id}, order_id={filled_order.order_id}, "
                f"fill_key={fill_key}, parent_order_id={filled_order.parent_order_id}, "
                f"reverse_order_id={filled_order.reverse_order_id})"
            )

            #  触发持仓查询（订单成交后立即查询持仓，带5秒去重）
            asyncio.create_task(
                self.position_monitor.trigger_event_query("订单成交")
            )

            #  解锁网格层级：如果这笔成交是反向止盈单，解锁该层级
            grid_id_check = filled_order.grid_id
            if grid_id_check in self._grid_level_locks:
                lock_info = self._grid_level_locks[grid_id_check]
                if lock_info['tp_side'] == filled_order.side.value:
                    self.logger.info(
                            f"Unlocked Grid {grid_id_check} after take-profit fill "
                            f"{filled_order.side.value}@{filled_order.price} "
                        )
                    del self._grid_level_locks[grid_id_check]

            # 1. 更新状态
            state_updated = self._mark_state_order_filled_with_fallback(filled_order)
            if not state_updated:
                context_label, context_details = self._describe_fill_tracking_gap(
                    filled_order=filled_order,
                    engine_matches=[],
                )
                self.logger.info(
                    f"{context_label}; skip local reverse-order handling: "
                    f"order_id={filled_order.order_id}, grid_id={filled_order.grid_id}, "
                    f"side={filled_order.side.value}, price={filled_order.price}, "
                    f"{context_details}"
                )
                return

            # The source order is now authoritatively filled. Until a reverse
            # is intentionally skipped or secured, any error must fail closed.
            fill_committed = True

            #  2. 记录交易历史（不影响持仓，只用于统计和显示）
            # 持仓数据完全来自 position_monitor 的REST查询
            # 此方法只记录交易历史和统计，不更新持仓
            self.tracker.record_filled_order(filled_order)

            #  2.5. 记录现货买入手续费（仅现货且启用预留）
            if self.reserve_manager and filled_order.side.value == 'buy':
                fee = self.reserve_manager.record_buy_fee(
                    filled_order.filled_amount or filled_order.amount
                )
                status = self.reserve_manager.get_status()
                self.logger.info(
                    f"Spot buy fee recorded: {fee} {self.reserve_manager.base_currency}, "
                    f"reserve_health={status['health_percent']:.1f}%"
                )

            # 3. Check scalping mode (using the helper module).
            if self.scalping_manager and self.scalping_ops:
                # Check whether this fill is the take-profit order.
                if self._is_take_profit_order_filled(filled_order):
                    await self.scalping_ops.handle_take_profit_filled()
                    reverse_secured = True
                    return  # 止盈成交后不再挂反向订单

                # 🆕 更新最后一次方向性订单ID（做多追踪买单，做空追踪卖单）
                self.scalping_ops.update_last_directional_order(
                    order_id=filled_order.order_id,
                    order_side=filled_order.side.value
                )

                # In scalping mode, wait until position sync completes before updating the take-profit order.
                # 原因：REST API持仓同步有延迟，订单成交时tracker可能还没更新
                # 解决方案：等待position_monitor的REST查询完成
                await asyncio.sleep(1.0)  # 等待1秒让REST持仓同步完成

                #  强制更新余额（确保当前权益计算准确）
                # 原因：余额监控器默认10秒更新一次，订单成交后BTC/USDC数量变化需要立即反映
                # 这样止盈价格计算才能使用最新的权益数据
                self.logger.debug("Refreshing balances after fill")
                await self.balance_monitor.update_balance()

                # Push the latest position into the scalping manager.
                current_position = self.tracker.get_current_position()
                average_cost = self.tracker.get_average_cost()
                symbol_snapshot = self.get_symbol_isolated_snapshot()
                self.scalping_manager.update_position(
                    current_position,
                    average_cost,
                    symbol_snapshot["initial_capital"],
                    symbol_snapshot["current_equity"],
                )

                # Check whether the take-profit order needs to be refreshed.
                await self.scalping_ops.update_take_profit_order_if_needed()

            # ️ 3.5. 检查本金保护模式
            if self.capital_protection_manager:
                current_price = filled_order.filled_price
                current_grid_index = self.config.find_nearest_grid_index(
                    current_price)
                await self._check_capital_protection_mode(current_price, current_grid_index)

            # 4. 计算反向订单参数
            # Scalping mode may intentionally skip reverse-order placement.
            if self.scalping_manager and self.scalping_manager.is_active():
                # Scalping mode keeps entry-side behavior only and does not place exit-side reverse orders.
                if not self._should_place_reverse_order_in_scalping(filled_order):
                    self.logger.info("Scalping mode active; skipping reverse order placement")
                    reverse_secured = True
                    return

            new_side, new_price, new_grid_id = self.strategy.calculate_reverse_order(
                filled_order,
                self.config.grid_interval,
                self.config.reverse_order_grid_distance
            )

            #  网格层级锁定检查：防止重复挂单
            grid_id = new_grid_id
            if grid_id in self._grid_level_locks:
                lock_info = self._grid_level_locks[grid_id]
                if lock_info['tp_side'] == new_side.value:
                    self.logger.info(
                        f"Grid {grid_id} already has pending "
                        f"{lock_info['tp_side']}@{lock_info['tp_price']}; "
                        f"skipping duplicate placement"
                    )
                    reverse_secured = True
                    return

            #  反向订单去重：检查当前挂单中是否已有相同 grid_id + 方向 + 价格 的订单
            pending_orders = self.engine.get_pending_orders()
            for pending in pending_orders:
                if (pending.grid_id == new_grid_id and
                    pending.side == new_side and
                    pending.price == new_price):
                    self.logger.info(
                        f"Matching reverse order already exists: "
                        f"Grid {new_grid_id} {new_side.value}@{new_price}; "
                        f"skipping duplicate placement"
                    )
                    reverse_secured = True
                    return

            #  反向订单不做 Taker 防护
            # 反向单是平仓单，持仓已存在，不挂单的风险（持仓暴露）远大于 taker 手续费
            # 原 taker 防护会在价格快速移动时静默丢弃反向单，导致持仓单边累积

            # 5. 创建反向订单
            reverse_order = GridOrder(
                order_id="",  # Filled in by the execution engine.
                grid_id=new_grid_id,
                side=new_side,
                price=new_price,
                amount=filled_order.filled_amount or filled_order.amount,  # 数量完全一致
                status=GridOrderStatus.PENDING,
                created_at=datetime.now(),
                parent_order_id=filled_order.order_id
            )

            if not self.can_place_order_within_max_position(
                reverse_order.side,
                reverse_order.amount,
            ):
                self.logger.warning(
                    f"Reverse order skipped by max_position: {new_side.value} "
                    f"{reverse_order.amount}@{new_price} (Grid {new_grid_id})"
                )
                self._fail_stop_for_unmanaged_fill(
                    filled_order,
                    RuntimeError("reverse order rejected by max_position"),
                )
                return

            # Reserve the reverse intent before the network request so health
            # repair cannot refill the opening side after an unmanaged fill.
            self._grid_level_locks[new_grid_id] = {
                'tp_side': new_side.value,
                'tp_price': new_price,
                'tp_order_id': None,
            }

            # 6. 下反向订单
            try:
                placed_order = await self.engine.place_order(reverse_order)
            except Exception as exc:
                self._fail_stop_for_unmanaged_reverse(reverse_order, exc)
                raise
            if placed_order is None:
                failure = RuntimeError("reverse order placement returned no result")
                self._fail_stop_for_unmanaged_reverse(reverse_order, failure)
                self._fail_stop_for_unmanaged_fill(filled_order, failure)
                self.logger.critical(
                    f"Reverse order was not submitted; fail-stop requested: "
                    f"{new_side.value} {reverse_order.amount}@{new_price} "
                    f"(Grid {new_grid_id})"
                )
                return
            reverse_secured = True
            self.state.add_order(placed_order)

            # 7. 记录关联关系
            filled_order.reverse_order_id = placed_order.order_id

            self.logger.info(
                f"Reverse order placed: {new_side.value} "
                f"{reverse_order.amount}@{new_price} "
                f"(Grid {new_grid_id})"
            )

            #  锁定网格层级：记录此 grid 已有未成交的反向订单
            self._grid_level_locks[new_grid_id] = {
                'tp_side': new_side.value,
                'tp_price': new_price,
                'tp_order_id': placed_order.order_id,
            }
            self.logger.info(
                f"Grid {new_grid_id} locked until {new_side.value}@{new_price} fills"
            )

            #  Lighter专用：链上交易所需要等待，避免nonce冲突和交易拥堵
            # 剧烈波动时多个订单成交，反手单必须串行提交，不能并发
            if self.config.exchange == 'lighter':
                self.logger.debug(
                    "Lighter throttle: waiting 0.5s before the next reverse order"
                )
                await asyncio.sleep(0.5)  # 等待链上确认

            # 8. 更新当前价格
            current_price = await self.engine.get_current_price()
            current_grid_id = self.config.get_grid_index_by_price(
                current_price)
            self.state.update_current_price(current_price, current_grid_id)

            # 9. Check whether scalping mode should activate or exit.
            await self._check_scalping_mode(current_price, current_grid_id)

            # 重置错误计数
            self._error_count = 0

        except Exception as e:
            self.logger.error(f"Order fill handling failed: {e}")
            if fill_committed and not reverse_secured:
                self._fail_stop_for_unmanaged_fill(filled_order, e)
            self._handle_error(e)
        finally:
            self._active_fill_callbacks = max(
                0,
                getattr(self, "_active_fill_callbacks", 1) - 1,
            )

    def _fail_stop_for_unmanaged_reverse(
        self,
        reverse_order: GridOrder,
        error: Exception,
    ) -> None:
        """Keep the reverse intent locked and stop after an unmanaged fill."""
        fatal_reason = (
            "Reverse placement failed after a fill: "
            f"grid_id={reverse_order.grid_id}, side={reverse_order.side.value}, "
            f"amount={reverse_order.amount}, price={reverse_order.price}, error={error}"
        )
        if not getattr(self, "_fatal_stop_reason", None):
            self._fatal_stop_reason = fatal_reason
        self.logger.critical(
            "Reverse placement failed after a fill; stopping to prevent base-grid "
            f"replenishment: grid_id={reverse_order.grid_id}, "
            f"side={reverse_order.side.value}, amount={reverse_order.amount}, "
            f"price={reverse_order.price}, error={error}"
        )
        self._request_fatal_stop(fatal_reason)

    def _fail_stop_for_unmanaged_fill(
        self,
        filled_order: GridOrder,
        error: Exception,
    ) -> None:
        """Lock an accepted fill whose reverse exposure was never secured."""
        locks = getattr(self, "_grid_level_locks", None)
        if locks is None:
            locks = {}
            self._grid_level_locks = locks
        locks.setdefault(
            filled_order.grid_id,
            {
                "tp_side": filled_order.side.value,
                "tp_price": filled_order.price,
                "tp_order_id": None,
                "reason": "unmanaged_fill",
            },
        )
        fatal_reason = (
            "Fill committed without a secured reverse order: "
            f"grid_id={filled_order.grid_id}, side={filled_order.side.value}, "
            f"amount={filled_order.filled_amount or filled_order.amount}, "
            f"price={filled_order.price}, error={error}"
        )
        self.logger.critical(fatal_reason)
        self._request_fatal_stop(fatal_reason)

    def _request_fatal_stop(self, reason: str) -> None:
        """Record a process-visible fatal reason and schedule one safe shutdown."""
        if not getattr(self, "_fatal_stop_reason", None):
            self._fatal_stop_reason = str(reason)
        already_requested = getattr(self, "_emergency_stop_requested", False)
        self._emergency_stop_requested = True
        self._manual_pause_owned = False
        self._paused = True
        pause_placements = getattr(
            getattr(self, "engine", None),
            "pause_placements",
            None,
        )
        if callable(pause_placements):
            try:
                pause_placements()
            except Exception as exc:
                self.logger.error(f"Failed to close engine placement gate: {exc}")
        pause_state = getattr(getattr(self, "state", None), "pause", None)
        if callable(pause_state):
            try:
                pause_state()
            except Exception as exc:
                self.logger.error(f"Failed to pause coordinator state: {exc}")
        if already_requested:
            return
        self._emergency_stop_task = asyncio.create_task(
            self._stop_after_error_threshold()
        )

    @staticmethod
    def _fill_key(filled_order: GridOrder) -> str:
        """Return the stable identity used for fill deduplication and deferral."""
        return (
            f"{filled_order.order_id}:{filled_order.side.value}:"
            f"{filled_order.price}:{filled_order.filled_amount or filled_order.amount}"
        )

    def _defer_fill_during_rest_pause(self, filled_order: GridOrder) -> bool:
        """Retain fills while a recoverable REST or manual pause owns the gate."""
        monitor = getattr(self, "position_monitor", None)
        if (
            not (
                getattr(monitor, "_rest_pause_owned", False)
                or getattr(self, "_manual_pause_owned", False)
            )
            or getattr(self, "_resetting", False)
            or getattr(self, "is_emergency_stopped", False)
        ):
            return False

        deferred_fills = getattr(self, "_deferred_fills", None)
        if deferred_fills is None:
            deferred_fills = {}
            self._deferred_fills = deferred_fills
        fill_key = self._fill_key(filled_order)
        if fill_key not in deferred_fills:
            deferred_fills[fill_key] = filled_order
            self.logger.warning(
                "Deferring fill while REST-owned pause is active: "
                f"grid_id={filled_order.grid_id}, order_id={filled_order.order_id}, "
                f"fill_key={fill_key}"
            )
        return True

    def schedule_deferred_fill_drain(self) -> None:
        """Schedule queued REST-pause fills after the monitor restores service."""
        if (
            not getattr(self, "_deferred_fills", None)
            or not getattr(self, "_running", False)
            or getattr(self, "_paused", False)
            or getattr(self, "_resetting", False)
            or getattr(self, "is_emergency_stopped", False)
        ):
            return

        active_task = getattr(self, "_deferred_fill_drain_task", None)
        if active_task and not active_task.done():
            return
        self._deferred_fill_drain_task = asyncio.create_task(
            self._drain_deferred_fills()
        )

    async def _drain_deferred_fills(self) -> None:
        """Replay each deferred terminal fill once while the runtime stays active."""
        try:
            while (
                getattr(self, "_deferred_fills", None)
                and getattr(self, "_running", False)
                and not getattr(self, "_paused", False)
                and not getattr(self, "_resetting", False)
            ):
                fill_key, filled_order = next(iter(self._deferred_fills.items()))
                del self._deferred_fills[fill_key]
                await self._on_order_filled(filled_order)
        finally:
            self._deferred_fill_drain_task = None

    def _clear_deferred_fills(self) -> None:
        """Discard fills that belong to a stopped or reset runtime generation."""
        deferred_fills = getattr(self, "_deferred_fills", None)
        if deferred_fills is not None:
            deferred_fills.clear()

    async def _on_batch_orders_filled(self, filled_orders: List[GridOrder]):
        """
        批量订单成交处理

        处理价格剧烈波动导致的多订单同时成交

        Args:
            filled_orders: 已成交订单列表
        """
        try:
            if getattr(self, "_emergency_stop_requested", False):
                self.logger.warning("Fatal stop is pending; skipping batch fill handling")
                return

            #  关键检查：防止在重置期间处理订单
            if self._paused:
                deferred_count = sum(
                    1
                    for order in filled_orders
                    if self._defer_fill_during_rest_pause(order)
                )
                if deferred_count == len(filled_orders):
                    return
                self.logger.warning("System is paused; skipping batch fill handling")
                return

            if self._resetting:
                self.logger.warning("System reset in progress; skipping batch fill handling")
                return

            self.logger.info(
                f"Batch fill received: {len(filled_orders)} orders"
            )

            # 1. 批量更新状态和记录
            for order in filled_orders:
                self.state.mark_order_filled(
                    order.order_id,
                    order.filled_price,
                    order.filled_amount or order.amount
                )
                #  记录交易历史（不影响持仓）
                self.tracker.record_filled_order(order)

            # 2. 批量计算反向订单
            reverse_params = self.strategy.calculate_batch_reverse_orders(
                filled_orders,
                self.config.grid_interval,
                self.config.reverse_order_grid_distance
            )

            # 3. 创建反向订单列表
            reverse_orders = []
            for index, (side, price, grid_id, amount) in enumerate(reverse_params):
                order = GridOrder(
                    order_id="",
                    grid_id=grid_id,
                    side=side,
                    price=price,
                    amount=amount,
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now(),
                    parent_order_id=filled_orders[index].order_id,
                )
                reverse_orders.append(order)

            reverse_orders = self.filter_orders_within_max_position(
                reverse_orders,
                "batch reverse orders",
            )

            # 4. 批量下单
            placed_orders = await self.engine.place_batch_orders(reverse_orders)
            if len(placed_orders) != len(reverse_orders):
                raise RuntimeError(
                    "Partial batch reverse placement: "
                    f"submitted={len(placed_orders)}/{len(reverse_orders)}"
                )

            # 5. 批量更新状态
            for order in placed_orders:
                self.state.add_order(order)

            self.logger.info(
                f"Batch reverse placement completed: {len(placed_orders)} orders"
            )

            # 6. 更新当前价格
            current_price = await self.engine.get_current_price()
            current_grid_id = self.config.get_grid_index_by_price(
                current_price)
            self.state.update_current_price(current_price, current_grid_id)

            # 重置错误计数
            self._error_count = 0

        except Exception as e:
            self.logger.error(f"Batch fill handling failed: {e}")
            self._handle_error(e)

    def _handle_error(self, error: Exception):
        """
        处理异常

        策略：
        1. 记录错误
        2. 增加错误计数
        3. 超过阈值则暂停系统

        Args:
            error: 异常对象
        """
        self._error_count += 1

        self.logger.error(
            f"Error count ({self._error_count}/{self._max_errors}): {error}"
        )

        # Repeated order-handling failures leave fills unmanaged, so stop and cancel live orders.
        if self._error_count >= self._max_errors:
            if self._emergency_stop_requested:
                return
            fatal_reason = (
                f"Order handling failed {self._error_count} consecutive times: {error}"
            )
            self.logger.error(
                f"Error threshold exceeded ({self._max_errors}); stopping system and cancelling open orders"
            )
            self._request_fatal_stop(fatal_reason)

    async def _stop_after_error_threshold(self) -> None:
        """Stop after repeated fill errors without losing cleanup exceptions."""
        try:
            await self.stop()
        except Exception as exc:
            self.logger.critical(
                f"UNSAFE STOP after error threshold: {exc}. "
                "Open orders may remain; verify the exchange immediately."
            )

    async def _cleanup_before_start(self):
        """
        启动前清理旧订单和持仓

        目的：
        1. 避免ORDER_LIMIT错误（交易所订单数量上限）
        2. 确保系统从干净状态启动
        3. 避免本地状态与交易所状态不一致

        清理步骤：
        1. 取消所有开放订单
        2. 平掉所有持仓（市价单）
        3. 等待清理生效
        """
        self.logger.info("=" * 80)
        self.logger.info("Pre-start cleanup: removing old orders and positions")
        self.logger.info("=" * 80)

        # 步骤1: 取消所有旧订单
        self.logger.info("Cleanup step 1: cancelling existing open orders")
        existing_orders = await self.engine.exchange.get_open_orders(
            symbol=self.config.symbol
        )

        if existing_orders:
            self.logger.warning(
                f"Detected {len(existing_orders)} existing orders; cancelling them now"
            )
            cancel_errors = []
            for order in existing_orders:
                try:
                    cancel_result = await self.engine.exchange.cancel_order(
                        order_id=order.id,
                        symbol=self.config.symbol
                    )
                    status = getattr(cancel_result, "status", None)
                    status_value = str(
                        getattr(status, "value", status) or "unknown"
                    ).lower()
                    terminal_flags = [
                        payload.get("cancel_terminal")
                        for payload in (
                            getattr(cancel_result, "params", None),
                            getattr(cancel_result, "raw_data", None),
                        )
                        if isinstance(payload, dict)
                        and "cancel_terminal" in payload
                    ]
                    if (
                        status_value not in {"canceled", "cancelled"}
                        or False in terminal_flags
                    ):
                        cancel_errors.append(
                            f"{order.id}: cancellation is not terminal "
                            f"(status={status_value})"
                        )
                except Exception as exc:
                    cancel_errors.append(f"{order.id}: {exc}")

            await asyncio.sleep(2)
            remaining_orders = await self.engine.exchange.get_open_orders(
                symbol=self.config.symbol
            )
            if cancel_errors or remaining_orders:
                details = "; ".join(cancel_errors) or "none"
                raise RuntimeError(
                    "Pre-start order cleanup failed: "
                    f"cancel_errors={details}, remaining={len(remaining_orders)}"
                )
            self.logger.info("All existing orders cleared")
        else:
            self.logger.info("No existing orders found; skipping order cleanup")

        # 步骤2: 平掉所有持仓
        self.logger.info("Cleanup step 2: checking current position")
        positions = await self.engine.exchange.get_positions(
            symbols=[self.config.symbol]
        )

        if positions:
            position = positions[0]
            position_size = abs(Decimal(str(position.size or Decimal('0'))))

            if position_size != 0:
                if position.side == PositionSide.LONG:
                    order_side = OrderSide.SELL
                elif position.side == PositionSide.SHORT:
                    order_side = OrderSide.BUY
                else:
                    raise RuntimeError(
                        f"Unsupported position side for close-out: {position.side}"
                    )

                self.logger.warning(
                    f"Detected open {position.side.value} position: "
                    f"{position_size} {self.config.symbol.split('_')[0]}, "
                    f"entry=${position.entry_price}, "
                    f"unrealized_pnl=${position.unrealized_pnl}"
                )

                ticker = await self.engine.exchange.get_ticker(self.config.symbol)
                placed_order = await self.engine.exchange.create_order(
                    symbol=self.config.symbol,
                    side=order_side,
                    order_type=OrderType.MARKET,
                    amount=position_size,
                    price=ticker.last,
                    params={"reduce_only": True},
                )
                if placed_order is None:
                    raise RuntimeError("Exchange returned no close-out order")
                self.logger.info(
                    f"Close-out order submitted: "
                    f"{getattr(placed_order, 'id', None) or getattr(placed_order, 'order_id', None)}"
                )

                await asyncio.sleep(3)
                new_positions = await self.engine.exchange.get_positions(
                    symbols=[self.config.symbol]
                )
                remaining_size = max(
                    (abs(Decimal(str(item.size or Decimal('0')))) for item in new_positions),
                    default=Decimal('0'),
                )
                if remaining_size != 0:
                    raise RuntimeError(
                        f"Position not fully closed; remaining={remaining_size}"
                    )
                self.logger.info("Position fully closed")
            else:
                self.logger.info("No open position; skipping close-out")
        else:
            self.logger.info("No open position; skipping close-out")

        self.logger.info("=" * 80)
        self.logger.info("Pre-start cleanup completed")
        self.logger.info("=" * 80)
        self.logger.info("")  # 空行分隔

    async def start(self):
        """Start the grid runtime."""
        if self._running:
            self.logger.warning("Grid system is already running")
            return

        # 🆕 启动前清理旧订单和持仓
        self._clear_deferred_fills()
        self._manual_pause_owned = False
        self._paused = False
        self._emergency_stop_requested = False
        self._shutdown_completed = False
        self._shutdown_order_cancellation_failed = False
        self._shutdown_cleanup_error = None
        self._unsafe_shutdown_incident = None
        self._fatal_stop_reason = None
        self._grid_level_locks.clear()

        await self._cleanup_before_start()

        await self.initialize()
        if not self.engine.is_running():
            await self.engine.start()

        #  主动同步初始持仓到WebSocket缓存
        # Backpack的WebSocket只在持仓变化时推送，不会推送初始状态
        # 所以我们需要在启动时主动获取一次
        position_data = {'size': Decimal('0'), 'entry_price': Decimal(
            '0'), 'unrealized_pnl': Decimal('0')}
        try:
            self.logger.info("Syncing initial position snapshot")
            position_data = await self.engine.get_real_time_position(self.config.symbol)

            # 如果WebSocket缓存为空，使用REST API获取并同步
            if position_data['size'] == 0 and position_data['entry_price'] == 0:
                positions = await self.engine.exchange.get_positions(symbols=[self.config.symbol])
                if positions and len(positions) > 0:
                    position = positions[0]
                    real_size = abs(position.size or Decimal('0'))
                    real_entry_price = position.entry_price or Decimal('0')

                    if position.side == PositionSide.SHORT:
                        real_size = -real_size
                    elif position.side != PositionSide.LONG and real_size != 0:
                        raise RuntimeError(
                            f"Unsupported position side in startup snapshot: {position.side}"
                        )

                    # 同步到WebSocket缓存
                    if hasattr(self.engine.exchange, '_position_cache'):
                        self.engine.exchange._position_cache[self.config.symbol] = {
                            'size': real_size,
                            'entry_price': real_entry_price,
                            'unrealized_pnl': position.unrealized_pnl or Decimal('0'),
                            'side': position.side.value,
                            'timestamp': datetime.now()
                        }
                        self.logger.info(
                            f"Seeded websocket position cache from startup snapshot: "
                            f"{real_size} {self.config.symbol.split('_')[0]}, "
                            f"entry=${real_entry_price:,.2f}"
                        )
                        # 更新position_data供后续使用
                        position_data = {
                            'size': real_size,
                            'entry_price': real_entry_price,
                            'unrealized_pnl': position.unrealized_pnl or Decimal('0')
                        }
            else:
                # WebSocket缓存已有数据
                self.logger.info(
                    f"Using existing websocket position cache: "
                    f"{position_data['size']} {self.config.symbol.split('_')[0]}, "
                    f"entry=${position_data['entry_price']:,.2f}"
                )
        except Exception as e:
            self.logger.warning(f"Initial position sync failed but startup will continue: {e}")

        # Check whether scalping mode should activate immediately at startup.
        # 如果启动时已有持仓，且价格已在触发阈值以下，立即激活
        if self.config.is_scalping_enabled():
            try:
                current_price = await self.engine.get_current_price()
                current_grid_id = self.config.get_grid_index_by_price(
                    current_price)

                # 更新scalping_manager的持仓信息
                if position_data['size'] != 0:
                    symbol_snapshot = self.get_symbol_isolated_snapshot(
                        current_price=current_price
                    )
                    self.scalping_manager.update_position(
                        position_data['size'],
                        position_data['entry_price'],
                        symbol_snapshot["initial_capital"],
                        symbol_snapshot["current_equity"],
                    )

                # Check whether scalping mode should activate (requires current_price and current_grid_id).
                if self.scalping_manager.should_trigger(current_price, current_grid_id):
                    self.logger.info(
                        f"Startup price is already inside the scalping trigger zone "
                        f"(Grid {current_grid_id} <= Grid {self.config.get_scalping_trigger_grid()}); "
                        f"activating scalping mode immediately"
                    )
                    #  使用新模块
                    if self.scalping_ops:
                        await self.scalping_ops.activate()
                else:
                    self.logger.info(
                        f"Scalping mode idle until trigger "
                        f"(current_grid={current_grid_id}, "
                        f"trigger_grid={self.config.get_scalping_trigger_grid()})"
                    )
            except Exception as e:
                self.logger.warning(f"Failed to evaluate scalping mode on startup: {e}")
                import traceback
                self.logger.error(traceback.format_exc())

        # Follow-grid mode: start the price-escape monitor.
        if self.config.is_follow_mode():
            asyncio.create_task(self._price_escape_monitor())
            self.logger.info("Price escape monitor started")

        if self.config.is_stop_loss_enabled():
            self._stop_loss_triggered = False
            self._stop_loss_monitor_task = asyncio.create_task(
                self._stop_loss_monitor()
            )
            self.logger.info(
                f"Stop-loss monitor started at ${self.config.stop_loss_price:,.4f}"
            )

        #  启动余额轮询监控（使用新模块 BalanceMonitor）
        await self.balance_monitor.start_monitoring()

        self.logger.info("Grid system started")

    async def pause(self):
        """暂停网格系统（保留挂单）"""
        self._manual_pause_owned = True
        self._refresh_recoverable_pause_state()

        self.logger.info("Grid system paused")

    async def resume(self):
        """恢复网格系统"""
        self._manual_pause_owned = False
        still_paused = self._refresh_recoverable_pause_state()
        self._error_count = 0  # 重置错误计数
        if not still_paused:
            self.schedule_deferred_fill_drain()

        if still_paused:
            self.logger.warning(
                "Manual pause released, but a REST or emergency pause remains active"
            )
        else:
            self.logger.info("Grid system resumed")

    def _refresh_recoverable_pause_state(self) -> bool:
        """Apply independent manual, REST-outage, and emergency pause ownership."""
        monitor = getattr(self, "position_monitor", None)
        should_pause = bool(
            getattr(self, "_manual_pause_owned", False)
            or getattr(monitor, "_rest_pause_owned", False)
            or getattr(self, "_emergency_stop_requested", False)
            or getattr(self, "is_emergency_stopped", False)
        )
        self._paused = should_pause
        state_method = getattr(
            self.state,
            "pause" if should_pause else "resume",
            None,
        )
        if callable(state_method):
            state_method()
        return should_pause

    async def stop(self):
        """Stop once, while allowing a later retry after failed cleanup."""
        if not hasattr(self, "_stop_lock"):
            self._stop_lock = asyncio.Lock()

        async with self._stop_lock:
            if getattr(self, "_shutdown_completed", False):
                return
            await self._stop_once()

    async def _stop_once(self):
        """停止网格系统（取消所有挂单）"""
        self._running = False
        self._paused = True  #  修复：设为 True 阻止 shutdown 期间的订单回调
        self._manual_pause_owned = False
        self._clear_deferred_fills()

        #  停止余额监控（使用新模块）
        if hasattr(self.engine, 'begin_shutdown'):
            self.engine.begin_shutdown()

        current_task = asyncio.current_task()
        if (
            self._stop_loss_monitor_task
            and self._stop_loss_monitor_task is not current_task
            and not self._stop_loss_monitor_task.done()
        ):
            self._stop_loss_monitor_task.cancel()
            try:
                await self._stop_loss_monitor_task
            except asyncio.CancelledError:
                pass
        if self._stop_loss_monitor_task is not current_task:
            self._stop_loss_monitor_task = None

        shutdown_errors = []
        try:
            await self.balance_monitor.stop_monitoring()
        except Exception as exc:
            shutdown_errors.append(f"balance monitor: {exc}")
            self.logger.error(f"Failed to stop balance monitor: {exc}")

        #  停止持仓同步监控（使用新模块）
        try:
            await self.position_monitor.stop_monitoring()
        except Exception as exc:
            shutdown_errors.append(f"position monitor: {exc}")
            self.logger.error(f"Failed to stop position monitor: {exc}")

        # Stop health checks and polling before shutdown cancellation starts.
        try:
            await self.engine.stop()
        except Exception as exc:
            shutdown_errors.append(f"engine stop: {exc}")
            self.logger.error(f"Failed to stop grid engine: {exc}")

        try:
            # 取消所有挂单；保留持仓是原有的 Ctrl+C 语意
            cancelled_count = await self._cancel_all_orders_with_retry()
            self.logger.info(f"Cancelled {cancelled_count} open orders")
        except Exception as exc:
            shutdown_errors.append(f"order cancellation: {exc}")
            self.logger.error(f"Failed to verify open-order cancellation: {exc}")

        try:
            self.state.stop()
        except Exception as exc:
            shutdown_errors.append(f"state stop: {exc}")
            self.logger.error(f"Failed to stop grid state: {exc}")

        if shutdown_errors:
            self._shutdown_cleanup_error = "; ".join(shutdown_errors)
            if not getattr(self, "_unsafe_shutdown_incident", None):
                self._unsafe_shutdown_incident = self._shutdown_cleanup_error
            self.logger.critical(
                "UNSAFE STOP: cleanup did not complete. Open orders may remain; "
                f"verify the exchange immediately. Details: {self._shutdown_cleanup_error}"
            )
            raise RuntimeError(
                "Grid stopped with cleanup errors: " + "; ".join(shutdown_errors)
            )

        self._shutdown_cleanup_error = None
        self._shutdown_completed = True
        if getattr(self, "_unsafe_shutdown_incident", None):
            self.logger.warning(
                "Grid system is now stopped safely, but an earlier unsafe cleanup "
                "incident remains recorded until the next start"
            )
        else:
            self.logger.info("Grid system stopped safely")

    async def _cancel_all_orders_with_retry(
        self,
        max_attempts: int = 5,
        base_delay: float = 1.0,
    ) -> int:
        """Cancel and verify open orders with bounded exponential backoff."""
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                cancelled_count = await self.engine.cancel_all_orders()
                self._shutdown_order_cancellation_failed = False
                return cancelled_count
            except Exception as exc:
                last_error = exc
                if attempt == max_attempts:
                    break

                retry_delay = min(8.0, base_delay * (2 ** (attempt - 1)))
                self.logger.warning(
                    f"Cancel-all attempt {attempt}/{max_attempts} failed: {exc}; "
                    f"retrying in {retry_delay:.1f}s"
                )
                await asyncio.sleep(retry_delay)

        self._shutdown_order_cancellation_failed = True
        self.logger.critical(
            f"Cancel-all failed after {max_attempts} attempts: {last_error}. "
            "Open orders may remain; verify the exchange immediately."
        )
        raise RuntimeError(
            f"cancel-all failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    async def get_statistics(self) -> GridStatistics:
        """
        Return statistics, preferring websocket-backed live position data when valid.

        Returns:
            网格统计数据
        """
        # 更新当前价格
        try:
            current_price = await self.engine.get_current_price()
            current_grid_id = self.config.get_grid_index_by_price(
                current_price)
            self.state.update_current_price(current_price, current_grid_id)
        except Exception as e:
            self.logger.warning(f"Failed to fetch current price: {e}")

        #  同步engine的最新订单统计到state
        self._sync_orders_from_engine()

        # Get the local tracker statistics snapshot.
        stats = self.tracker.get_statistics()
        tracker_position = stats.current_position
        tracker_average_cost = stats.average_cost
        state_position = getattr(self.state, "current_position", Decimal("0"))
        state_average_cost = getattr(self.state, "average_cost", Decimal("0"))

        def has_meaningful_position(position: Decimal, average_cost: Decimal) -> bool:
            return position != 0 or average_cost > 0

        selected_position = tracker_position
        selected_average_cost = tracker_average_cost
        if has_meaningful_position(tracker_position, tracker_average_cost):
            stats.position_data_source = "PositionTracker"
        elif has_meaningful_position(state_position, state_average_cost):
            selected_position = state_position
            selected_average_cost = state_average_cost
            stats.position_data_source = "State snapshot"
        else:
            stats.position_data_source = "REST API"

        #  优先使用WebSocket缓存的真实持仓数据（但需要检查WebSocket是否可用）
        # 注意：只有在WebSocket缓存有效且WebSocket监控正常时才使用缓存
        try:
            position_data = await self.engine.get_real_time_position(self.config.symbol)
            ws_position = position_data['size']
            ws_entry_price = position_data['entry_price']
            has_cache = position_data.get('has_cache', False)
            ws_has_meaningful_position = has_meaningful_position(ws_position, ws_entry_price)
            local_has_meaningful_position = has_meaningful_position(
                selected_position, selected_average_cost
            )

            #  关键修复：只有在WebSocket启用且缓存有效时才使用WebSocket缓存
            # 如果WebSocket已失效（切换到REST备用模式），则使用PositionTracker数据
            if has_cache and self._position_ws_enabled:
                if ws_has_meaningful_position or not local_has_meaningful_position:
                    selected_position = ws_position
                    selected_average_cost = ws_entry_price
                    stats.position_data_source = "WebSocket cache"

                    self.logger.debug(
                        f"Using websocket position cache: size={ws_position}, entry=${ws_entry_price}"
                    )
                else:
                    self.logger.debug(
                        "Ignoring empty websocket position cache because tracker/state "
                        f"already has size={selected_position}, entry=${selected_average_cost}"
                    )
            else:
                # WebSocket失效或缓存无效，使用PositionTracker的数据
                if self._position_ws_enabled:
                    if stats.position_data_source == "PositionTracker":
                        stats.position_data_source = "WebSocket callback -> PositionTracker"
                elif stats.position_data_source == "PositionTracker":
                    stats.position_data_source = "REST API fallback -> PositionTracker"

                self.logger.debug(
                    f"Using local position snapshot: size={selected_position}, "
                    f"entry=${selected_average_cost}, source={stats.position_data_source} "
                    f"(ws_enabled={self._position_ws_enabled}, cache={has_cache})"
                )
        except Exception as e:
            self.logger.debug(
                f"Failed to read websocket position; using local position snapshot: {e}"
            )

        stats.current_position = selected_position
        stats.average_cost = selected_average_cost
        if selected_position != 0 and selected_average_cost > 0 and current_price > 0:
            stats.unrealized_profit = selected_position * (current_price - selected_average_cost)
        else:
            stats.unrealized_profit = Decimal('0')

        if hasattr(self.position_monitor, "get_last_liquidation_price"):
            stats.liquidation_price = self.position_monitor.get_last_liquidation_price()

        #  添加监控方式信息
        stats.monitoring_mode = self.engine.get_monitoring_mode()

        #  使用真实的账户余额（从 BalanceMonitor 获取）
        balances = self.balance_monitor.get_balances()
        stats.spot_balance = balances['spot_balance']
        stats.collateral_balance = balances['collateral_balance']
        stats.order_locked_balance = balances['order_locked_balance']
        stats.total_balance = balances['total_balance']

        #  初始本金和盈亏（始终设置，无论是否启用本金保护）
        symbol_snapshot = self.get_symbol_isolated_snapshot(current_price=current_price)
        stats.initial_capital = symbol_snapshot['initial_capital']
        stats.strategy_equity = symbol_snapshot['current_equity']
        stats.capital_profit_loss = symbol_snapshot['net_profit']

        stats.total_profit = stats.realized_profit + stats.unrealized_profit
        stats.net_profit = stats.total_profit - stats.total_fees
        stats.profit_rate = symbol_snapshot['profit_rate']

        # ️ 本金保护模式状态
        if self.capital_protection_manager:
            stats.capital_protection_enabled = True
            stats.capital_protection_active = self.capital_protection_manager.is_active()

        # Follow-grid price-escape monitor status.
        if self.config.is_follow_mode() and self._price_escape_start_time is not None:
            import time
            escape_duration = int(time.time() - self._price_escape_start_time)
            stats.price_escape_active = True
            stats.price_escape_duration = escape_duration
            stats.price_escape_timeout = self.config.follow_timeout
            stats.price_escape_remaining = max(
                0, self.config.follow_timeout - escape_duration)

            # 判断脱离方向
            if current_price < self.config.lower_price:
                stats.price_escape_direction = "down"
            elif current_price > self.config.upper_price:
                stats.price_escape_direction = "up"

        #  止盈模式状态
        if self.take_profit_manager:
            stats.take_profit_enabled = True
            stats.take_profit_active = self.take_profit_manager.is_active()
            stats.take_profit_initial_capital = self.take_profit_manager.get_initial_capital()
            stats.take_profit_current_profit = self.take_profit_manager.get_profit_amount(
                symbol_snapshot['current_equity'])
            stats.take_profit_profit_rate = self.take_profit_manager.get_profit_percentage(
                symbol_snapshot['current_equity'])
            stats.take_profit_threshold = self.config.take_profit_percentage * 100  # 转为百分比

        #  价格锁定模式状态
        if self.price_lock_manager:
            stats.price_lock_enabled = True
            stats.price_lock_active = self.price_lock_manager.is_locked()
            stats.price_lock_threshold = self.config.price_lock_threshold

        if self.config.is_stop_loss_enabled():
            stats.stop_loss_enabled = True
            stats.stop_loss_triggered = self._stop_loss_triggered
            stats.stop_loss_price = self.config.stop_loss_price
            stats.stop_loss_trigger_count = self._stop_loss_trigger_count

        # 🆕 触发次数统计（仅标记）
        stats.scalping_trigger_count = self._scalping_trigger_count
        stats.price_escape_trigger_count = self._price_escape_trigger_count
        stats.take_profit_trigger_count = self._take_profit_trigger_count
        stats.capital_protection_trigger_count = self._capital_protection_trigger_count

        return stats

    def get_state(self) -> GridState:
        """获取网格状态"""
        return self.state

    def _strategy_opening_side(self) -> Optional[GridOrderSide]:
        """Return the side that increases exposure for the configured grid."""
        if self.config.is_long():
            return GridOrderSide.BUY
        if self.config.is_short():
            return GridOrderSide.SELL
        return None

    def can_place_order_within_max_position(
        self,
        side: GridOrderSide,
        amount: Decimal,
        additional_open_amount: Decimal = Decimal("0"),
    ) -> bool:
        """Check worst-case strategy exposure including pending entry orders."""
        max_position = getattr(self.config, "max_position", None)
        if max_position is None:
            return True

        opening_side = self._strategy_opening_side()
        if opening_side is None or side != opening_side:
            return True

        current_position = Decimal(str(self.tracker.get_current_position()))
        pending_open_amount = sum(
            (
                Decimal(str(order.amount))
                for order in self.engine.get_pending_orders()
                if order.side == opening_side
            ),
            Decimal("0"),
        )
        opening_sign = Decimal("1") if opening_side == GridOrderSide.BUY else Decimal("-1")
        projected_position = current_position + opening_sign * (
            pending_open_amount
            + Decimal(str(additional_open_amount))
            + Decimal(str(amount))
        )
        within_limit = opening_sign * projected_position <= Decimal(str(max_position))

        if not within_limit:
            self.logger.warning(
                f"max_position blocked {side.value} {amount}: "
                f"current={current_position}, pending_entries={pending_open_amount}, "
                f"additional_entries={additional_open_amount}, "
                f"projected={projected_position}, limit={max_position}"
            )
        return within_limit

    def filter_orders_within_max_position(
        self,
        orders: List[GridOrder],
        context: str,
    ) -> List[GridOrder]:
        """Keep a batch within max_position while always allowing closing orders."""
        if getattr(self.config, "max_position", None) is None:
            return orders

        opening_side = self._strategy_opening_side()
        reserved_open_amount = Decimal("0")
        accepted_orders: List[GridOrder] = []

        for order in orders:
            if self.can_place_order_within_max_position(
                order.side,
                order.amount,
                additional_open_amount=reserved_open_amount,
            ):
                accepted_orders.append(order)
                if order.side == opening_side:
                    reserved_open_amount += Decimal(str(order.amount))

        rejected_count = len(orders) - len(accepted_orders)
        if rejected_count:
            self.logger.warning(
                f"max_position filtered {rejected_count}/{len(orders)} {context}"
            )
        return accepted_orders

    def is_running(self) -> bool:
        """Return whether the runtime is currently running."""
        return self._running and not self._paused

    @property
    def is_paused(self) -> bool:
        """Return whether the runtime is currently paused."""
        return self._paused

    @is_paused.setter
    def is_paused(self, value: bool) -> None:
        self._paused = bool(value)

    def is_stopped(self) -> bool:
        """Return whether the runtime has stopped."""
        return not self._running

    def get_status_text(self) -> str:
        """Return a short human-readable runtime status."""
        if getattr(self, "_shutdown_cleanup_error", None) or getattr(
            self,
            "_shutdown_order_cancellation_failed",
            False,
        ):
            return "UNSAFE STOP: CLEANUP FAILED"
        if not self._running:
            if getattr(self, "_unsafe_shutdown_incident", None):
                return "Stopped (unsafe cleanup incident recovered)"
            return "Stopped"
        if self._paused:
            return "Paused"
        return "Running"

    def get_unsafe_shutdown_incident(self) -> Optional[str]:
        """Return the sticky cleanup incident recorded during this run, if any."""
        return getattr(self, "_unsafe_shutdown_incident", None)

    def get_fatal_stop_reason(self) -> Optional[str]:
        """Return why the strategy stopped itself after a runtime safety failure."""
        return getattr(self, "_fatal_stop_reason", None)

    async def _scalping_position_monitor_loop(self):
        """
        [Deprecated] Scalping-mode position monitor loop (REST polling).

        ️ 此方法已被WebSocket事件驱动方式取代，保留仅作备份
        现在使用 _on_position_update_from_ws() 实时处理持仓更新
        """
        self.logger.warning(
            "Deprecated REST polling monitor is active; websocket event handling should be used instead"
        )
        self.logger.info("Scalping position monitor loop started")

        last_position = Decimal('0')
        last_entry_price = Decimal('0')

        try:
            while self.scalping_manager and self.scalping_manager.is_active():
                try:
                    # 从API获取实时持仓
                    position_data = await self.engine.get_real_time_position(self.config.symbol)
                    current_position = position_data['size']
                    current_entry_price = position_data['entry_price']

                    # 检查是否有变化
                    position_changed = (
                        current_position != last_position or
                        current_entry_price != last_entry_price
                    )

                    if position_changed:
                        self.logger.info(
                            f"Position change detected: "
                            f"size {last_position} -> {current_position}, "
                            f"entry ${last_entry_price:,.2f} -> ${current_entry_price:,.2f}"
                        )

                        # Update the scalping manager with the latest position.
                        symbol_snapshot = self.get_symbol_isolated_snapshot(
                            current_price=current_entry_price
                        )
                        self.scalping_manager.update_position(
                            current_position,
                            current_entry_price,
                            symbol_snapshot["initial_capital"],
                            symbol_snapshot["current_equity"],
                        )

                        # Update the take-profit order.
                        await self._update_take_profit_order_after_position_change(
                            current_position,
                            current_entry_price
                        )

                        # 更新记录
                        last_position = current_position
                        last_entry_price = current_entry_price

                    # 等待下次检查
                    await asyncio.sleep(self._scalping_position_check_interval)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger.error(f"Position monitor error: {e}")
                    await asyncio.sleep(self._scalping_position_check_interval)

        except asyncio.CancelledError:
            self.logger.info("Scalping position monitor loop cancelled")
        except Exception as e:
            self.logger.error(f"Scalping position monitor loop failed: {e}")
        finally:
            self.logger.info("Scalping position monitor loop ended")

    async def _update_take_profit_order_after_position_change(
        self,
        new_position: Decimal,
        new_entry_price: Decimal
    ):
        """
        Update the take-profit order after position changes.

        Args:
            new_position: 新的持仓数量
            new_entry_price: 新的平均成本价
        """
        if new_position == 0:
            # Position returned to zero; cancel the take-profit order.
            if self.scalping_manager.get_current_take_profit_order():
                tp_order = self.scalping_manager.get_current_take_profit_order()
                try:
                    cancelled = await self.engine.cancel_order(tp_order.order_id)
                    if cancelled is not True:
                        self.logger.error(
                            "Take-profit cancellation was not confirmed; "
                            "retaining local state"
                        )
                        return
                    self.state.remove_order(tp_order.order_id)
                    self.logger.info("Position returned to zero; take-profit order cancelled")
                except Exception as e:
                    self.logger.error(f"Failed to cancel take-profit order: {e}")
            return

        # Cancel the previous take-profit order.
        old_tp_order = self.scalping_manager.get_current_take_profit_order()
        if old_tp_order:
            try:
                cancelled = await self.engine.cancel_order(old_tp_order.order_id)
                if cancelled is not True:
                    self.logger.error(
                        "Previous take-profit cancellation was not confirmed; "
                        "replacement suppressed"
                    )
                    return
                self.state.remove_order(old_tp_order.order_id)
                self.logger.info(
                    f"Cancelled previous take-profit order: {old_tp_order.order_id}"
                )
            except Exception as e:
                self.logger.error(f"Failed to cancel previous take-profit order: {e}")
                return

        # Place the new take-profit order.
        await self._place_take_profit_order()
        self.logger.info("Take-profit order updated")

    async def _on_position_update_from_ws(self, position_info: Dict[str, Any]) -> None:
        """
        WebSocket持仓更新回调（事件驱动，实时响应）

        当WebSocket收到持仓更新推送时自动调用
        """
        try:
            # Process only while scalping mode is active.
            if not self.scalping_manager or not self.scalping_manager.is_active():
                return

            # 只处理当前交易对的持仓
            if position_info.get('symbol') != self.config.symbol:
                return

            current_position = position_info.get('size', Decimal('0'))
            entry_price = position_info.get('entry_price', Decimal('0'))

            # 检查是否有变化
            position_changed = (
                current_position != self._last_ws_position_size or
                entry_price != self._last_ws_position_price
            )

            if position_changed:
                self.logger.info(
                    f"Websocket position changed: "
                    f"size {self._last_ws_position_size} -> {current_position}, "
                    f"entry ${self._last_ws_position_price:,.2f} -> ${entry_price:,.2f}"
                )

                # Update the scalping manager.
                symbol_snapshot = self.get_symbol_isolated_snapshot(
                    current_price=entry_price
                )
                self.scalping_manager.update_position(
                    current_position,
                    entry_price,
                    symbol_snapshot["initial_capital"],
                    symbol_snapshot["current_equity"],
                )

                # Update the take-profit order.
                await self._update_take_profit_order_after_position_change(
                    current_position,
                    entry_price
                )

                # 更新记录
                self._last_ws_position_size = current_position
                self._last_ws_position_price = entry_price

        except Exception as e:
            self.logger.error(f"Failed to handle websocket position update: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

    def __repr__(self) -> str:
        return (
            f"GridCoordinator("
            f"status={self.get_status_text()}, "
            f"position={self.tracker.get_current_position()}, "
            f"errors={self._error_count})"
        )

    # ==================== 价格移动网格专用方法 ====================

    async def _stop_loss_monitor(self):
        """Monitor current price and execute a configured stop loss once."""
        self.logger.info("Stop-loss monitor loop started")

        while self._running:
            try:
                if self._stop_loss_triggered:
                    await asyncio.sleep(self.config.stop_loss_check_interval)
                    continue

                if self._resetting or self._is_resetting:
                    await asyncio.sleep(self.config.stop_loss_check_interval)
                    continue

                current_price = await self.engine.get_current_price()
                if self.config.check_stop_loss(current_price):
                    self._stop_loss_triggered = True
                    self.logger.warning(
                        f"Stop loss condition met: current=${current_price:,.4f}, "
                        f"threshold=${self.config.stop_loss_price:,.4f}"
                    )
                    shutdown_complete = (
                        await self.reset_manager.execute_stop_loss_shutdown(current_price)
                    )
                    if shutdown_complete or not self._running:
                        break

                    # A concurrent reset may have won the reset gate between the
                    # price read and shutdown attempt. Re-arm the monitor afterward.
                    self._stop_loss_triggered = False

                await asyncio.sleep(self.config.stop_loss_check_interval)

            except asyncio.CancelledError:
                self.logger.info("Stop-loss monitor stopped")
                break
            except Exception as e:
                self.logger.error(f"Stop-loss monitor error: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
                await asyncio.sleep(max(1, self.config.stop_loss_check_interval))

    async def _price_escape_monitor(self):
        """
        Price-escape monitor (follow-grid only).

        定期检查价格是否脱离网格范围，如果脱离时间超过阈值则重置网格
        """
        import time

        self.logger.info("Price escape monitor loop started")

        while self._running and not self._paused:
            try:
                current_time = time.time()

                # 检查间隔
                if current_time - self._last_escape_check_time < self._escape_check_interval:
                    await asyncio.sleep(1)
                    continue

                self._last_escape_check_time = current_time

                # 获取当前价格
                current_price = await self.engine.get_current_price()

                # 检查是否脱离
                should_reset, direction = self.config.check_price_escape(
                    current_price)

                if should_reset:
                    # 记录脱离开始时间
                    if self._price_escape_start_time is None:
                        self._price_escape_start_time = current_time
                        self.logger.warning(
                            f"Price escaped the grid range ({direction}): "
                            f"current=${current_price:,.2f}, "
                            f"grid=[${self.config.lower_price:,.2f}, ${self.config.upper_price:,.2f}]"
                        )

                    # 检查脱离时间是否超过阈值
                    escape_duration = current_time - self._price_escape_start_time

                    if escape_duration >= self.config.follow_timeout:
                        self.logger.warning(
                            f"Price escape timeout reached ({escape_duration:.0f}s >= "
                            f"{self.config.follow_timeout}s); resetting grid"
                        )
                        #  使用新模块
                        await self.reset_manager.execute_price_follow_reset(current_price, direction)
                        self._price_escape_start_time = None
                    else:
                        self.logger.info(
                            f"Price escape still active ({direction}); "
                            f"elapsed {escape_duration:.0f}/{self.config.follow_timeout}s"
                        )
                else:
                    # 价格回到范围内，重置脱离计时
                    if self._price_escape_start_time is not None:
                        self.logger.info(
                            f"Price returned to grid range: ${current_price:,.2f}"
                        )
                        self._price_escape_start_time = None

                    #  检查是否需要解除价格锁定
                    if self.price_lock_manager and self.price_lock_manager.is_locked():
                        if self.price_lock_manager.check_unlock_condition(
                            current_price,
                            self.config.lower_price,
                            self.config.upper_price
                        ):
                            self.price_lock_manager.deactivate_lock()
                            self.logger.info("Price lock released; resuming normal grid trading")

                await asyncio.sleep(1)

            except asyncio.CancelledError:
                self.logger.info("Price escape monitor stopped")
                break
            except Exception as e:
                self.logger.error(f"Price escape monitor error: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
                await asyncio.sleep(10)  # 出错后等待10秒再继续

    async def _check_scalping_mode(self, current_price: Decimal, current_grid_index: int):
        """
        Check whether to enter or exit scalping mode.

        Args:
            current_price: 当前价格
            current_grid_index: 当前网格索引
        """
        if not self.scalping_manager or not self.scalping_ops:
            return

        # Check whether scalping mode should activate (new helper path).
        if self.scalping_manager.should_trigger(current_price, current_grid_index):
            await self.scalping_ops.activate()

        # Check whether scalping mode should deactivate (new helper path).
        elif self.scalping_manager.should_exit(current_price, current_grid_index):
            await self.scalping_ops.deactivate()

    async def _check_capital_protection_mode(self, current_price: Decimal, current_grid_index: int):
        """
        检查是否触发本金保护模式

        Args:
            current_price: 当前价格
            current_grid_index: 当前网格索引
        """
        if not self.capital_protection_manager:
            return

        # 如果已经触发，检查是否回本
        if self.capital_protection_manager.is_active():
            # 检查抵押品是否回本
            symbol_snapshot = self.get_symbol_isolated_snapshot(
                current_price=current_price
            )
            if self.capital_protection_manager.check_capital_recovery(
                symbol_snapshot["current_equity"]
            ):
                self.logger.warning(
                    "Capital protection target recovered; resetting grid"
                )
                #  使用新模块
                await self.reset_manager.execute_capital_protection_reset()
        else:
            # 检查是否应该触发
            if self.capital_protection_manager.should_trigger(current_price, current_grid_index):
                self.capital_protection_manager.activate()
                self.logger.warning(
                    f"Capital protection activated; waiting for recovery. "
                    f"initial_capital=${self.capital_protection_manager.get_initial_capital():,.2f}"
                )

    async def _reset_fixed_range_grid(self, new_capital: Optional[Decimal] = None):
        """重置固定范围网格（保持原有范围）

        Args:
            new_capital: 新的初始本金（止盈后使用）
        """
        try:
            self.logger.info("Resetting fixed-range grid while keeping the price band")
            self._clear_deferred_fills()

            # 重置所有管理器状态
            if self.scalping_manager:
                self.scalping_manager.reset()
            if self.capital_protection_manager:
                self.capital_protection_manager.reset()
            if self.take_profit_manager:
                self.take_profit_manager.reset()

            # 重置追踪器和状态
            self.tracker.reset()
            self.state.active_orders.clear()  # 清空所有活跃订单
            self.state.pending_buy_orders = 0
            self.state.pending_sell_orders = 0
            self._grid_level_locks.clear()

            # 重新初始化网格层级（保持原有价格区间）
            self.state.initialize_grid_levels(
                self.config.grid_count,
                self.config.get_grid_price
            )

            # 生成并挂出新订单（使用原有价格范围，传入市价过滤 taker）
            current_price = await self.engine.get_current_price()
            self.logger.info(
                f"Reinitializing fixed-range grid and placing orders across "
                f"${self.config.lower_price:,.2f} - ${self.config.upper_price:,.2f}, "
                f"market=${current_price:,.2f}"
            )
            initial_orders = self.strategy.initialize(self.config, current_price)
            self.logger.info(f"Generated {len(initial_orders)} initial orders")

            if hasattr(self.engine, "suspend_health_repairs"):
                self.engine.suspend_health_repairs("fixed-range grid reset placement")

            placed_orders = await self.engine.place_batch_orders(initial_orders)
            self.logger.info(f"Placed {len(placed_orders)} reset orders")

            #  关键修复：等待WebSocket处理立即成交的订单
            await asyncio.sleep(2)

            # 添加到状态追踪（只添加未成交的订单）
            added_count = 0
            skipped_filled = 0
            skipped_exists = 0

            try:
                # 获取当前实际挂单（从引擎）
                engine_pending_orders = self.engine.get_pending_orders()
                engine_pending_ids = {
                    order.order_id for order in engine_pending_orders}

                for order in placed_orders:
                    if order.order_id in self.state.active_orders:
                        skipped_exists += 1
                        continue
                    #  关键：检查订单是否真的还在挂单中
                    if order.order_id not in engine_pending_ids:
                        self.logger.debug(
                            f"Order {order.order_id} already filled or cancelled; skipping state add"
                        )
                        skipped_filled += 1
                        continue
                    self.state.add_order(order)
                    added_count += 1
            except Exception as e:
                self.logger.warning(
                    f"Could not fetch pending orders from engine; falling back to order status: {e}"
                )
                # Fallback：使用订单自身的状态
                for order in placed_orders:
                    if order.order_id in self.state.active_orders:
                        skipped_exists += 1
                        continue
                    if order.status == GridOrderStatus.FILLED:
                        self.logger.debug(
                            f"Order {order.order_id} filled immediately; skipping state add"
                        )
                        skipped_filled += 1
                        continue
                    self.state.add_order(order)
                    added_count += 1

            buy_count = len(
                [o for o in self.state.active_orders.values() if o.side == GridOrderSide.BUY])
            sell_count = len(
                [o for o in self.state.active_orders.values() if o.side == GridOrderSide.SELL])
            self.logger.info(
                f"Reset state add summary: "
                f"added={added_count}, "
                f"skipped_filled={skipped_filled}, "
                f"skipped_existing={skipped_exists}"
            )
            self.logger.info(
                f"Reset state summary: "
                f"buy_orders={buy_count}, "
                f"sell_orders={sell_count}, "
                f"active_orders={len(self.state.active_orders)}"
            )
            if hasattr(self.engine, "resume_health_repairs"):
                self.engine.resume_health_repairs("fixed-range grid reset placement")

            #  重新初始化本金（止盈后）
            if new_capital is not None:
                isolated_capital = self.ensure_symbol_isolated_capital(
                    current_price=current_price,
                    is_reinit=True,
                )
                self.logger.info(f"Capital reinitialized: ${isolated_capital:,.3f}")

            self.logger.info("Fixed-range grid reset completed")

        except Exception as e:
            self.logger.error(f"Fixed-range grid reset failed: {e}")
            if hasattr(self.engine, "resume_health_repairs"):
                self.engine.resume_health_repairs("fixed-range grid reset placement")
            raise

    def _is_spot_mode(self) -> bool:
        """Return whether the runtime is operating in spot mode."""
        try:
            from ....adapters.exchanges.interface import ExchangeType
            if hasattr(self.engine, 'exchange') and hasattr(self.engine.exchange, 'config'):
                return self.engine.exchange.config.exchange_type == ExchangeType.SPOT
        except Exception as e:
            self.logger.debug(f"Failed to detect spot mode: {e}")
        return False

    def _get_reserve_amount(self) -> Decimal:
        """
        Return the reserve amount (spot mode only).

        Returns:
            Reserved BTC amount. Returns 0 when not in spot mode or when no reserve manager exists.
        """
        if not self._is_spot_mode():
            return Decimal('0')

        try:
            if self.reserve_manager:
                return self.reserve_manager.reserve_amount
        except Exception as e:
            self.logger.debug(f"Failed to read reserve amount: {e}")

        return Decimal('0')

    def _resolve_symbol_reference_price(
        self,
        current_price: Optional[Decimal] = None,
    ) -> Decimal:
        """Return the best available price for symbol-isolated valuation."""
        candidates = [current_price, self._symbol_reference_price]

        state_price = getattr(self.state, "current_price", None)
        if state_price is not None:
            candidates.append(state_price)

        if hasattr(self.tracker, "get_average_cost"):
            try:
                candidates.append(self.tracker.get_average_cost())
            except Exception:
                pass

        state_average_cost = getattr(self.state, "average_cost", None)
        if state_average_cost is not None:
            candidates.append(state_average_cost)

        try:
            candidates.append(self.config.get_first_order_price())
        except Exception:
            pass

        for candidate in candidates:
            if candidate is None:
                continue
            try:
                candidate_decimal = Decimal(str(candidate))
            except Exception:
                continue
            if candidate_decimal > 0:
                return candidate_decimal

        return Decimal('0')

    def _estimate_symbol_initial_capital(
        self,
        current_price: Optional[Decimal] = None,
    ) -> Decimal:
        """Estimate per-symbol strategy capital without using account equity."""
        reference_price = self._resolve_symbol_reference_price(current_price)
        if reference_price <= 0:
            return Decimal('0')

        estimated_capital = (
            self.config.order_amount
            * Decimal(str(self.config.grid_count))
            * reference_price
        )

        reserve_amount = self._get_reserve_amount()
        if reserve_amount > 0:
            estimated_capital += abs(reserve_amount) * reference_price

        return estimated_capital

    def ensure_symbol_isolated_capital(
        self,
        current_price: Optional[Decimal] = None,
        is_reinit: bool = False,
    ) -> Decimal:
        """Initialize or refresh the symbol-scoped capital baseline."""
        if self._symbol_initial_capital > 0 and not is_reinit:
            return self._symbol_initial_capital

        reference_price = self._resolve_symbol_reference_price(current_price)
        if reference_price <= 0:
            return self._symbol_initial_capital

        estimated_capital = self._estimate_symbol_initial_capital(reference_price)
        if estimated_capital <= 0:
            return self._symbol_initial_capital

        self._symbol_reference_price = reference_price
        self._symbol_initial_capital = estimated_capital

        if self.capital_protection_manager:
            self.capital_protection_manager.initialize_capital(
                estimated_capital,
                is_reinit=is_reinit,
            )
        if self.take_profit_manager:
            self.take_profit_manager.initialize_capital(
                estimated_capital,
                is_reinit=is_reinit,
            )
        if self.scalping_manager:
            self.scalping_manager.initialize_capital(
                estimated_capital,
                is_reinit=is_reinit,
            )

        return estimated_capital

    def get_symbol_isolated_snapshot(
        self,
        current_price: Optional[Decimal] = None,
    ) -> Dict[str, Decimal]:
        """Return per-symbol equity and PnL metrics without cross-ticker noise."""
        reference_price = self._resolve_symbol_reference_price(current_price)
        initial_capital = self.ensure_symbol_isolated_capital(reference_price)
        if initial_capital <= 0:
            initial_capital = self._estimate_symbol_initial_capital(reference_price)

        current_position = Decimal('0')
        average_cost = Decimal('0')

        if hasattr(self.tracker, "get_current_position"):
            try:
                current_position = self.tracker.get_current_position()
            except Exception:
                current_position = Decimal('0')
        if hasattr(self.tracker, "get_average_cost"):
            try:
                average_cost = self.tracker.get_average_cost()
            except Exception:
                average_cost = Decimal('0')

        if current_position == 0:
            state_position = getattr(self.state, "current_position", None)
            if state_position is not None:
                current_position = Decimal(str(state_position))
        if average_cost <= 0:
            state_average_cost = getattr(self.state, "average_cost", None)
            if state_average_cost is not None:
                average_cost = Decimal(str(state_average_cost))

        realized_profit = Decimal('0')
        if hasattr(self.tracker, "get_realized_pnl"):
            try:
                realized_profit = self.tracker.get_realized_pnl()
            except Exception:
                realized_profit = Decimal('0')

        total_fees = getattr(self.tracker, "total_fees", Decimal('0'))
        try:
            total_fees = Decimal(str(total_fees))
        except Exception:
            total_fees = Decimal('0')

        if current_position != 0 and average_cost > 0 and reference_price > 0:
            unrealized_profit = current_position * (reference_price - average_cost)
        else:
            unrealized_profit = Decimal('0')

        net_profit = realized_profit + unrealized_profit - total_fees
        current_equity = initial_capital + net_profit if initial_capital > 0 else net_profit
        valuation_price = reference_price if reference_price > 0 else average_cost
        position_value = abs(current_position) * valuation_price
        profit_rate = (
            (net_profit / initial_capital) * 100
            if initial_capital > 0
            else Decimal('0')
        )

        return {
            "reference_price": reference_price,
            "initial_capital": initial_capital,
            "current_equity": current_equity,
            "net_profit": net_profit,
            "profit_rate": profit_rate,
            "realized_profit": realized_profit,
            "unrealized_profit": unrealized_profit,
            "total_fees": total_fees,
            "current_position": current_position,
            "average_cost": average_cost,
            "position_value": position_value,
        }

    async def _place_take_profit_order(self):
        """
        Place the take-profit order.

         Important: the take-profit order may be canceled and re-placed frequently after position changes.
        - 每次挂单后必须立即同步 order_index（仅 Lighter）
        - Ensures that fast fills can still identify the take-profit order correctly
        """
        if not self.scalping_manager or not self.scalping_manager.is_active():
            return

        # 获取当前价格
        current_price = await self.engine.get_current_price()

        # Calculate the take-profit order.
        # Spot mode: pass the reserved BTC amount for symmetric break-even calculations.
        reserve_amount = self._get_reserve_amount() if self._is_spot_mode() else None
        tp_order = self.scalping_manager.calculate_take_profit_order(
            current_price, reserve_amount=reserve_amount)

        if not tp_order:
            self.logger.warning(
                "Could not calculate take-profit order; initial capital may be unset or position may be zero"
            )
            return

        try:
            # Submit the take-profit order.
            placed_order = await self.engine.place_order(tp_order)
            self.state.add_order(placed_order)

            self.logger.info(
                f"Take-profit order placed: {placed_order.side.value} "
                f"{placed_order.amount}@{placed_order.price} "
                f"(Grid {placed_order.grid_id})"
            )
        except Exception as e:
            self.logger.error(f"Failed to place take-profit order: {e}")

    def _is_take_profit_order_filled(self, filled_order: GridOrder) -> bool:
        """Return whether the filled order is the take-profit order."""
        if not self.scalping_manager or not self.scalping_manager.is_active():
            return False

        tp_order = self.scalping_manager.get_current_take_profit_order()
        if not tp_order:
            return False

        return filled_order.order_id == tp_order.order_id

    def _should_place_reverse_order_in_scalping(self, filled_order: GridOrder) -> bool:
        """
        Return whether reverse orders should be placed in scalping mode.

        Scalping mode does not place reverse orders.

        核心原则：
        - Scalping mode only keeps passively filled orders from existing maker orders
        - Other than the take-profit order managed by scalping_ops, do not actively place new orders
        - After fills, update only the take-profit order and do not replenish new orders

        工作流程：
        1. Long grid: when price drops and a buy fills, update only the take-profit order and do not place a new buy
        2. Long grid: when price rises and the take-profit order fills, exit scalping mode and reset the grid
        3. Any other fill -> update the take-profit order without placing reverse orders

        Args:
            filled_order: 已成交订单

        Returns:
            False - scalping mode disables all reverse orders
        """
        return False  # Scalping mode disables all reverse orders

    def _mark_state_order_filled_with_fallback(self, filled_order: GridOrder) -> bool:
        """Mark one filled order in shared state, even if the tracked key drifted."""
        filled_amount = filled_order.filled_amount or filled_order.amount
        if self.state.mark_order_filled(
            filled_order.order_id,
            filled_order.filled_price,
            filled_amount,
        ):
            self.logger.info(
                f"State fill matched by direct order id: "
                f"order_id={filled_order.order_id}, grid_id={filled_order.grid_id}, "
                f"side={filled_order.side.value}, price={filled_order.price}"
            )
            return True

        matches = [
            order
            for order in self.state.active_orders.values()
            if getattr(order, "status", None) == GridOrderStatus.PENDING
            and order.grid_id == filled_order.grid_id
            and order.side == filled_order.side
            and order.price == filled_order.price
        ]
        if not matches:
            suspend_reason = ""
            if hasattr(self.engine, "get_health_repair_suspend_reason"):
                suspend_reason = self.engine.get_health_repair_suspend_reason() or ""
            if suspend_reason == "startup initial grid placement":
                self.logger.warning(
                    "Fill arrived before startup state registration; "
                    f"registering authoritative engine fill first: "
                    f"order_id={filled_order.order_id}, grid_id={filled_order.grid_id}, "
                    f"side={filled_order.side.value}, price={filled_order.price}"
                )
                self.state.add_order(filled_order)
                return self.state.mark_order_filled(
                    filled_order.order_id,
                    filled_order.filled_price,
                    filled_amount,
                )

            engine_matches = [
                order
                for order in self.engine.get_pending_orders()
                if getattr(order, "status", None) == GridOrderStatus.PENDING
                and (
                    getattr(order, "order_id", None) == filled_order.order_id
                    or (
                        order.grid_id == filled_order.grid_id
                        and order.side == filled_order.side
                        and order.price == filled_order.price
                    )
                )
            ]
            context_label, context_details = self._describe_fill_tracking_gap(
                filled_order=filled_order,
                engine_matches=engine_matches,
            )
            log_level = "warning" if engine_matches else "info"
            getattr(self.logger, log_level)(
                f"{context_label}: "
                f"order_id={filled_order.order_id}, grid_id={filled_order.grid_id}, "
                f"side={filled_order.side.value}, price={filled_order.price}, "
                f"{context_details}"
            )
            if not engine_matches:
                return False

            fallback_order = sorted(
                engine_matches,
                key=lambda order: (
                    getattr(order, "created_at", None) or datetime.min,
                    order.order_id,
                ),
            )[0]
            if fallback_order.order_id not in self.state.active_orders:
                self.state.add_order(fallback_order)
            self.logger.warning(
                f"Recovered state fill tracking from engine cache: "
                f"filled_order_id={filled_order.order_id}, fallback_order_id={fallback_order.order_id}, "
                f"grid_id={filled_order.grid_id}, side={filled_order.side.value}, "
                f"price={filled_order.price}"
            )
            matched = self.state.mark_order_filled(
                fallback_order.order_id,
                filled_order.filled_price,
                filled_amount,
            )
            self.logger.info(
                f"State engine-cache fill result: matched={matched}, "
                f"filled_order_id={filled_order.order_id}, fallback_order_id={fallback_order.order_id}, "
                f"grid_id={filled_order.grid_id}, side={filled_order.side.value}, "
                f"price={filled_order.price}"
            )
            return matched

        fallback_order = sorted(
            matches,
            key=lambda order: (
                getattr(order, "created_at", None) or datetime.min,
                order.order_id,
            ),
        )[0]
        self.logger.warning(
            f"State order id mismatch during fill reconciliation: "
            f"filled_order_id={filled_order.order_id}, fallback_order_id={fallback_order.order_id}, "
            f"grid_id={filled_order.grid_id}, side={filled_order.side.value}, "
            f"price={filled_order.price}, matching_orders={len(matches)}"
        )
        matched = self.state.mark_order_filled(
            fallback_order.order_id,
            filled_order.filled_price,
            filled_amount,
        )
        self.logger.info(
            f"State fallback fill result: matched={matched}, "
            f"filled_order_id={filled_order.order_id}, fallback_order_id={fallback_order.order_id}, "
            f"grid_id={filled_order.grid_id}, side={filled_order.side.value}, "
            f"price={filled_order.price}"
        )
        return matched

    def _describe_fill_tracking_gap(
        self,
        filled_order: GridOrder,
        engine_matches: List[GridOrder],
    ) -> Tuple[str, str]:
        """Summarize why a fill could not be matched directly in coordinator state."""
        state_active_count = len(self.state.active_orders)
        suspend_reason = ""
        if hasattr(self.engine, "get_health_repair_suspend_reason"):
            suspend_reason = self.engine.get_health_repair_suspend_reason() or ""

        if engine_matches:
            return (
                "Recovered state fill tracking from engine cache",
                f"fallback_matches={len(engine_matches)}, "
                f"state_active_orders={state_active_count}, "
                f"health_repairs_suspended={bool(suspend_reason)}, "
                f"suspend_reason={suspend_reason or 'none'}",
            )

        if state_active_count == 0 and suspend_reason == "startup initial grid placement":
            return (
                "Fill arrived before startup state tracking was populated",
                "classification=startup_race_or_external_intervention, "
                f"state_active_orders={state_active_count}, "
                f"engine_pending_orders={len(self.engine.get_pending_orders())}, "
                f"suspend_reason={suspend_reason}",
            )

        return (
            "Ignoring fill after external/manual intervention or prior reconciliation",
            "classification=external_or_manual_intervention, "
            f"state_active_orders={state_active_count}, "
            f"engine_pending_orders={len(self.engine.get_pending_orders())}, "
            f"suspend_reason={suspend_reason or 'none'}",
        )

    def _sync_orders_from_engine(self):
        """
        Sync the latest order statistics from the engine into state.

        健康检查后，engine的_pending_orders可能已更新，需要同步到state
        这样UI才能显示正确的订单数量

         修复：同时同步state.active_orders，确保订单成交时能正确更新统计
        """
        try:
            # 从engine获取当前挂单
            engine_orders = self.engine.get_pending_orders()

            # 统计买单和卖单数量
            buy_count = sum(
                1 for order in engine_orders if order.side == GridOrderSide.BUY)
            sell_count = sum(
                1 for order in engine_orders if order.side == GridOrderSide.SELL)

            # 更新state的统计数据
            self.state.pending_buy_orders = buy_count
            self.state.pending_sell_orders = sell_count

            #  新增：同步state.active_orders
            # 确保state.active_orders包含所有engine中的订单
            engine_order_ids = {order.order_id for order in engine_orders}
            state_order_ids = set(self.state.active_orders.keys())

            # 1. 移除state中已不存在于engine的订单
            removed_orders = state_order_ids - engine_order_ids
            for order_id in removed_orders:
                if order_id in self.state.active_orders:
                    del self.state.active_orders[order_id]

            # 2. 添加engine中存在但state中没有的订单（健康检查新增的）
            added_orders = engine_order_ids - state_order_ids
            for order in engine_orders:
                if order.order_id in added_orders:
                    # 添加到state.active_orders，这样成交时能正确更新统计
                    self.state.active_orders[order.order_id] = order

            # 记录同步信息
            if removed_orders or added_orders:
                self.logger.debug(
                    f"Order sync summary: state_added={len(added_orders)}, "
                    f"state_removed={len(removed_orders)}, "
                    f"active_now={len(self.state.active_orders)}"
                )

            # 如果engine和state的订单数量差异较大，记录日志
            state_total = len(self.state.active_orders)
            engine_total = len(engine_orders)

            if abs(state_total - engine_total) > 5:
                self.logger.warning(
                    f"Order sync still mismatched after reconciliation: "
                    f"state={state_total}, engine={engine_total}, "
                    f"delta={abs(state_total - engine_total)}"
                )

        except Exception as e:
            self.logger.debug(f"Failed to sync order statistics: {e}")

    def _safe_decimal(self, value, default='0') -> Decimal:
        """安全转换为Decimal"""
        try:
            if value is None:
                return Decimal(default)
            return Decimal(str(value))
        except:
            return Decimal(default)

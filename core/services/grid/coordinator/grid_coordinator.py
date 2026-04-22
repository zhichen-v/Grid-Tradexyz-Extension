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
from typing import Any, Dict, List, Optional
from decimal import Decimal
from datetime import datetime

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
        self._resetting = False  # Reset-in-progress flag used by protection and scalping flows.

        #  Transaction deduplication mechanism: Prevents the same transaction from being processed repeatedly by multiple detection mechanisms.
        # key = 'grid_id:side:price', value = timestamp
        self._recent_fills: Dict[str, float] = {}
        self._fill_dedup_window: float = 10.0
        self._last_fill_time: float = 0

        #  Grid-level locking: Tracks the status of pending take-profit orders at each grid level.
        # key = grid_id, value = {'tp_side': str, 'tp_price': Decimal}
        self._grid_level_locks: Dict[int, Dict] = {}

        # 系统状态管理（REST失败保护）
        self.is_paused = False  # REST失败时暂停订单操作
        self.is_emergency_stopped = False  # 持仓异常时紧急停止

        # 异常计数
        self._error_count = 0
        self._max_errors = 5  # 最大错误次数，超过则暂停

        # 触发次数统计（仅标记次数，无实质性功能）
        self._scalping_trigger_count = 0  # Scalping-mode trigger count.
        self._price_escape_trigger_count = 0  # 价格朝有利方向脱离触发次数
        self._take_profit_trigger_count = 0  # 止盈模式触发次数
        self._capital_protection_trigger_count = 0  # 本金保护模式触发次数

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
        try:
            # Critical: do not process fills after the system has stopped; this prevents new orders during shutdown.
            if not self._running:
                self.logger.debug("System is stopped; skipping order handling")
                return

            #  关键检查：防止在重置期间处理订单
            if self._paused:
                self.logger.warning("System is paused; skipping order handling")
                return

            if self._resetting:
                self.logger.warning("System reset in progress; skipping order handling")
                return

            #  成交去重：防止同一笔成交被 REST 轮询和健康检查同步重复处理
            import time as _time
            fill_key = (
                f"{filled_order.order_id}:{filled_order.side.value}:"
                f"{filled_order.price}:{filled_order.filled_amount or filled_order.amount}"
            )
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
                self.logger.info(
                    f"Skipping already-processed or untracked fill: "
                    f"order_id={filled_order.order_id}, grid_id={filled_order.grid_id}, "
                    f"side={filled_order.side.value}, price={filled_order.price}, "
                    f"state_active_orders={len(self.state.active_orders)}"
                )
                return

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
                    return

            new_side, new_price, new_grid_id = self.strategy.calculate_reverse_order(
                filled_order,
                self.config.grid_interval,
                self.config.reverse_order_grid_distance
            )

            #  网格层级锁定检查：防止重复挂单
            grid_id = filled_order.grid_id
            if grid_id in self._grid_level_locks:
                lock_info = self._grid_level_locks[grid_id]
                if lock_info['tp_side'] == new_side.value:
                    self.logger.info(
                        f"Grid {grid_id} already has pending "
                        f"{lock_info['tp_side']}@{lock_info['tp_price']}; "
                        f"skipping duplicate placement"
                    )
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

            # 6. 下反向订单
            placed_order = await self.engine.place_order(reverse_order)
            if placed_order is None:
                self.logger.warning(
                    f"Reverse order was not submitted: {new_side.value} "
                    f"{reverse_order.amount}@{new_price} (Grid {new_grid_id})"
                )
                return
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
            self._handle_error(e)

    async def _on_batch_orders_filled(self, filled_orders: List[GridOrder]):
        """
        批量订单成交处理

        处理价格剧烈波动导致的多订单同时成交

        Args:
            filled_orders: 已成交订单列表
        """
        try:
            #  关键检查：防止在重置期间处理订单
            if self._paused:
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
            for side, price, grid_id, amount in reverse_params:
                order = GridOrder(
                    order_id="",
                    grid_id=grid_id,
                    side=side,
                    price=price,
                    amount=amount,
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now()
                )
                reverse_orders.append(order)

            # 4. 批量下单
            placed_orders = await self.engine.place_batch_orders(reverse_orders)

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

        # 如果错误次数过多，暂停系统
        if self._error_count >= self._max_errors:
            self.logger.error(
                f"Error threshold exceeded ({self._max_errors}); pausing system"
            )
            asyncio.create_task(self.pause())

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
        try:
            self.logger.info("Cleanup step 1: cancelling existing open orders")

            # 获取当前所有订单
            existing_orders = await self.engine.exchange.get_open_orders(
                symbol=self.config.symbol
            )

            if len(existing_orders) > 0:
                self.logger.warning(
                    f"Detected {len(existing_orders)} existing orders; cancelling them now"
                )

                # 批量取消订单
                cancel_count = 0
                for order in existing_orders:
                    try:
                        await self.engine.exchange.cancel_order(
                            order_id=order.id,
                            symbol=self.config.symbol
                        )
                        cancel_count += 1
                    except Exception as e:
                        self.logger.warning(f"Failed to cancel order {order.id}: {e}")

                self.logger.info(
                    f"Cancelled {cancel_count}/{len(existing_orders)} existing orders"
                )

                # 等待取消生效
                await asyncio.sleep(2)

                # 验证是否清理成功
                remaining_orders = await self.engine.exchange.get_open_orders(
                    symbol=self.config.symbol
                )
                if len(remaining_orders) > 0:
                    self.logger.warning(
                        f"{len(remaining_orders)} orders remain open after cleanup"
                    )
                else:
                    self.logger.info("All existing orders cleared")
            else:
                self.logger.info("No existing orders found; skipping order cleanup")

        except Exception as e:
            self.logger.error(f"Failed to clean existing orders: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

        # 步骤2: 平掉所有持仓
        try:
            self.logger.info("Cleanup step 2: checking current position")

            # 获取当前持仓
            positions = await self.engine.exchange.get_positions(
                symbols=[self.config.symbol]
            )

            if positions and len(positions) > 0:
                position = positions[0]
                position_size = position.size or Decimal('0')

                if position_size != 0:
                    self.logger.warning(
                        f"Detected open position: {position_size} {self.config.symbol.split('_')[0]}, "
                        f"entry=${position.entry_price}, "
                        f"unrealized_pnl=${position.unrealized_pnl}"
                    )

                    # 计算平仓方向和数量
                    close_side = 'Sell' if position_size > 0 else 'Buy'
                    close_amount = abs(position_size)

                    self.logger.warning(
                        f"Closing existing position: {close_side} {close_amount} (market)"
                    )

                    # 使用市价单平仓（参考 order_health_checker.py 的实现）
                    try:
                        from ....adapters.exchanges.models import OrderSide, OrderType

                        #  修复：获取当前市场价格（Hyperliquid市价单需要价格计算滑点）
                        ticker = await self.engine.exchange.get_ticker(self.config.symbol)
                        current_price = ticker.last

                        # 确定平仓方向：平多仓=卖出，平空仓=买入
                        order_side = OrderSide.SELL if close_side == 'Sell' else OrderSide.BUY

                        # 调用交易所接口平仓（使用市价单）
                        # 注意：
                        # - Backpack: 不支持 reduceOnly，price=None即可
                        # - Hyperliquid: 市价单需要price来计算滑点（默认5%）
                        placed_order = await self.engine.exchange.create_order(
                            symbol=self.config.symbol,
                            side=order_side,
                            order_type=OrderType.MARKET,
                            amount=close_amount,
                            price=current_price  # Hyperliquid需要价格计算滑点，Backpack会忽略
                        )

                        self.logger.info(f"Close-out order submitted: {placed_order.id}")

                        # 等待平仓完成
                        await asyncio.sleep(3)

                        # 验证是否平仓成功
                        new_positions = await self.engine.exchange.get_positions(
                            symbols=[self.config.symbol]
                        )
                        if new_positions and len(new_positions) > 0:
                            new_position_size = new_positions[0].size or Decimal(
                                '0')
                            if new_position_size == 0:
                                self.logger.info("Position fully closed")
                            else:
                                self.logger.warning(
                                    f"Position not fully closed; remaining={new_position_size}"
                                )
                        else:
                            self.logger.info("Position fully closed")

                    except Exception as e:
                        self.logger.error(f"Failed to close position: {e}")
                        import traceback
                        self.logger.error(traceback.format_exc())
                else:
                    self.logger.info("No open position; skipping close-out")
            else:
                self.logger.info("No open position; skipping close-out")

        except Exception as e:
            self.logger.error(f"Position cleanup failed: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

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
                    real_size = position.size or Decimal('0')
                    real_entry_price = position.entry_price or Decimal('0')

                    # 同步到WebSocket缓存
                    if hasattr(self.engine.exchange, '_position_cache'):
                        self.engine.exchange._position_cache[self.config.symbol] = {
                            'size': real_size,
                            'entry_price': real_entry_price,
                            'unrealized_pnl': position.unrealized_pnl or Decimal('0'),
                            'side': 'Long' if real_size > 0 else 'Short',
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

        #  启动余额轮询监控（使用新模块 BalanceMonitor）
        await self.balance_monitor.start_monitoring()

        self.logger.info("Grid system started")

    async def pause(self):
        """暂停网格系统（保留挂单）"""
        self._paused = True
        self.state.pause()

        self.logger.info("Grid system paused")

    async def resume(self):
        """恢复网格系统"""
        self._paused = False
        self._error_count = 0  # 重置错误计数
        self.state.resume()

        self.logger.info("Grid system resumed")

    async def stop(self):
        """停止网格系统（取消所有挂单）"""
        self._running = False
        self._paused = True  #  修复：设为 True 阻止 shutdown 期间的订单回调

        #  停止余额监控（使用新模块）
        if hasattr(self.engine, 'begin_shutdown'):
            self.engine.begin_shutdown()
        await self.balance_monitor.stop_monitoring()

        #  停止持仓同步监控（使用新模块）
        await self.position_monitor.stop_monitoring()

        # 取消所有挂单
        cancelled_count = await self.engine.cancel_all_orders()
        self.logger.info(f"Cancelled {cancelled_count} open orders")

        # 停止引擎
        await self.engine.stop()

        # 更新状态
        self.state.stop()

        self.logger.info("Grid system stopped")

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

        # 🆕 触发次数统计（仅标记）
        stats.scalping_trigger_count = self._scalping_trigger_count
        stats.price_escape_trigger_count = self._price_escape_trigger_count
        stats.take_profit_trigger_count = self._take_profit_trigger_count
        stats.capital_protection_trigger_count = self._capital_protection_trigger_count

        return stats

    def get_state(self) -> GridState:
        """获取网格状态"""
        return self.state

    def is_running(self) -> bool:
        """Return whether the runtime is currently running."""
        return self._running and not self._paused

    def is_paused(self) -> bool:
        """Return whether the runtime is currently paused."""
        return self._paused

    def is_stopped(self) -> bool:
        """Return whether the runtime has stopped."""
        return not self._running

    def get_status_text(self) -> str:
        """Return a short human-readable runtime status."""
        if self._paused:
            return "Paused"
        elif self._running:
            return "Running"
        else:
            return "Stopped"

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
                    await self.engine.cancel_order(tp_order.order_id)
                    self.state.remove_order(tp_order.order_id)
                    self.logger.info("Position returned to zero; take-profit order cancelled")
                except Exception as e:
                    self.logger.error(f"Failed to cancel take-profit order: {e}")
            return

        # Cancel the previous take-profit order.
        old_tp_order = self.scalping_manager.get_current_take_profit_order()
        if old_tp_order:
            try:
                await self.engine.cancel_order(old_tp_order.order_id)
                self.state.remove_order(old_tp_order.order_id)
                self.logger.info(
                    f"Cancelled previous take-profit order: {old_tp_order.order_id}"
                )
            except Exception as e:
                self.logger.error(f"Failed to cancel previous take-profit order: {e}")

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
            self.logger.warning(
                f"State fill reconciliation found no pending match: "
                f"order_id={filled_order.order_id}, grid_id={filled_order.grid_id}, "
                f"side={filled_order.side.value}, price={filled_order.price}, "
                f"state_active_orders={len(self.state.active_orders)}"
            )
            return False

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

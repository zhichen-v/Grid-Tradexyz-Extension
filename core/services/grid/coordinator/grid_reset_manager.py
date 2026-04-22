"""
网格重置管理器

统一处理所有网格重置场景（本金保护、止盈、价格脱离）
"""

import asyncio
from typing import Optional
from decimal import Decimal
from datetime import datetime

from ....logging import get_logger
from ..models import GridOrder, GridOrderSide, GridOrderStatus
from .order_operations import OrderOperations


class GridResetManager:
    """
    网格重置管理器

    职责：
    1. 统一网格重置工作流
    2. 处理本金保护重置
    3. 处理止盈重置
    4. 处理价格脱离重置
    5. 消除重复的重置逻辑
    """

    def __init__(self, coordinator, config, state, engine, tracker, strategy):
        """
        初始化网格重置管理器

        Args:
            coordinator: 协调器引用
            config: 网格配置
            state: 网格状态
            engine: 执行引擎
            tracker: 持仓跟踪器
            strategy: 网格策略
        """
        self.logger = get_logger(__name__)
        self.coordinator = coordinator
        self.config = config
        self.state = state
        self.engine = engine
        self.tracker = tracker
        self.strategy = strategy

        # 创建订单操作实例
        self.order_ops = OrderOperations(engine, state, config, coordinator)

    async def execute_capital_protection_reset(self):
        """执行本金保护重置（平仓后重置并重新初始化本金）"""
        try:
            #  关键：设置重置标志，防止并发操作
            self.coordinator._resetting = True
            self.logger.warning("️ 开始执行本金保护重置（锁定系统）...")

            # 执行通用重置工作流
            #  对于价格移动网格，本金保护重置时也应该更新价格区间
            new_capital = await self._generic_reset_workflow(
                reset_type="本金保护",
                should_close_position=True,  # 需要平仓
                should_reinit_capital=True,  # 需要重新初始化本金
                continue_after_cancel_fail=False,  # 取消失败则中止
                update_price_range=self.config.is_follow_mode()  # 价格移动网格时更新区间
            )

            if new_capital is None:
                self.logger.error(" 本金保护重置失败")
                return

            # 根据网格类型决定后续操作
            if self.config.is_follow_mode():
                # 价格移动网格：重置并重新启动
                self.logger.info(" 价格移动网格：重置网格并重新启动...")
                await self._restart_grid_after_reset(new_capital)
                # 🆕 增加本金保护触发次数（仅标记）
                self.coordinator._capital_protection_trigger_count += 1
                self.logger.info(
                    f" 本金保护触发次数: {self.coordinator._capital_protection_trigger_count}")
                self.logger.info(" 本金保护重置完成，网格已重新启动")
            else:
                # 固定范围网格：停止运行
                self.logger.info("⏹️ 固定范围网格：停止运行...")
                await self.coordinator.stop()
                # 🆕 增加本金保护触发次数（仅标记）
                self.coordinator._capital_protection_trigger_count += 1
                self.logger.info(
                    f" 本金保护触发次数: {self.coordinator._capital_protection_trigger_count}")
                self.logger.warning("️ 本金保护：固定范围网格已停止，请手动重新启动")

        except Exception as e:
            self.logger.error(f" 本金保护重置失败: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
        finally:
            #  关键：无论成功或失败，都要释放重置锁
            self.coordinator._resetting = False
            self.logger.info(" 系统锁定已释放")

    async def execute_take_profit_reset(self):
        """执行止盈重置（无论哪种网格都重置并重启）"""
        try:
            #  关键：设置重置标志，防止并发操作
            self.coordinator._resetting = True
            self.logger.warning(" 开始执行止盈重置（锁定系统）...")

            # 执行通用重置工作流
            #  对于价格移动网格，止盈重置时也应该更新价格区间
            new_capital = await self._generic_reset_workflow(
                reset_type="止盈",
                should_close_position=True,  # 需要平仓
                should_reinit_capital=True,  # 需要重新初始化本金
                continue_after_cancel_fail=False,  # 取消失败则中止
                update_price_range=self.config.is_follow_mode()  # 价格移动网格时更新区间
            )

            if new_capital is None:
                self.logger.error(" 止盈重置失败")
                return

            # 重置网格（价格移动网格和固定范围网格都重置）
            if self.config.is_follow_mode():
                # 价格移动网格：根据当前价格重新挂单
                self.logger.info(" 价格移动网格：重置并重新启动...")
                await self._restart_grid_after_reset(new_capital)
                # 🆕 增加止盈触发次数（仅标记）
                self.coordinator._take_profit_trigger_count += 1
                self.logger.info(
                    f" 止盈触发次数: {self.coordinator._take_profit_trigger_count}")
                self.logger.info(" 止盈重置完成，价格移动网格已重启")
            else:
                # 固定范围网格：保持原有范围重新挂单
                self.logger.info(" 固定范围网格：保持范围重新挂单...")
                await self._restart_fixed_range_grid(new_capital)
                # 🆕 增加止盈触发次数（仅标记）
                self.coordinator._take_profit_trigger_count += 1
                self.logger.info(
                    f" 止盈触发次数: {self.coordinator._take_profit_trigger_count}")
                self.logger.info(" 止盈重置完成，固定范围网格已重启")

        except Exception as e:
            self.logger.error(f" 止盈重置失败: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
        finally:
            #  关键：无论成功或失败，都要释放重置锁
            self.coordinator._resetting = False
            self.logger.info(" 系统锁定已释放")

    async def execute_price_follow_reset(
        self,
        current_price: Decimal,
        direction: str
    ):
        """
        执行价格移动网格重置

        Args:
            current_price: 当前价格
            direction: 脱离方向 ("up" 或 "down")
        """
        if self.coordinator._is_resetting:
            self.logger.warning("网格正在重置中，跳过本次重置")
            return

        #  检查是否应该锁定而不是重置
        if self.coordinator.price_lock_manager:
            if self.coordinator.price_lock_manager.should_lock_instead_of_reset(
                current_price, direction
            ):
                # 激活价格锁定，不执行重置
                self.coordinator.price_lock_manager.activate_lock(
                    current_price)
                self.logger.info(
                    f" 价格锁定已激活，不执行重置。"
                    f"保留订单和持仓，等待价格回归..."
                )
                return

        try:
            self.coordinator._is_resetting = True

            self.logger.info(
                f" 开始重置网格: 当前价格=${current_price:,.2f}, 脱离方向={direction}"
            )

            # 判断是否需要平仓（价格朝有利方向脱离 = 止盈）
            should_close_position = False
            if self.config.is_long() and direction == "up":
                # 做多 + 价格向上 = 盈利方向，需要平仓止盈
                should_close_position = True
                self.logger.info(" 做多网格价格向上脱离 → 需要平仓止盈")
            elif self.config.is_short() and direction == "down":
                # 做空 + 价格向下 = 盈利方向，需要平仓止盈
                should_close_position = True
                self.logger.info(" 做空网格价格向下脱离 → 需要平仓止盈")
            else:
                self.logger.info(" 价格朝不利方向脱离 → 保留持仓")

            # 执行通用重置工作流
            new_capital = await self._generic_reset_workflow(
                reset_type="价格脱离",
                should_close_position=should_close_position,
                should_reinit_capital=should_close_position,  # 只有平仓后才需要重新初始化本金
                continue_after_cancel_fail=False,  # 取消失败则中止
                update_price_range=True  # 价格移动网格需要更新价格区间
            )

            if new_capital is not None or not should_close_position:
                # 🆕 只有在有利方向脱离（平仓止盈）时才增加计数
                if should_close_position:
                    self.coordinator._price_escape_trigger_count += 1
                    self.logger.info(
                        f" 价格朝有利方向脱离触发次数: {self.coordinator._price_escape_trigger_count}")
                self.logger.info(" 价格脱离重置完成")
            else:
                self.logger.error(" 价格脱离重置失败")

        except Exception as e:
            self.logger.error(f" 价格脱离重置失败: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
        finally:
            self.coordinator._is_resetting = False

    async def _generic_reset_workflow(
        self,
        reset_type: str,
        should_close_position: bool = False,
        should_reinit_capital: bool = False,
        continue_after_cancel_fail: bool = False,
        update_price_range: bool = False
    ) -> Optional[Decimal]:
        """
        通用重置工作流（消除90%重复代码）

        流程：
        1. 取消所有订单（带验证）
        2. 平仓（如果需要）
        3. 清空状态
        4. 更新价格区间（如果是价格移动网格）
        5. 重新挂单
        6. 重新初始化本金（如果需要）

        Args:
            reset_type: 重置类型描述（用于日志）
            should_close_position: 是否需要平仓
            should_reinit_capital: 是否需要重新初始化本金
            continue_after_cancel_fail: 取消订单失败后是否继续（不推荐）
            update_price_range: 是否需要更新价格区间（价格移动网格）

        Returns:
            新本金（如果平仓），否则返回None
        """
        self.logger.info(f" 开始执行{reset_type}重置工作流...")

        # ======== 步骤1: 取消所有订单（带验证）========
        self.logger.info(" 步骤 1/7: 取消所有订单...")
        cancel_verified = await self.order_ops.cancel_all_orders_with_verification(
            max_retries=3,
            retry_delay=1.5,
            first_delay=0.8
        )

        if not cancel_verified and not continue_after_cancel_fail:
            self.logger.error(f" 由于订单取消验证失败，{reset_type}重置已中止")
            return None

        # ======== 步骤2: 平仓（如果需要）========
        new_capital = None
        if should_close_position:
            self.logger.info(" 步骤 2/7: 平仓...")
            current_position = self.tracker.get_current_position()
            if current_position != 0:
                self.logger.info(f" {reset_type}平仓: {current_position:+.4f}")
                try:
                    # 使用市价单平仓
                    side = GridOrderSide.SELL if current_position > 0 else GridOrderSide.BUY
                    await self.engine.place_market_order(
                        side=side,
                        amount=abs(current_position)
                    )
                    self.logger.info(f" {reset_type}平仓完成")
                except Exception as e:
                    self.logger.error(f" {reset_type}平仓失败: {e}")
                    # 即使平仓失败也继续重置流程
            else:
                self.logger.info(" 步骤 2/7: 无持仓，跳过平仓")

            # 等待一小段时间，让平仓完成并余额更新
            await asyncio.sleep(2)

            # 重新获取抵押品余额（平仓后的新本金）
            if should_reinit_capital:
                self.logger.info(" 步骤 3/7: 获取新本金...")
                try:
                    await self.coordinator.balance_monitor.update_balance()
                    current_price = await self.engine.get_current_price()
                    new_capital = self.coordinator.ensure_symbol_isolated_capital(
                        current_price=current_price,
                        is_reinit=True,
                    )
                    self.logger.info(
                        f" {reset_type}后新本金: ${new_capital:,.3f}")
                except Exception as e:
                    self.logger.error(f"️ 获取平仓后余额失败: {e}")
                    new_capital = self.coordinator.ensure_symbol_isolated_capital(
                        is_reinit=True,
                    )
        else:
            self.logger.info(" 步骤 2-3/7: 不平仓，跳过")

        # ======== 步骤4: 清空状态 ========
        self.logger.info(" 步骤 4/7: 清空网格状态...")
        self.state.active_orders.clear()
        self.state.pending_buy_orders = 0
        self.state.pending_sell_orders = 0

        # 重置所有管理器状态
        if self.coordinator.scalping_manager:
            self.coordinator.scalping_manager.reset()
        if self.coordinator.capital_protection_manager:
            self.coordinator.capital_protection_manager.reset()
        if self.coordinator.take_profit_manager:
            self.coordinator.take_profit_manager.reset()
        if self.coordinator.price_lock_manager:
            self.coordinator.price_lock_manager.reset()

        # 重置追踪器
        self.tracker.reset()
        self.logger.info(" 网格状态已清空")

        # ======== 步骤5: 更新价格区间（如果需要）========
        if update_price_range:
            self.logger.info(" 步骤 5/7: 更新价格区间...")
            current_price = await self.engine.get_current_price()
            old_range = (self.config.lower_price, self.config.upper_price)
            self.config.update_price_range_for_follow_mode(current_price)
            self.logger.info(
                f" 价格区间已更新: "
                f"[${old_range[0]:,.2f}, ${old_range[1]:,.2f}] → "
                f"[${self.config.lower_price:,.2f}, ${self.config.upper_price:,.2f}]"
            )
        else:
            self.logger.info(" 步骤 5/7: 保持原有价格区间")

        # ======== 步骤6: 重新初始化网格层级 ========
        self.logger.info(" 步骤 6/7: 重新初始化网格层级...")
        self.state.initialize_grid_levels(
            self.config.grid_count,
            self.config.get_grid_price
        )
        self.logger.info(f" 网格层级已重新初始化，共{self.config.grid_count}个")

        #  重置后进入初始化阶段（避免持仓变化误报）
        if hasattr(self.coordinator, 'position_monitor') and self.coordinator.position_monitor:
            self.coordinator.position_monitor.restart_initial_phase(
                duration=60)

        # ======== 步骤7: 生成并挂出新订单 ========
        self.logger.info(" 步骤 7/7: 生成并挂出新订单...")
        current_price = await self.engine.get_current_price()
        initial_orders = self.strategy.initialize(self.config, current_price)
        placed_orders = await self.engine.place_batch_orders(initial_orders)

        # 等待立即成交的订单完成
        await asyncio.sleep(2)

        # 添加到状态（只添加未成交的订单）
        added_count = 0
        skipped_count = 0

        try:
            # 获取当前实际挂单（从引擎）
            engine_pending_orders = self.engine.get_pending_orders()
            engine_pending_ids = {
                order.order_id for order in engine_pending_orders}

            for order in placed_orders:
                if order.order_id in self.state.active_orders:
                    skipped_count += 1
                    continue
                # 检查订单是否真的还在挂单中
                if order.order_id not in engine_pending_ids:
                    skipped_count += 1
                    continue
                self.state.add_order(order)
                added_count += 1
        except Exception as e:
            self.logger.warning(f"️ 无法从引擎获取挂单列表: {e}")
            # Fallback：使用订单自身的状态
            for order in placed_orders:
                if order.order_id in self.state.active_orders:
                    skipped_count += 1
                    continue
                if order.status == GridOrderStatus.FILLED:
                    skipped_count += 1
                    continue
                self.state.add_order(order)
                added_count += 1

        self.logger.info(
            f" 订单添加详情: 新增={added_count}, 跳过={skipped_count}"
        )

        # ======== 步骤8: 重新初始化本金（如果需要）========
        if new_capital is not None and should_reinit_capital:
            self.logger.info(f" 本金已重新初始化: ${new_capital:,.3f}")

        self.logger.info(
            f" {reset_type}重置完成！成功挂出 {len(placed_orders)} 个订单"
        )

        return new_capital

    async def _restart_grid_after_reset(self, new_capital: Optional[Decimal] = None):
        """
        重启网格（价格移动网格）

        注意：此方法不包含取消订单的逻辑，因为已在 _generic_reset_workflow 中处理

        Args:
            new_capital: 新的初始本金（止盈后使用）
        """
        # 实际上，这个方法的工作已经在 _generic_reset_workflow 中完成
        # 这里只是为了保持接口一致性
        self.logger.info(" 价格移动网格重启完成")

    async def _restart_fixed_range_grid(self, new_capital: Optional[Decimal] = None):
        """
        重启固定范围网格

        注意：此方法不包含取消订单的逻辑，因为已在 _generic_reset_workflow 中处理

        Args:
            new_capital: 新的初始本金（止盈后使用）
        """
        # 实际上，这个方法的工作已经在 _generic_reset_workflow 中完成
        # 这里只是为了保持接口一致性
        self.logger.info(" 固定范围网格重启完成")

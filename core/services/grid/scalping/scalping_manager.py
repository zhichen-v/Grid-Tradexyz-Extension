"""
剥头皮管理器

负责管理剥头皮模式的触发、止盈订单、退出逻辑
"""

from typing import Optional, Tuple, Dict
from decimal import Decimal
from datetime import datetime

from ....logging import get_logger
from ..models import GridConfig, GridOrder, GridOrderSide, GridOrderStatus, GridType


class ScalpingManager:
    """
    剥头皮管理器

    功能：
    1. 监控价格，判断是否触发剥头皮模式
    2. 计算止盈价格和止盈订单
    3. 动态更新止盈订单（持仓变化时）
    4. 判断是否退出剥头皮模式
    """

    def __init__(self, config: GridConfig):
        """
        初始化剥头皮管理器

        Args:
            config: 网格配置
        """
        self.logger = get_logger(__name__)
        self.config = config

        # 剥头皮状态
        self._is_scalping_active = False
        self._trigger_grid = config.get_scalping_trigger_grid()

        # 持仓信息
        self._current_position = Decimal('0')      # 当前持仓数量
        self._average_cost_price = Decimal('0')    # 平均成本价（用于日志显示）
        self._cost_grid_index = 0                  # 成本价对应的网格索引（用于日志显示）

        # 🔥 本金信息（用于计算精确止盈价格）
        self._initial_capital = Decimal('0')       # 初始本金
        self._current_equity = Decimal('0')        # 当前策略权益

        # 止盈订单
        self._take_profit_order: Optional[GridOrder] = None
        self._take_profit_grid_index = 0

        self.logger.info(
            f"剥头皮管理器初始化: "
            f"触发阈值={config.scalping_trigger_percent}% (第{self._trigger_grid}格), "
            f"止盈网格数={config.scalping_take_profit_grids}"
        )

    def is_active(self) -> bool:
        """是否处于剥头皮模式"""
        return self._is_scalping_active

    def _is_long_grid(self) -> bool:
        return self.config.grid_type in [
            GridType.LONG,
            GridType.FOLLOW_LONG,
            GridType.MARTINGALE_LONG,
        ]

    def should_trigger(self, current_price: Decimal, current_grid_index: int) -> bool:
        """
        判断是否应该触发剥头皮模式

        Args:
            current_price: 当前价格
            current_grid_index: 当前价格所在的网格索引

        Returns:
            是否应该触发剥头皮

        逻辑（已修正）：
            做多网格：Grid 1 = 最低价，当价格跌到前N%区域时触发
            做空网格：Grid 1 = 最高价，当价格涨到前N%区域时触发
        """
        # 🔥 价格有效性检查：如果价格为0或无效，不触发剥头皮
        if current_price <= 0:
            self.logger.debug(
                f"⏸️  价格无效（{current_price}），跳过剥头皮检查")
            return False

        if self._is_scalping_active:
            return False  # 已经在剥头皮模式中

        if not self.config.is_scalping_enabled():
            return False  # 未启用剥头皮

        # 🔥 做多网格：Grid 1 = 最低价，价格跌到低位时触发（Grid ID <= trigger_grid）
        if self._is_long_grid():
            should_trigger = current_grid_index <= self._trigger_grid
            if should_trigger:
                self.logger.warning(
                    f"🔴 触发剥头皮模式 (做多): "
                    f"当前价格{current_price}, 第{current_grid_index}格 <= 触发点第{self._trigger_grid}格"
                )
            return should_trigger

        # 🔥 做空网格：Grid 1 = 最高价，价格涨到低位时触发（Grid ID <= trigger_grid）
        else:
            should_trigger = current_grid_index <= self._trigger_grid
            if should_trigger:
                self.logger.warning(
                    f"🔴 触发剥头皮模式 (做空): "
                    f"当前价格{current_price}, 第{current_grid_index}格 <= 触发点第{self._trigger_grid}格"
                )
            return should_trigger

    def should_exit(self, current_price: Decimal, current_grid_index: int) -> bool:
        """
        判断是否应该退出剥头皮模式

        Args:
            current_price: 当前价格
            current_grid_index: 当前价格所在的网格索引

        Returns:
            是否应该退出剥头皮

        逻辑（已修正）：
            做多网格：Grid 1 = 最低价，当价格反弹回高位时退出
            做空网格：Grid 1 = 最高价，当价格回落到高位时退出
        """
        # 🔥 价格有效性检查：如果价格为0或无效，不处理退出逻辑
        if current_price <= 0:
            self.logger.debug(
                f"⏸️  价格无效（{current_price}），跳过剥头皮退出检查")
            return False

        if not self._is_scalping_active:
            return False  # 不在剥头皮模式中

        # 🔥 做多网格：Grid 1 = 最低价，价格反弹回高位时退出（Grid ID > trigger_grid）
        if self._is_long_grid():
            should_exit = current_grid_index > self._trigger_grid
            if should_exit:
                self.logger.info(
                    f"🟢 退出剥头皮模式 (做多): "
                    f"当前价格{current_price}, 第{current_grid_index}格 > 触发点第{self._trigger_grid}格"
                )
            return should_exit

        # 🔥 做空网格：Grid 1 = 最高价，价格回落到高位时退出（Grid ID > trigger_grid）
        else:
            should_exit = current_grid_index > self._trigger_grid
            if should_exit:
                self.logger.info(
                    f"🟢 退出剥头皮模式 (做空): "
                    f"当前价格{current_price}, 第{current_grid_index}格 > 触发点第{self._trigger_grid}格"
                )
            return should_exit

    def activate(self):
        """激活剥头皮模式"""
        self._is_scalping_active = True
        self.logger.info("✅ 剥头皮模式已激活")

    def deactivate(self):
        """停用剥头皮模式"""
        self._is_scalping_active = False
        self._take_profit_order = None
        self._take_profit_grid_index = 0
        self.logger.info("⏸️  剥头皮模式已停用")

    def update_position(self, position: Decimal, average_cost: Decimal, initial_capital: Decimal, current_equity: Decimal):
        """
        更新持仓信息

        Args:
            position: 当前持仓数量（正数=做多，负数=做空）
            average_cost: 平均持仓成本价（用于日志显示）
            initial_capital: 初始本金
            current_equity: 当前策略权益
        """
        old_position = self._current_position
        self._current_position = position
        self._average_cost_price = average_cost
        self._initial_capital = initial_capital
        self._current_equity = current_equity

        # 找到成本价对应的最接近的网格索引（保守）- 仅用于日志显示
        if average_cost > 0:
            self._cost_grid_index = self.config.find_nearest_grid_index(
                average_cost,
                direction="conservative"
            )

            if old_position != position and self._is_scalping_active:
                realized_pnl = current_equity - initial_capital
                self.logger.info(
                    f"📊 持仓更新: 数量{position}, 成本{average_cost}, "
                    f"成本网格第{self._cost_grid_index}格, "
                    f"已实现盈亏{realized_pnl:+.2f}"
                )

    def calculate_take_profit_order(
        self,
        current_price: Decimal,
        reserve_amount: Optional[Decimal] = None
    ) -> Optional[GridOrder]:
        """
        计算止盈订单（基于初始本金和当前抵押品计算精确止盈价格）

        Args:
            current_price: 当前价格
            reserve_amount: 现货模式预留数量（可选，仅现货模式使用）

        Returns:
            止盈订单，如果持仓为0则返回None

        🔥 现货模式特殊处理：
        - 下跌时：预留BTC和持仓BTC的价值下跌都计入总亏损
        - 上涨时：预留BTC和持仓BTC的价值上涨都用于回本（对称计算）
        - 止盈时：只卖出持仓BTC，预留BTC保持不动
        """
        if not self._is_scalping_active:
            return None

        if self._current_position == 0:
            return None

        if self._initial_capital == 0:
            self.logger.warning("⚠️ 初始本金未设置，无法计算精确止盈价格")
            return None

        # 🔥 新算法：基于已实现盈亏计算回本价格
        is_long_grid = self._is_long_grid()

        if is_long_grid and self._current_position <= 0:
            self.logger.warning(
                f"Skip scalping take-profit calculation: long-grid position "
                f"must be positive, got {self._current_position}"
            )
            return None

        if not is_long_grid and self._current_position >= 0:
            self.logger.warning(
                f"Skip scalping take-profit calculation: short-grid position "
                f"must be negative, got {self._current_position}"
            )
            return None

        realized_pnl = self._current_equity - self._initial_capital
        position_abs = abs(self._current_position)

        # 🔥 现货模式：计算回本价格时，需要将预留BTC也计入
        # 原因：下跌时预留BTC的价值损失已经计入realized_pnl，
        #      上涨时预留BTC的价值增长也应该用于回本（对称性）
        is_spot_mode = reserve_amount is not None and reserve_amount > 0

        # 计算回本所需的价格变动
        if position_abs > 0:
            if is_spot_mode:
                # 现货模式：使用 (持仓BTC + 预留BTC) 作为分母
                total_btc = position_abs + abs(reserve_amount)
                required_price_move = -realized_pnl / total_btc
                self.logger.info(
                    f"💰 现货模式止盈计算: "
                    f"持仓BTC={position_abs:.8f}, 预留BTC={reserve_amount:.8f}, "
                    f"总BTC={total_btc:.8f}, 已实现盈亏={realized_pnl:+.2f}"
                )
            else:
                # 合约模式：只使用持仓作为分母（保持原有逻辑）
                required_price_move = -realized_pnl / position_abs
        else:
            self.logger.warning("⚠️ 持仓为0，无法计算止盈价格")
            return None

        # 🔥 做多网格：Grid 1 = 最低价
        if self._is_long_grid():
            # 回本价格 = 当前价格 + 需要上涨的幅度
            breakeven_price = current_price + required_price_move
            order_side = GridOrderSide.SELL  # 卖出平仓

            # 找到回本价格对应的网格
            breakeven_grid = self.config.find_nearest_grid_index(
                breakeven_price, direction="conservative")

            # 🔥 检查回本网格是否超出范围
            if breakeven_grid > self.config.grid_count:
                # 回本价格超出网格范围，使用最高网格作为止盈价格
                self.logger.warning(
                    f"⚠️ 回本价格 ${breakeven_price:,.2f} (Grid {breakeven_grid}) 超出网格范围 "
                    f"(最高Grid {self.config.grid_count})，止盈价格将使用最高网格"
                )
                self._take_profit_grid_index = self.config.grid_count
            else:
                # 止盈网格 = 回本网格 + 止盈网格数（不超过网格范围）
                take_profit_grids = self.config.scalping_take_profit_grids
                self._take_profit_grid_index = min(
                    self.config.grid_count,
                    breakeven_grid + take_profit_grids
                )

        # 🔥 做空网格：Grid 1 = 最高价
        else:
            # 回本价格 = 当前价格 - 需要下跌的幅度
            breakeven_price = current_price - required_price_move
            order_side = GridOrderSide.BUY  # 买入平仓

            # 找到回本价格对应的网格
            breakeven_grid = self.config.find_nearest_grid_index(
                breakeven_price, direction="conservative")

            # 🔥 检查回本网格是否超出范围
            if breakeven_grid > self.config.grid_count:
                # 回本价格超出网格范围（低于最低网格），使用最低网格作为止盈价格
                self.logger.warning(
                    f"⚠️ 回本价格 ${breakeven_price:,.2f} (Grid {breakeven_grid}) 超出网格范围 "
                    f"(最低Grid {self.config.grid_count})，止盈价格将使用最低网格"
                )
                self._take_profit_grid_index = self.config.grid_count
            else:
                # 止盈网格 = 回本网格 + 止盈网格数（不超过最低网格）
                # Short grids descend as the index increases, so a buy TP must
                # move to a larger grid index than breakeven.
                take_profit_grids = self.config.scalping_take_profit_grids
                self._take_profit_grid_index = min(
                    self.config.grid_count,
                    breakeven_grid + take_profit_grids
                )

        # 从网格配置中获取精确的止盈价格
        take_profit_price = self.config.get_grid_price(
            self._take_profit_grid_index)

        # 创建止盈订单
        if order_side == GridOrderSide.BUY and take_profit_price >= current_price:
            take_profit_grids = max(1, self.config.scalping_take_profit_grids)
            current_grid = self.config.find_nearest_grid_index(
                current_price,
                direction="conservative",
            )
            adjusted_grid = min(
                self.config.grid_count,
                max(self._take_profit_grid_index, current_grid + take_profit_grids),
            )
            adjusted_price = self.config.get_grid_price(adjusted_grid)

            if adjusted_price >= current_price:
                adjusted_grid = min(self.config.grid_count, current_grid + 1)
                adjusted_price = self.config.get_grid_price(adjusted_grid)

            if adjusted_price >= current_price:
                self.logger.warning(
                    f"Skip short-grid scalping TP because no passive buy price "
                    f"is available below current price ${current_price:.2f}: "
                    f"calculated ${take_profit_price:.2f}, adjusted ${adjusted_price:.2f}"
                )
                return None

            self.logger.warning(
                f"Adjusted short-grid scalping TP below market to avoid an "
                f"immediate buy fill: ${take_profit_price:.2f} -> "
                f"${adjusted_price:.2f} (Grid {adjusted_grid})"
            )
            self._take_profit_grid_index = adjusted_grid
            take_profit_price = adjusted_price

        self._take_profit_order = GridOrder(
            order_id=f"scalping_tp_{self._take_profit_grid_index}",
            grid_id=self._take_profit_grid_index,
            side=order_side,
            price=take_profit_price,
            amount=abs(self._current_position),  # 全仓平仓
            status=GridOrderStatus.PENDING,
            created_at=datetime.now()
        )

        # 计算预期盈利（相对于回本）
        if order_side == GridOrderSide.SELL:
            expected_profit = (take_profit_price -
                               breakeven_price) * position_abs
        else:
            expected_profit = (breakeven_price -
                               take_profit_price) * position_abs

        self.logger.info(
            f"💰 计算止盈订单（新算法）: "
            f"初始本金${self._initial_capital:.2f}, "
            f"当前策略权益${self._current_equity:.2f}, "
            f"已实现盈亏{realized_pnl:+.2f}"
        )
        self.logger.info(
            f"   当前价格${current_price:.2f}, "
            f"回本价格${breakeven_price:.2f} (第{breakeven_grid}格), "
            f"止盈价格${take_profit_price:.2f} (第{self._take_profit_grid_index}格)"
        )
        self.logger.info(
            f"   持仓{position_abs} → 预期止盈利润${expected_profit:+.2f}"
        )

        return self._take_profit_order

    def get_current_take_profit_order(self) -> Optional[GridOrder]:
        """获取当前的止盈订单"""
        return self._take_profit_order

    def update_take_profit_order_with_real_id(self, real_order: GridOrder):
        """
        更新止盈订单为交易所返回的真实订单

        Args:
            real_order: 交易所返回的真实订单（包含真实订单ID）
        """
        if self._take_profit_order:
            # 保留临时订单的grid_id，使用真实订单的其他信息
            self._take_profit_order = GridOrder(
                order_id=real_order.order_id,  # 🔥 使用真实订单ID
                grid_id=self._take_profit_order.grid_id,
                side=real_order.side,
                price=real_order.price,
                amount=real_order.amount,
                status=real_order.status,
                created_at=real_order.created_at
            )
            self.logger.debug(
                f"🔄 更新止盈订单ID: 临时ID → {real_order.order_id}"
            )

    def is_take_profit_order_outdated(self, current_position: Decimal) -> bool:
        """
        判断当前止盈订单是否已过时（持仓数量发生变化）

        Args:
            current_position: 当前持仓数量

        Returns:
            止盈订单是否需要更新
        """
        if not self._take_profit_order:
            return False

        # 持仓数量与止盈订单数量不一致
        return abs(current_position) != self._take_profit_order.amount

    def get_orders_to_cancel_on_trigger(self) -> str:
        """
        获取触发剥头皮时需要取消的订单类型

        Returns:
            "buy" 或 "sell"
        """
        # 做多网格：取消所有卖单
        if self._is_long_grid():
            return "sell"
        # 做空网格：取消所有买单
        else:
            return "buy"

    def initialize_capital(self, initial_capital: Decimal, is_reinit: bool = False):
        """
        初始化/重新初始化本金

        Args:
            initial_capital: 初始本金
            is_reinit: 是否为重新初始化（止盈后）
        """
        self._initial_capital = initial_capital
        if is_reinit:
            self.logger.info(f"💰 剥头皮管理器重新初始化本金: ${initial_capital:,.2f}")
        else:
            self.logger.info(f"💰 剥头皮管理器初始化本金: ${initial_capital:,.2f}")

    def get_initial_capital(self) -> Decimal:
        """获取初始本金"""
        return self._initial_capital

    def reset(self):
        """
        重置剥头皮管理器状态

        在网格重置时调用，清除所有状态
        """
        self._is_scalping_active = False
        self._trigger_grid = self.config.get_scalping_trigger_grid()
        self._current_position = Decimal('0')
        self._average_cost_price = Decimal('0')
        self._cost_grid_index = 0
        self._initial_capital = Decimal('0')
        self._current_equity = Decimal('0')
        self._take_profit_order = None
        self._take_profit_grid_index = 0

        self.logger.info("🔄 剥头皮管理器已重置")

    def __repr__(self) -> str:
        return (
            f"ScalpingManager("
            f"active={self._is_scalping_active}, "
            f"trigger_grid={self._trigger_grid}, "
            f"position={self._current_position}, "
            f"cost_grid={self._cost_grid_index})"
        )

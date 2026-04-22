"""
网格状态模型

定义网格系统的运行状态和网格层级状态
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from decimal import Decimal
from datetime import datetime

from .grid_order import GridOrder, GridOrderSide


class GridStatus(Enum):
    """网格系统状态"""
    INITIALIZING = "initializing"  # 初始化中
    RUNNING = "running"            # 运行中
    PAUSED = "paused"              # 已暂停
    STOPPED = "stopped"            # 已停止
    ERROR = "error"                # 错误状态


class GridLevelStatus(Enum):
    """网格层级状态"""
    IDLE = "idle"                  # 空闲（未触发）
    PENDING_BUY = "pending_buy"    # 买单挂单中
    PENDING_SELL = "pending_sell"  # 卖单挂单中
    FILLED_BUY = "filled_buy"      # 买单已成交，等待卖出
    FILLED_SELL = "filled_sell"    # 卖单已成交，等待买入
    COMPLETED = "completed"        # 完成一轮循环


@dataclass
class GridLevel:
    """
    网格层级
    
    代表网格中的一个价格层级及其状态
    """
    
    # 基本信息
    grid_id: int                              # 网格层级ID (1-based)
    price: Decimal                            # 网格价格
    
    # 状态信息
    status: GridLevelStatus                   # 层级状态
    
    # 当前订单
    current_order: Optional[GridOrder] = None # 当前活跃订单
    
    # 历史信息
    buy_count: int = 0                        # 买入次数
    sell_count: int = 0                       # 卖出次数
    completed_cycles: int = 0                 # 完成的循环次数
    
    # 盈亏信息
    realized_profit: Decimal = Decimal('0')   # 已实现盈亏
    total_buy_volume: Decimal = Decimal('0')  # 总买入量
    total_sell_volume: Decimal = Decimal('0') # 总卖出量
    
    def set_order(self, order: GridOrder):
        """设置当前订单"""
        self.current_order = order
        
        # 更新状态
        if order.is_buy_order():
            self.status = GridLevelStatus.PENDING_BUY
        else:
            self.status = GridLevelStatus.PENDING_SELL
    
    def mark_order_filled(self):
        """标记订单已成交"""
        if not self.current_order:
            return
        
        if self.current_order.is_buy_order():
            self.status = GridLevelStatus.FILLED_BUY
            self.buy_count += 1
            self.total_buy_volume += self.current_order.filled_amount or self.current_order.amount
        else:
            self.status = GridLevelStatus.FILLED_SELL
            self.sell_count += 1
            self.total_sell_volume += self.current_order.filled_amount or self.current_order.amount
    
    def add_profit(self, profit: Decimal):
        """添加已实现盈亏"""
        self.realized_profit += profit
        
        # 如果是一买一卖配对，增加完成循环次数
        if self.buy_count > 0 and self.sell_count > 0:
            self.completed_cycles = min(self.buy_count, self.sell_count)
    
    def is_pending(self) -> bool:
        """是否有挂单"""
        return self.status in [GridLevelStatus.PENDING_BUY, GridLevelStatus.PENDING_SELL]
    
    def is_filled(self) -> bool:
        """是否有已成交未平仓的订单"""
        return self.status in [GridLevelStatus.FILLED_BUY, GridLevelStatus.FILLED_SELL]
    
    def __repr__(self) -> str:
        return (
            f"GridLevel(id={self.grid_id}, price={self.price}, "
            f"status={self.status.value}, cycles={self.completed_cycles})"
        )


@dataclass
class GridState:
    """
    网格系统状态
    
    管理整个网格系统的运行状态
    """
    
    # 系统状态
    status: GridStatus = GridStatus.INITIALIZING
    
    # 网格层级
    grid_levels: Dict[int, GridLevel] = field(default_factory=dict)  # grid_id -> GridLevel
    
    # 当前价格信息
    current_price: Optional[Decimal] = None
    current_grid_id: Optional[int] = None
    
    # 持仓信息
    current_position: Decimal = Decimal('0')     # 当前持仓数量（正数=做多，负数=做空）
    average_cost: Decimal = Decimal('0')         # 平均持仓成本
    
    # 订单统计
    total_buy_orders: int = 0                    # 总买单数
    total_sell_orders: int = 0                   # 总卖单数
    pending_buy_orders: int = 0                  # 挂单中的买单
    pending_sell_orders: int = 0                 # 挂单中的卖单
    
    # 成交统计
    filled_buy_count: int = 0                    # 买单成交次数
    filled_sell_count: int = 0                   # 卖单成交次数
    completed_cycles: int = 0                    # 完成的循环次数
    
    # 盈亏统计
    realized_profit: Decimal = Decimal('0')      # 已实现盈亏
    unrealized_profit: Decimal = Decimal('0')    # 未实现盈亏
    total_fees: Decimal = Decimal('0')           # 总手续费
    
    # 时间信息
    started_at: Optional[datetime] = None
    last_update_at: Optional[datetime] = None
    
    # 活跃订单跟踪
    active_orders: Dict[str, GridOrder] = field(default_factory=dict)  # order_id -> GridOrder
    
    def initialize_grid_levels(self, grid_count: int, price_calculator):
        """
        初始化网格层级
        
        Args:
            grid_count: 网格数量
            price_calculator: 价格计算函数 (grid_id) -> price
        """
        self.grid_levels = {}
        for grid_id in range(1, grid_count + 1):
            price = price_calculator(grid_id)
            self.grid_levels[grid_id] = GridLevel(
                grid_id=grid_id,
                price=price,
                status=GridLevelStatus.IDLE
            )
    
    def add_order(self, order: GridOrder):
        """添加订单"""
        self.active_orders[order.order_id] = order
        
        # 更新对应的网格层级
        if order.grid_id in self.grid_levels:
            self.grid_levels[order.grid_id].set_order(order)
        
        # 更新统计
        if order.is_buy_order():
            self.total_buy_orders += 1
            self.pending_buy_orders += 1
        else:
            self.total_sell_orders += 1
            self.pending_sell_orders += 1
        
        self.last_update_at = datetime.now()
    
    def mark_order_filled(self, order_id: str, filled_price: Decimal, filled_amount: Decimal) -> bool:
        """标记订单已成交"""
        if order_id not in self.active_orders:
            return False
        
        order = self.active_orders[order_id]
        order.mark_filled(filled_price, filled_amount)
        
        # 更新网格层级
        if order.grid_id in self.grid_levels:
            self.grid_levels[order.grid_id].mark_order_filled()
        
        # 更新统计
        if order.is_buy_order():
            self.filled_buy_count += 1
            self.pending_buy_orders -= 1
            # 更新持仓
            self.current_position += filled_amount
        else:
            self.filled_sell_count += 1
            self.pending_sell_orders -= 1
            # 更新持仓
            self.current_position -= filled_amount
        
        # 更新完成循环次数
        self.completed_cycles = min(self.filled_buy_count, self.filled_sell_count)
        
        # 🔥 从活跃订单中移除（已成交订单不再是活跃订单）
        del self.active_orders[order_id]
        
        self.last_update_at = datetime.now()
    
    def remove_order(self, order_id: str):
        """移除订单（取消或失败）"""
        if order_id in self.active_orders:
            order = self.active_orders[order_id]
            
            # 更新统计
            if order.is_pending():
                if order.is_buy_order():
                    self.pending_buy_orders -= 1
                else:
                    self.pending_sell_orders -= 1
            
            del self.active_orders[order_id]
            self.last_update_at = datetime.now()
    
    def update_current_price(self, price: Decimal, grid_id: int):
        """更新当前价格"""
        self.current_price = price
        self.current_grid_id = grid_id
        self.last_update_at = datetime.now()
    
    def calculate_unrealized_profit(self) -> Decimal:
        """计算未实现盈亏"""
        if self.current_price is None or self.current_position == 0:
            return Decimal('0')
        
        # 未实现盈亏 = (当前价格 - 平均成本) * 持仓数量
        self.unrealized_profit = (self.current_price - self.average_cost) * self.current_position
        return self.unrealized_profit
    
    def get_grid_utilization(self) -> float:
        """获取网格利用率"""
        if not self.grid_levels:
            return 0.0
        
        triggered_count = sum(
            1 for level in self.grid_levels.values()
            if level.buy_count > 0 or level.sell_count > 0
        )
        
        return (triggered_count / len(self.grid_levels)) * 100
    
    def get_pending_orders_count(self) -> tuple:
        """获取挂单数量 (买单, 卖单)"""
        return (self.pending_buy_orders, self.pending_sell_orders)
    
    def start(self):
        """启动网格系统"""
        self.status = GridStatus.RUNNING
        self.started_at = datetime.now()
        self.last_update_at = datetime.now()
    
    def pause(self):
        """暂停网格系统"""
        self.status = GridStatus.PAUSED
        self.last_update_at = datetime.now()
    
    def resume(self):
        """恢复网格系统"""
        self.status = GridStatus.RUNNING
        self.last_update_at = datetime.now()
    
    def stop(self):
        """停止网格系统"""
        self.status = GridStatus.STOPPED
        self.last_update_at = datetime.now()
    
    def set_error(self):
        """设置错误状态"""
        self.status = GridStatus.ERROR
        self.last_update_at = datetime.now()
    
    def is_running(self) -> bool:
        """是否运行中"""
        return self.status == GridStatus.RUNNING
    
    def sync_position_snapshot(self, position: Decimal, average_cost: Decimal):
        """Sync the state position snapshot from the latest exchange position data."""
        self.current_position = position
        self.average_cost = average_cost
        self.last_update_at = datetime.now()
        return True

    def __repr__(self) -> str:
        return (
            f"GridState(status={self.status.value}, "
            f"position={self.current_position}, "
            f"pending_orders={self.pending_buy_orders}B/{self.pending_sell_orders}S, "
            f"cycles={self.completed_cycles})"
        )


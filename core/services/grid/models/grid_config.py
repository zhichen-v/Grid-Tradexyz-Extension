"""
网格配置模型

定义网格交易系统的配置参数
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from decimal import Decimal
from core.logging import get_logger


class GridType(Enum):
    """网格类型"""
    LONG = "long"                          # 做多网格（普通）
    SHORT = "short"                        # 做空网格（普通）
    MARTINGALE_LONG = "martingale_long"    # 马丁做多网格
    MARTINGALE_SHORT = "martingale_short"  # 马丁做空网格
    FOLLOW_LONG = "follow_long"            # 价格移动做多网格
    FOLLOW_SHORT = "follow_short"          # 价格移动做空网格


class GridDirection(Enum):
    """网格方向（内部使用）"""
    UP = "up"      # 向上（价格上涨方向）
    DOWN = "down"  # 向下（价格下跌方向）


@dataclass
class GridConfig:
    """
    网格配置

    所有参数由用户在配置文件中设置
    """

    # 基础参数（必需参数）
    exchange: str                           # 交易所名称 (如 "backpack")
    symbol: str                             # 交易对符号 (如 "BTC_USDC_PERP")
    grid_type: GridType                     # 网格类型（做多/做空）
    grid_interval: Decimal                  # 网格间隔（等差）
    order_amount: Decimal                   # 每格订单数量（基础金额）

    # 价格区间参数（可选参数，价格移动网格时不需要）
    lower_price: Optional[Decimal] = None   # 价格下限（价格移动网格时可选）
    upper_price: Optional[Decimal] = None   # 价格上限（价格移动网格时可选）

    # 计算得出的参数
    grid_count: int = field(init=False)     # 网格数量（自动计算或用户指定）

    # 可选参数
    max_position: Optional[Decimal] = None  # 最大持仓限制
    enable_notifications: bool = True        # 是否启用通知
    order_health_check_interval: int = 300   # 订单健康检查间隔（秒，默认5分钟）
    fee_rate: Decimal = Decimal('0.0001')    # 手续费率（默认万分之1）

    # 交易精度参数（重要！）
    quantity_precision: int = 3              # 数量精度（小数位数，默认3位，如BNB）
    # 说明：不同代币的交易所数量精度不同，需要根据实际情况设置
    # - BTC: 8位小数 (0.00000001)
    # - ETH: 6位小数 (0.000001)
    # - BNB: 3位小数 (0.001)
    # - SOL: 4位小数 (0.0001)
    # 💡 查看方法：在交易所下单界面查看最小下单单位

    price_decimals: int = 2                  # 价格精度（小数位数，默认2位，如USD）
    # 说明：不同交易所和交易对的价格精度不同
    # - Backpack BTC/USD: 2位小数 ($110,000.50)
    # - Lighter BTC: 1位小数 ($110,000.5)
    # - Hyperliquid BTC: 1位小数 ($110,000.5)
    # 💡 查看方法：在交易所订单簿中查看价格显示格式

    # 马丁网格参数（可选）
    martingale_increment: Optional[Decimal] = None  # 马丁网格递增金额（None表示不启用马丁模式）

    # 价格移动网格参数（可选）
    follow_grid_count: Optional[int] = None         # 价格移动网格数量（用户指定）
    follow_timeout: int = 300                       # 脱离超时时间（秒，默认5分钟）
    follow_distance: int = 1                        # 脱离距离（网格数，默认1格）
    price_offset_grids: int = 0                     # 价格偏移网格数（默认0，即以当前价格为边界）
    # 说明：用于调整网格启动时的价格边界位置
    # - 默认值0：以当前价格为边界（旧行为）
    # - 做多网格：当前价格 + offset格 = 上边界，然后向下计算下边界
    # - 做空网格：当前价格 - offset格 = 下边界，然后向上计算上边界
    # - 效果：当前价格在网格内部，可立即触发交易
    # - 推荐值：3-10格（让当前价格处于网格内部靠近边界的位置）

    # 剥头皮模式参数（可选）
    scalping_enabled: bool = False                   # 是否启用剥头皮模式
    scalping_trigger_percent: int = 80               # 触发剥头皮的网格进度百分比（默认80%）
    scalping_take_profit_grids: int = 2              # 止盈使用的网格数量（默认2格）

    # 本金保护模式参数（可选）
    capital_protection_enabled: bool = False         # 是否启用本金保护模式
    capital_protection_trigger_percent: int = 50     # 触发本金保护的网格进度百分比（默认50%）

    # 止盈模式参数（可选）
    take_profit_enabled: bool = False                # 是否启用止盈模式
    take_profit_percentage: Decimal = Decimal('0.01')  # 止盈百分比（默认1%，即0.01）

    # 价格锁定模式参数（可选）
    price_lock_enabled: bool = False                 # 是否启用价格锁定模式
    # 价格锁定阈值（做多：价格>=阈值时锁定；做空：价格<=阈值时锁定）
    price_lock_threshold: Optional[Decimal] = None
    stop_loss_enabled: bool = False                  # Price-based stop loss switch.
    stop_loss_price: Optional[Decimal] = None        # Long: trigger at/below; short: trigger at/above.
    stop_loss_check_interval: int = 1                # Stop-loss monitor interval in seconds.
    price_lock_start_at_threshold: bool = False      # 启动时使用阈值作为起点（仅价格移动网格+价格超出阈值时生效）
    # 说明：仅对价格移动网格（FOLLOW模式）生效
    # - 做多：如果当前价格 > 阈值，则以阈值为网格上限启动
    # - 做空：如果当前价格 < 阈值，则以阈值为网格下限启动
    # - 如果价格未超出阈值，此参数无效，始终以当前价格为起点

    # 反手挂单参数（可选）
    reverse_order_grid_distance: int = 1             # 反手挂单的格子距离（默认1格，可提高以增加利润）
    # 说明：成交后反手挂单的距离（单位：网格格子数）
    # - 默认值1：成交价 ± 1格（原始逻辑）
    # - 可设置2-5格：增加利润空间，但风险也相应增加
    # - 例如：买单成交@$2.00，grid_interval=$0.01，distance=2
    #         → 反手卖单@$2.02（而非$2.01）

    # 🔥 现货预留管理配置（可选，仅现货需要）
    spot_reserve: Optional[dict] = None              # 现货预留管理配置
    # 说明：用于管理现货交易中的币种预留（例如预留BTC用于手续费）
    # - 仅对现货市场生效
    # - 包含 enabled, reserve_amount, spot_buy_fee_rate 等配置

    # 🔥 健康检查容错配置（可选）
    position_tolerance: Optional[dict] = None        # 健康检查容错配置
    # 说明：用于设置持仓健康检查的误差容忍度
    # - 包含 tolerance_multiplier 等配置
    # - 避免因手续费等微小差异导致的错误告警

    def __post_init__(self):
        """初始化后计算网格数量"""
        # 初始化 logger
        self.logger = get_logger(self.__class__.__name__)

        # 🔥 价格移动网格：使用用户指定的网格数量
        if self.is_follow_mode():
            if self.follow_grid_count is None:
                raise ValueError("价格移动网格必须指定 follow_grid_count")
            self.grid_count = self.follow_grid_count
            # 价格区间将在运行时根据当前价格动态计算
        else:
            # 普通网格和马丁网格：根据价格区间计算网格数量
            if self.upper_price is None or self.lower_price is None:
                raise ValueError("普通网格和马丁网格必须指定 upper_price 和 lower_price")
            price_range = abs(self.upper_price - self.lower_price)
            self.grid_count = int(price_range / self.grid_interval)

        # 验证参数
        self._validate()

    def _validate(self):
        """验证配置参数"""
        if self.stop_loss_enabled:
            if self.stop_loss_price is None or self.stop_loss_price <= 0:
                raise ValueError("stop_loss_price must be greater than 0 when stop_loss_enabled is true")
            if self.stop_loss_check_interval <= 0:
                raise ValueError("stop_loss_check_interval must be greater than 0")

        # 价格移动网格的价格区间在运行时动态设置，跳过验证
        if self.is_follow_mode():
            if self.follow_grid_count is None or self.follow_grid_count <= 0:
                raise ValueError("价格移动网格必须指定有效的 follow_grid_count")
            if self.grid_interval is None or self.grid_interval <= 0:
                raise ValueError("网格间隔必须大于0")
            return

        # 普通网格和马丁网格验证
        if self.lower_price >= self.upper_price:
            raise ValueError("下限价格必须小于上限价格")

        if self.grid_interval <= 0:
            raise ValueError("网格间隔必须大于0")

        if self.order_amount <= 0:
            raise ValueError("订单数量必须大于0")

        if self.grid_count <= 0:
            raise ValueError(f"网格数量必须大于0，当前计算结果: {self.grid_count}")

    def get_first_order_price(self) -> Decimal:
        """
        获取第一个订单的价格

        做多网格：上限 - 1个网格间隔
        做空网格：下限 + 1个网格间隔
        """
        if self.grid_type == GridType.LONG:
            return self.upper_price - self.grid_interval
        else:  # SHORT
            return self.lower_price + self.grid_interval

    def get_grid_price(self, grid_index: int) -> Decimal:
        """
        获取指定网格索引的价格

        Args:
            grid_index: 网格索引 (1-based)

        Returns:
            该网格的价格

        逻辑：
            做多网格：Grid 1 = 最低价（lower_price），向上递增
            做空网格：Grid 1 = 最高价（upper_price），向下递减
        """
        if self.grid_type in [GridType.LONG, GridType.FOLLOW_LONG, GridType.MARTINGALE_LONG]:
            # 做多网格：从下限开始向上递增
            # Grid 1 = 最低价，Grid N = 最高价
            return self.lower_price + ((grid_index - 1) * self.grid_interval)
        else:  # SHORT, FOLLOW_SHORT, MARTINGALE_SHORT
            # 做空网格：从上限开始向下递减
            # Grid 1 = 最高价，Grid N = 最低价
            return self.upper_price - ((grid_index - 1) * self.grid_interval)

    def get_grid_index_by_price(self, price: Decimal) -> int:
        """
        根据价格获取网格索引

        Args:
            price: 价格

        Returns:
            网格索引 (1-based)

        逻辑：
            做多网格：Grid 1 = lower_price（最低价），向上递增
            做空网格：Grid 1 = upper_price（最高价），向下递减

        修复：
            使用round()代替int()避免浮点数精度问题
            例如：174.999999... 会被round为175，而不是int为174
        """
        if self.grid_type in [GridType.LONG, GridType.FOLLOW_LONG, GridType.MARTINGALE_LONG]:
            # 做多网格：Grid 1 = lower_price
            # 计算价格距离下限有多少个网格间隔
            # 🔥 使用round()避免浮点数精度问题（如174.999999被int截断为174）
            index = round((price - self.lower_price) / self.grid_interval) + 1
        else:  # SHORT, FOLLOW_SHORT, MARTINGALE_SHORT
            # 做空网格：Grid 1 = upper_price
            # 计算价格距离上限有多少个网格间隔
            # 🔥 使用round()避免浮点数精度问题
            index = round((self.upper_price - price) / self.grid_interval) + 1

        # 确保索引在有效范围内（1到grid_count）
        return max(1, min(index, self.grid_count))

    def is_price_in_range(self, price: Decimal) -> bool:
        """检查价格是否在网格区间内"""
        return self.lower_price <= price <= self.upper_price

    def is_martingale_mode(self) -> bool:
        """
        判断是否为马丁网格模式（订单金额递增）

        Returns:
            True: 启用了马丁模式（订单金额递增）
            False: 固定金额模式

        注意：
            - 只要设置了 martingale_increment，就视为启用马丁模式
            - 适用于所有网格类型（普通/马丁/跟随移动）
        """
        return (
            self.martingale_increment is not None and
            self.martingale_increment > 0
        )

    def is_follow_mode(self) -> bool:
        """
        判断是否为价格移动网格模式

        Returns:
            True: 价格移动网格模式
            False: 其他模式
        """
        return self.grid_type in [GridType.FOLLOW_LONG, GridType.FOLLOW_SHORT]

    def is_long(self) -> bool:
        """
        判断是否为做多网格（普通或价格移动）

        Returns:
            True: 做多网格
            False: 做空网格
        """
        return self.grid_type in [GridType.LONG, GridType.FOLLOW_LONG]

    def is_short(self) -> bool:
        """
        判断是否为做空网格（普通或价格移动）

        Returns:
            True: 做空网格
            False: 做多网格
        """
        return self.grid_type in [GridType.SHORT, GridType.FOLLOW_SHORT]

    def update_price_range_for_follow_mode(self, current_price: Decimal):
        """
        为价格移动网格动态更新价格区间

        Args:
            current_price: 当前市场价格

        逻辑：
            做多网格：以当前价格为上限，向下计算下限
            做空网格：以当前价格为下限，向上计算上限

            特殊逻辑（price_lock_start_at_threshold=True时）：
            - 做多：如果当前价格 > 阈值，使用阈值为上限
            - 做空：如果当前价格 < 阈值，使用阈值为下限

            价格偏移逻辑（price_offset_grids > 0时）：
            - 做多：当前价格 + offset格 = 上限，让当前价格处于网格内部
            - 做空：当前价格 - offset格 = 下限，让当前价格处于网格内部
        """
        if not self.is_follow_mode():
            return

        if self.grid_type == GridType.FOLLOW_LONG:
            # 做多网格：检查是否使用价格锁定阈值作为起点
            if (self.price_lock_enabled and
                self.price_lock_threshold and
                self.price_lock_start_at_threshold and
                    current_price > self.price_lock_threshold):
                # 使用阈值作为上限
                base_price = self.price_lock_threshold
                self.logger.info(
                    f"🔒 做多网格: 当前价格${current_price:,.{self.price_decimals}f}高于阈值${self.price_lock_threshold:,.{self.price_decimals}f}，"
                    f"根据配置使用阈值作为网格起点"
                )
            else:
                # 使用当前价格作为上限（默认行为）
                base_price = current_price

            # 🆕 应用价格偏移（做多：向上偏移）
            if self.price_offset_grids > 0:
                offset_amount = self.grid_interval * self.price_offset_grids
                self.upper_price = base_price + offset_amount
                self.logger.info(
                    f"📊 做多网格: 应用价格偏移 +{self.price_offset_grids}格 "
                    f"(${offset_amount:,.4f}), "
                    f"上边界 ${base_price:,.{self.price_decimals}f} → ${self.upper_price:,.{self.price_decimals}f}"
                )
            else:
                self.upper_price = base_price

            self.lower_price = self.upper_price - \
                (self.grid_count * self.grid_interval)

        elif self.grid_type == GridType.FOLLOW_SHORT:
            # 做空网格：检查是否使用价格锁定阈值作为起点
            if (self.price_lock_enabled and
                self.price_lock_threshold and
                self.price_lock_start_at_threshold and
                    current_price < self.price_lock_threshold):
                # 使用阈值作为下限
                base_price = self.price_lock_threshold
                self.logger.info(
                    f"🔒 做空网格: 当前价格${current_price:,.{self.price_decimals}f}低于阈值${self.price_lock_threshold:,.{self.price_decimals}f}，"
                    f"根据配置使用阈值作为网格起点"
                )
            else:
                # 使用当前价格作为下限（默认行为）
                base_price = current_price

            # 🆕 应用价格偏移（做空：向下偏移）
            if self.price_offset_grids > 0:
                offset_amount = self.grid_interval * self.price_offset_grids
                self.lower_price = base_price - offset_amount
                self.logger.info(
                    f"📊 做空网格: 应用价格偏移 -{self.price_offset_grids}格 "
                    f"(${offset_amount:,.4f}), "
                    f"下边界 ${base_price:,.{self.price_decimals}f} → ${self.lower_price:,.{self.price_decimals}f}"
                )
            else:
                self.lower_price = base_price

            self.upper_price = self.lower_price + \
                (self.grid_count * self.grid_interval)

    def check_price_escape(self, current_price: Decimal) -> tuple[bool, str]:
        """
        检查价格是否脱离网格范围

        Args:
            current_price: 当前市场价格

        Returns:
            (是否需要重置, 脱离方向)

        逻辑：
            做多网格：只在向上脱离时重置（盈利方向）
            做空网格：只在向下脱离时重置（盈利方向）
        """
        if not self.is_follow_mode():
            return False, ""

        escape_threshold = self.grid_interval * self.follow_distance

        if self.grid_type == GridType.FOLLOW_LONG:
            # 做多网格：检查向上脱离（盈利方向）
            if current_price > self.upper_price + escape_threshold:
                return True, "up"
            # 向下脱离（亏损方向）不重置
            return False, ""

        elif self.grid_type == GridType.FOLLOW_SHORT:
            # 做空网格：检查向下脱离（盈利方向）
            if current_price < self.lower_price - escape_threshold:
                return True, "down"
            # 向上脱离（亏损方向）不重置
            return False, ""

        return False, ""

    def get_grid_order_amount(self, grid_index: int) -> Decimal:
        """
        获取指定网格的订单金额（理论值，未格式化）

        Args:
            grid_index: 网格索引 (1-based)

        Returns:
            该网格的订单金额（理论值）

        逻辑：
            普通网格：固定金额 = order_amount

            马丁网格（做多）：
                - 价格越低（grid_index 越小），数量越多
                - Grid 1（最低价）买最多，Grid N（最高价）买最少
                - 金额 = order_amount + (grid_count - grid_index) * martingale_increment

            马丁网格（做空）：
                - 价格越高（grid_index 越大），数量越多
                - Grid 1（最低价）买最少，Grid N（最高价）买最多
                - 金额 = order_amount + (grid_index - 1) * martingale_increment

        注意：
            此方法返回理论金额，下单时应使用 get_formatted_grid_order_amount()
            以确保金额符合交易所精度要求
        """
        # 如果设置了马丁递增参数，则使用递增金额
        if self.martingale_increment is not None and self.martingale_increment > 0:
            # 判断网格方向
            if self.grid_type in [GridType.LONG, GridType.FOLLOW_LONG, GridType.MARTINGALE_LONG]:
                # 做多：价格越低（grid_index 越小），数量越多
                # Grid 1 = order_amount + (200-1) * increment（最多）
                # Grid 200 = order_amount + (200-200) * increment（最少）
                return self.order_amount + (self.grid_count - grid_index) * self.martingale_increment
            else:
                # 做空：价格越高（grid_index 越大），数量越多
                # Grid 1 = order_amount + 0 * increment（最少）
                # Grid 200 = order_amount + 199 * increment（最多）
                return self.order_amount + (grid_index - 1) * self.martingale_increment

        # 否则使用固定金额
        return self.order_amount

    def get_formatted_grid_order_amount(self, grid_index: int) -> Decimal:
        """
        获取指定网格的订单金额（格式化到交易所精度）

        🔥 重要：下单时应使用此方法，而不是 get_grid_order_amount()

        Args:
            grid_index: 网格索引 (1-based)

        Returns:
            格式化后的订单金额（符合交易所精度要求）

        说明：
            1. 获取理论金额（可能有4位小数，如0.0015）
            2. 格式化到交易所精度（如3位小数，四舍五入为0.002）
            3. 确保与交易所实际处理结果一致
        """
        from decimal import ROUND_HALF_UP

        # 获取理论金额
        raw_amount = self.get_grid_order_amount(grid_index)

        # 格式化到交易所精度（四舍五入）
        precision_quantizer = Decimal('0.1') ** self.quantity_precision
        formatted_amount = raw_amount.quantize(
            precision_quantizer, rounding=ROUND_HALF_UP)

        return formatted_amount

    def get_scalping_trigger_grid(self) -> int:
        """
        获取剥头皮触发的网格索引

        Returns:
            触发剥头皮的网格索引（1-based）

        逻辑说明：
            做多网格：Grid 1=最低价，Grid N=最高价
                     从Grid N（高价）往下数 trigger_percent
                     例如：90% → Grid 20（接近低价）
                     意义：价格下跌，买单成交90%

            做空网格：Grid 1=最高价，Grid N=最低价
                     从Grid N（低价）往上数 trigger_percent
                     例如：90% → Grid 20（接近高价）
                     意义：价格上涨，卖单成交90%

        例如（无论做多还是做空）：
            grid_count=200, trigger_percent=90%
            触发点 = 200 - (200 * 90%) = 20

            做多：价格从$2.49下跌到接近Grid 20（约$2.13），买单成交90%
            做空：价格从$2.09上涨到接近Grid 20（约$2.45），卖单成交90%
        """
        # 无论做多还是做空，计算方式相同
        trigger_offset = int(
            self.grid_count * self.scalping_trigger_percent / 100)
        trigger_grid = self.grid_count - trigger_offset
        return max(1, trigger_grid)  # 确保至少为1

    def find_nearest_grid_index(self, price: Decimal, direction: str = "conservative") -> int:
        """
        根据价格找到最接近的网格索引

        Args:
            price: 价格
            direction: 取整方向
                - "conservative": 保守（做多向下，做空向上）
                - "exact": 精确（四舍五入）

        Returns:
            最接近的网格索引（1-based）

        逻辑：
            做多网格：Grid 1 = 最低价，Grid N = 最高价
            做空网格：Grid 1 = 最高价，Grid N = 最低价
        """
        if self.grid_type in [GridType.LONG, GridType.FOLLOW_LONG, GridType.MARTINGALE_LONG]:
            # 做多网格：Grid 1 = lower_price，向上递增
            index = (price - self.lower_price) / self.grid_interval + 1
            if direction == "conservative":
                # 向下取整（保守，持仓成本往低取）
                return max(1, int(index))
            else:
                # 四舍五入
                return max(1, min(int(round(index)), self.grid_count))
        else:
            # 做空网格：Grid 1 = upper_price，向下递减
            index = (self.upper_price - price) / self.grid_interval + 1
            if direction == "conservative":
                # 向下取整（保守，持仓成本往高取）
                return max(1, int(index))
            else:
                # 四舍五入
                return max(1, min(int(round(index)), self.grid_count))

    def is_scalping_enabled(self) -> bool:
        """判断是否启用剥头皮模式"""
        return self.scalping_enabled

    def get_capital_protection_trigger_grid(self) -> int:
        """
        获取本金保护触发的网格索引

        Returns:
            触发本金保护的网格索引（1-based）

        逻辑说明：
            做多网格：Grid 1=最低价，Grid N=最高价
                     从Grid N（高价）往下数 trigger_percent
                     例如：1% → Grid 198（接近高价）
                     意义：价格轻微下跌1%就触发保护

            做空网格：Grid 1=最高价，Grid N=最低价
                     从Grid N（低价）往上数 trigger_percent
                     例如：1% → Grid 198（接近高价）
                     意义：价格轻微上涨1%就触发保护

        例如（无论做多还是做空）：
            grid_count=200, trigger_percent=1%
            触发点 = 200 - (200 * 1%) = 198

            grid_count=200, trigger_percent=40%
            触发点 = 200 - (200 * 40%) = 120
        """
        # 无论做多还是做空，计算方式相同
        trigger_offset = int(
            self.grid_count * self.capital_protection_trigger_percent / 100)
        trigger_grid = self.grid_count - trigger_offset
        return max(1, trigger_grid)  # 确保至少为1

    def is_capital_protection_enabled(self) -> bool:
        """判断是否启用本金保护模式"""
        return self.capital_protection_enabled

    def is_stop_loss_enabled(self) -> bool:
        """Return whether price-based stop loss is enabled."""
        return self.stop_loss_enabled

    def check_stop_loss(self, current_price: Decimal) -> bool:
        """Return whether current price has crossed the configured stop-loss level."""
        if not self.stop_loss_enabled or self.stop_loss_price is None:
            return False

        if self.grid_type in [GridType.LONG, GridType.FOLLOW_LONG, GridType.MARTINGALE_LONG]:
            return current_price <= self.stop_loss_price

        if self.grid_type in [GridType.SHORT, GridType.FOLLOW_SHORT, GridType.MARTINGALE_SHORT]:
            return current_price >= self.stop_loss_price

        return False

    def __repr__(self) -> str:
        mode = "Martingale" if self.is_martingale_mode() else "Normal"
        return (
            f"GridConfig(exchange={self.exchange}, symbol={self.symbol}, "
            f"type={self.grid_type.value}, mode={mode}, "
            f"range=[{self.lower_price}, {self.upper_price}], "
            f"interval={self.grid_interval}, grids={self.grid_count})"
        )

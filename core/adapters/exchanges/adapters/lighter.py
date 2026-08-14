"""
Lighter交易所适配器

基于MESA架构的Lighter适配器，提供统一的交易接口。
使用Lighter SDK进行API交互和WebSocket连接。
整合了分离的模块：lighter_base.py、lighter_rest.py、lighter_websocket.py
"""

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from decimal import Decimal
import yaml
import os

from ....logging import get_logger

from ..adapter import ExchangeAdapter
from ..interface import ExchangeConfig
from ..models import *
from ..subscription_manager import create_subscription_manager, DataType
from .lighter_rest import LighterRest
from .lighter_websocket import LighterWebSocket


class LighterAdapter(ExchangeAdapter):
    """Lighter交易所适配器 - 统一接口"""

    def __init__(self, config: ExchangeConfig, event_bus=None):
        super().__init__(config, event_bus)

        # 初始化各个模块
        config_dict = self._convert_config_to_dict(config)
        self._rest = LighterRest(config_dict)
        self._websocket = LighterWebSocket(
            config_dict, signer_client=self._rest.signer_client)
        self._base = self._rest

        # 共享数据缓存
        shared_position_cache = {}
        shared_order_cache = {}

        self._position_cache = shared_position_cache
        self._order_cache = shared_order_cache

        # 设置回调列表
        shared_position_callbacks = []
        shared_order_callbacks = []

        self._position_callbacks = shared_position_callbacks
        self._order_callbacks = shared_order_callbacks

        # 设置基础URL
        self.base_url = getattr(
            config, 'base_url', None) or self._base.base_url
        self.ws_url = getattr(config, 'ws_url', None) or self._base.ws_url

        # 符号映射
        self._symbol_mapping = getattr(config, 'symbol_mapping', {})

        # 连接状态
        self._connected = False
        self._authenticated = False

        # 缓存支持的交易对
        self._supported_symbols = []
        self._market_info = {}

        # 初始化订阅管理器
        try:
            config_dict = self._load_lighter_config()

            symbol_cache_service = self._get_symbol_cache_service()

            self._subscription_manager = create_subscription_manager(
                exchange_config=config_dict,
                symbol_cache_service=symbol_cache_service,
                logger=self.logger
            )

            if self.logger:
                self.logger.info(
                    f"✅ Lighter订阅管理器初始化成功，模式: {config_dict.get('subscription_mode', {}).get('mode', 'unknown')}")

        except Exception as e:
            if self.logger:
                self.logger.warning(f"创建Lighter订阅管理器失败，使用默认配置: {e}")
            # 使用默认配置
            default_config = {
                'exchange_id': 'lighter',
                'subscription_mode': {
                    'mode': 'predefined',
                    'predefined': {
                        'symbols': ['BTC-USD', 'ETH-USD', 'SOL-USD'],
                        'data_types': {'ticker': True, 'orderbook': True, 'trades': False, 'user_data': False}
                    }
                }
            }

            symbol_cache_service = self._get_symbol_cache_service()
            self._subscription_manager = create_subscription_manager(
                exchange_config=default_config,
                symbol_cache_service=symbol_cache_service,
                logger=self.logger
            )

        self.logger.info("Lighter适配器初始化完成")

    def _convert_config_to_dict(self, config: ExchangeConfig) -> Dict[str, Any]:
        """
        将ExchangeConfig转换为字典

        如果ExchangeConfig中没有Lighter特有的配置，则从lighter_config.yaml加载

        Args:
            config: ExchangeConfig对象

        Returns:
            配置字典
        """
        # 先尝试从ExchangeConfig获取
        config_dict = {
            "testnet": getattr(config, 'testnet', False),
            "api_key_private_key": getattr(config, 'api_key_private_key', ''),
            "account_index": getattr(config, 'account_index', 0),
            "api_key_index": getattr(config, 'api_key_index', 0),
        }

        # 如果api_key_private_key为空，从配置文件加载
        if not config_dict.get('api_key_private_key'):
            try:
                lighter_config = self._load_lighter_config()
                api_config = lighter_config.get('api_config', {})
                auth_config = api_config.get('auth', {})

                config_dict['api_key_private_key'] = auth_config.get(
                    'api_key_private_key', '')
                config_dict['account_index'] = auth_config.get(
                    'account_index', 0)
                config_dict['api_key_index'] = auth_config.get(
                    'api_key_index', 0)
                config_dict['testnet'] = api_config.get('testnet', False)

                if self.logger:
                    self.logger.info("✅ 从lighter_config.yaml加载API配置")
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"⚠️ 无法从配置文件加载Lighter配置: {e}")

        # 添加可选配置
        if getattr(config, 'base_url', None):
            config_dict['api_url'] = config.base_url
        if getattr(config, 'ws_url', None):
            config_dict['ws_url'] = config.ws_url

        return config_dict

    def _load_lighter_config(self) -> Dict[str, Any]:
        """加载Lighter配置文件"""
        config_path = "config/exchanges/lighter_config.yaml"

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                if self.logger:
                    self.logger.info(f"✅ 加载Lighter配置文件: {config_path}")
                return config
        except FileNotFoundError:
            if self.logger:
                self.logger.warning(f"Lighter配置文件未找到: {config_path}")
            return {'exchange_id': 'lighter'}
        except Exception as e:
            if self.logger:
                self.logger.error(f"加载Lighter配置文件失败: {e}")
            return {'exchange_id': 'lighter'}

    def _get_symbol_cache_service(self):
        """获取符号缓存服务实例"""
        try:
            from core.services.symbol_manager.implementations.symbol_conversion_service import SymbolConversionService
            return SymbolConversionService.get_instance()
        except Exception as e:
            if self.logger:
                self.logger.warning(f"无法获取符号缓存服务: {e}")
            return None

    # ============= 连接管理 =============

    async def connect(self) -> bool:
        """
        建立连接

        Returns:
            是否连接成功
        """
        try:
            if self._connected:
                self.logger.info("已经连接到Lighter")
                return True

            # 初始化REST客户端
            await self._rest.initialize()

            # 建立WebSocket连接
            await self._websocket.connect()

            # 加载市场信息
            await self._load_market_info()

            self._connected = True
            self._authenticated = bool(self._rest.signer_client)

            self.logger.info("✅ 成功连接到Lighter交易所")
            return True

        except Exception as e:
            self.logger.error(f"连接Lighter失败: {e}")
            return False

    async def disconnect(self):
        """断开连接"""
        try:
            # 关闭WebSocket
            await self._websocket.disconnect()

            # 关闭REST客户端
            await self._rest.close()

            self._connected = False
            self._authenticated = False

            self.logger.info("已断开与Lighter的连接")

        except Exception as e:
            self.logger.error(f"断开Lighter连接时出错: {e}")

    async def authenticate(self) -> bool:
        """
        进行身份认证（ExchangeInterface标准方法）

        Returns:
            bool: 认证是否成功
        """
        # Lighter的认证在初始化时完成（通过SignerClient）
        # 这里只需要检查是否已经认证
        if self._rest.signer_client:
            self._authenticated = True
            self.logger.info("✅ Lighter认证已完成")
            return True
        else:
            self.logger.warning("⚠️ Lighter未配置SignerClient")
            return False

    async def health_check(self) -> Dict[str, Any]:
        """
        健康检查（ExchangeInterface标准方法）

        Returns:
            Dict: 健康状态信息
        """
        status = {
            "exchange": "lighter",
            "connected": self._connected,
            "authenticated": self._authenticated,
            "timestamp": datetime.now().isoformat()
        }

        try:
            # 尝试获取交易所信息作为健康检查
            if self._connected:
                info = await self.get_exchange_info()
                status["healthy"] = True
                status["market_count"] = len(
                    info.symbols) if info and info.symbols else 0
            else:
                status["healthy"] = False
                status["error"] = "Not connected"
        except Exception as e:
            status["healthy"] = False
            status["error"] = str(e)

        return status

    async def _load_market_info(self):
        """加载市场信息"""
        try:
            exchange_info = await self._rest.get_exchange_info()

            if exchange_info and exchange_info.symbols:
                self._supported_symbols = [s['symbol']
                                           for s in exchange_info.symbols]
                self._market_info = {s['symbol']                                     : s for s in exchange_info.symbols}

                # 更新base模块的市场缓存
                self._base.update_markets_cache(exchange_info.symbols)

                # 同步到REST和WebSocket模块
                self._rest._markets_cache = self._base._markets_cache
                self._rest._symbol_to_market_index = self._base._symbol_to_market_index
                self._websocket._markets_cache = self._base._markets_cache
                self._websocket._symbol_to_market_index = self._base._symbol_to_market_index

                self.logger.info(f"加载了 {len(self._supported_symbols)} 个交易对")
        except Exception as e:
            self.logger.error(f"加载市场信息失败: {e}")

    def is_connected(self) -> bool:
        """
        检查是否已连接

        Returns:
            是否已连接
        """
        return self._connected

    # ============= 市场数据 =============

    async def get_exchange_info(self) -> ExchangeInfo:
        """
        获取交易所信息

        Returns:
            ExchangeInfo对象
        """
        return await self._rest.get_exchange_info()

    async def get_ticker(self, symbol: str) -> Optional[TickerData]:
        """
        获取ticker数据

        Args:
            symbol: 交易对符号

        Returns:
            TickerData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_ticker(normalized_symbol)

    async def get_tickers(self, symbols: Optional[List[str]] = None) -> List[TickerData]:
        """
        获取多个交易对行情（ExchangeInterface标准方法）

        Args:
            symbols: 交易对符号列表，None表示获取所有

        Returns:
            List[TickerData]: 行情数据列表
        """
        if symbols is None:
            # 获取所有支持的交易对
            symbols = self._supported_symbols

        tickers = []
        for symbol in symbols:
            try:
                ticker = await self.get_ticker(symbol)
                if ticker:
                    tickers.append(ticker)
            except Exception as e:
                self.logger.error(f"获取ticker失败 {symbol}: {e}")

        return tickers

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Optional[OrderBookData]:
        """
        获取订单簿

        Args:
            symbol: 交易对符号
            limit: 深度限制

        Returns:
            OrderBookData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_orderbook(normalized_symbol, limit)

    async def get_trades(
        self,
        symbol: str,
        since: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[TradeData]:
        """
        获取最近成交记录（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            since: 开始时间（暂不支持）
            limit: 数据条数限制

        Returns:
            List[TradeData]: 成交数据列表
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_recent_trades(normalized_symbol, limit or 100)

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[TradeData]:
        """
        获取最近成交（兼容旧接口）

        Args:
            symbol: 交易对符号
            limit: 数量限制

        Returns:
            TradeData列表
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_recent_trades(normalized_symbol, limit)

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[OHLCVData]:
        """
        获取K线数据（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            timeframe: 时间框架（如'1m', '5m', '1h', '1d'）
            since: 开始时间
            limit: 数据条数限制

        Returns:
            List[OHLCVData]: K线数据列表
        """
        # Lighter SDK目前可能不支持K线数据
        # 返回空列表，但记录警告
        self.logger.warning(f"Lighter适配器暂不支持K线数据查询")
        return []

    # ============= 账户信息 =============

    async def get_balances(self) -> List[BalanceData]:
        """
        获取账户余额（ExchangeInterface标准方法）

        Returns:
            List[BalanceData]: 余额数据列表
        """
        return await self._rest.get_account_balance()

    async def get_account_balance(self) -> List[BalanceData]:
        """
        获取账户余额（兼容旧接口）

        Returns:
            BalanceData列表
        """
        return await self._rest.get_account_balance()

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        获取活跃订单

        Args:
            symbol: 交易对符号（可选）

        Returns:
            OrderData列表
        """
        normalized_symbol = self._normalize_symbol(symbol) if symbol else None
        return await self._rest.get_open_orders(normalized_symbol)

    async def get_positions(self, symbols: Optional[List[str]] = None) -> List[PositionData]:
        """
        获取持仓信息（ExchangeInterface标准方法）

        Args:
            symbols: 交易对符号列表，None表示获取所有

        Returns:
            List[PositionData]: 持仓数据列表
        """
        # 🔥 重要修复：始终从REST API获取最新持仓数据，不使用缓存
        # 原因：缓存可能不同步，导致持仓数据不准确（特别是多笔成交累加的情况）
        # position_monitor依赖准确的持仓数据进行监控和异常检测
        positions = await self._rest.get_positions(symbols)

        # 🔥 更新缓存，确保缓存与REST API同步
        # 清空旧缓存
        if symbols:
            for symbol in symbols:
                self._position_cache.pop(symbol, None)

        # 写入新数据
        if positions:
            from decimal import Decimal
            for position in positions:
                # 统一使用LONG=正数, SHORT=负数的符号约定
                signed_size = position.size if position.side.value.lower() == 'long' else - \
                    position.size
                self._position_cache[position.symbol] = {
                    'symbol': position.symbol,
                    'size': signed_size,
                    'side': position.side.value,
                    'entry_price': position.entry_price,
                    'unrealized_pnl': position.unrealized_pnl or Decimal('0'),
                    'timestamp': position.timestamp,
                }

        return positions

    async def get_order_history(
        self,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[OrderData]:
        """
        获取历史订单（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            since: 开始时间（暂不支持）
            limit: 数据条数限制（暂不支持）

        Returns:
            List[OrderData]: 历史订单列表
        """
        # Lighter SDK可能不直接支持历史订单查询
        # 暂时返回空列表
        self.logger.warning("Lighter适配器暂不支持历史订单查询")
        return []

    # ============= 交易功能 =============

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: Decimal,
        price: Optional[Decimal] = None,
        params: Optional[Dict[str, Any]] = None,
        batch_mode: bool = False
    ) -> OrderData:
        """
        创建订单（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            side: 订单方向（OrderSide枚举）
            order_type: 订单类型（OrderType枚举）
            amount: 数量
            price: 价格（限价单必需）
            params: 额外参数
            batch_mode: 批量模式（避免频繁查询order_index）

        Returns:
            OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)

        # 转换枚举类型为字符串
        side_str = side.value.lower()  # "buy" 或 "sell"
        order_type_str = order_type.value.lower()  # "limit" 或 "market"

        # 调用内部的place_order方法，传递 batch_mode
        return await self._rest.place_order(
            normalized_symbol, side_str, order_type_str, amount, price,
            batch_mode=batch_mode, **(params or {})
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        **kwargs
    ) -> Optional[OrderData]:
        """
        下单（兼容旧接口）

        Args:
            symbol: 交易对符号
            side: 订单方向 ("buy" 或 "sell")
            order_type: 订单类型 ("limit" 或 "market")
            quantity: 数量
            price: 价格（限价单必需）
            **kwargs: 其他参数

        Returns:
            OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.place_order(
            normalized_symbol, side, order_type, quantity, price, **kwargs
        )

    async def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        reduce_only: bool = False,
        skip_order_index_query: bool = False
    ) -> Optional[OrderData]:
        """
        下市价单（便捷方法）

        Args:
            symbol: 交易对符号
            side: 订单方向
            quantity: 数量
            reduce_only: 只减仓模式（平仓专用，不会开新仓或加仓）
            skip_order_index_query: 跳过 order_index 查询（Volume Maker 使用）

        Returns:
            订单数据 或 None
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.place_market_order(
            normalized_symbol, side, quantity, reduce_only, skip_order_index_query
        )

    async def get_order(self, order_id: str, symbol: str) -> OrderData:
        """
        获取订单信息（ExchangeInterface标准方法）

        Args:
            order_id: 订单ID
            symbol: 交易对符号

        Returns:
            OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        return await self._rest.get_order(order_id, normalized_symbol)

    async def cancel_order(self, order_id: str, symbol: str) -> OrderData:
        """
        取消订单（ExchangeInterface标准方法）

        Args:
            order_id: 订单ID
            symbol: 交易对符号

        Returns:
            被取消的OrderData对象
        """
        normalized_symbol = self._normalize_symbol(symbol)
        success = await self._rest.cancel_order(normalized_symbol, order_id)

        if success:
            # 尝试获取订单信息
            try:
                order = await self.get_order(order_id, symbol)
                return order
            except:
                # 如果获取失败，返回一个基本的OrderData对象
                return OrderData(
                    id=order_id,
                    order_id=order_id,
                    symbol=normalized_symbol,
                    side=OrderSide.BUY,  # 占位符
                    order_type=OrderType.LIMIT,  # 占位符
                    amount=Decimal("0"),
                    filled=Decimal("0"),
                    remaining=Decimal("0"),
                    status=OrderStatus.CANCELED,
                    price=None,
                    average_price=None,
                    timestamp=datetime.now(),
                    raw_data={}
                )
        else:
            raise Exception(f"Failed to cancel order {order_id}")

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        取消所有订单（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号（可选，为None时取消所有）

        Returns:
            被取消的订单列表
        """
        try:
            orders = await self.get_open_orders(symbol)

            cancelled_orders = []
            for order in orders:
                try:
                    # 注意：cancel_order现在接受 (order_id, symbol) 的顺序
                    cancelled_order = await self.cancel_order(order.order_id, order.symbol)
                    cancelled_orders.append(cancelled_order)
                except Exception as e:
                    self.logger.error(f"取消订单 {order.order_id} 失败: {e}")

            return cancelled_orders

        except Exception as e:
            self.logger.error(f"取消所有订单失败: {e}")
            return []

    # ============= WebSocket订阅 =============

    async def subscribe_user_data(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        订阅用户数据流（订单更新、持仓变化等）

        这是ExchangeInterface标准方法，网格系统使用此方法监控订单成交

        Args:
            callback: 数据回调函数
        """
        # Lighter的用户数据流包括订单和持仓更新
        # 我们订阅订单更新流，这是网格系统最关键的需求
        await self._websocket.subscribe_orders(callback)
        self.logger.info("✅ 已订阅Lighter用户数据流（订单更新）")

    async def subscribe_ticker(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅ticker数据

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.subscribe_ticker(normalized_symbol, callback)

    async def subscribe_orderbook(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅订单簿

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.subscribe_orderbook(
            normalized_symbol, callback=callback)

    async def subscribe_trades(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅成交数据

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.subscribe_trades(normalized_symbol, callback)

    async def subscribe_orders(self, callback: Optional[Callable] = None):
        """
        订阅订单更新

        Args:
            callback: 数据回调函数
        """
        if callback:
            self._order_callbacks.append(callback)
        await self._websocket.subscribe_orders(callback)

    async def subscribe_positions(self, callback: Optional[Callable] = None):
        """
        订阅持仓更新

        Args:
            callback: 数据回调函数
        """
        if callback:
            self._position_callbacks.append(callback)
        await self._websocket.subscribe_positions(callback)

    async def unsubscribe_ticker(self, symbol: str):
        """取消订阅ticker"""
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.unsubscribe_ticker(normalized_symbol)

    async def unsubscribe_orderbook(self, symbol: str):
        """取消订阅订单簿"""
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.unsubscribe_orderbook(normalized_symbol)

    async def unsubscribe_trades(self, symbol: str):
        """取消订阅成交"""
        normalized_symbol = self._normalize_symbol(symbol)
        await self._websocket.unsubscribe_trades(normalized_symbol)

    async def unsubscribe(self, symbol: Optional[str] = None) -> None:
        """
        取消订阅（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号，None表示取消所有订阅
        """
        try:
            if symbol:
                # 取消特定符号的所有订阅
                normalized_symbol = self._normalize_symbol(symbol)
                await self._websocket.unsubscribe_ticker(normalized_symbol)
                await self._websocket.unsubscribe_orderbook(normalized_symbol)
                await self._websocket.unsubscribe_trades(normalized_symbol)
                self.logger.info(f"✅ 已取消订阅: {symbol}")
            else:
                # 取消所有订阅
                await self._websocket.disconnect()
                self.logger.info("✅ 已取消所有订阅")
        except Exception as e:
            self.logger.error(f"取消订阅失败: {e}")

    # ============= 杠杆和保证金 =============

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """
        设置杠杆倍数（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            leverage: 杠杆倍数

        Returns:
            Dict: 设置结果
        """
        # Lighter SDK中可能有对应的方法，这里先返回警告
        self.logger.warning("Lighter适配器暂不支持设置杠杆")
        return {
            "success": False,
            "message": "Lighter暂不支持杠杆设置"
        }

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> Dict[str, Any]:
        """
        设置保证金模式（ExchangeInterface标准方法）

        Args:
            symbol: 交易对符号
            margin_mode: 保证金模式（'cross' 或 'isolated'）

        Returns:
            Dict: 设置结果
        """
        # Lighter支持保证金模式，但需要通过SDK实现
        self.logger.warning("Lighter适配器暂不支持设置保证金模式")
        return {
            "success": False,
            "message": "Lighter暂不支持保证金模式设置"
        }

    # ============= 辅助方法 =============

    def _normalize_symbol(self, symbol: str) -> str:
        """
        标准化交易对符号

        Args:
            symbol: 原始符号

        Returns:
            标准化后的符号
        """
        if not symbol:
            return symbol

        # 自定义映射的目标也必须转换成 Lighter 当前使用的原生 symbol。
        mapped_symbol = self._symbol_mapping.get(symbol, symbol)
        return self._base.normalize_symbol(mapped_symbol)

    def get_supported_symbols(self) -> List[str]:
        """
        获取支持的交易对列表

        Returns:
            交易对符号列表
        """
        return self._supported_symbols.copy()

    def __repr__(self) -> str:
        """字符串表示"""
        return f"LighterAdapter(connected={self._connected}, symbols={len(self._supported_symbols)})"

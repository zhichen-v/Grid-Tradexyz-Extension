"""
Lighter交易所适配器 - WebSocket模块

封装Lighter SDK的WebSocket功能，提供实时数据流
"""

from typing import Dict, Any, Optional, List, Callable
from decimal import Decimal, InvalidOperation
from datetime import datetime
import asyncio
import logging
import json
import time

try:
    import lighter
    from lighter import WsClient
    LIGHTER_AVAILABLE = True
except ImportError:
    LIGHTER_AVAILABLE = False
    logging.warning("lighter SDK未安装")

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    logging.warning("websockets库未安装，无法使用直接订阅功能")

from .lighter_base import LighterBase
from ..models import (
    TickerData, OrderBookData, TradeData, OrderData, PositionData,
    OrderBookLevel, OrderStatus, OrderSide, OrderType, PositionSide, MarginMode
)

logger = logging.getLogger(__name__)


class LighterWebSocket(LighterBase):
    """Lighter WebSocket客户端"""

    def __init__(self, config: Dict[str, Any], signer_client: Any = None):
        """
        初始化Lighter WebSocket客户端

        Args:
            config: 配置字典
        """
        if not LIGHTER_AVAILABLE:
            raise ImportError("lighter SDK未安装，无法使用Lighter WebSocket")

        super().__init__(config, signer_client=signer_client)

        # WebSocket客户端（SDK的WsClient，用于订阅account_all）
        self.ws_client: Optional[WsClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._stopping_sdk_ws = False

        # 直接WebSocket连接（用于account_all_orders、market_stats和trade）
        self._direct_ws = None
        self._direct_ws_task: Optional[asyncio.Task] = None
        self._direct_ws_connected = False
        self._account_orders_subscribed = False
        self._direct_last_message_time = 0.0
        self._subscribed_market_stats: List[int] = []  # 订阅的market_stats市场
        self._subscribed_trades: List[int] = []

        # 保存事件循环引用
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        # 订阅的市场和账户
        self._subscribed_markets: List[int] = []
        self._subscribed_accounts: List[int] = []

        # 数据缓存
        self._order_books: Dict[str, OrderBookData] = {}
        self._account_data: Dict[str, Any] = {}

        # 回调函数
        self._ticker_callbacks: List[Callable] = []
        self._orderbook_callbacks: List[Callable] = []
        self._trade_callbacks: List[Callable] = []
        self._order_callbacks: List[Callable] = []
        self._order_fill_callbacks: List[Callable] = []  # 🔥 新增：订单成交回调
        self._position_callbacks: List[Callable] = []

        # 连接状态
        self._connected = False
        self._reconnect_attempts = 0
        self._reconnect_count = 0
        self._direct_reconnect_count = 0
        self._max_reconnect_attempts = 10
        self._lifecycle_lock = asyncio.Lock()
        self._reconnect_task: Optional[asyncio.Task] = None
        self._connection_generation = 0
        self._explicit_stop = True

        # 🔥 确保logger有文件handler，写入ExchangeAdapter.log
        self._setup_logger()

        logger.info("Lighter WebSocket客户端初始化完成")

    def _setup_logger(self):
        """设置logger的文件handler"""
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        # 确保logs目录存在
        Path("logs").mkdir(parents=True, exist_ok=True)

        # 检查是否已有文件handler
        has_file_handler = any(
            isinstance(h, RotatingFileHandler) and 'ExchangeAdapter.log' in str(
                h.baseFilename)
            for h in logger.handlers
        )

        if not has_file_handler:
            # 添加文件handler
            file_handler = RotatingFileHandler(
                'logs/ExchangeAdapter.log',
                maxBytes=10*1024*1024,  # 10MB
                backupCount=3,
                encoding='utf-8'
            )
            file_handler.setLevel(logging.INFO)
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.setLevel(logging.INFO)  # 确保logger级别至少是INFO
            logger.info("✅ ExchangeAdapter.log 文件handler已配置")

    # ============= 连接管理 =============

    async def connect(self):
        """建立WebSocket连接"""
        async with self._lifecycle_lock:
            if self._connected:
                logger.warning("WebSocket已连接")
                return

            self._explicit_stop = False
            self._connection_generation += 1
            await self._connect_locked()

    async def _connect_locked(self):
        """Mark the client connected while the lifecycle lock is held."""
        try:
            # 🔥 保存事件循环引用（用于线程安全的回调调度）
            self._event_loop = asyncio.get_event_loop()

            # 注意：lighter的WsClient是同步的，需要在单独的线程中运行
            # 这里我们先不启动，等待订阅后再启动
            self._connected = True
            logger.info("Lighter WebSocket准备就绪")

        except Exception as e:
            logger.error(f"WebSocket连接失败: {e}")
            raise

    async def disconnect(self):
        """断开WebSocket连接"""
        self._explicit_stop = True
        self._connection_generation += 1
        self._connected = False

        current_task = asyncio.current_task()
        reconnect_task = self._reconnect_task
        if (
            reconnect_task is not None
            and reconnect_task is not current_task
            and not reconnect_task.done()
        ):
            reconnect_task.cancel()
            try:
                await reconnect_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"等待WebSocket重连任务退出时出错: {e}")

        if self._reconnect_task is reconnect_task:
            self._reconnect_task = None

        try:
            async with self._lifecycle_lock:
                await self._disconnect_locked()
            logger.info("✅ WebSocket已断开（包括直接订阅）")
        except Exception as e:
            logger.error(f"断开WebSocket时出错: {e}")
        finally:
            self._connected = False
            self._direct_ws_connected = False
            self._account_orders_subscribed = False

    async def _disconnect_locked(self):
        """Close active sockets while the lifecycle lock is held."""
        self._connected = False

        await self._stop_sdk_ws_client()

        # 关闭直接WebSocket连接
        if self._direct_ws_task and not self._direct_ws_task.done():
            self._direct_ws_task.cancel()
            try:
                await self._direct_ws_task
            except asyncio.CancelledError:
                pass
        self._direct_ws_task = None

        if self._direct_ws:
            try:
                await self._direct_ws.close()
            except Exception:
                pass
            self._direct_ws = None

        self._direct_ws_connected = False
        self._account_orders_subscribed = False

    def _schedule_reconnect(self):
        """Schedule at most one reconnect for the active connection generation."""
        if self._explicit_stop or not self._connected:
            return None

        existing = self._reconnect_task
        if existing is not None and not existing.done():
            return existing

        generation = self._connection_generation
        task = asyncio.create_task(self.reconnect(generation))
        self._reconnect_task = task

        def clear_reconnect_task(done_task):
            if self._reconnect_task is done_task:
                self._reconnect_task = None
            if not done_task.cancelled():
                try:
                    error = done_task.exception()
                except asyncio.CancelledError:
                    return
                if error is not None:
                    logger.error(f"WebSocket重连任务异常退出: {error}")

        task.add_done_callback(clear_reconnect_task)
        return task

    async def reconnect(self, expected_generation: Optional[int] = None):
        """重新连接WebSocket"""
        generation = (
            self._connection_generation
            if expected_generation is None
            else expected_generation
        )

        while self._reconnect_attempts < self._max_reconnect_attempts:
            if self._explicit_stop or generation != self._connection_generation:
                return

            self._reconnect_count = getattr(self, "_reconnect_count", 0) + 1
            logger.info("尝试重新连接WebSocket...")

            try:
                async with self._lifecycle_lock:
                    if self._explicit_stop or generation != self._connection_generation:
                        return
                    await self._disconnect_locked()
                    if self._explicit_stop or generation != self._connection_generation:
                        return

                await asyncio.sleep(min(self._reconnect_attempts * 2, 30))
                if self._explicit_stop or generation != self._connection_generation:
                    return

                async with self._lifecycle_lock:
                    if self._explicit_stop or generation != self._connection_generation:
                        return
                    await self._connect_locked()
                    await self._resubscribe_all()
                    if self._explicit_stop or generation != self._connection_generation:
                        return

                self._reconnect_attempts = 0
                logger.info("WebSocket重连成功")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._reconnect_attempts += 1
                logger.error(
                    f"WebSocket重连失败 (尝试 {self._reconnect_attempts}/{self._max_reconnect_attempts}): {e}")

    async def _resubscribe_all(self):
        """重新订阅所有频道"""
        # Subscription lists survive disconnect(), so the public subscribe
        # methods would be no-ops here. Recreate the SDK stream directly.
        if self._subscribed_markets or self._subscribed_accounts:
            await self._recreate_ws_client()

        if self._needs_direct_ws():
            await self._ensure_direct_ws_running()

    def _needs_direct_ws(self) -> bool:
        """Return whether any active subscription depends on the direct socket."""
        return bool(
            self._order_callbacks
            or self._order_fill_callbacks
            or self._subscribed_market_stats
            or self._subscribed_trades
        )

    def get_connection_status(self) -> Dict[str, Any]:
        """Return connection health including the direct account-order stream."""
        direct_required = self._needs_direct_ws()
        account_orders_required = bool(
            self._order_callbacks or self._order_fill_callbacks
        )
        sdk_required = bool(
            getattr(self, "_subscribed_markets", ())
            or getattr(self, "_subscribed_accounts", ())
        )
        sdk_task_running = bool(
            getattr(self, "_ws_task", None)
            and not self._ws_task.done()
        )
        task_running = bool(
            self._direct_ws_task and not self._direct_ws_task.done()
        )
        sdk_healthy = not sdk_required or sdk_task_running
        direct_healthy = (
            not direct_required
            or (
                task_running
                and self._direct_ws_connected
                and (
                    not account_orders_required
                    or self._account_orders_subscribed
                )
            )
        )
        reconnect_count = getattr(self, "_reconnect_count", 0)
        direct_reconnect_count = getattr(self, "_direct_reconnect_count", 0)
        return {
            "connected": self._connected,
            "reconnect_attempts": getattr(self, "_reconnect_attempts", 0),
            "reconnect_count": reconnect_count + direct_reconnect_count,
            "public_reconnect_count": reconnect_count,
            "direct_reconnect_count": direct_reconnect_count,
            "sdk_required": sdk_required,
            "sdk_task_running": sdk_task_running,
            "direct_required": direct_required,
            "direct_task_running": task_running,
            "direct_connected": self._direct_ws_connected,
            "account_orders_subscribed": self._account_orders_subscribed,
            "last_direct_message_time": self._direct_last_message_time,
            "healthy": self._connected and sdk_healthy and direct_healthy,
        }

    # ============= 订阅管理 =============

    async def subscribe_ticker(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅ticker数据（使用market_stats频道）

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        market_index = self.get_market_index(symbol)
        if market_index is None:
            logger.warning(f"未找到交易对 {symbol} 的市场索引")
            return

        if callback:
            self._ticker_callbacks.append(callback)

        # 🔥 使用market_stats代替orderbook
        await self.subscribe_market_stats(market_index, symbol)

    async def subscribe_orderbook(
        self,
        market_index_or_symbol,
        symbol: Optional[str] = None,
        callback: Optional[Callable] = None,
    ):
        """
        订阅订单簿

        Args:
            market_index_or_symbol: 市场索引或交易对符号
            symbol: 交易对符号（如果第一个参数是市场索引）
        """
        if callback:
            self._orderbook_callbacks.append(callback)

        if isinstance(market_index_or_symbol, str):
            symbol = market_index_or_symbol
            market_index = self.get_market_index(symbol)
            if market_index is None:
                logger.warning(f"未找到交易对 {symbol} 的市场索引")
                return
        else:
            market_index = market_index_or_symbol
            if symbol is None:
                symbol = self._get_symbol_from_market_index(market_index)

        if market_index not in self._subscribed_markets:
            self._subscribed_markets.append(market_index)
            logger.info(f"已订阅订单簿: {symbol} (market_index={market_index})")

            # 如果WsClient已创建，需要重新创建以包含新的订阅
            await self._recreate_ws_client()

    async def subscribe_market_stats(self, market_index_or_symbol, symbol: Optional[str] = None):
        """
        订阅市场统计数据（market_stats频道，用于获取价格）

        Args:
            market_index_or_symbol: 市场索引或交易对符号
            symbol: 交易对符号（如果第一个参数是市场索引）
        """
        if isinstance(market_index_or_symbol, str):
            symbol = market_index_or_symbol
            market_index = self.get_market_index(symbol)
            if market_index is None:
                logger.warning(f"未找到交易对 {symbol} 的市场索引")
                return
        else:
            market_index = market_index_or_symbol
            if symbol is None:
                symbol = self._get_symbol_from_market_index(market_index)

        if market_index not in self._subscribed_market_stats:
            self._subscribed_market_stats.append(market_index)
            logger.info(
                f"🔔 已订阅market_stats: {symbol} (market_index={market_index})")

            # 启动直接WebSocket订阅（如果尚未启动）
            await self._ensure_direct_ws_running()

    async def _ensure_direct_ws_running(self):
        """确保直接WebSocket订阅任务正在运行"""
        if not WEBSOCKETS_AVAILABLE:
            logger.warning("⚠️ websockets库未安装，无法直接订阅market_stats")
            return

        if self._direct_ws_task and not self._direct_ws_task.done():
            # 任务已在运行，发送新的订阅消息
            if self._direct_ws:
                await self._send_market_stats_subscriptions()
                await self._send_trade_subscriptions()
        else:
            # 启动新任务
            self._direct_ws_task = asyncio.create_task(
                self._run_direct_ws_subscription())
            logger.info("🚀 已启动直接订阅WebSocket任务（market_stats）")

    async def _send_market_stats_subscriptions(self):
        """发送market_stats订阅消息"""
        if not self._direct_ws:
            return

        for market_index in self._subscribed_market_stats:
            subscribe_msg = {
                "type": "subscribe",
                "channel": f"market_stats/{market_index}"
            }
            try:
                await self._direct_ws.send(json.dumps(subscribe_msg))
                logger.info(f"📤 发送market_stats订阅: market_index={market_index}")
            except Exception as e:
                logger.error(f"发送market_stats订阅失败: {e}")

    async def _send_trade_subscriptions(self):
        """发送公开成交频道订阅消息。"""
        if not self._direct_ws:
            return

        for market_index in self._subscribed_trades:
            try:
                await self._direct_ws.send(json.dumps({
                    "type": "subscribe",
                    "channel": f"trade/{market_index}",
                }))
                logger.info(f"📤 发送trade订阅: market_index={market_index}")
            except Exception as e:
                logger.error(f"发送trade订阅失败: {e}")

    async def subscribe_trades(self, symbol: str, callback: Optional[Callable] = None):
        """
        订阅成交数据

        Args:
            symbol: 交易对符号
            callback: 数据回调函数
        """
        if callback:
            self._trade_callbacks.append(callback)

        market_index = self.get_market_index(symbol)
        if market_index is None:
            logger.warning(f"未找到交易对 {symbol} 的市场索引")
            return

        if market_index not in self._subscribed_trades:
            self._subscribed_trades.append(market_index)
            await self._ensure_direct_ws_running()

    async def subscribe_account(self, account_index: Optional[int] = None):
        """
        订阅账户数据

        Args:
            account_index: 账户索引（默认使用配置中的账户）
        """
        if account_index is None:
            account_index = self.account_index

        if account_index not in self._subscribed_accounts:
            self._subscribed_accounts.append(account_index)
            logger.info(f"已订阅账户数据: account_index={account_index}")

            # 如果WsClient已创建，需要重新创建以包含新的订阅
            await self._recreate_ws_client()

    async def subscribe_orders(self, callback: Optional[Callable] = None):
        """
        订阅订单更新

        使用直接WebSocket连接订阅account_all_orders频道
        这样可以接收挂单状态推送，而不仅仅是成交推送

        Args:
            callback: 数据回调函数
        """
        if callback:
            self._order_callbacks.append(callback)

        # account_all_orders包含完整订单状态；单笔trade不能推导整单FILLED。
        await self._subscribe_account_all_orders()

    async def subscribe_order_fills(self, callback: Callable) -> None:
        """
        订阅订单成交（专门监控FILLED状态的订单）

        Args:
            callback: 订单成交回调函数，参数为OrderData
        """
        if callback:
            self._order_fill_callbacks.append(callback)

        await self._subscribe_account_all_orders()

    async def subscribe_positions(self, callback: Optional[Callable] = None):
        """
        订阅持仓更新

        Args:
            callback: 数据回调函数
        """
        if callback:
            self._position_callbacks.append(callback)

        await self.subscribe_account()

    async def unsubscribe_ticker(self, symbol: str):
        """取消订阅ticker"""
        market_index = self.get_market_index(symbol)
        if market_index is not None and market_index in self._subscribed_market_stats:
            self._subscribed_market_stats.remove(market_index)
            await self._send_direct_unsubscribe(f"market_stats/{market_index}")

    async def unsubscribe_orderbook(self, symbol: str):
        """取消订阅订单簿"""
        market_index = self.get_market_index(symbol)
        if market_index is not None and market_index in self._subscribed_markets:
            self._subscribed_markets.remove(market_index)
            await self._recreate_ws_client()

    async def unsubscribe_trades(self, symbol: str):
        """取消订阅成交"""
        market_index = self.get_market_index(symbol)
        if market_index is not None and market_index in self._subscribed_trades:
            self._subscribed_trades.remove(market_index)
            await self._send_direct_unsubscribe(f"trade/{market_index}")

    async def _send_direct_unsubscribe(self, channel: str):
        if not self._direct_ws:
            return
        try:
            await self._direct_ws.send(json.dumps({
                "type": "unsubscribe",
                "channel": channel,
            }))
        except Exception as e:
            logger.warning(f"取消订阅 {channel} 失败: {e}")

    # ============= WebSocket客户端管理 =============

    async def _recreate_ws_client(self):
        """重新创建WebSocket客户端（当订阅变化时）"""
        try:
            await self._stop_sdk_ws_client()

            if not self._subscribed_markets and not self._subscribed_accounts:
                logger.info("没有订阅，已关闭WsClient")
                return

            # 创建新的WsClient
            # 🔥 从ws_url中提取host（去掉协议和路径）
            if not self.ws_url:
                logger.error("❌ WebSocket URL未配置，无法创建WebSocket客户端")
                return

            ws_host = self.ws_url.replace("wss://", "").replace("ws://", "")
            # 如果URL中包含路径，去掉路径（SDK会自动添加/stream）
            if "/" in ws_host:
                ws_host = ws_host.split("/")[0]

            self.ws_client = WsClient(
                host=ws_host,
                path="/stream",  # 明确指定path
                order_book_ids=self._subscribed_markets,
                account_ids=self._subscribed_accounts,
                on_order_book_update=self._on_order_book_update,
                on_account_update=self._on_account_update,
            )

            # 使用SDK原生异步串流，确保连接建立中也能被取消。
            self._ws_task = asyncio.create_task(self._run_ws_client())

            logger.info(
                f"✅ WebSocket已连接 - account: {self._subscribed_accounts[0] if self._subscribed_accounts else 'N/A'}")

        except Exception as e:
            logger.error(f"创建WebSocket客户端失败: {e}")

    async def _stop_sdk_ws_client(self):
        """Close and cancel the SDK WebSocket task without leaking a worker thread."""
        client = self.ws_client
        task = self._ws_task
        self._stopping_sdk_ws = True
        if task and not task.done():
            task.cancel()
        try:
            connection = getattr(client, "ws", None) if client else None
            if connection is not None:
                try:
                    await connection.close()
                except Exception as e:
                    logger.warning(f"关闭SDK WebSocket连接失败: {e}")
        finally:
            try:
                if task and not task.done():
                    task.cancel()
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            finally:
                self._ws_task = None
                self.ws_client = None
                self._stopping_sdk_ws = False

    async def _run_ws_client(self):
        """运行SDK原生异步WebSocket串流。"""
        try:
            await self.ws_client.run_async()
            logger.warning("⚠️ WebSocket客户端run_async()方法退出了")
            if self._connected and not self._stopping_sdk_ws:
                self._schedule_reconnect()
        except asyncio.CancelledError:
            logger.info("WebSocket任务已取消")
        except Exception as e:
            logger.error(f"❌ WebSocket运行出错: {e}", exc_info=True)
            if self._connected and not self._stopping_sdk_ws:
                self._schedule_reconnect()

    # ============= 消息处理 =============

    def _on_order_book_update(self, market_id: str, order_book: Dict[str, Any]):
        """
        订单簿更新回调

        Args:
            market_id: 市场ID
            order_book: 订单簿数据
        """
        try:
            market_index = int(market_id)
            symbol = self._get_symbol_from_market_index(market_index)

            if not symbol:
                logger.warning(f"未找到market_index={market_index}对应的符号")
                return

            # 解析订单簿
            order_book_data = self._parse_order_book(symbol, order_book)

            # 缓存
            self._order_books[symbol] = order_book_data

            # 触发回调
            self._trigger_orderbook_callbacks(order_book_data)

            # 从订单簿中提取ticker数据
            if self._ticker_callbacks:
                ticker = self._extract_ticker_from_orderbook(
                    symbol, order_book, order_book_data)
                if ticker:
                    self._trigger_ticker_callbacks(ticker)

        except Exception as e:
            logger.error(f"处理订单簿更新失败: {e}")

    def _on_account_update(self, account_id: str, account: Dict[str, Any]):
        """
        账户数据更新回调

        SDK 1.1.2的account_all数据包含trades、positions与assets等账户资料。
        订单更新来自独立的account_all_orders频道。

        Args:
            account_id: 账户ID
            account: 账户数据
        """
        try:
            # 缓存账户数据
            self._account_data[account_id] = account

            logger.debug(
                f"📥 收到账户更新: account_id={account_id}, keys={list(account.keys())}")

            # account_all只包含成交与持仓等账户资料；订单状态由
            # account_all_orders频道处理，避免把单笔成交误判为整单FILLED。
            if "trades" in account and account["trades"]:
                trades_data = account["trades"]
                if isinstance(trades_data, dict):
                    for market_index, trade_list in trades_data.items():
                        if isinstance(trade_list, list):
                            for trade_info in trade_list:
                                normalized_trade = dict(trade_info)
                                normalized_trade.setdefault(
                                    "market_id", int(market_index))
                                trade = self._parse_trade_data(normalized_trade)
                                if trade:
                                    self._trigger_trade_callbacks(trade)

            # 解析持仓更新
            if "positions" in account and self._position_callbacks:
                positions = self._parse_positions(account["positions"])
                for position in positions:
                    self._trigger_position_callbacks(position)

        except Exception as e:
            logger.error(f"❌ 处理账户更新失败: {e}", exc_info=True)

    # ============= 数据解析 =============

    def _parse_order_book(self, symbol: str, order_book: Dict[str, Any]) -> OrderBookData:
        """解析订单簿数据"""
        bids = []
        asks = []

        if "bids" in order_book:
            for bid in order_book["bids"]:
                # Lighter WebSocket返回字典格式：{'price': '...', 'size': '...'}
                if isinstance(bid, dict):
                    bids.append(OrderBookLevel(
                        price=self._safe_decimal(bid.get('price', 0)),
                        size=self._safe_decimal(bid.get('size', 0))
                    ))
                # 兼容列表/元组格式：['price', 'size']
                elif isinstance(bid, (list, tuple)) and len(bid) >= 2:
                    bids.append(OrderBookLevel(
                        price=self._safe_decimal(bid[0]),
                        size=self._safe_decimal(bid[1])
                    ))

        if "asks" in order_book:
            for ask in order_book["asks"]:
                # Lighter WebSocket返回字典格式：{'price': '...', 'size': '...'}
                if isinstance(ask, dict):
                    asks.append(OrderBookLevel(
                        price=self._safe_decimal(ask.get('price', 0)),
                        size=self._safe_decimal(ask.get('size', 0))
                    ))
                # 兼容列表/元组格式：['price', 'size']
                elif isinstance(ask, (list, tuple)) and len(ask) >= 2:
                    asks.append(OrderBookLevel(
                        price=self._safe_decimal(ask[0]),
                        size=self._safe_decimal(ask[1])
                    ))

        return OrderBookData(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(),
            nonce=None
        )

    def _extract_ticker_from_orderbook(self, symbol: str, raw_data: Dict[str, Any], order_book: OrderBookData) -> Optional[TickerData]:
        """从订单簿中提取ticker数据"""
        try:
            best_bid = order_book.bids[0].price if order_book.bids else Decimal(
                "0")
            best_ask = order_book.asks[0].price if order_book.asks else Decimal(
                "0")

            # 最新价格取中间价
            last_price = (best_bid + best_ask) / \
                2 if best_bid > 0 and best_ask > 0 else best_bid or best_ask

            return TickerData(
                symbol=symbol,
                timestamp=datetime.now(),
                bid=best_bid,
                ask=best_ask,
                last=last_price,
                volume=self._safe_decimal(raw_data.get("volume_24h", 0)),
                high=self._safe_decimal(
                    raw_data.get("high_24h", last_price)),
                low=self._safe_decimal(
                    raw_data.get("low_24h", last_price))
            )
        except Exception as e:
            logger.error(f"提取ticker数据失败: {e}")
            return None

    def _parse_orders(self, orders_data: Dict[str, Any]) -> List[OrderData]:
        """解析订单列表"""
        orders = []
        for market_index_str, order_list in orders_data.items():
            try:
                market_index = int(market_index_str)
                symbol = self._get_symbol_from_market_index(market_index)

                for order_info in order_list:
                    orders.append(self._parse_order(order_info, symbol))
            except Exception as e:
                logger.error(f"解析订单失败: {e}")

        return orders

    def _parse_order_from_ws(self, order_info: Dict[str, Any]) -> Optional[OrderData]:
        """
        解析WebSocket推送的Order JSON

        ⚠️ 根据Lighter官方Go结构文档，实际字段名是缩写形式：
        - "i":  OrderIndex (int64) - 订单ID
        - "u":  ClientOrderIndex (int64) - 客户端订单ID  
        - "is": InitialBaseAmount (int64) - 初始数量（动态size_decimals）
        - "rs": RemainingBaseAmount (int64) - 剩余数量（动态size_decimals）
        - "p":  Price (uint32) - 价格（需要除以price_multiplier）
        - "ia": IsAsk (uint8) - 是否卖单 (0=buy, 1=sell)
        - "st": Status (uint8) - 状态码 (0=Failed, 1=Pending, 2=Executed, 3=Pending-Final)
        """
        try:
            from ..models import OrderSide, OrderType, OrderStatus

            # 🔥 使用实际的缩写字段名
            order_index = order_info.get("i")  # OrderIndex
            client_order_index = order_info.get("u")  # ClientOrderIndex

            if order_index is None:
                logger.warning(
                    f"⚠️ 订单数据缺少OrderIndex(i): keys={list(order_info.keys())}")
                return None

            order_id = str(order_index)

            # 获取市场索引和符号（假设字段名是"m"）
            # TODO: 需要确认market_index的实际字段名
            market_index = order_info.get("m")
            symbol = self._get_symbol_from_market_index(
                market_index) if market_index is not None else "UNKNOWN"

            market_info = self._markets_cache.get(market_index, {})
            size_decimals = market_info.get(
                "size_decimals", market_info.get("supported_size_decimals"))
            price_decimals = market_info.get(
                "price_decimals", market_info.get("supported_price_decimals"))
            if size_decimals is None or price_decimals is None:
                logger.warning(f"⚠️ 市场 {market_index} 缺少价格或数量精度")
                return None

            size_multiplier = Decimal(10) ** int(size_decimals)
            price_multiplier = Decimal(10) ** int(price_decimals)

            # 解析动态精度的整数数量。
            initial_amount_raw = order_info.get("is", 0)  # InitialBaseAmount
            remaining_amount_raw = order_info.get(
                "rs", 0)  # RemainingBaseAmount

            initial_amount = self._safe_decimal(
                initial_amount_raw) / size_multiplier
            remaining_amount = self._safe_decimal(
                remaining_amount_raw) / size_multiplier
            filled_amount = initial_amount - remaining_amount

            # 暂时无法从Order结构直接获取filled_quote，设置为0
            filled_quote = Decimal("0")

            # 计算成交均价（如果有成交且有价格）
            average_price = None
            price_raw = order_info.get("p", 0)  # Price (uint32)
            price = self._safe_decimal(price_raw) / price_multiplier
            if filled_amount > 0 and price > 0:
                average_price = price  # 近似使用订单价格

            # 🔥 解析订单方向（使用缩写字段）
            is_ask = order_info.get("ia", 0)  # IsAsk (uint8: 0=buy, 1=sell)
            side = OrderSide.SELL if is_ask else OrderSide.BUY

            # 🔥 解析订单状态（使用缩写字段，状态是数字）
            status_code = order_info.get("st", 1)  # Status (uint8)
            if status_code == 2:  # Executed
                status = OrderStatus.FILLED
            elif status_code == 0:  # Failed
                status = OrderStatus.CANCELED
            elif status_code == 1 or status_code == 3:  # Pending / Pending-Final
                status = OrderStatus.OPEN
            else:
                status = OrderStatus.PENDING

            # 构造OrderData
            return OrderData(
                id=order_id,                                    # ✅ OrderIndex的字符串形式
                client_id=str(
                    client_order_index) if client_order_index else "",
                symbol=symbol,
                side=side,
                type=OrderType.LIMIT,
                amount=initial_amount,
                filled=filled_amount,
                remaining=remaining_amount,
                price=price,
                average=average_price,
                cost=filled_quote,
                status=status,
                timestamp=datetime.now(),
                updated=None,
                fee=None,
                trades=[],
                params={},
                raw_data=order_info
            )

        except Exception as e:
            logger.error(f"解析WebSocket订单失败: {e}", exc_info=True)
            return None

    def _parse_order(self, order_info: Dict[str, Any], symbol: str) -> OrderData:
        """解析单个订单（兼容旧版本）"""
        # 🔥 使用新的解析方法
        result = self._parse_order_from_ws(order_info)
        if result:
            return result

        # 降级处理
        # 🔥 计算成交均价：根据Lighter SDK数据结构
        filled_base = self._safe_decimal(
            order_info.get("filled_base_amount", 0))
        filled_quote = self._safe_decimal(
            order_info.get("filled_quote_amount", 0))

        # 计算平均成交价 = 成交金额 / 成交数量
        average_price = None
        if filled_base > 0 and filled_quote > 0:
            average_price = filled_quote / filled_base

        order_data = OrderData(
            order_id=str(order_info.get("order_index", "")),
            client_order_id=str(order_info.get("client_order_index", "")),
            symbol=symbol,
            side=self._parse_order_side(order_info.get("is_ask", False)),
            order_type=self._parse_order_type(order_info.get("type", 0)),
            quantity=self._safe_decimal(
                order_info.get("initial_base_amount", 0)),
            price=self._safe_decimal(order_info.get("price", 0)),
            filled_quantity=filled_base,
            status=self._parse_order_status(
                order_info.get("status", "unknown")),
            timestamp=self._parse_timestamp(order_info.get("timestamp")),
            exchange="lighter"
        )

        # 🔥 设置成交均价（如果有）
        if average_price:
            order_data.average = average_price

        return order_data

    def _parse_trade_as_order(self, trade_info: Dict[str, Any]) -> Optional[OrderData]:
        """
        将trade数据解析为OrderData（用于WebSocket订单成交通知）

        Lighter WebSocket中，交易成交数据在'trades'键中

        Trade JSON格式（根据文档）:
        {
            "trade_id": INTEGER,
            "tx_hash": STRING,
            "market_id": INTEGER,
            "size": STRING,
            "price": STRING,
            "ask_id": INTEGER,        # 卖单订单ID
            "bid_id": INTEGER,        # 买单订单ID
            "ask_account_id": INTEGER, # 卖方账户ID
            "bid_account_id": INTEGER, # 买方账户ID
            "is_maker_ask": BOOLEAN   # maker是卖方(true)还是买方(false)
        }
        """
        try:
            # 🔥 获取市场ID
            market_id = trade_info.get("market_id")
            if market_id is None:
                logger.warning(f"交易数据缺少market_id: {trade_info}")
                return None

            symbol = self._get_symbol_from_market_index(market_id)

            # 🔥 判断当前账户是买方还是卖方
            ask_account_id = trade_info.get("ask_account_id")
            bid_account_id = trade_info.get("bid_account_id")

            # 根据账户ID判断是买还是卖
            is_sell = (ask_account_id == self.account_index)
            is_buy = (bid_account_id == self.account_index)

            if not (is_sell or is_buy):
                # 这个trade不属于当前账户
                return None

            # 🔥 获取正确的订单ID（ask_id或bid_id）
            order_id = trade_info.get(
                "ask_id") if is_sell else trade_info.get("bid_id")
            if order_id is None:
                logger.warning(f"交易数据缺少订单ID: {trade_info}")
                return None

            # 解析交易数量和价格
            size_str = trade_info.get("size", "0")
            price_str = trade_info.get("price", "0")
            usd_amount_str = trade_info.get("usd_amount", "0")

            base_amount = self._safe_decimal(size_str)
            trade_price = self._safe_decimal(price_str)
            usd_amount = self._safe_decimal(usd_amount_str)

            # 🔥 构造OrderData
            from ..models import OrderSide, OrderType, OrderStatus

            order_data = OrderData(
                id=str(order_id),  # ✅ 使用ask_id或bid_id作为订单ID
                client_id="",
                symbol=symbol,
                side=OrderSide.SELL if is_sell else OrderSide.BUY,
                type=OrderType.LIMIT,  # Trade可能来自限价单
                amount=base_amount,
                price=trade_price,
                filled=base_amount,  # 交易全部成交
                remaining=Decimal("0"),  # 已全部成交
                cost=usd_amount,  # 成交金额
                average=trade_price,  # 成交价
                status=OrderStatus.FILLED,  # 已成交
                timestamp=self._parse_timestamp(trade_info.get("timestamp")),
                updated=self._parse_timestamp(trade_info.get("timestamp")),
                fee=None,
                trades=[],
                params={},
                raw_data=trade_info
            )

            return order_data

        except Exception as e:
            logger.error(f"解析交易数据失败: {e}", exc_info=True)
            return None

    def _parse_trade_data(self, trade_info: Dict[str, Any]) -> Optional[TradeData]:
        """Parse the current Lighter Trade schema into the shared model."""
        try:
            market_id = trade_info.get("market_id")
            if market_id is None:
                return None

            price = self._safe_decimal(trade_info.get("price"))
            amount = self._safe_decimal(trade_info.get("size"))
            usd_amount = trade_info.get("usd_amount")
            cost = (
                self._safe_decimal(usd_amount)
                if usd_amount is not None else price * amount
            )
            is_maker_ask = bool(trade_info.get("is_maker_ask", False))
            taker_order_id = (
                trade_info.get("bid_id")
                if is_maker_ask else trade_info.get("ask_id")
            )

            return TradeData(
                id=str(trade_info.get(
                    "trade_id_str", trade_info.get("trade_id", ""))),
                symbol=self._get_symbol_from_market_index(int(market_id)),
                side=OrderSide.BUY if is_maker_ask else OrderSide.SELL,
                amount=amount,
                price=price,
                cost=cost,
                fee=None,
                timestamp=(
                    self._parse_timestamp(trade_info.get("timestamp"))
                    or datetime.now()
                ),
                order_id=(
                    str(taker_order_id)
                    if taker_order_id is not None else None
                ),
                raw_data=trade_info,
            )
        except Exception as e:
            logger.error(f"解析成交数据失败: {e}", exc_info=True)
            return None

    def _parse_positions(self, positions_data: Dict[str, Any]) -> List[PositionData]:
        """解析持仓列表"""
        positions = []
        for market_index_str, position_info in positions_data.items():
            try:
                market_index = int(market_index_str)
                symbol = self._get_symbol_from_market_index(market_index)

                try:
                    raw_position_size = Decimal(
                        str(position_info.get("position", 0))
                    )
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"invalid position size for {symbol}"
                    ) from exc
                if not raw_position_size.is_finite():
                    raise ValueError(f"invalid position size for {symbol}")
                if raw_position_size == 0:
                    continue

                # SDK 1.1.2 sends a positive magnitude and a separate sign.
                # Keep compatibility with older signed payloads only when the
                # sign field is absent, matching the authenticated REST parser.
                sign_raw = position_info.get("sign")
                if sign_raw is None:
                    position_size = raw_position_size
                else:
                    try:
                        position_sign = int(sign_raw)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"invalid position sign for {symbol}"
                        ) from exc
                    if position_sign not in {-1, 1}:
                        raise ValueError(f"invalid position sign for {symbol}")
                    if raw_position_size < 0:
                        raise ValueError(
                            f"negative position magnitude for {symbol}"
                        )
                    position_size = raw_position_size * position_sign

                liquidation_raw = position_info.get("liquidation_price")
                liquidation_price = (
                    None
                    if liquidation_raw in (None, "", "0", "0.0")
                    else self._safe_decimal(liquidation_raw)
                )

                positions.append(PositionData(
                    symbol=symbol,
                    side=(
                        PositionSide.LONG
                        if position_size > 0 else PositionSide.SHORT
                    ),
                    size=abs(position_size),
                    entry_price=self._safe_decimal(
                        position_info.get("avg_entry_price", 0)),
                    mark_price=None,
                    current_price=None,
                    unrealized_pnl=self._safe_decimal(
                        position_info.get("unrealized_pnl", 0)),
                    realized_pnl=self._safe_decimal(
                        position_info.get("realized_pnl", 0)),
                    percentage=None,
                    leverage=self._leverage_from_initial_margin_fraction(
                        position_info.get("initial_margin_fraction", 0)),
                    margin_mode=(
                        MarginMode.CROSS
                        if position_info.get("margin_mode", 0) == 0
                        else MarginMode.ISOLATED
                    ),
                    margin=self._safe_decimal(
                        position_info.get("allocated_margin", 0)),
                    liquidation_price=liquidation_price,
                    timestamp=datetime.now(),
                    raw_data=position_info,
                ))
            except Exception as e:
                logger.error(f"解析持仓失败: {e}")

        return positions

    def _get_symbol_from_market_index(self, market_index: int) -> str:
        """从市场索引获取符号"""
        market_info = self._markets_cache.get(market_index)
        if market_info:
            return market_info.get("symbol", "")
        return f"MARKET_{market_index}"

    # ============= 回调触发 =============

    def _trigger_ticker_callbacks(self, ticker: TickerData):
        """触发ticker回调（线程安全）"""
        for callback in self._ticker_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # 🔥 WebSocket在同步线程中运行，需要线程安全地调度协程
                    if self._event_loop and self._event_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            callback(ticker), self._event_loop)
                    else:
                        logger.debug("⚠️ 事件循环未运行，跳过ticker回调")
                else:
                    callback(ticker)
            except Exception as e:
                logger.error(f"ticker回调执行失败: {e}", exc_info=True)

    def _trigger_orderbook_callbacks(self, orderbook: OrderBookData):
        """触发订单簿回调（线程安全）"""
        for callback in self._orderbook_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # 🔥 WebSocket在同步线程中运行，需要线程安全地调度协程
                    if self._event_loop and self._event_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            callback(orderbook), self._event_loop)
                    else:
                        logger.debug("⚠️ 事件循环未运行，跳过订单簿回调")
                else:
                    callback(orderbook)
            except Exception as e:
                logger.error(f"订单簿回调执行失败: {e}")

    def _trigger_trade_callbacks(self, trade: TradeData):
        """触发成交回调（线程安全）"""
        for callback in self._trade_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # 🔥 WebSocket在同步线程中运行，需要线程安全地调度协程
                    if self._event_loop and self._event_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            callback(trade), self._event_loop)
                    else:
                        logger.debug("⚠️ 事件循环未运行，跳过成交回调")
                else:
                    callback(trade)
            except Exception as e:
                logger.error(f"成交回调执行失败: {e}")

    def _trigger_order_callbacks(self, order: OrderData):
        """触发订单回调（线程安全，带错误捕获）"""
        for callback in self._order_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # 🔥 WebSocket在同步线程中运行，需要线程安全地调度协程
                    if self._event_loop and self._event_loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            callback(order), self._event_loop)

                        # 🔥 添加完成回调来捕获异常
                        def log_error(fut):
                            try:
                                fut.result()  # 获取结果，如果有异常会抛出
                            except Exception as e:
                                logger.error(
                                    f"❌ 订单回调执行出错: order_id={order.id}, "
                                    f"side={order.side}, status={order.status}, "
                                    f"error={e}",
                                    exc_info=True
                                )

                        future.add_done_callback(log_error)
                    else:
                        logger.warning("⚠️ 事件循环未运行，跳过订单回调")
                else:
                    callback(order)
            except Exception as e:
                logger.error(f"订单回调执行失败: {e}", exc_info=True)

    def _trigger_order_fill_callbacks(self, order: OrderData):
        """触发订单成交回调（线程安全，带错误捕获）"""
        for callback in self._order_fill_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # 🔥 WebSocket在同步线程中运行，需要线程安全地调度协程
                    if self._event_loop and self._event_loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(
                            callback(order), self._event_loop)

                        # 🔥 添加完成回调来捕获异常
                        def log_error(fut):
                            try:
                                fut.result()
                            except Exception as e:
                                logger.error(
                                    f"❌ 订单成交回调执行出错: order_id={order.id}, "
                                    f"error={e}",
                                    exc_info=True
                                )

                        future.add_done_callback(log_error)
                    else:
                        logger.warning("⚠️ 事件循环未运行，无法调度异步回调")
                else:
                    callback(order)
            except Exception as e:
                logger.error(f"订单成交回调执行失败: {e}", exc_info=True)

    def _trigger_position_callbacks(self, position: PositionData):
        """触发持仓回调（线程安全）"""
        for callback in self._position_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    # 🔥 WebSocket在同步线程中运行，需要线程安全地调度协程
                    if self._event_loop and self._event_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            callback(position), self._event_loop)
                    else:
                        logger.debug("⚠️ 事件循环未运行，跳过持仓回调")
                else:
                    callback(position)
            except Exception as e:
                logger.error(f"持仓回调执行失败: {e}")

    # ============= 直接订阅account_all_orders =============

    async def _subscribe_account_all_orders(self):
        """
        直接订阅account_all_orders频道

        根据Lighter WebSocket文档，订阅account_all_orders需要：
        1. 建立WebSocket连接到 wss://mainnet.zklighter.elliot.ai/stream
        2. 发送订阅消息，包含auth token
        3. 接收订单推送（包括挂单状态）
        """
        if not WEBSOCKETS_AVAILABLE:
            logger.warning("⚠️ websockets库未安装，无法直接订阅订单")
            return

        if self._direct_ws_task and not self._direct_ws_task.done():
            logger.info("⚠️ 直接订阅任务已在运行")
            return

        # 启动直接订阅任务
        self._direct_ws_task = asyncio.create_task(
            self._run_direct_ws_subscription())
        logger.info("🚀 已启动直接订阅account_all_orders任务")

    async def _run_direct_ws_subscription(self):
        """运行直接WebSocket订阅（永久运行，自动重连）"""
        retry_count = 0

        # 🔥 外层循环：确保任务永不退出
        while self._connected:
            try:
                # market_stats是公开频道；只有账户订单订阅需要认证token。
                auth_token = None
                if self.signer_client:
                    auth_token, auth_error = (
                        self.signer_client.create_auth_token_with_expiry(
                            deadline=3600,
                            api_key_index=self.api_key_index,
                        )
                    )
                    if auth_error:
                        logger.warning(f"⚠️ 生成认证token失败: {auth_error}")
                        auth_token = None
                    else:
                        logger.info("✅ 已生成1小时有效的认证token")
                elif self.account_index:
                    logger.warning("⚠️ SignerClient未初始化，跳过账户订单订阅")

                # 连接WebSocket
                ws_url = self.ws_url
                logger.info(f"🔗 连接WebSocket: {ws_url}")

                # 🔥 修复1：移除ping/pong参数，允许Lighter长时间静默
                # Lighter不会主动发送心跳，只在订单更新时推送消息
                async with websockets.connect(
                    ws_url,
                    close_timeout=10       # 只保留关闭连接的超时时间
                ) as ws:
                    self._direct_ws = ws
                    self._direct_ws_connected = True
                    self._account_orders_subscribed = False

                    # 发送订阅消息
                    # 1️⃣ 订阅account_all_orders（需要认证）
                    if self.signer_client and auth_token:
                        subscribe_msg = {
                            "type": "subscribe",
                            "channel": f"account_all_orders/{self.account_index}",
                            "auth": auth_token
                        }
                        await ws.send(json.dumps(subscribe_msg))
                        logger.info(
                            f"已发送订阅请求: account_all_orders/{self.account_index}"
                        )

                    # 2️⃣ 订阅market_stats（无需认证）
                    await self._send_market_stats_subscriptions()

                    # 3️⃣ 订阅公开成交（无需认证）
                    await self._send_trade_subscriptions()

                    # 重置重连计数（连接成功）
                    retry_count = 0

                    # 持续接收消息
                    async for message in ws:
                        try:
                            self._direct_last_message_time = time.time()
                            data = json.loads(message)
                            await self._handle_direct_ws_message(data)
                        except json.JSONDecodeError as e:
                            logger.error(f"❌ JSON解析失败: {e}")
                        except Exception as e:
                            logger.error(f"❌ 处理消息失败: {e}", exc_info=True)

                    if self._connected:
                        self._direct_ws_connected = False
                        self._account_orders_subscribed = False
                        self._direct_ws = None
                        retry_count += 1
                        self._direct_reconnect_count = (
                            getattr(self, "_direct_reconnect_count", 0) + 1
                        )
                        logger.warning(
                            "直接WebSocket正常关闭，5秒后重新连接 "
                            f"(第{retry_count}次)..."
                        )
                        await asyncio.sleep(5)

            except websockets.exceptions.ConnectionClosedError as e:
                # WebSocket连接关闭
                self._direct_ws_connected = False
                self._account_orders_subscribed = False
                self._direct_ws = None
                retry_count += 1
                self._direct_reconnect_count = (
                    getattr(self, "_direct_reconnect_count", 0) + 1
                )
                logger.warning(
                    f"⚠️ WebSocket连接已关闭: {e}，5秒后重连 (第{retry_count}次)...")
                await asyncio.sleep(5)
                continue  # 外层循环会自动重连

            except Exception as e:
                # 🔥 修复2：捕获所有异常，确保任务不退出
                self._direct_ws_connected = False
                self._account_orders_subscribed = False
                self._direct_ws = None
                retry_count += 1
                self._direct_reconnect_count = (
                    getattr(self, "_direct_reconnect_count", 0) + 1
                )
                retry_delay = min(retry_count * 5, 60)  # 指数退避，最多60秒
                logger.error(
                    f"❌ 直接WebSocket订阅失败 (第{retry_count}次): {e}，"
                    f"{retry_delay}秒后重连...",
                    exc_info=True
                )
                await asyncio.sleep(retry_delay)
                # 外层循环会自动重连

            finally:
                self._direct_ws_connected = False
                self._account_orders_subscribed = False
                self._direct_ws = None

        logger.info("🛑 WebSocket订阅任务已停止")

    async def _handle_direct_ws_message(self, data: Dict[str, Any]):
        """
        处理直接WebSocket消息

        根据文档，account_all_orders返回：
        {
            "channel": "account_all_orders:{ACCOUNT_ID}",
            "orders": {
                "{MARKET_INDEX}": [Order]
            },
            "type": "update/account_all_orders"
        }
        """
        try:
            msg_type = data.get("type", "")
            channel = data.get("channel", "")

            logger.debug(
                f"📥 收到直接WebSocket推送: channel={channel}, type={msg_type}")

            if msg_type in {
                "subscribed/account_all_orders",
                "update/account_all_orders",
            }:
                self._account_orders_subscribed = True
            elif (
                msg_type in {"error", "subscription/error", "failed/subscribe"}
                and "account_all_orders" in str(channel)
            ):
                self._account_orders_subscribed = False
                logger.error("account_all_orders subscription was rejected")

            # 处理订单更新
            if msg_type in {
                "subscribed/account_all_orders",
                "update/account_all_orders",
            } and "orders" in data:
                orders_data = data["orders"]

                if isinstance(orders_data, dict):
                    for market_index, order_list in orders_data.items():
                        if isinstance(order_list, list):
                            for order_info in order_list:
                                logger.debug(f"🔍 订单完整数据: {order_info}")

                                # 解析订单（使用完整的Order JSON格式）
                                order = self._parse_order_from_direct_ws(
                                    order_info)
                                if order:
                                    logger.debug(
                                        f"📝 订单推送: id={order.id}, "
                                        f"状态={order.status.value}, 价格={order.price}, "
                                        f"数量={order.amount}, 已成交={order.filled}")

                                    # 触发订单回调
                                    if self._order_callbacks:
                                        for callback in self._order_callbacks:
                                            if asyncio.iscoroutinefunction(callback):
                                                await callback(order)
                                            else:
                                                callback(order)

                                    # 如果是成交状态，触发成交回调
                                    if order.status == OrderStatus.FILLED and self._order_fill_callbacks:
                                        for callback in self._order_fill_callbacks:
                                            if asyncio.iscoroutinefunction(callback):
                                                await callback(order)
                                            else:
                                                callback(order)

            # 🔥 处理market_stats更新
            elif msg_type in ("subscribed/market_stats", "update/market_stats") and "market_stats" in data:
                await self._handle_market_stats_update(data["market_stats"])

            # subscribed/trade包含历史快照；只把增量推送给实时回调。
            elif msg_type == "update/trade":
                trade_rows = data.get("trades", []) + data.get(
                    "liquidation_trades", [])
                for trade_info in trade_rows:
                    trade = self._parse_trade_data(trade_info)
                    if not trade:
                        continue
                    for callback in self._trade_callbacks:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(trade)
                        else:
                            callback(trade)

        except Exception as e:
            logger.error(f"❌ 处理直接WebSocket消息失败: {e}", exc_info=True)

    async def _handle_market_stats_update(self, market_stats: Dict[str, Any]):
        """
        处理market_stats更新

        market_stats格式:
        {
            "market_id": 1,
            "index_price": "110687.2",
            "mark_price": "110660.1",
            "last_trade_price": "110657.5",
            "open_interest": "308919704.542476",
            "current_funding_rate": "0.0012",
            ...
        }
        """
        try:
            market_id = market_stats.get("market_id")
            if market_id is None:
                return

            symbol = self._get_symbol_from_market_index(market_id)
            if not symbol:
                return

            # 🔥 提取价格数据
            last_price = self._safe_decimal(
                market_stats.get("last_trade_price", 0))
            if not last_price:
                return

            # 构造TickerData（使用正确的字段名）
            ticker = TickerData(
                symbol=symbol,
                timestamp=datetime.now(),  # ✅ 必需字段，使用datetime对象
                last=last_price,  # ✅ 最新成交价
                bid=self._safe_decimal(market_stats.get(
                    "best_bid_price", last_price)),
                ask=self._safe_decimal(market_stats.get(
                    "best_ask_price", last_price)),
                volume=self._safe_decimal(
                    market_stats.get("daily_base_token_volume", 0)),  # 24小时成交量
                high=self._safe_decimal(
                    market_stats.get("daily_price_high", 0)),  # 24小时最高价
                low=self._safe_decimal(
                    market_stats.get("daily_price_low", 0)),  # 24小时最低价
                funding_rate=self._safe_decimal(
                    market_stats.get("current_funding_rate", 0))  # 资金费率
            )

            logger.debug(f"📊 market_stats更新: {symbol}, 价格={last_price}")

            # 触发ticker回调
            if self._ticker_callbacks:
                for callback in self._ticker_callbacks:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(ticker)
                    else:
                        callback(ticker)

        except Exception as e:
            logger.error(f"❌ 处理market_stats更新失败: {e}", exc_info=True)

    @staticmethod
    def _parse_direct_order_type(value: Any) -> OrderType:
        normalized = str(value or "limit").lower().replace("_", "-")
        return {
            "market": OrderType.MARKET,
            "liquidation": OrderType.MARKET,
            "limit": OrderType.LIMIT,
            "stop-loss": OrderType.STOP,
            "stop-loss-limit": OrderType.STOP_LIMIT,
            "take-profit": OrderType.TAKE_PROFIT,
            "take-profit-limit": OrderType.TAKE_PROFIT_LIMIT,
        }.get(normalized, OrderType.LIMIT)

    @staticmethod
    def _parse_direct_order_status(value: Any) -> OrderStatus:
        normalized = str(value or "unknown").lower()
        if normalized.startswith("canceled") or normalized == "cancelled":
            return OrderStatus.CANCELED
        return {
            "pending": OrderStatus.PENDING,
            "in-progress": OrderStatus.OPEN,
            "open": OrderStatus.OPEN,
            "partial": OrderStatus.OPEN,
            "partially_filled": OrderStatus.OPEN,
            "filled": OrderStatus.FILLED,
            "expired": OrderStatus.EXPIRED,
            "rejected": OrderStatus.REJECTED,
            "failed": OrderStatus.REJECTED,
        }.get(normalized, OrderStatus.UNKNOWN)

    def _parse_order_from_direct_ws(self, order_info: Dict[str, Any]) -> Optional[OrderData]:
        """
        解析来自account_all_orders的订单数据

        根据文档，Order JSON格式：
        {
            "order_index": INTEGER,
            "client_order_index": INTEGER,
            "market_index": INTEGER,
            "initial_base_amount": STRING,
            "price": STRING,
            "remaining_base_amount": STRING,
            "filled_base_amount": STRING,
            "filled_quote_amount": STRING,
            "is_ask": BOOL,
            "status": STRING,  # "open", "filled", "canceled"
            ...
        }
        """
        try:
            # 获取市场符号
            market_index = order_info.get("market_index")
            if market_index is None:
                return None

            symbol = self._get_symbol_from_market_index(market_index)

            # 订单ID
            order_index = order_info.get("order_index")
            order_id = str(order_index) if order_index is not None else ""

            # 数量和价格
            initial_amount = self._safe_decimal(
                order_info.get("initial_base_amount", "0"))
            remaining_amount = self._safe_decimal(
                order_info.get("remaining_base_amount", "0"))
            filled_amount = self._safe_decimal(
                order_info.get("filled_base_amount", "0"))
            price = self._safe_decimal(order_info.get("price", "0"))

            # 成交金额和均价
            filled_quote = self._safe_decimal(
                order_info.get("filled_quote_amount", "0"))
            average_price = filled_quote / filled_amount if filled_amount > 0 else None

            # 方向
            is_ask = order_info.get("is_ask", False)
            side = OrderSide.SELL if is_ask else OrderSide.BUY

            exchange_status = str(
                order_info.get("status") or "unknown"
            ).strip().lower()
            status = self._parse_direct_order_status(exchange_status)

            # 创建OrderData
            return OrderData(
                id=order_id,
                client_id=str(order_info.get("client_order_index", "")),
                symbol=symbol,
                side=side,
                type=self._parse_direct_order_type(order_info.get("type")),
                amount=initial_amount,
                price=price,
                filled=filled_amount,
                remaining=remaining_amount,
                cost=filled_quote,
                average=average_price,
                status=status,
                timestamp=self._parse_timestamp(order_info.get("timestamp")),
                updated=None,
                fee=None,
                trades=[],
                params={},
                raw_data={
                    **order_info,
                    "post_only_canceled": (
                        exchange_status == "canceled-post-only"
                    ),
                }
            )

        except Exception as e:
            logger.error(f"解析订单失败: {e}", exc_info=True)
            return None

    def get_cached_orderbook(self, symbol: str) -> Optional[OrderBookData]:
        """获取缓存的订单簿"""
        return self._order_books.get(symbol)

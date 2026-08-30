"""
Lighter交易所适配器 - REST API模块

封装Lighter SDK的REST API功能，提供市场数据、账户信息和交易功能

⚠️  Lighter交易所特殊说明：

1. Market ID从0开始（不是1）：
   - market_id=0: ETH
   - market_id=1: BTC
   - market_id=2: SOL

2. 动态价格精度（关键特性）：
   - 不同交易对使用不同的价格与数量精度
   - 价格乘数公式: price_int = price_usd × (10 ** price_decimals)
   - 数量乘数公式: base_amount = quantity × (10 ** size_decimals)

   示例：
   - ETH (2位小数): $4127.39 × 100 = 412739
   - BTC (1位小数): $114357.8 × 10 = 1143578
   - SOL (3位小数): $199.058 × 1000 = 199058
   - DOGE (6位小数): $0.202095 × 1000000 = 202095

3. 必须使用order_books() API：
   - 获取完整市场列表必须用order_books()
   - order_book_details(market_id) 只返回活跃市场
   - 不能通过循环遍历market_id来发现市场

这些设计是Lighter作为Layer 2 DEX的优化选择，与传统CEX不同！
"""

from typing import Dict, Any, Optional, List, Callable
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime
import asyncio
import logging
import threading
import time
from urllib.parse import urlparse

from aiohttp import ClientConnectorDNSError

try:
    import lighter
    from lighter import Configuration, ApiClient
    from lighter.api import AccountApi, OrderApi, TransactionApi, CandlestickApi, FundingApi
    LIGHTER_AVAILABLE = True
except ImportError:
    LIGHTER_AVAILABLE = False
    logging.warning("lighter SDK未安装。请执行: uv pip install lighter-sdk==1.1.2")

from .lighter_base import LighterBase
from ..exceptions import (
    OrderSubmissionNotSentError,
    OrderSubmissionRejectedError,
)
from ..models import (
    TickerData, OrderBookData, TradeData, BalanceData,
    OrderData, PositionData, ExchangeInfo, OrderBookLevel, OrderSide, OrderType, OrderStatus
)

# 配置 logger 输出到文件
logger = logging.getLogger(__name__)
if not logger.handlers:
    import os
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    # 确保日志目录存在
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # 添加文件处理器
    log_file = log_dir / "ExchangeAdapter.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5*1024*1024,  # 5MB
        backupCount=3,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.WARNING)  # 只记录 WARNING 及以上级别
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.WARNING)


def _require_finite_decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid Lighter {name}") from exc
    if not parsed.is_finite():
        raise RuntimeError(f"Invalid Lighter {name}")
    return parsed


class LighterRest(LighterBase):
    """Lighter REST API封装类"""

    MANAGED_ORDER_INTEGRATOR_FEE_TICK = 0
    MUTATION_RECONCILIATION_ATTEMPTS = 2
    MUTATION_RECONCILIATION_DELAY = 0.25
    CANCELLATION_RECONCILIATION_ATTEMPTS = 4
    CANCELLATION_RECONCILIATION_DELAY = 0.5

    def __init__(self, config: Dict[str, Any]):
        """
        初始化Lighter REST客户端

        Args:
            config: 配置字典
        """
        if not LIGHTER_AVAILABLE:
            raise ImportError("lighter SDK未安装，无法使用Lighter适配器")

        super().__init__(config)

        # 初始化客户端
        self.api_client: Optional[ApiClient] = None

        # API实例
        self.account_api: Optional[AccountApi] = None
        self.order_api: Optional[OrderApi] = None
        self.transaction_api: Optional[TransactionApi] = None
        self.candlestick_api: Optional[CandlestickApi] = None
        self.funding_api: Optional[FundingApi] = None

        # 连接状态
        self._connected = False
        self._safety_request_depth = 0

        # 🔥 初始化markets字典（用于WebSocket共享）
        self.markets = {}

        # 🔥 市场信息缓存（避免频繁调用API触发429限流）
        # 这是关键修复！没有这个缓存会导致批量下单时触发429
        self._market_info_cache = {}  # {symbol: {info, timestamp}}

        # All REST and signer traffic shares one cooldown so concurrent monitors
        # cannot keep the account pinned behind a 429 response.
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._rate_limit_failures = 0
        self._request_interval = 0.05
        self._max_rate_limit_delay = 30.0
        self._client_order_index_lock = threading.Lock()
        self._last_client_order_index = 0
        self._uncertain_cancellations = set()
        self._capture_terminal_cancellation_outcomes = False
        self._terminal_cancellation_outcomes = {}
        self._unresolved_submissions = {}

        # WebSocket由统一adapter持有，避免重复连接与SignerClient nonce状态。
        self._websocket = None

        logger.info("Lighter REST客户端初始化完成")

    @staticmethod
    def _require_success_response(response: Any, operation: str) -> None:
        """Reject SDK/API response models that do not explicitly report success."""
        code = getattr(response, 'code', None)
        if code != 200:
            raise RuntimeError(f"{operation} failed (code={code})")

    @staticmethod
    def _is_rate_limited(value: Any) -> bool:
        """Return whether an SDK result or exception represents HTTP 429."""
        if isinstance(value, (tuple, list)):
            return any(LighterRest._is_rate_limited(item) for item in value)
        code = getattr(value, "code", None)
        if code is None:
            code = getattr(value, "status", None)
        if str(code) == "429":
            return True
        text = str(value or "").lower()
        return any(
            marker in text
            for marker in (
                "too many requests",
                "status code: 429",
                "http 429",
                "rate limited",
            )
        )

    def _record_rate_limit(self, operation: str) -> float:
        """Advance the shared exponential cooldown after a 429 response."""
        self._rate_limit_failures += 1
        delay = min(
            2 ** (self._rate_limit_failures - 1),
            self._max_rate_limit_delay,
        )
        now = asyncio.get_running_loop().time()
        self._next_request_at = max(self._next_request_at, now + delay)
        logger.warning(
            f"Lighter rate limit reached during {operation}; "
            f"shared cooldown={delay:.1f}s"
        )
        return delay

    async def _call_api(
        self,
        operation: str,
        request_factory: Callable[[], Any],
        *,
        retry_on_429: bool = True,
    ) -> Any:
        """Serialize API traffic and retry read-only requests once after a 429."""
        if not hasattr(self, "_request_lock"):
            self._request_lock = asyncio.Lock()
            self._next_request_at = 0.0
            self._rate_limit_failures = 0
            self._request_interval = 0.05
            self._max_rate_limit_delay = 30.0

        max_attempts = 2 if retry_on_429 else 1
        for attempt in range(max_attempts):
            rate_limited = False
            async with self._request_lock:
                loop = asyncio.get_running_loop()
                wait_for = self._next_request_at - loop.time()
                # A previously rate-limited read must not delay a shutdown/safety
                # cancellation. The cancellation itself stays single-shot.
                safety_priority = (
                    operation == "order cancellation"
                    or getattr(self, "_safety_request_depth", 0) > 0
                )
                if wait_for > 0 and not safety_priority:
                    await asyncio.sleep(wait_for)

                try:
                    result = await request_factory()
                except Exception as exc:
                    if not self._is_rate_limited(exc):
                        raise
                    self._record_rate_limit(operation)
                    rate_limited = True
                else:
                    if self._is_rate_limited(result):
                        self._record_rate_limit(operation)
                        rate_limited = True
                    else:
                        self._rate_limit_failures = 0
                        self._next_request_at = max(
                            self._next_request_at,
                            loop.time() + self._request_interval,
                        )
                        return result

            if not rate_limited:
                break
            if attempt + 1 >= max_attempts:
                raise RuntimeError(f"{operation} rate limited (HTTP 429)")

        raise RuntimeError(f"{operation} failed")

    def begin_safety_requests(self) -> None:
        """Let bounded shutdown/cancel reads bypass an older read cooldown."""
        self._safety_request_depth = (
            getattr(self, "_safety_request_depth", 0) + 1
        )

    def end_safety_requests(self) -> None:
        self._safety_request_depth = max(
            0, getattr(self, "_safety_request_depth", 0) - 1
        )

    def _configured_account(self, response: Any, operation: str) -> Any:
        accounts = getattr(response, "accounts", None) or []
        if len(accounts) != 1:
            raise RuntimeError(
                f"{operation} did not return exactly one configured account"
            )
        account = accounts[0]
        returned_index = getattr(account, "account_index", None)
        if returned_index is None:
            returned_index = getattr(account, "index", None)
        parsed_index = _require_finite_decimal(
            returned_index, f"account index during {operation}"
        )
        if (
            parsed_index != parsed_index.to_integral_value()
            or int(parsed_index) != self.account_index
        ):
            raise RuntimeError(
                f"{operation} returned a different account index"
            )
        return account

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected

    def _next_client_order_index(self) -> int:
        """Return a process-unique, monotonically increasing Lighter client id."""
        lock = getattr(self, "_client_order_index_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._client_order_index_lock = lock

        with lock:
            # Epoch milliseconds keep the sequence unique across process/host
            # restarts; the local counter handles multiple orders in one ms.
            now_ms = time.time_ns() // 1_000_000
            value = max(
                now_ms,
                getattr(self, "_last_client_order_index", 0) + 1,
            )
            self._last_client_order_index = value
            return value

    @staticmethod
    def _is_definitive_mutation_exception(exc: Exception) -> bool:
        """Return whether an exception proves the mutation never crossed the API gate."""
        # Once the signer call starts, ValueError/TypeError can come from response
        # decoding after the request was accepted. Only an explicit HTTP 429 is
        # safe to classify from the exception alone; response/err objects are
        # handled separately by their callers.
        return LighterRest._is_rate_limited(exc)

    @staticmethod
    def _is_invalid_nonce_rejection(tx: Any, response: Any, error: Any) -> bool:
        """Return whether Lighter explicitly rejected a submission's nonce."""
        return (
            tx is None
            and response is None
            and isinstance(error, str)
            and error.strip()
            == (
                "HTTP response body: code=21104 message='invalid nonce' "
                "additional_properties={}"
            )
        )

    def _is_configured_api_dns_failure(self, exc: Exception) -> bool:
        """Return whether DNS failed for this adapter's configured API host."""
        if not isinstance(exc, ClientConnectorDNSError):
            return False
        expected_host = urlparse(str(getattr(self, "base_url", ""))).hostname
        return bool(expected_host) and exc.host == expected_host

    def _restore_nonce_after_pre_send_failure(self) -> bool:
        """Undo the SDK's optimistic nonce increment for the configured key."""
        signer = getattr(self, "signer_client", None)
        manager = getattr(signer, "nonce_manager", None)
        api_key_index = getattr(self, "api_key_index", None)
        configured_keys = list(getattr(manager, "api_keys_list", ()))
        acknowledge = getattr(manager, "acknowledge_failure", None)
        if (
            manager is None
            or api_key_index is None
            or configured_keys != [api_key_index]
            or not callable(acknowledge)
        ):
            return False
        acknowledge(api_key_index)
        return True

    @staticmethod
    def _order_matches_client_id(order: OrderData, client_order_id: int) -> bool:
        target = str(client_order_id)
        return str(getattr(order, "client_id", "") or "") == target

    def _register_unresolved_submission(
        self,
        client_order_id: int,
        symbol: str,
        order_type: str,
        side: str,
        amount: Decimal,
        price: Decimal,
    ) -> None:
        registry = getattr(self, "_unresolved_submissions", None)
        if registry is None:
            registry = {}
            self._unresolved_submissions = registry
        registry[str(client_order_id)] = {
            "client_order_id": str(client_order_id),
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": Decimal(str(amount)),
            "price": Decimal(str(price)),
            "time": datetime.now(),
        }

    def _clear_unresolved_submission(self, client_order_id: Any) -> None:
        registry = getattr(self, "_unresolved_submissions", None)
        if registry is not None:
            registry.pop(str(client_order_id), None)

    def _clear_resolved_submissions_from_orders(
        self,
        orders: List[OrderData],
    ) -> None:
        registry = getattr(self, "_unresolved_submissions", None)
        if not registry:
            return
        for order in orders:
            client_id = str(getattr(order, "client_id", "") or "")
            if client_id in registry:
                registry.pop(client_id, None)

    def get_unresolved_submissions(self) -> List[Dict[str, Any]]:
        """Return copies of mutation intents that remain unconfirmed."""
        registry = getattr(self, "_unresolved_submissions", {})
        return [dict(item) for item in registry.values()]

    def get_unresolved_cancellations(self) -> List[tuple[str, str]]:
        """Return exact keys for cancellation outcomes lacking terminal proof."""
        return sorted(getattr(self, "_uncertain_cancellations", set()))

    def enable_terminal_cancellation_outcomes(self) -> None:
        """Enable the MM-only exact outcome side channel."""
        self._capture_terminal_cancellation_outcomes = True

    def get_terminal_cancellation_outcome(
        self,
        symbol: str,
        order_id: str,
    ) -> Optional[OrderData]:
        """Return exact terminal proof captured during cancel reconciliation."""
        key = (str(symbol), str(order_id))
        if key not in getattr(self, "_uncertain_cancellations", set()):
            return None
        outcomes = getattr(self, "_terminal_cancellation_outcomes", {})
        return outcomes.get(key)

    def confirm_terminal_cancellation_outcome(
        self,
        symbol: str,
        order_id: str,
        status: Any,
    ) -> bool:
        """Clear one cancel marker only after an exact terminal order update."""
        status_value = getattr(status, "value", status)
        if str(status_value or "").lower() not in {
            "filled",
            "canceled",
            "cancelled",
            "rejected",
            "expired",
        }:
            return False
        uncertain = getattr(self, "_uncertain_cancellations", set())
        key = (str(symbol), str(order_id))
        outcomes = getattr(self, "_terminal_cancellation_outcomes", {})
        had_uncertain = key in uncertain
        had_outcome = key in outcomes
        if not had_uncertain and not had_outcome:
            return False
        if had_outcome:
            stored_status = getattr(outcomes[key], "status", None)
            stored_value = str(
                getattr(stored_status, "value", stored_status) or ""
            ).lower()
            supplied_value = str(status_value or "").lower()
            if stored_value == "cancelled":
                stored_value = "canceled"
            if supplied_value == "cancelled":
                supplied_value = "canceled"
            if stored_value != supplied_value:
                return False
        uncertain.discard(key)
        outcomes.pop(key, None)
        return True

    async def resolve_unresolved_submissions(self) -> List[OrderData]:
        """Resolve registered intents with bounded bulk snapshots."""
        registry = list(getattr(self, "_unresolved_submissions", {}).values())
        pending_by_symbol: Dict[str, Dict[str, int]] = {}
        for item in registry:
            client_order_id = int(item["client_order_id"])
            pending_by_symbol.setdefault(item["symbol"], {})[
                str(client_order_id)
            ] = client_order_id

        resolved: List[OrderData] = []
        for attempt in range(self.MUTATION_RECONCILIATION_ATTEMPTS):
            for symbol, pending in pending_by_symbol.items():
                for source, fetch in (
                    ("open", self.get_open_orders),
                    ("history", self.get_order_history),
                ):
                    if not pending:
                        break
                    try:
                        orders = await fetch(symbol)
                    except Exception as exc:
                        logger.warning(
                            "Failed to reconcile ambiguous order submissions: "
                            f"symbol={symbol}, source={source}, error={exc}"
                        )
                        continue

                    for order in orders:
                        client_id = str(getattr(order, "client_id", "") or "")
                        if client_id not in pending:
                            continue
                        self._clear_unresolved_submission(pending.pop(client_id))
                        resolved.append(order)
                        logger.warning(
                            "Reconciled ambiguous order submission by client id: "
                            f"client_order_id={client_id}, order_id={order.id}, "
                            f"source={source}"
                        )

            if not any(pending_by_symbol.values()):
                break
            if attempt + 1 < self.MUTATION_RECONCILIATION_ATTEMPTS:
                await asyncio.sleep(self.MUTATION_RECONCILIATION_DELAY)
        return resolved

    async def _reconcile_order_submission(
        self,
        symbol: str,
        client_order_id: int,
    ) -> Optional[OrderData]:
        """Look up one ambiguous submission without repeating the mutation."""
        for attempt in range(self.MUTATION_RECONCILIATION_ATTEMPTS):
            for source, fetch in (
                ("open", self.get_open_orders),
                ("history", self.get_order_history),
            ):
                try:
                    orders = await fetch(symbol)
                except Exception as exc:
                    logger.warning(
                        "Failed to reconcile ambiguous order submission: "
                        f"client_order_id={client_order_id}, source={source}, error={exc}"
                    )
                    continue

                for order in orders:
                    if self._order_matches_client_id(order, client_order_id):
                        self._clear_unresolved_submission(client_order_id)
                        logger.warning(
                            "Reconciled ambiguous order submission by client id: "
                            f"client_order_id={client_order_id}, order_id={order.id}, "
                            f"source={source}"
                        )
                        return order

            if attempt + 1 < self.MUTATION_RECONCILIATION_ATTEMPTS:
                await asyncio.sleep(self.MUTATION_RECONCILIATION_DELAY)
        return None

    def _build_uncertain_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal,
        client_order_id: int,
        reason: str,
        **kwargs,
    ) -> OrderData:
        """Return a tracked placeholder so callers do not retry with a new id."""
        client_id = str(client_order_id)
        self._register_unresolved_submission(
            client_order_id,
            symbol,
            order_type,
            side,
            quantity,
            price,
        )
        params = dict(kwargs)
        params["client_order_id"] = client_order_id
        params["submission_uncertain"] = True
        return OrderData(
            id=None,
            client_id=client_id,
            symbol=symbol,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            type=OrderType.MARKET if order_type == "market" else OrderType.LIMIT,
            amount=quantity,
            price=price,
            filled=Decimal("0"),
            remaining=quantity,
            cost=Decimal("0"),
            average=None,
            status=OrderStatus.PENDING,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params=params,
            raw_data={
                "submission_uncertain": True,
                "client_order_id": client_id,
                "uncertainty_reason": reason,
            },
        )

    async def _handle_ambiguous_order_submission(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal,
        reason: str,
        **kwargs,
    ) -> OrderData:
        client_order_id = kwargs["client_order_id"]
        reconciled = await self._reconcile_order_submission(symbol, client_order_id)
        if reconciled is not None:
            return reconciled

        logger.error(
            "Order submission outcome remains uncertain; preserving client id without retry: "
            f"client_order_id={client_order_id}, reason={reason}"
        )
        order_kwargs = dict(kwargs)
        order_kwargs.pop("client_order_id", None)
        return self._build_uncertain_order(
            symbol,
            side,
            order_type,
            quantity,
            price,
            client_order_id,
            reason,
            **order_kwargs,
        )

    async def _reconcile_cancellation(
        self,
        symbol: str,
        order_id: str,
    ) -> Optional[bool]:
        """Resolve cancellation only from an exact active or terminal-history match."""
        target = str(order_id)
        active_seen = False

        def matches(order: OrderData) -> bool:
            return target in {
                str(getattr(order, "id", "") or ""),
                str(getattr(order, "client_id", "") or ""),
            }

        for attempt in range(self.CANCELLATION_RECONCILIATION_ATTEMPTS):
            try:
                active_orders = await self.get_open_orders(symbol)
            except Exception as exc:
                logger.warning(
                    "Failed to reconcile ambiguous cancellation: "
                    f"order_id={order_id}, source=open, error={exc}"
                )
            else:
                if any(matches(order) for order in active_orders):
                    active_seen = True

            try:
                history = await self.get_order_history(symbol)
            except Exception as exc:
                logger.warning(
                    "Failed to reconcile ambiguous cancellation: "
                    f"order_id={order_id}, source=history, error={exc}"
                )
            else:
                matching_orders = [order for order in history if matches(order)]
                for order in matching_orders:
                    status = getattr(order, "status", None)
                    status_value = getattr(status, "value", status)
                    normalized_status = str(status_value or "").lower()
                    if normalized_status == "filled" and bool(
                        getattr(
                            self,
                            "_capture_terminal_cancellation_outcomes",
                            False,
                        )
                    ):
                        outcomes = getattr(
                            self, "_terminal_cancellation_outcomes", None
                        )
                        if outcomes is None:
                            outcomes = {}
                            self._terminal_cancellation_outcomes = outcomes
                        outcomes[(str(symbol), target)] = order
                        return False
                for order in matching_orders:
                    status = getattr(order, "status", None)
                    status_value = getattr(status, "value", status)
                    normalized_status = str(status_value or "").lower()
                    if normalized_status in {
                        "canceled",
                        "cancelled",
                        "rejected",
                        "expired",
                    }:
                        return True

            if attempt + 1 < self.CANCELLATION_RECONCILIATION_ATTEMPTS:
                await asyncio.sleep(self.CANCELLATION_RECONCILIATION_DELAY)
        return False if active_seen else None

    async def _handle_ambiguous_cancellation(
        self,
        symbol: str,
        order_id: str,
        reason: str,
    ) -> bool:
        """Reconcile an uncertain cancel and prevent blind mutation retries."""
        uncertain = getattr(self, "_uncertain_cancellations", None)
        if uncertain is None:
            uncertain = set()
            self._uncertain_cancellations = uncertain
        key = (symbol, str(order_id))
        uncertain.add(key)

        self.begin_safety_requests()
        try:
            reconciled = await self._reconcile_cancellation(symbol, order_id)
        finally:
            self.end_safety_requests()
        if reconciled is True:
            confirmed = self.confirm_terminal_cancellation_outcome(
                symbol, order_id, OrderStatus.CANCELED
            )
            if confirmed:
                logger.warning(
                    "Reconciled ambiguous cancellation as complete: "
                    f"order_id={order_id}, reason={reason}"
                )
                return True

        if self.get_terminal_cancellation_outcome(symbol, order_id) is not None:
            logger.warning(
                "Reconciled ambiguous cancellation as an exact terminal fill; "
                f"order_id={order_id}, reason={reason}"
            )
            return False

        logger.error(
            "Cancellation outcome remains uncertain; signer mutation will not be repeated: "
            f"order_id={order_id}, reason={reason}"
        )
        return False

    async def connect(self):
        """连接（调用initialize）"""
        await self.initialize()

    async def initialize(self):
        """初始化API客户端"""
        try:
            # 创建API客户端
            configuration = Configuration(host=self.base_url)
            self.api_client = ApiClient(configuration=configuration)

            # 创建各种API实例
            self.account_api = AccountApi(self.api_client)
            self.order_api = OrderApi(self.api_client)
            self.transaction_api = TransactionApi(self.api_client)
            self.candlestick_api = CandlestickApi(self.api_client)
            self.funding_api = FundingApi(self.api_client)

            # SignerClient由LighterBase创建；这里只验证配置是否与账户匹配。
            if self.signer_client:
                # 检查客户端
                err = self.signer_client.check_client()
                if err is not None:
                    error_msg = self.parse_error(err)
                    logger.error(f"SignerClient检查失败: {error_msg}")
                    raise Exception(f"SignerClient初始化失败: {error_msg}")

            self._connected = True
            logger.info("Lighter REST客户端连接成功")

            # 加载市场信息
            await self._load_markets()

        except Exception as e:
            logger.error(f"Lighter REST客户端初始化失败: {e}")
            raise

    async def close(self):
        """关闭连接"""
        try:
            # 🔥 断开WebSocket
            if self._websocket:
                try:
                    await self._websocket.disconnect()
                except Exception as ws_err:
                    logger.warning(f"断开WebSocket时出错: {ws_err}")

            if self.signer_client:
                await self.signer_client.close()
            if self.api_client:
                await self.api_client.close()
            self._connected = False
            logger.info("Lighter REST客户端已关闭")
        except Exception as e:
            logger.error(f"关闭Lighter REST客户端时出错: {e}")

    async def disconnect(self):
        """断开连接（调用close）"""
        await self.close()

    # ============= 市场数据 =============

    async def _load_markets(self):
        """
        加载市场信息

        ⚠️  重要说明：
        - Lighter的market_id从0开始，不是从1开始
        - ETH的market_id是0（这是最重要的市场）
        - 必须使用order_books() API获取完整列表，不能用循环遍历market_id
        - order_book_details(market_id) 只返回有交易的市场，可能漏掉不活跃的市场

        市场ID示例：
        - market_id=0: ETH (价格精度: 2位小数, 乘数: 100)
        - market_id=1: BTC (价格精度: 1位小数, 乘数: 10)
        - market_id=2: SOL (价格精度: 3位小数, 乘数: 1000)
        """
        try:
            # 获取订单簿列表（包含市场信息）
            # ⚠️ 必须使用此API，它会返回所有市场包括market_id=0的ETH
            response = await self._call_api(
                "markets query",
                lambda: self.order_api.order_books(filter="perp"),
            )

            if hasattr(response, 'order_books'):
                markets = []
                self.markets.clear()
                self._markets_cache.clear()
                self._symbol_to_market_index.clear()
                for order_book_info in response.order_books:
                    if hasattr(order_book_info, 'symbol') and hasattr(order_book_info, 'market_id'):
                        if getattr(order_book_info, 'status', None) != 'active':
                            continue
                        market_info = {
                            "market_id": order_book_info.market_id,
                            "symbol": order_book_info.symbol,
                            "market_type": getattr(order_book_info, 'market_type', 'perp'),
                            "status": getattr(order_book_info, 'status', 'active'),
                            "supported_price_decimals": getattr(
                                order_book_info, 'supported_price_decimals', None),
                            "supported_size_decimals": getattr(
                                order_book_info, 'supported_size_decimals', None),
                            "min_base_amount": getattr(
                                order_book_info, 'min_base_amount', None),
                            "min_quote_amount": getattr(
                                order_book_info, 'min_quote_amount', None),
                        }
                        markets.append(market_info)

                        # 🔥 同时填充 self.markets 字典（用于WebSocket）
                        self.markets[order_book_info.symbol] = market_info

                self.update_markets_cache(markets)
                logger.info(f"加载了 {len(markets)} 个市场")

                if not markets:
                    raise RuntimeError("Lighter API未返回任何活跃的perp市场")

        except Exception as e:
            logger.error(f"加载市场信息失败: {e}")
            raise

    async def get_exchange_info(self) -> ExchangeInfo:
        """
        获取交易所信息

        Returns:
            ExchangeInfo对象
        """
        try:
            # 获取订单簿信息
            response = await self._call_api(
                "exchange info query",
                lambda: self.order_api.order_books(filter="perp"),
            )

            symbols = []
            if hasattr(response, 'order_books'):
                for ob in response.order_books:
                    if hasattr(ob, 'symbol') and hasattr(ob, 'market_id'):
                        if getattr(ob, 'status', None) != 'active':
                            continue
                        symbols.append({
                            "symbol": ob.symbol,
                            "market_id": ob.market_id,
                            "base_asset": ob.symbol,
                            "quote_asset": "USD",
                            "status": getattr(ob, 'status', 'trading'),
                            "price_decimals": getattr(
                                ob, 'supported_price_decimals', None),
                            "size_decimals": getattr(
                                ob, 'supported_size_decimals', None),
                            "min_base_amount": getattr(
                                ob, 'min_base_amount', None),
                            "min_quote_amount": getattr(
                                ob, 'min_quote_amount', None),
                        })

            # 创建 ExchangeInfo 对象
            info = ExchangeInfo(
                name="Lighter",
                id="lighter",
                type=None,
                supported_features=[],
                rate_limits={},
                precision={},
                fees={},
                markets={s['symbol']: s for s in symbols},
                status="online",
                timestamp=datetime.now()
            )
            info.symbols = symbols  # 添加 symbols 属性以保持兼容性
            return info

        except Exception as e:
            logger.error(f"获取交易所信息失败: {e}")
            raise

    async def get_ticker(self, symbol: str) -> Optional[TickerData]:
        """
        获取ticker数据

        Args:
            symbol: 交易对符号

        Returns:
            TickerData对象
        """
        try:
            market_id = self.get_market_index(symbol)
            if market_id is None:
                logger.warning(f"未找到交易对 {symbol} 的市场ID")
                return None

            # 获取市场统计信息（包含价格信息）
            response = await self._call_api(
                "ticker query",
                lambda: self.order_api.order_book_details(market_id=market_id),
            )

            if not response or not hasattr(response, 'order_book_details') or not response.order_book_details:
                return None

            detail = response.order_book_details[0]

            # 解析ticker数据（基于实际API返回字段）
            # 🔥 修复：不使用0作为默认值，避免返回无效价格（多交易所兼容性）
            last_price = self._safe_decimal(
                getattr(detail, 'last_trade_price', None))
            daily_high = self._safe_decimal(
                getattr(detail, 'daily_price_high', None))
            daily_low = self._safe_decimal(
                getattr(detail, 'daily_price_low', None))
            daily_volume = self._safe_decimal(
                getattr(detail, 'daily_base_token_volume', None))

            # 尝试获取最佳买卖价（从订单簿）
            bid_price = last_price
            ask_price = last_price
            try:
                orderbook_response = await self._call_api(
                    "best price query",
                    lambda: self.order_api.order_book_orders(
                        market_id=market_id,
                        limit=1,
                    ),
                )
                if orderbook_response.bids:
                    bid_price = self._safe_decimal(
                        orderbook_response.bids[0].price)
                if orderbook_response.asks:
                    ask_price = self._safe_decimal(
                        orderbook_response.asks[0].price)
            except Exception as e:
                logger.debug(f"无法获取订单簿最佳价格: {e}")

            return TickerData(
                symbol=symbol,
                last=last_price,
                bid=bid_price,
                ask=ask_price,
                volume=daily_volume,
                high=daily_high,
                low=daily_low,
                timestamp=datetime.now()
            )

        except Exception as e:
            logger.error(f"获取ticker失败 {symbol}: {e}")
            return None

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Optional[OrderBookData]:
        """
        获取订单簿

        Args:
            symbol: 交易对符号
            limit: 深度限制

        Returns:
            OrderBookData对象
        """
        try:
            market_id = self.get_market_index(symbol)
            if market_id is None:
                logger.warning(f"未找到交易对 {symbol} 的市场ID")
                return None

            # 使用 order_book_orders 获取订单簿深度
            response = await self._call_api(
                "order book query",
                lambda: self.order_api.order_book_orders(
                    market_id=market_id,
                    limit=limit,
                ),
            )

            if not response:
                return None

            # 解析买单和卖单
            # Lighter返回完整订单对象：{price, remaining_base_amount, order_id, ...}
            bids = []
            asks = []

            if hasattr(response, 'bids') and response.bids:
                for bid in response.bids[:limit]:
                    # 提取 price 和 remaining_base_amount
                    price = self._safe_decimal(getattr(bid, 'price', 0))
                    quantity = self._safe_decimal(
                        getattr(bid, 'remaining_base_amount', 0))

                    if price > 0 and quantity > 0:
                        bids.append(OrderBookLevel(
                            price=price,
                            size=quantity
                        ))

            if hasattr(response, 'asks') and response.asks:
                for ask in response.asks[:limit]:
                    # 提取 price 和 remaining_base_amount
                    price = self._safe_decimal(getattr(ask, 'price', 0))
                    quantity = self._safe_decimal(
                        getattr(ask, 'remaining_base_amount', 0))

                    if price > 0 and quantity > 0:
                        asks.append(OrderBookLevel(
                            price=price,
                            size=quantity
                        ))

            return OrderBookData(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(),
                nonce=None
            )

        except Exception as e:
            logger.error(f"获取订单簿失败 {symbol}: {e}")
            return None

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[TradeData]:
        """
        获取最近成交

        Args:
            symbol: 交易对符号
            limit: 数量限制

        Returns:
            TradeData列表
        """
        try:
            market_id = self.get_market_index(symbol)
            if market_id is None:
                logger.warning(f"未找到交易对 {symbol} 的市场ID")
                return []

            # 获取最近成交
            response = await self._call_api(
                "recent trades query",
                lambda: self.order_api.recent_trades(
                    market_id=market_id,
                    limit=limit,
                ),
            )

            trades = []
            if hasattr(response, 'trades') and response.trades:
                for trade in response.trades:
                    price = self._safe_decimal(trade.price) if hasattr(
                        trade, 'price') else Decimal("0")
                    amount = self._safe_decimal(trade.size) if hasattr(
                        trade, 'size') else Decimal("0")
                    cost = price * amount

                    # is_maker_ask=true表示卖方是maker，因此taker方向为买。
                    is_maker_ask = getattr(trade, 'is_maker_ask', False)
                    side = OrderSide.BUY if is_maker_ask else OrderSide.SELL
                    taker_order_id = (
                        getattr(trade, 'bid_id', None)
                        if is_maker_ask
                        else getattr(trade, 'ask_id', None)
                    )

                    trades.append(TradeData(
                        id=str(getattr(
                            trade, 'trade_id_str',
                            getattr(trade, 'trade_id', ''))),
                        symbol=symbol,
                        side=side,
                        amount=amount,
                        price=price,
                        cost=cost,
                        fee=None,
                        timestamp=self._parse_timestamp(
                            getattr(trade, 'timestamp', None)) or datetime.now(),
                        order_id=(
                            str(taker_order_id)
                            if taker_order_id is not None else None
                        ),
                        raw_data={'trade': trade}
                    ))

            return trades

        except Exception as e:
            logger.error(f"获取最近成交失败 {symbol}: {e}")
            return []

    async def get_account_trades(
        self, symbol: str, limit: int = 100
    ) -> List[TradeData]:
        """Return authenticated account trades with exact role, fee and PnL."""
        if not self.signer_client:
            raise RuntimeError("未配置SignerClient，无法获取账户成交")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("account trade limit must be between 1 and 100")
        market_id = self.get_market_index(symbol)
        if market_id is None:
            raise ValueError(f"未找到交易对 {symbol} 的市场索引")

        auth_result = self.signer_client.create_auth_token_with_expiry(
            deadline=3600,
            api_key_index=self.api_key_index,
        )
        if isinstance(auth_result, tuple):
            auth_token, error = auth_result
            if error:
                raise RuntimeError(f"生成认证令牌失败: {error}")
        else:
            auth_token = auth_result
        if not auth_token:
            raise RuntimeError("生成认证令牌失败: empty token")

        response = await self._call_api(
            "account trades query",
            lambda: self.order_api.trades(
                sort_by="timestamp",
                limit=limit,
                authorization=auth_token,
                market_id=market_id,
                market_type="perp",
                account_index=self.account_index,
                sort_dir="desc",
                aggregate=False,
            ),
        )
        self._require_success_response(response, "account trades query")
        if not hasattr(response, "trades"):
            raise RuntimeError("账户成交 API 回应缺少 trades 字段")

        parsed: List[TradeData] = []
        for trade in response.trades or []:
            own_ask = getattr(trade, "ask_account_id", None) == self.account_index
            own_bid = getattr(trade, "bid_account_id", None) == self.account_index
            if own_ask == own_bid:
                raise RuntimeError(
                    "账户成交无法唯一归因，可能是 self-trade 或账户索引不符"
                )
            is_maker_ask = getattr(trade, "is_maker_ask", None)
            if type(is_maker_ask) is not bool:
                raise RuntimeError("账户成交缺少可信 maker/taker 角色")
            is_maker = is_maker_ask == own_ask
            fee_tick = getattr(
                trade, "maker_fee" if is_maker else "taker_fee", None
            )
            if fee_tick is None:
                raise RuntimeError("账户成交缺少实际 fee tick")
            def parse_value(value: Any, name: str) -> Decimal:
                try:
                    result = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise RuntimeError(f"账户成交 {name} 无效") from exc
                if not result.is_finite():
                    raise RuntimeError(f"账户成交 {name} 无效")
                return result

            fee_tick_decimal = parse_value(fee_tick, "fee tick")
            amount = parse_value(getattr(trade, "size", None), "size")
            price = parse_value(getattr(trade, "price", None), "price")
            turnover = parse_value(
                getattr(trade, "usd_amount", None), "usd amount"
            )
            gross_value = getattr(
                trade,
                "ask_account_pnl" if own_ask else "bid_account_pnl",
                None,
            )
            # Lighter returns null when this fill does not realize inventory PnL.
            gross = (
                Decimal("0")
                if gross_value is None
                else parse_value(gross_value, "account pnl")
            )
            if (
                fee_tick_decimal < 0
                or fee_tick_decimal != fee_tick_decimal.to_integral_value()
            ):
                raise RuntimeError("账户成交 fee tick 无效")
            if amount <= 0:
                raise RuntimeError("账户成交 size 无效")
            if price <= 0:
                raise RuntimeError("账户成交 price 无效")
            if turnover <= 0:
                raise RuntimeError("账户成交 usd amount 无效")
            integrator_fee = getattr(
                trade,
                "integrator_maker_fee" if is_maker else "integrator_taker_fee",
                None,
            )
            integrator_fee_tick = (
                None
                if integrator_fee is None
                else parse_value(integrator_fee, "integrator fee")
            )
            if integrator_fee_tick is not None and integrator_fee_tick != 0:
                raise RuntimeError("账户成交包含未纳入的 integrator fee")
            fee_rate = fee_tick_decimal / Decimal("1000000")
            order_id = getattr(
                trade,
                "ask_id_str" if own_ask else "bid_id_str",
                None,
            ) or getattr(trade, "ask_id" if own_ask else "bid_id", None)
            timestamp = getattr(trade, "timestamp", None)
            if type(timestamp) is not int or timestamp < 0:
                raise RuntimeError("账户成交 timestamp 无效")
            trade_sequence_value = getattr(trade, "trade_id", None)
            trade_id = getattr(trade, "trade_id_str", None) or trade_sequence_value
            try:
                trade_sequence = int(str(trade_sequence_value))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("账户成交 trade id 无效") from exc
            if trade_sequence < 0 or trade_id is None or not str(trade_id):
                raise RuntimeError("账户成交 trade id 无效")
            parsed.append(
                TradeData(
                    id=str(trade_id),
                    symbol=symbol,
                    side=OrderSide.SELL if own_ask else OrderSide.BUY,
                    amount=amount,
                    price=price,
                    cost=turnover,
                    fee={
                        "role": "maker" if is_maker else "taker",
                        "tick": int(fee_tick_decimal),
                        "rate": fee_rate,
                        "cost": turnover * fee_rate,
                        "currency": (
                            "USDG"
                            if self.network in {"robinhood", "robinhood_testnet"}
                            else "USDC"
                        ),
                    },
                    timestamp=self._parse_timestamp(timestamp) or datetime.now(),
                    order_id=str(order_id) if order_id is not None else None,
                    raw_data={
                        "timestamp": timestamp,
                        "trade_sequence": trade_sequence,
                        "trade_type": str(getattr(trade, "type", "")),
                        "realized_pnl": gross,
                        "integrator_fee_tick": integrator_fee_tick,
                    },
                )
            )
        return parsed

    # ============= 账户信息 =============

    async def get_account_balance(self) -> List[BalanceData]:
        """
        获取账户余额

        Returns:
            BalanceData列表
        """
        if not self.signer_client:
            raise RuntimeError("未配置SignerClient，无法获取账户信息")

        try:
            # 获取账户信息
            response = await self._call_api(
                "account balance query",
                lambda: self.account_api.account(
                    by="index",
                    value=str(self.account_index),
                ),
            )
            self._require_success_response(response, "account balance query")

            balances = []

            # 解析 DetailedAccounts 结构
            account = self._configured_account(response, "account balance query")

            # 获取可用余额和抵押品
            available_balance = _require_finite_decimal(
                getattr(account, 'available_balance', None),
                "available balance",
            )
            collateral = _require_finite_decimal(
                getattr(account, 'collateral', None), "collateral"
            )
            if collateral < 0:
                raise RuntimeError("Invalid negative Lighter collateral")

            # 计算锁定余额（抵押品 - 可用余额）
            locked = max(collateral - available_balance, Decimal("0"))

            collateral_currency = (
                "USDG"
                if self.network in {"robinhood", "robinhood_testnet"}
                else "USDC"
            )
            from datetime import datetime
            balances.append(BalanceData(
                currency=collateral_currency,
                free=available_balance,
                used=locked,
                total=collateral,
                usd_value=collateral,
                timestamp=datetime.now(),
                raw_data={'account': account}
            ))

            # 注意：Lighter是合约交易所，持仓不是余额
            # 持仓应该通过 get_positions() 方法查询

            return balances

        except Exception as e:
            logger.error(f"获取账户余额失败: {e}")
            raise

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        """
        获取活跃订单

        Args:
            symbol: 交易对符号（可选，为None时获取所有）

        Returns:
            OrderData列表
        """
        if not self.signer_client:
            raise RuntimeError("未配置SignerClient，无法获取订单信息")

        try:
            # 🔥 修复：Lighter 需要使用专门的订单查询 API，而不是 account API
            # 生成认证令牌
            auth_result = self.signer_client.create_auth_token_with_expiry(
                deadline=3600,
                api_key_index=self.api_key_index,
            )

            # SDK 返回元组 (token, error)
            if isinstance(auth_result, tuple):
                auth_token, error = auth_result
                if error:
                    raise RuntimeError(f"生成认证令牌失败: {error}")
            else:
                auth_token = auth_result
            if not auth_token:
                raise RuntimeError("生成认证令牌失败: empty token")

            # 获取 market_id
            market_id = None
            if symbol:
                market_id = self.get_market_index(symbol)
                if market_id is None:
                    raise ValueError(f"未找到交易对 {symbol} 的市场索引")

            # 使用 account_active_orders API（SDK 方法是异步的，直接 await）
            response = await self._call_api(
                "active orders query",
                lambda: self.order_api.account_active_orders(
                    authorization=auth_token,
                    account_index=self.account_index,
                    market_id=market_id,
                    market_type="perp",
                ),
            )
            self._require_success_response(response, "active orders query")

            if not hasattr(response, 'orders'):
                raise RuntimeError("活跃订单 API 回应缺少 orders 字段")

            orders = []

            # 🔥 account_active_orders 返回 orders 列表，不是 accounts
            if hasattr(response, 'orders') and response.orders:
                logger.info(f"🔍 REST API返回 {len(response.orders)} 个活跃订单")

                for order_info in response.orders:
                    order_symbol = self._get_symbol_from_market_index(
                        getattr(order_info, 'market_index', None))

                    # 如果指定了symbol，过滤
                    if symbol and order_symbol != symbol:
                        continue

                    orders.append(self._parse_order(order_info, order_symbol))
            else:
                logger.info(f"✅ REST API确认无活跃订单")

            self._clear_resolved_submissions_from_orders(orders)
            return orders

        except Exception as e:
            logger.error(f"获取活跃订单失败: {e}")
            raise

    async def get_order(self, order_id: str, symbol: str) -> OrderData:
        """
        获取单个订单信息（网格系统关键方法）

        Args:
            order_id: 订单ID
            symbol: 交易对符号

        Returns:
            OrderData对象

        Raises:
            Exception: 如果订单不存在或查询失败
        """
        if not self.signer_client:
            logger.error("未配置SignerClient，无法获取订单信息")
            raise Exception("未配置SignerClient")

        try:
            target_id = str(order_id)

            def matches(order: OrderData) -> bool:
                return target_id == str(order.id) or (
                    order.client_id not in (None, '')
                    and target_id == str(order.client_id)
                )

            # 获取所有活跃订单
            open_orders = await self.get_open_orders(symbol)

            # 在活跃订单中查找
            for order in open_orders:
                if matches(order):
                    logger.debug(f"找到订单: {order_id}, 状态={order.status.value}")
                    return order

            # 活跃订单中不存在时，只能由交易所的最终订单记录确认状态。
            history = await self.get_order_history(symbol)
            for order in history:
                if matches(order):
                    logger.debug(
                        f"找到历史订单: {order_id}, 状态={order.status.value}"
                    )
                    return order

            raise LookupError(f"Lighter order {order_id} not found for {symbol}")

        except Exception as e:
            logger.error(f"获取订单 {order_id} 失败: {e}")
            raise

    async def get_order_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[OrderData]:
        """
        获取历史订单（已完成/取消）

        Args:
            symbol: 交易对符号（可选）
            limit: 返回数量限制

        Returns:
            OrderData列表
        """
        if not self.signer_client:
            raise RuntimeError("未配置SignerClient，无法获取订单历史")

        try:
            # 生成认证令牌
            auth_result = self.signer_client.create_auth_token_with_expiry(
                deadline=3600,
                api_key_index=self.api_key_index,
            )
            if isinstance(auth_result, tuple):
                auth_token, error = auth_result
                if error:
                    raise RuntimeError(f"生成認證令牌失敗: {error}")
            else:
                auth_token = auth_result
            if not auth_token:
                raise RuntimeError("生成認證令牌失敗: empty token")

            # 获取市场ID（如果指定了symbol）
            market_id = None
            if symbol:
                market_id = self.get_market_index(symbol)
                if market_id is None:
                    raise ValueError(f"未找到交易對 {symbol} 的市場索引")

            # 获取历史订单
            response = await self._call_api(
                "inactive orders query",
                lambda: self.order_api.account_inactive_orders(
                    authorization=auth_token,
                    account_index=self.account_index,
                    limit=limit,
                    market_id=market_id,
                    market_type="perp",
                ),
            )
            self._require_success_response(response, "inactive orders query")

            if not hasattr(response, 'orders'):
                raise RuntimeError("歷史訂單 API 回應缺少 orders 欄位")

            orders = []
            if hasattr(response, 'orders') and response.orders:
                for order_info in response.orders:
                    order_symbol = self._get_symbol_from_market_index(
                        getattr(order_info, 'market_index', None))

                    orders.append(self._parse_order(order_info, order_symbol))

            self._clear_resolved_submissions_from_orders(orders)
            return orders

        except Exception as e:
            logger.error(f"获取历史订单失败: {e}")
            raise

    async def get_positions(self, symbols: Optional[List[str]] = None) -> List[PositionData]:
        """
        获取持仓信息

        Args:
            symbols: 交易对符号列表（Lighter会忽略，返回所有持仓）

        Returns:
            PositionData列表
        """
        if not self.signer_client:
            raise RuntimeError("未配置SignerClient，无法获取持仓信息")

        try:
            # 获取账户信息（包含持仓）
            response = await self._call_api(
                "positions query",
                lambda: self.account_api.account(
                    by="index",
                    value=str(self.account_index),
                ),
            )
            self._require_success_response(response, "positions query")

            positions = []
            # 解析 DetailedAccounts 结构
            account = self._configured_account(response, "positions query")

            if hasattr(account, 'positions') and account.positions:
                for idx, position_info in enumerate(account.positions):
                    symbol = position_info.symbol if hasattr(
                        position_info, 'symbol') else ""

                    # `position` is safety-critical: a malformed value must never
                    # be interpreted as a flat account during startup/stop-loss.
                    if not hasattr(position_info, 'position'):
                        raise RuntimeError(
                            f"Lighter position response is missing size for {symbol or idx}"
                        )
                    try:
                        raw_position_size = Decimal(str(position_info.position))
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        raise RuntimeError(
                            f"Invalid Lighter position size for {symbol or idx}"
                        ) from exc
                    if not raw_position_size.is_finite():
                        raise RuntimeError(
                            f"Invalid Lighter position size for {symbol or idx}"
                        )

                    if raw_position_size == 0:
                        continue

                    # SDK 1.1.2 returns a positive `position` magnitude and keeps
                    # direction in the separate `sign` field. Retain compatibility
                    # with older signed-position responses when `sign` is absent.
                    sign_raw = getattr(position_info, 'sign', None)
                    if sign_raw is None:
                        position_size = raw_position_size
                    else:
                        try:
                            position_sign = int(sign_raw)
                        except (TypeError, ValueError) as exc:
                            raise RuntimeError(
                                f"Invalid Lighter position sign for {symbol}: {sign_raw}"
                            ) from exc
                        if position_sign not in {-1, 1}:
                            raise RuntimeError(
                                f"Invalid Lighter position sign for {symbol}: {position_sign}"
                            )
                        if raw_position_size < 0:
                            raise RuntimeError(
                                f"Invalid negative Lighter position magnitude for {symbol or idx}"
                            )
                        position_size = raw_position_size * position_sign

                    from datetime import datetime
                    from ..models import PositionSide, MarginMode

                    # 🔥 Lighter持仓方向定义（与传统CEX一致）
                    # 正数 = 多头 (LONG) | 负数 = 空头 (SHORT)
                    # ✅ 测试验证：BUY订单成交后，position返回正数，表示做多
                    position_side = PositionSide.LONG if position_size > 0 else PositionSide.SHORT

                    unrealized_pnl = _require_finite_decimal(
                        getattr(position_info, 'unrealized_pnl', None),
                        f"unrealized PnL for {symbol or idx}",
                    )
                    margin_fraction = _require_finite_decimal(
                        getattr(position_info, 'initial_margin_fraction', None),
                        f"initial margin fraction for {symbol or idx}",
                    )
                    if margin_fraction <= 0:
                        raise RuntimeError(
                            f"Invalid Lighter initial margin fraction for {symbol or idx}"
                        )
                    if margin_fraction > 100:
                        raise RuntimeError(
                            f"Invalid Lighter initial margin fraction for {symbol or idx}"
                        )
                    # Lighter exposes percentage IMF with two-decimal precision
                    # (for example 33.33 for 3x), so its reciprocal is not
                    # necessarily an exact integer. Preserve the shared adapter's
                    # established conversion; MM validates exact 1x from raw IMF.
                    leverage_value = self._leverage_from_initial_margin_fraction(
                        margin_fraction
                    )
                    margin_mode_raw = getattr(position_info, 'margin_mode', None)
                    if type(margin_mode_raw) is bool:
                        raise RuntimeError(
                            f"Invalid Lighter margin mode for {symbol or idx}"
                        )
                    try:
                        margin_mode_value = int(str(margin_mode_raw))
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            f"Invalid Lighter margin mode for {symbol or idx}"
                        ) from exc
                    if margin_mode_value not in {0, 1}:
                        raise RuntimeError(
                            f"Invalid Lighter margin mode for {symbol or idx}"
                        )

                    positions.append(PositionData(
                        symbol=symbol,
                        side=position_side,
                        size=abs(position_size),
                        entry_price=self._safe_decimal(
                            getattr(position_info, 'avg_entry_price', 0)),
                        mark_price=None,  # Lighter不提供标记价格
                        current_price=None,  # 需要单独查询
                        unrealized_pnl=unrealized_pnl,
                        realized_pnl=self._safe_decimal(
                            getattr(position_info, 'realized_pnl', 0)),
                        percentage=None,  # 可以计算
                        leverage=leverage_value,
                        margin_mode=(
                            MarginMode.CROSS
                            if margin_mode_value == 0
                            else MarginMode.ISOLATED
                        ),
                        margin=self._safe_decimal(
                            getattr(position_info, 'allocated_margin', 0)),
                        liquidation_price=self._safe_decimal(getattr(position_info, 'liquidation_price', 0)) if getattr(
                            position_info, 'liquidation_price', '0') != '0' else None,
                        timestamp=datetime.now(),
                        raw_data={'position_info': position_info}
                    ))

            # 🔥 如果指定了symbols，只返回匹配的持仓
            if symbols:
                positions = [p for p in positions if p.symbol in symbols]

            return positions

        except Exception as e:
            logger.error(f"获取持仓信息失败: {e}")
            raise

    # ============= 交易功能 =============

    def _validate_order_preconditions(self) -> bool:
        """验证下单前置条件"""
        if not self.signer_client:
            logger.error("❌ 未配置SignerClient，无法下单")
            logger.error(
                "   请检查lighter_config.yaml中的api_key_private_key和account_index")
            return False
        return True

    async def _get_market_info(self, symbol: str) -> Optional[Dict]:
        """
        获取市场信息（索引、价格精度、乘数）

        🔥 使用缓存机制避免频繁API调用触发429限流
        这是关键修复！批量下单时必须使用缓存

        Returns:
            包含 market_index, price_decimals, price_multiplier 的字典，或 None
        """
        import time

        # 🔥 检查缓存（5分钟有效期）
        # 注意：缓存的是市场的静态配置（市场索引、价格精度），不是动态数据
        # 这些配置基本不会变化，所以可以缓存较长时间
        if symbol in self._market_info_cache:
            cache_entry = self._market_info_cache[symbol]
            cache_age = time.time() - cache_entry['timestamp']
            if cache_age < 300:  # 缓存5分钟内有效（300秒）
                logger.debug(f"✅ 使用缓存的市场信息: {symbol} (缓存年龄: {cache_age:.1f}秒)")
                return cache_entry['info']

        try:
            market_index = self.get_market_index(symbol)
            logger.debug(f"✅ 获取market_index: {market_index}")

            if market_index is None:
                logger.error(f"❌ 未找到交易对 {symbol} 的市场索引")
                logger.error(
                    f"   可用市场: {list(self.markets.keys()) if self.markets else '未加载'}")
                return None

            # 获取市场详情，动态获取价格精度
            logger.debug(f"🔍 获取市场详情: market_id={market_index}")
            market_details = await self._call_api(
                "market details query",
                lambda: self.order_api.order_book_details(
                    market_id=market_index,
                ),
            )
            if not market_details.order_book_details:
                raise RuntimeError(f"Lighter未返回市场详情: {symbol}")

            detail = market_details.order_book_details[0]
            price_decimals = int(detail.supported_price_decimals)
            size_decimals = int(detail.supported_size_decimals)

            logger.debug(
                f"✅ 获取市场精度成功: price_decimals={price_decimals}, "
                f"size_decimals={size_decimals}")

            price_multiplier = Decimal(10 ** price_decimals)
            size_multiplier = Decimal(10 ** size_decimals)
            logger.debug(
                f"{symbol} 价格乘数: {price_multiplier}, 数量乘数: {size_multiplier}")

            cached_market = self._markets_cache.get(market_index, {})
            market_info = {
                'market_index': market_index,
                'price_decimals': price_decimals,
                'price_multiplier': price_multiplier,
                'size_decimals': size_decimals,
                'size_multiplier': size_multiplier,
                'min_base_amount': cached_market.get('min_base_amount'),
                'min_quote_amount': cached_market.get('min_quote_amount'),
            }

            # 🔥 缓存市场信息（关键！）
            self._market_info_cache[symbol] = {
                'info': market_info,
                'timestamp': time.time()
            }
            logger.debug(f"💾 已缓存市场信息: {symbol}")

            return market_info

        except Exception as e:
            logger.error(f"获取市场信息失败: {e}")
            return None

    @staticmethod
    def _validate_order_minimums(
        quantity: Decimal,
        price: Optional[Decimal],
        market_info: Dict,
        enforce_market_minimums: bool = True,
    ) -> None:
        """Validate Lighter's base and quote minimums before signing an order."""
        quantity = Decimal(str(quantity))
        if quantity <= 0:
            raise ValueError("Order quantity must be greater than 0")
        if not enforce_market_minimums:
            return

        try:
            min_base = Decimal(str(market_info.get('min_base_amount') or 0))
            min_quote = Decimal(str(market_info.get('min_quote_amount') or 0))
        except Exception as exc:
            raise ValueError("Invalid Lighter market minimum metadata") from exc

        if min_base > 0 and quantity < min_base:
            raise ValueError(
                f"Order quantity {quantity} is below min_base_amount {min_base}"
            )
        if price is not None and min_quote > 0:
            notional = quantity * Decimal(str(price))
            if notional < min_quote:
                raise ValueError(
                    f"Order notional {notional} is below min_quote_amount {min_quote}"
                )

    async def _calculate_slippage_protection_price(
        self,
        symbol: str,
        side: str,
        provided_price: Optional[Decimal] = None
    ) -> Optional[Decimal]:
        """
        Calculate the market-order protection price from configured slippage.

        Args:
            symbol: 交易对符号
            side: 订单方向
            provided_price: 用户提供的价格（如果有）

        Returns:
            滑点保护价格，或 None
        """
        try:
            is_sell = (side.lower() == "sell")
            slippage = Decimal(str(
                self.config.get('market_order_slippage')
                if self.config.get('market_order_slippage') is not None
                else '0.01'
            ))
            if slippage < 0 or slippage >= 1:
                raise ValueError(
                    "market_order_slippage must be between 0 (inclusive) and 1"
                )

            if provided_price:
                base_price = Decimal(str(provided_price))
            else:
                orderbook = await self.get_orderbook(symbol)
                if not orderbook or not orderbook.bids or not orderbook.asks:
                    logger.error(f"无法获取{symbol}的订单簿，市价单需要价格")
                    return None
                base_price = (
                    orderbook.bids[0].price if is_sell else orderbook.asks[0].price
                )

            protection_price = base_price * (
                Decimal('1') - slippage if is_sell else Decimal('1') + slippage
            )

            logger.debug(
                f"市价单滑点保护价格: {protection_price} "
                f"(基准: {base_price}, 滑点: {slippage:.2%})")
            return protection_price

        except Exception as e:
            logger.error(f"计算滑点保护价格失败: {e}")
            return None

    @staticmethod
    def _convert_base_amount(market_info: Dict, quantity: Decimal) -> int:
        """Convert a decimal size without silently truncating unsupported precision."""
        scaled_amount = quantity * market_info['size_multiplier']
        if scaled_amount != scaled_amount.to_integral_value():
            raise ValueError(
                f"数量 {quantity} 超过市场允许的 "
                f"{market_info['size_decimals']} 位小数精度"
            )
        return int(scaled_amount)

    def _convert_market_order_params(
        self,
        market_info: Dict,
        quantity: Decimal,
        avg_execution_price: Decimal,
        side: str,
        **kwargs
    ) -> Dict:
        """转换市价单参数为Lighter格式"""
        # 🔥 先对价格应用精度规则（与限价单保持一致）
        price_decimals = market_info['price_decimals']
        if price_decimals == 0:
            quantize_precision = Decimal("1")
        else:
            quantize_precision = Decimal(10) ** (-price_decimals)

        is_ask = (side.lower() == "sell")
        avg_execution_price_rounded = avg_execution_price.quantize(
            quantize_precision,
            rounding=ROUND_CEILING if is_ask else ROUND_FLOOR,
        )

        base_amount_int = self._convert_base_amount(market_info, quantity)
        avg_price_int = int(avg_execution_price_rounded *
                            market_info['price_multiplier'])
        logger.debug(f"  symbol参数中的market_index={market_info['market_index']}")
        logger.debug(
            f"  价格精度: {market_info['price_decimals']}位小数, 乘数: {market_info['price_multiplier']}")
        logger.debug(
            f"  avg_execution_price={avg_execution_price} -> 四舍五入后={avg_execution_price_rounded}, avg_price_int={avg_price_int}")
        logger.debug(
            f"  reduce_only={kwargs.get('reduce_only', False)}")
        client_order_index = kwargs.get("client_order_id")
        if client_order_index is None:
            client_order_index = self._next_client_order_index()

        return {
            'market_index': market_info['market_index'],
            'client_order_index': client_order_index,
            'base_amount': base_amount_int,
            'avg_execution_price': avg_price_int,
            'is_ask': is_ask,
            'reduce_only': kwargs.get("reduce_only", False)
        }

    def _convert_limit_order_params(
        self,
        market_info: Dict,
        quantity: Decimal,
        price: Decimal,
        side: str,
        **kwargs
    ) -> Dict:
        """转换限价单参数为Lighter格式"""
        import lighter

        # 🔥 根据price_decimals动态调整价格精度（直接使用quantize避免浮点误差）
        # 例如：price_decimals=1 -> quantize(Decimal("0.1"))
        #      price_decimals=2 -> quantize(Decimal("0.01"))
        price_decimals = market_info['price_decimals']
        if price_decimals == 0:
            quantize_precision = Decimal("1")
        else:
            quantize_precision = Decimal(10) ** (-price_decimals)

        price_rounded = price.quantize(quantize_precision)

        base_amount_int = self._convert_base_amount(market_info, quantity)
        price_int = int(price_rounded * market_info['price_multiplier'])
        is_ask = (side.lower() == "sell")

        # 🔍 简化日志：只在 DEBUG 级别输出详细参数
        logger.debug(f"Lighter限价单参数: market_id={market_info['market_index']}, "
                     f"price={price_rounded}, quantity={quantity}, "
                     f"base_amount={base_amount_int}, is_ask={is_ask}")

        time_in_force = kwargs.get("time_in_force", "GTT")
        tif_map = {
            "IOC": lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            "GTT": lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            "POST_ONLY": lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
        }
        client_order_index = kwargs.get("client_order_id")
        if client_order_index is None:
            client_order_index = self._next_client_order_index()

        return {
            'market_index': market_info['market_index'],
            'client_order_index': client_order_index,
            'base_amount': base_amount_int,
            'price': price_int,
            'is_ask': is_ask,
            'order_type': lighter.SignerClient.ORDER_TYPE_LIMIT,
            'time_in_force': tif_map.get(time_in_force,
                                         lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME),
            'reduce_only': kwargs.get("reduce_only", False),
            'trigger_price': 0,
            # Pin the signer defaults so a managed order can never acquire an
            # unaccounted integrator fee through an SDK/default change.
            'integrator_account_index': 0,
            'integrator_taker_fee': self.MANAGED_ORDER_INTEGRATOR_FEE_TICK,
            'integrator_maker_fee': self.MANAGED_ORDER_INTEGRATOR_FEE_TICK,
        }

    async def _execute_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        provided_price: Optional[Decimal],
        market_info: Dict,
        batch_mode: bool = False,
        skip_order_index_query: bool = False,
        **kwargs
    ) -> Optional[OrderData]:
        """执行市价单"""
        # 🔥 生成唯一的 client_order_id（确保整个流程使用同一个值）
        if kwargs.get("client_order_id") is None:
            kwargs["client_order_id"] = self._next_client_order_index()

        # 计算滑点保护价格
        avg_execution_price = await self._calculate_slippage_protection_price(
            symbol, side, provided_price
        )
        if not avg_execution_price:
            return None

        try:
            self._validate_order_minimums(
                quantity,
                avg_execution_price,
                market_info,
                enforce_market_minimums=not kwargs.get("reduce_only", False),
            )
        except ValueError as exc:
            logger.error(f"订单低于Lighter市场最小限制: {exc}")
            return None

        # 转换参数
        params = self._convert_market_order_params(
            market_info, quantity, avg_execution_price, side, **kwargs
        )

        # 执行下单
        try:
            tx, response, err = await self._call_api(
                "market order submission",
                lambda: self.signer_client.create_market_order(**params),
                retry_on_429=False,
            )

            # 处理结果
            return await self._handle_order_result(
                tx, response, err, symbol, side, "market",
                quantity, avg_execution_price, batch_mode=batch_mode,
                skip_order_index_query=skip_order_index_query, **kwargs
            )
        except Exception as exc:
            if self._is_definitive_mutation_exception(exc):
                logger.error("执行市价单失败: HTTP 429 rate limited")
                raise RuntimeError(
                    "market order submission rate limited (HTTP 429)"
                ) from None
            return await self._handle_ambiguous_order_submission(
                symbol,
                side,
                "market",
                quantity,
                avg_execution_price,
                str(exc),
                **kwargs,
            )

    async def _execute_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
        market_info: Dict,
        batch_mode: bool = False,
        skip_order_index_query: bool = False,
        **kwargs
    ) -> Optional[OrderData]:
        """执行限价单"""
        if not price:
            logger.error("限价单必须指定价格")
            return None

        # 🔥 生成唯一的 client_order_id（确保整个流程使用同一个值）
        if kwargs.get("client_order_id") is None:
            kwargs["client_order_id"] = self._next_client_order_index()

        raise_on_pre_send_failure = bool(
            kwargs.pop("_raise_on_definitive_pre_send_failure", False)
        )
        raise_on_submission_rejection = bool(
            kwargs.pop("_raise_on_definitive_submission_rejection", False)
        )

        # 转换参数
        params = self._convert_limit_order_params(
            market_info, quantity, price, side, **kwargs
        )

        price_decimals = market_info['price_decimals']
        quantize_precision = (
            Decimal("1")
            if price_decimals == 0
            else Decimal(10) ** (-price_decimals)
        )
        price_rounded = price.quantize(quantize_precision)

        # 执行下单
        try:
            import lighter
            tx, response, err = await self._call_api(
                "limit order submission",
                lambda: self.signer_client.create_order(**params),
                retry_on_429=False,
            )
        except OrderSubmissionNotSentError:
            raise
        except Exception as exc:
            if (
                raise_on_pre_send_failure
                and self._is_configured_api_dns_failure(exc)
                and self._restore_nonce_after_pre_send_failure()
            ):
                logger.warning(
                    "Lighter limit order was not submitted: DNS resolution failed"
                )
                raise OrderSubmissionNotSentError(
                    "limit order was not submitted: DNS resolution failed"
                ) from None
            if self._is_definitive_mutation_exception(exc):
                logger.error("执行限价单失败: HTTP 429 rate limited")
                raise RuntimeError(
                    "limit order submission rate limited (HTTP 429)"
                ) from None
            return await self._handle_ambiguous_order_submission(
                symbol,
                side,
                "limit",
                quantity,
                price_rounded,
                str(exc),
                **kwargs,
            )

        if (
            raise_on_submission_rejection
            and self._is_invalid_nonce_rejection(tx, response, err)
        ):
            raise OrderSubmissionRejectedError(
                "order submission rejected: invalid nonce"
            ) from None

        # Keep post-send response handling outside the pre-connect DNS catch.
        # A later confirmation read must never make an accepted submission
        # look retry-safe.
        return await self._handle_order_result(
            tx, response, err, symbol, side, "limit",
            quantity, price_rounded, batch_mode=batch_mode,
            skip_order_index_query=skip_order_index_query, **kwargs
        )

    async def _handle_order_result(
        self,
        tx,
        response,
        err,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal,
        batch_mode: bool = False,
        skip_order_index_query: bool = False,
        **kwargs
    ) -> Optional[OrderData]:
        """
        处理下单结果

        ⚠️ 重要：Lighter下单API返回的是transaction hash，不是order_id
        真正的order_id需要从WebSocket推送或REST查询中获取

        Args:
            batch_mode: 批量下单模式，True时不立即查询order_index
        """
        # 检查错误
        if err:
            error_msg = self.parse_error(err) if err else "未知错误"
            mutation_error = err if isinstance(err, Exception) else RuntimeError(error_msg)
            if self._is_definitive_mutation_exception(mutation_error):
                logger.error("Lighter下单失败: HTTP 429 rate limited")
                raise RuntimeError(
                    "order submission rate limited (HTTP 429)"
                ) from None
            return await self._handle_ambiguous_order_submission(
                symbol,
                side,
                order_type,
                quantity,
                price,
                error_msg,
                **kwargs,
            )

        if response is None or getattr(response, "code", None) is None:
            return await self._handle_ambiguous_order_submission(
                symbol,
                side,
                order_type,
                quantity,
                price,
                "submission response missing success code",
                **kwargs,
            )

        try:
            self._require_success_response(response, "order submission")
        except RuntimeError as exc:
            if self._is_definitive_mutation_exception(exc):
                raise RuntimeError(
                    "order submission rate limited (HTTP 429)"
                ) from None
            logger.error(f"❌ Lighter下单失败: {exc}")
            return None

        tx_hash_str = str(getattr(response, 'tx_hash', '') or '')
        if not tx or not tx_hash_str:
            return await self._handle_ambiguous_order_submission(
                symbol,
                side,
                order_type,
                quantity,
                price,
                "successful response missing tx or tx_hash",
                **kwargs,
            )

        # 🔥 提取transaction hash（这不是order_id！）
        logger.info(f"✅ Lighter下单成功: tx_hash={tx_hash_str}")

        # 🔥 Lighter特殊处理：REST API无法立即查询到新下的订单
        # 原因：
        # 1. Lighter是Layer 2，订单需要时间上链
        # 2. account_api.account()不返回订单列表（只返回账户信息）
        # 3. order_api.account_active_orders()需要复杂的认证token
        #
        # 解决方案：
        # 1. 返回带tx_hash的临时OrderData
        # 2. 依赖WebSocket推送真正的order_id和状态
        # 3. 网格系统通过WebSocket回调更新订单信息
        logger.info(f"⚠️ Lighter下单成功，等待WebSocket推送真正的order_id")
        logger.info(f"   tx_hash: {tx_hash_str}")
        logger.info(f"   将通过WebSocket回调获取order_id和订单状态")

        # 🔥 Lighter专属：订单ID获取策略
        #
        # 批量模式（batch_mode=True）：
        # - 不立即查询 order_index（避免API频率限制）
        # - 使用 client_order_id 作为临时ID
        # - 依赖批量同步建立 order_index 映射
        #
        # 跳过查询模式（skip_order_index_query=True）：
        # - Volume Maker 刷量程序使用
        # - 市价单立即成交，查询必然失败且浪费资源
        # - 使用状态机匹配（基于方向+数量，不依赖 order_id）
        #
        # 单个模式（batch_mode=False, skip_order_index_query=False，默认）：
        # - 网格程序使用
        # - 立即查询 order_index（确保反手单可靠性）
        # - 直接使用 order_index 作为唯一标识
        from datetime import datetime
        client_order_id = kwargs.get("client_order_id")
        if client_order_id is None:
            raise RuntimeError("order submission lost its client_order_id")

        unresolved_reason: Optional[str] = None
        if batch_mode:
            # Batch mode deliberately skips the exchange order-index lookup.
            order_id = None
            unresolved_reason = "batch submission acknowledged without exchange order index"
            logger.info(
                f"📦 批量模式：等待 client_order_id={client_order_id} "
                "对应的 exchange order_index"
            )
        elif skip_order_index_query:
            # Skipping the lookup does not turn a client id into an exchange id.
            order_id = None
            unresolved_reason = "submission acknowledged without exchange order-index lookup"
            logger.debug(
                f"🔖 跳过查询模式：等待 client_order_id={client_order_id} "
                "对应的 exchange order_index"
            )
        else:
            # 单个模式：立即查询 order_index（网格程序）
            logger.info(f"🔍 单个模式：立即查询 order_index...")

            order_index = await self._query_order_index(
                symbol=symbol,
                side=side,
                price=price,
                amount=quantity,
                client_order_id=client_order_id,
                max_retries=3
            )

            if order_index:
                # ✅ 成功获取 order_index
                order_id = str(order_index)
                logger.info(
                    f"✅ 使用 order_index 作为订单ID: {order_id}"
                )
            else:
                # A client id and an exchange order index are different
                # namespaces. Keep the exchange id unset until an exact read
                # confirms it so cancellation can never target the wrong order.
                order_id = None
                unresolved_reason = "submission acknowledged but exchange order index lookup missed"
                logger.warning(
                    f"⚠️ 尚未取得 client_order_id={client_order_id} 的 order_index，"
                    f"tx_hash={tx_hash_str[:16]}..."
                )

        if order_id is None:
            placeholder_params = dict(kwargs)
            placeholder_params.pop("client_order_id", None)
            placeholder = self._build_uncertain_order(
                symbol,
                side,
                order_type,
                quantity,
                price,
                int(client_order_id),
                unresolved_reason or "exchange order index is unresolved",
                **placeholder_params,
            )
            placeholder.raw_data.update({
                'tx': tx,
                'response': response,
                'tx_hash_str': tx_hash_str,
                'submission_acknowledged': True,
            })
            return placeholder

        return OrderData(
            id=order_id,
            client_id=str(client_order_id),
            symbol=symbol,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            type=OrderType.MARKET if order_type == "market" else OrderType.LIMIT,
            amount=quantity,
            price=price,
            filled=Decimal("0"),
            remaining=quantity,
            cost=Decimal("0"),
            average=None,
            status=OrderStatus.PENDING,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params=kwargs,
            raw_data={'tx': tx, 'response': response, 'tx_hash_str': tx_hash_str}
        )

    async def _query_order_index(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        amount: Decimal,
        client_order_id: Optional[int] = None,
        max_retries: int = 3,
        retry_delay: float = 0.5
    ) -> Optional[str]:
        """
        通过价格和数量匹配查询 order_index

        🔥 Lighter专属：解决下单后无法立即获得 order_index 的问题

        背景：
        - Lighter 下单返回 tx_hash，不是 order_index
        - order_index 需要等待区块确认后才生成
        - 我们需要立即获取 order_index 以避免 WebSocket 匹配失败

        Args:
            symbol: 交易对符号
            side: 订单方向 ("buy" 或 "sell")
            price: 订单价格
            amount: 订单数量
            max_retries: 最大重试次数（默认3次）
            retry_delay: 重试延迟（秒，默认0.5秒）

        Returns:
            order_index (字符串) 或 None（查询失败）
        """
        for attempt in range(max_retries):
            try:
                # 首次查询前稍微等待，让订单上链
                if attempt == 0:
                    await asyncio.sleep(0.3)  # 首次等待300ms
                elif attempt > 0:
                    await asyncio.sleep(retry_delay * attempt)  # 递增延迟

                # 查询挂单列表
                open_orders = await self.get_open_orders(symbol)

                if not open_orders:
                    logger.debug(
                        f"🔍 尝试 {attempt+1}/{max_retries}: "
                        f"暂无挂单（订单可能还在上链）"
                    )
                    continue

                if client_order_id is not None:
                    for order in open_orders:
                        if self._order_matches_client_id(order, client_order_id):
                            logger.info(
                                f"✅ 查询到 order_index: {order.id} "
                                f"(client_order_id={client_order_id})"
                            )
                            return order.id
                    continue

                # Legacy fallback when a caller has no client id.
                for order in open_orders:
                    # 价格匹配（容差 0.01 USD）
                    price_match = abs(float(order.price) - float(price)) < 0.01
                    # 数量匹配（容差 0.00001）
                    amount_match = abs(float(order.amount) -
                                       float(amount)) < 0.00001
                    # 方向匹配
                    side_str = "BUY" if side.lower() == "buy" else "SELL"
                    side_match = order.side.name == side_str

                    if price_match and amount_match and side_match:
                        logger.info(
                            f"✅ 查询到 order_index: {order.id} "
                            f"({side} {amount}@{price})"
                        )
                        return order.id

                logger.debug(
                    f"🔍 尝试 {attempt+1}/{max_retries}: "
                    f"未找到匹配订单 ({side} {amount}@{price}，"
                    f"当前挂单数: {len(open_orders)})"
                )

            except Exception as e:
                logger.warning(
                    f"⚠️ 查询订单失败 (尝试 {attempt+1}/{max_retries}): {e}"
                )

        # 快市時訂單可能在 open orders 查詢窗口內就成交，已不在掛單列表；
        # 改從歷史訂單依 client_order_id 反查真正的 order_index。
        if client_order_id is not None:
            try:
                history_orders = await self.get_order_history(symbol, limit=100)
                for order in history_orders:
                    if self._order_matches_client_id(order, client_order_id):
                        logger.info(
                            f"✅ 从历史订单查到 order_index: {order.id} "
                            f"(client_order_id={client_order_id})"
                        )
                        return order.id
            except Exception as exc:
                logger.warning(
                    f"⚠️ 查询历史订单失败 (client_order_id={client_order_id}): {exc}"
                )

        # 所有重试都失败
        logger.warning(
            f"❌ 无法获取 order_index ({side} {amount}@{price})，"
            f"已重试 {max_retries} 次。将依赖健康检查兜底。"
        )
        return None

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        batch_mode: bool = False,
        skip_order_index_query: bool = False,
        **kwargs
    ) -> Optional[OrderData]:
        """
        下单（主流程编排）

        Args:
            symbol: 交易对符号
            side: 订单方向 ("buy" 或 "sell")
            order_type: 订单类型 ("limit" 或 "market")
            quantity: 数量
            price: 价格（限价单必需）
            **kwargs: 其他参数

        Returns:
            OrderData对象

        ⚠️  Lighter价格精度说明：
        - Lighter使用动态价格乘数，不同交易对精度不同
        - 价格乘数公式: price_int = price_usd × (10 ** price_decimals)
        - 数量使用市场的size_decimals: base_amount = quantity × 10 ** size_decimals

        示例：
        - ETH (price_decimals=2): $4127.39 × 100 = 412739
        - BTC (price_decimals=1): $114357.8 × 10 = 1143578
        - SOL (price_decimals=3): $199.058 × 1000 = 199058
        - DOGE (price_decimals=6): $0.202095 × 1000000 = 202095

        注意：这与大多数交易所不同！
        - 大多数CEX使用固定的1e8或1e6
        - Lighter根据价格大小动态选择精度，以优化Layer 2性能
        """
        # 🔥 nonce冲突已通过grid_engine_impl.py中的串行下单解决
        # 串行下单确保了nonce自然递增，无需在此添加延迟

        logger.debug(
            f"📝 开始下单: symbol={symbol}, side={side}, type={order_type}, qty={quantity}")

        try:
            # 1. 验证前置条件
            if not self._validate_order_preconditions():
                return None

            # 2. 获取市场信息（索引、价格精度、乘数）
            market_info = await self._get_market_info(symbol)
            if not market_info:
                return None

            validation_price = price if order_type.lower() != "market" else None
            try:
                self._validate_order_minimums(
                    quantity,
                    validation_price,
                    market_info,
                    enforce_market_minimums=not kwargs.get("reduce_only", False),
                )
            except ValueError as exc:
                logger.error(f"订单低于Lighter市场最小限制: {exc}")
                return None

            # 3. 根据订单类型执行下单
            if order_type.lower() == "market":
                # Market/IOC orders normally disappear from active orders before
                # an order_index lookup can observe them. Position verification is
                # the authoritative close check.
                skip_order_index_query = True
                return await self._execute_market_order(
                    symbol, side, quantity, price, market_info, batch_mode=batch_mode,
                    skip_order_index_query=skip_order_index_query, **kwargs
                )
            else:
                return await self._execute_limit_order(
                    symbol, side, quantity, price, market_info, batch_mode=batch_mode,
                    skip_order_index_query=skip_order_index_query, **kwargs
                )

        except (OrderSubmissionNotSentError, OrderSubmissionRejectedError):
            raise
        except Exception as e:
            if self._is_definitive_mutation_exception(e):
                logger.error(f"下单失败 {symbol}: HTTP 429 rate limited")
                raise RuntimeError(
                    "order submission rate limited (HTTP 429)"
                ) from None
            logger.error(f"下单失败 {symbol}: {e}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        取消订单

        Args:
            symbol: 交易对符号
            order_id: 订单ID

        Returns:
            是否成功
        """
        if not self.signer_client:
            logger.error("未配置SignerClient，无法取消订单")
            return False

        try:
            mutation_order_index = int(str(order_id))
        except (TypeError, ValueError):
            logger.error(
                "Cancellation order_index is not numeric; mutation was not sent: "
                f"order_id={order_id}"
            )
            return False
        if not 1 <= mutation_order_index < (1 << 60):
            logger.error(
                "Cancellation order_index is outside Lighter's valid range; "
                f"mutation was not sent: order_id={order_id}"
            )
            return False

        logical_order_id = str(mutation_order_index)
        uncertain = getattr(self, "_uncertain_cancellations", set())
        uncertain_key = (symbol, logical_order_id)
        if uncertain_key in uncertain:
            reconciled = await self._reconcile_cancellation(symbol, logical_order_id)
            if reconciled is True:
                if self.confirm_terminal_cancellation_outcome(
                    symbol, logical_order_id, OrderStatus.CANCELED
                ):
                    return True
            logger.error(
                "Skip repeated signer cancellation while prior outcome is uncertain: "
                f"order_id={order_id}"
            )
            return False

        try:
            market_index = self.get_market_index(symbol)
            if market_index is None:
                logger.error(f"未找到交易对 {symbol} 的市场索引")
                return False

            # 取消订单
            try:
                tx, response, err = await self._call_api(
                    "order cancellation",
                    lambda: self.signer_client.cancel_order(
                        market_index=market_index,
                        order_index=mutation_order_index,
                    ),
                    retry_on_429=False,
                )
            except Exception as exc:
                if self._is_definitive_mutation_exception(exc):
                    logger.error(
                        f"取消订单失败 {symbol}/{order_id}: HTTP 429 rate limited"
                    )
                    raise RuntimeError(
                        "order cancellation rate limited (HTTP 429)"
                    ) from None
                return await self._handle_ambiguous_cancellation(
                    symbol,
                    logical_order_id,
                    str(exc),
                )

            if err:
                error_msg = self.parse_error(err)
                mutation_error = (
                    err if isinstance(err, Exception) else RuntimeError(error_msg)
                )
                if self._is_definitive_mutation_exception(mutation_error):
                    raise RuntimeError(
                        "order cancellation rate limited (HTTP 429)"
                    ) from None
                logger.error(f"取消订单失败: {error_msg}")
                return False

            if response is None or getattr(response, "code", None) is None:
                return await self._handle_ambiguous_cancellation(
                    symbol,
                    logical_order_id,
                    "cancellation response missing success code",
                )

            try:
                self._require_success_response(response, "order cancellation")
            except RuntimeError as exc:
                if self._is_definitive_mutation_exception(exc):
                    raise RuntimeError(
                        "order cancellation rate limited (HTTP 429)"
                    ) from None
                logger.error(f"取消订单失败: {exc}")
                return False
            if not tx or not getattr(response, 'tx_hash', None):
                return await self._handle_ambiguous_cancellation(
                    symbol,
                    logical_order_id,
                    "successful cancellation response missing tx or tx_hash",
                )

            # A successful signer response only acknowledges transaction
            # submission. The order may still fill before the cancellation is
            # sequenced, so retain the intent until an exact terminal read.
            return await self._handle_ambiguous_cancellation(
                symbol,
                logical_order_id,
                "cancellation transaction acknowledged without terminal proof",
            )

        except Exception as e:
            if self._is_definitive_mutation_exception(e):
                logger.error(
                    f"取消订单失败 {symbol}/{order_id}: HTTP 429 rate limited"
                )
                raise RuntimeError(
                    "order cancellation rate limited (HTTP 429)"
                ) from None
            logger.error(f"取消订单失败 {symbol}/{order_id}: {e}")
            return False

    async def place_market_order(
            self,
            symbol: str,
            side: OrderSide,
            quantity: Decimal,
            reduce_only: bool = False,
            skip_order_index_query: bool = False) -> Optional[OrderData]:
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
        logger.debug(
            f"🚀 place_market_order被调用: symbol={symbol}, side={side}, qty={quantity}, reduce_only={reduce_only}")

        # 转换OrderSide枚举为字符串
        side_str = "buy" if side == OrderSide.BUY else "sell"

        logger.debug(f"   转换side: {side} → {side_str}")

        return await self.place_order(
            symbol=symbol,
            side=side_str,  # 🔥 修复：传递字符串而不是枚举
            order_type="market",  # 🔥 修复：传递字符串
            quantity=quantity,
            reduce_only=reduce_only,  # 🔥 新增：只减仓模式
            skip_order_index_query=skip_order_index_query
        )

    # ============= 辅助方法 =============

    def _get_symbol_from_market_index(self, market_index: int) -> str:
        """从市场索引获取符号"""
        market_info = self._markets_cache.get(market_index)
        if market_info:
            return market_info.get("symbol", "")
        return ""

    def _parse_order(self, order_info: Any, symbol: str) -> OrderData:
        """
        解析订单信息

        根据Lighter API文档，Order对象包含:
        - order_index: INTEGER (真正的订单ID)
        - order_id: STRING (order_index的字符串形式)
        - client_order_index: INTEGER
        - client_order_id: STRING
        """
        from datetime import datetime

        # 🔥 获取真正的订单ID (优先使用order_index，然后是order_id)
        order_index = getattr(order_info, 'order_index', None)
        order_id_str = getattr(order_info, 'order_id', None)

        # Cancellation and websocket reconciliation use the numeric indexes.
        # Fall back to the string ids only for older/incomplete API payloads.
        final_order_id = (
            str(order_index)
            if order_index is not None
            else str(order_id_str or '')
        )
        client_order_index = getattr(order_info, 'client_order_index', None)
        client_order_id = getattr(order_info, 'client_order_id', None)
        client_id = (
            str(client_order_index)
            if client_order_index is not None
            else str(client_order_id or '')
        )

        logger.debug(
            f"解析订单: order_index={order_index}, order_id={order_id_str}, final_id={final_order_id}")

        # 解析数量信息
        initial_amount = self._safe_decimal(
            getattr(order_info, 'initial_base_amount', 0))
        filled_amount = self._safe_decimal(
            getattr(order_info, 'filled_base_amount', 0))
        remaining_amount = self._safe_decimal(
            getattr(order_info, 'remaining_base_amount', 0))

        # 解析价格
        price = self._safe_decimal(getattr(order_info, 'price', 0))
        filled_quote = self._safe_decimal(
            getattr(order_info, 'filled_quote_amount', 0))

        # 计算平均价格
        average_price = filled_quote / filled_amount if filled_amount > 0 else None
        exchange_status = str(
            getattr(order_info, 'status', 'unknown') or 'unknown'
        ).strip().lower()

        return OrderData(
            # ✅ 使用真正的order_id（order_index的字符串形式）
            id=final_order_id,
            client_id=client_id,
            symbol=symbol,
            side=self._parse_order_side(getattr(order_info, 'is_ask', False)),
            type=self._parse_order_type(getattr(order_info, 'type', 'limit')),
            amount=initial_amount,
            price=price if price > 0 else None,
            filled=filled_amount,
            remaining=remaining_amount,
            cost=filled_quote,
            average=average_price,
            status=self._parse_order_status(exchange_status),
            timestamp=self._parse_timestamp(
                getattr(order_info, 'timestamp', None)) or datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params={},
            raw_data={
                'order_info': order_info,
                'post_only_canceled': exchange_status == 'canceled-post-only',
            }
        )

    def _parse_order_side(self, is_ask: bool) -> OrderSide:
        """解析订单方向"""
        return OrderSide.SELL if is_ask else OrderSide.BUY

    def _parse_order_type(self, order_type_str: str) -> OrderType:
        """解析订单类型"""
        type_mapping = {
            'market': OrderType.MARKET,
            'liquidation': OrderType.MARKET,
            'limit': OrderType.LIMIT,
            'stop-loss-limit': OrderType.STOP_LIMIT,
            'take-profit-limit': OrderType.TAKE_PROFIT_LIMIT,
            'stop-loss': OrderType.STOP,
            'take-profit': OrderType.TAKE_PROFIT,
            'stop': OrderType.STOP,
        }
        return type_mapping.get(order_type_str.lower().replace('_', '-'), OrderType.LIMIT)

    def _parse_order_status(self, status_str: str) -> OrderStatus:
        """解析订单状态"""
        normalized = status_str.lower()
        if normalized.startswith('canceled') or normalized == 'cancelled':
            return OrderStatus.CANCELED

        status_mapping = {
            'pending': OrderStatus.PENDING,
            'in-progress': OrderStatus.OPEN,
            'open': OrderStatus.OPEN,
            'filled': OrderStatus.FILLED,
            'expired': OrderStatus.EXPIRED,
            'rejected': OrderStatus.REJECTED,
        }
        return status_mapping.get(normalized, OrderStatus.UNKNOWN)

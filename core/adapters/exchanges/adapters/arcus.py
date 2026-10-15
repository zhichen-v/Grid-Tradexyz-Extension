"""Arcus perpetuals adapter (REST for mutations and reads, one WebSocket for pushes).

Docs: https://docs.arcus.xyz — orders are signed with an Ed25519 API key over a
compact, key-sorted "typed payload" built from engine-native integers.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
import time
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Callable, Dict, List, Optional

import aiohttp
import websockets
from nacl.signing import SigningKey

from ..adapter import ExchangeAdapter
from ..exceptions import OrderSubmissionNotSentError, OrderSubmissionRejectedError
from ..interface import ExchangeConfig
from ..models import (
    BalanceData,
    ExchangeInfo,
    ExchangeType,
    MarginMode,
    OHLCVData,
    OrderBookData,
    OrderBookLevel,
    OrderData,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionData,
    PositionSide,
    TickerData,
    TradeData,
)

NETWORKS = {
    "mainnet": ("https://api.arcus.xyz", "wss://api.arcus.xyz/v1/ws"),
    "testnet": ("https://api.testnet.arcus.xyz", "wss://api.testnet.arcus.xyz/v1/ws"),
}
OP_PLACE, OP_CANCEL = 1, 2
TIF_CODES = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}
# goodTilTime must be at least one month ahead, even on IOC orders.
GOOD_TIL_US = 40 * 86_400 * 1_000_000
# MARKET orders still carry a protective limit price within 10% of mark.
MARKET_SLIPPAGE = Decimal("0.05")
STATUS_MAP = {
    "PENDING": OrderStatus.PENDING,
    "ACK": OrderStatus.OPEN,
    "OPEN": OrderStatus.OPEN,
    "PARTIALLY_FILLED": OrderStatus.OPEN,
    "UNTRIGGERED": OrderStatus.OPEN,
    "CANCEL_PENDING": OrderStatus.OPEN,
    "CANCEL_ACKNOWLEDGED": OrderStatus.OPEN,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "MARGIN_CANCELED": OrderStatus.CANCELED,
    "TPSL_CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
}


class ArcusAPIError(RuntimeError):
    """Non-2xx REST response with the parsed Arcus error body."""

    def __init__(self, status: int, body: Any):
        error = body.get("error") if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}
        self.status = status
        self.error_type = error.get("errorType") or error.get("type")
        message = error.get("message") or (body if isinstance(body, str) else "")
        super().__init__(f"Arcus HTTP {status}: {self.error_type or ''} {message}".strip())


def canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def to_units(value: Decimal, unit: Decimal, what: str) -> int:
    """Exact decimal -> engine integer (ticks / quantums); never round silently."""
    units = value / unit
    if units != units.to_integral_value():
        raise ValueError(f"{what} {value} is not a multiple of {unit}")
    return int(units)


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _from_us(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000)
    except (TypeError, ValueError):
        return datetime.now()


class ArcusAdapter(ExchangeAdapter):
    """Grid-facing Arcus adapter for one (address, accountIndex) subaccount."""

    def __init__(self, config: ExchangeConfig, event_bus: Optional[Any] = None):
        super().__init__(config, event_bus)
        extra = config.extra_params or {}
        network = str(extra.get("network") or ("testnet" if config.testnet else "mainnet")).lower()
        if network not in NETWORKS:
            raise ValueError(f"Unknown Arcus network: {network}")
        self.network = network
        self._rest_url, self._ws_url = NETWORKS[network]
        self._address = str(config.wallet_address or "").strip()
        self._account_index = int(extra.get("account_index", 0))
        secret = str(config.api_secret or "").strip().removeprefix("0x")
        self._signing_key = SigningKey(bytes.fromhex(secret)) if secret else None
        self._api_key = (
            self._signing_key.verify_key.encode().hex() if self._signing_key else ""
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._markets: Dict[str, Dict[str, Any]] = {}
        # Read by the grid health checker for min-order checks.
        self._market_info: Dict[str, Dict[str, Any]] = {}
        # Read by the grid engine for websocket position snapshots.
        self._position_cache: Dict[str, Dict[str, Decimal]] = {}
        self._unresolved: Dict[str, Dict[str, Any]] = {}
        # (symbol, order_id) cancels whose response was lost; cleared by the engine.
        self._uncertain_cancellations: set = set()

        self._ws = None
        self._ws_task: Optional[asyncio.Task] = None
        self._dispatch_task: Optional[asyncio.Task] = None
        self._ws_queue: asyncio.Queue = asyncio.Queue()
        self._ws_connected = False
        self._ws_request_id = 0
        self._ws_subscriptions: Dict[tuple, Dict[str, Any]] = {}
        self._ticker_callbacks: Dict[str, List[Callable]] = {}
        self._user_callbacks: List[Callable] = []
        self._position_callbacks: List[Callable] = []
        self._last_heartbeat = datetime.now()

    # === lifecycle ===

    async def _do_connect(self) -> bool:
        if not self._address or not self._signing_key:
            self.logger.error("Arcus requires wallet_address and api_signing_key")
            return False
        # The grid engine calls connect() again on start; reuse the live session.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.config.request_timeout)
            )
        await self._load_markets()
        if self.config.enable_websocket and (self._ws_task is None or self._ws_task.done()):
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())
            self._ws_task = asyncio.create_task(self._ws_loop())
        self.logger.info(
            f"Arcus connected: network={self.network}, markets={len(self._markets)}, "
            f"account_index={self._account_index}"
        )
        return True

    async def _do_disconnect(self) -> None:
        for task in (self._ws_task, self._dispatch_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        self._ws_task = self._dispatch_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._ws_connected = False
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _do_authenticate(self) -> bool:
        return bool(self._signing_key)

    async def _do_health_check(self) -> Dict[str, Any]:
        return {"websocket_connected": self._ws_connected, "markets": len(self._markets)}

    async def _do_heartbeat(self) -> None:
        if self.config.enable_websocket and not self._ws_connected:
            self.logger.warning("Arcus WebSocket is disconnected during heartbeat")

    def is_connected(self) -> bool:
        connected = super().is_connected()
        if self.config.enable_websocket:
            return connected and self._ws_connected
        return connected

    # === REST plumbing ===

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("Arcus adapter is not connected")
        async with self._session.request(
            method,
            self._rest_url + path,
            params=params,
            data=canonical_json(body) if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        ) as response:
            text = await response.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = text
            if response.status >= 400:
                raise ArcusAPIError(response.status, data)
            return data

    def _account_params(self, symbol: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        params = {"address": self._address, "accountIndex": self._account_index, **extra}
        if symbol:
            params["market"] = self._market(symbol)["marketDisplayName"]
        return params

    def _auth_headers(self, timestamp: int, signature: str) -> Dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "X-Timestamp": str(timestamp),
            "X-Signature": signature,
        }

    def _sign(self, message: str) -> str:
        return self._signing_key.sign(message.encode()).signature.hex()

    # === markets ===

    async def _load_markets(self) -> None:
        data = await self._request("GET", "/v1/markets")
        self._markets = {
            str(m["marketDisplayName"]).upper(): m for m in data.get("markets", [])
        }
        self._market_info = {
            name: {
                "symbol": name,
                "min_base_amount": m.get("minOrderSize"),
                "min_quote_amount": m.get("minOrderNotional"),
            }
            for name, m in self._markets.items()
        }

    def _market(self, symbol: str) -> Dict[str, Any]:
        key = str(symbol).upper().replace("/", "-").replace("_", "-")
        market = self._markets.get(key) or self._markets.get(f"{key}-USD")
        if market is None:
            raise ValueError(f"Unknown Arcus market: {symbol}")
        return market

    def _tick_for(self, market: Dict[str, Any], price: Decimal) -> Decimal:
        for tier in market.get("tickTiers") or []:
            upper = tier.get("upToPrice")
            if upper is None or price < Decimal(str(upper)):
                return Decimal(str(tier["tick"]))
        return Decimal(str(market["tickSize"]))

    def _snap_price(self, market: Dict[str, Any], price: Decimal, side: OrderSide) -> Decimal:
        tick = self._tick_for(market, price)
        rounding = ROUND_CEILING if side == OrderSide.BUY else ROUND_FLOOR
        return (price / tick).to_integral_value(rounding=rounding) * tick

    # === orders ===

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: Decimal,
        price: Optional[Decimal] = None,
        params: Optional[Dict[str, Any]] = None,
        batch_mode: bool = False,
    ) -> OrderData:
        params = params or {}
        market = self._market(symbol)
        amount = Decimal(str(amount))
        is_market = order_type == OrderType.MARKET
        if is_market:
            reference = Decimal(str(price)) if price else await self._mark_price(market)
            bound = MARKET_SLIPPAGE if side == OrderSide.BUY else -MARKET_SLIPPAGE
            price = self._snap_price(market, reference * (1 + bound), side)
            tif = "IOC"
        else:
            if price is None:
                raise ValueError("Arcus limit orders require a price")
            price = Decimal(str(price))
            tif = "ALO" if str(params.get("time_in_force", "")).upper() in {"ALO", "POST_ONLY"} else "GTT"
        tick = self._tick_for(market, price)
        to_units(price, tick, "price")  # band tick validation; the signed divisor is tickSize
        ticks = to_units(price, Decimal(str(market["tickSize"])), "price")
        quantums = to_units(amount, Decimal(str(market["stepSize"])), "amount")
        reduce_only = bool(params.get("reduce_only") or params.get("reduceOnly"))
        client_id = str(params.get("client_order_id") or f"g{time.time_ns()}{secrets.token_hex(2)}")

        timestamp = time.time_ns()
        good_til_us = timestamp // 1000 + GOOD_TIL_US
        signed = {
            "ad": self._address.lower(),
            "ai": self._account_index,
            "c": client_id,
            "ct": timestamp,
            "g": good_til_us * 1000,
            "m": int(market["marketId"]),
            "op": OP_PLACE,
            "p": ticks,
            "q": quantums,
            "r": int(reduce_only),
            "s": 0 if side == OrderSide.BUY else 1,
            "t": TIF_CODES[tif],
            "v": 1,
        }
        body = {
            "address": self._address,
            "accountIndex": self._account_index,
            "marketId": int(market["marketId"]),
            "orderSide": "BUY" if side == OrderSide.BUY else "SELL",
            "orderType": "MARKET" if is_market else "LIMIT",
            "quantity": str(amount),
            "price": str(price),
            "timeInForce": tif,
            "goodTilTime": str(good_til_us),
            "clientId": client_id,
            "reduceOnly": reduce_only,
            "timestamp": timestamp,
        }
        intent = {
            "client_order_id": client_id,
            "symbol": market["marketDisplayName"],
            "side": side.value,
            "amount": str(amount),
            "price": str(price),
        }
        try:
            result = await self._request(
                "POST",
                "/v1/placeOrder",
                params={"address": self._address},
                body=body,
                headers=self._auth_headers(timestamp, self._sign(canonical_json(signed))),
            )
        except ArcusAPIError as exc:
            if exc.error_type == "Transmission":
                raise OrderSubmissionNotSentError(str(exc)) from exc
            if exc.status < 500:
                raise OrderSubmissionRejectedError(str(exc)) from exc
            return self._uncertain_placeholder(intent, symbol, side, order_type, amount, price, exc)
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            return self._uncertain_placeholder(intent, symbol, side, order_type, amount, price, exc)

        order = self._parse_order(result, symbol)
        order.params = {"client_order_id": client_id, "time_in_force": tif}
        if order.status == OrderStatus.REJECTED:
            self.logger.warning(
                f"Arcus rejected order {client_id}: {result.get('rejectionReason')}"
            )
        return order

    def _uncertain_placeholder(self, intent, symbol, side, order_type, amount, price, exc) -> OrderData:
        """The request may have reached the engine: keep the client id for read-only proof."""
        self._unresolved[intent["client_order_id"]] = {**intent, "created_at": time.time()}
        self.logger.warning(
            f"Arcus order submission outcome unknown ({type(exc).__name__}: {exc}); "
            f"client_id={intent['client_order_id']}"
        )
        flags = {"submission_uncertain": True, "client_order_id": intent["client_order_id"]}
        return OrderData(
            id="",
            client_id=intent["client_order_id"],
            symbol=symbol,
            side=side,
            type=order_type,
            amount=amount,
            price=price,
            filled=Decimal("0"),
            remaining=amount,
            cost=Decimal("0"),
            average=None,
            status=OrderStatus.PENDING,
            timestamp=datetime.now(),
            updated=None,
            fee=None,
            trades=[],
            params=dict(flags),
            raw_data=dict(flags),
        )

    def get_unresolved_submissions(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._unresolved.values()]

    async def resolve_unresolved_submissions(self) -> List[OrderData]:
        """Find ambiguous submissions by exact client id in open orders, then history."""
        resolved: List[OrderData] = []
        for client_id, intent in list(self._unresolved.items()):
            symbol = intent["symbol"]
            found = None
            for path in ("/v1/openOrders", "/v1/orders"):
                data = await self._request("GET", path, params=self._account_params(symbol))
                found = next(
                    (row for row in data.get("orders", []) if row.get("clientId") == client_id),
                    None,
                )
                if found:
                    break
            if found:
                resolved.append(self._parse_order(found, symbol))
                del self._unresolved[client_id]
        return resolved

    async def cancel_order(self, order_id: str, symbol: str) -> Optional[OrderData]:
        market = self._market(symbol)
        timestamp = time.time_ns()
        signed = {
            "ad": self._address.lower(),
            "ai": self._account_index,
            "ct": timestamp,
            "id": str(order_id),
            "m": int(market["marketId"]),
            "op": OP_CANCEL,
            "v": 1,
        }
        body = {
            "address": self._address,
            "accountIndex": self._account_index,
            "marketId": int(market["marketId"]),
            "kind": "orderId",
            "orderId": str(order_id),
            "timestamp": timestamp,
        }
        try:
            result = await self._request(
                "POST",
                "/v1/cancelOrder",
                params={"address": self._address},
                body=body,
                headers=self._auth_headers(timestamp, self._sign(canonical_json(signed))),
            )
        except ArcusAPIError as exc:
            if exc.status < 500 or exc.error_type == "Transmission":
                self.logger.error(f"Arcus cancel rejected for {order_id}: {exc}")
                return None
            self._uncertain_cancellations.add((market["marketDisplayName"], str(order_id)))
            return None
        except (asyncio.TimeoutError, aiohttp.ClientError):
            # The grid engine keeps local state until history proves the outcome.
            self._uncertain_cancellations.add((market["marketDisplayName"], str(order_id)))
            return None
        order = self._parse_order({**result, "orderId": order_id}, symbol)
        if str(result.get("status", "")).upper() == "CANCEL_ACKNOWLEDGED":
            # Asynchronous: the orders channel delivers the terminal state.
            order.params = {"cancel_terminal": False}
        return order

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        open_orders = await self.get_open_orders(symbol)
        timestamp = time.time_ns()
        body: Dict[str, Any] = {"address": self._address, "accountIndex": self._account_index}
        if symbol:
            body["marketId"] = int(self._market(symbol)["marketId"])
        message = f"{timestamp}cancelAllOrders{canonical_json(body)}"
        await self._request(
            "POST",
            "/v1/cancelAllOrders",
            params={"address": self._address},
            body=body,
            headers=self._auth_headers(timestamp, self._sign(message)),
        )
        return open_orders

    async def get_order(self, order_id: str, symbol: str) -> OrderData:
        data = await self._request(
            "GET", f"/v1/order/{order_id}", params=self._account_params()
        )
        return self._parse_order(data, symbol)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderData]:
        data = await self._request("GET", "/v1/openOrders", params=self._account_params(symbol))
        return [self._parse_order(row, symbol) for row in data.get("orders", [])]

    async def get_order_history(
        self,
        symbol: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[OrderData]:
        extra: Dict[str, Any] = {}
        if limit:
            extra["limit"] = int(limit)
        if since:
            extra["from"] = int(since.timestamp() * 1_000_000)
        data = await self._request(
            "GET", "/v1/orders", params=self._account_params(symbol, **extra)
        )
        return [self._parse_order(row, symbol) for row in data.get("orders", [])]

    def _parse_order(self, row: Dict[str, Any], symbol: Optional[str] = None) -> OrderData:
        raw_status = str(row.get("status") or row.get("state") or "").upper()
        if raw_status == "REJECTED" and row.get("rejectionReason") == "EXPIRED":
            status = OrderStatus.EXPIRED
        else:
            status = STATUS_MAP.get(raw_status, OrderStatus.UNKNOWN)
        amount = _dec(row.get("originalSize") or row.get("quantity"))
        remaining = _dec(row.get("remainingSize"), str(amount))
        filled = _dec(row.get("filledSize"), str(amount - remaining))
        average = _dec(row.get("avgFillPrice")) if row.get("avgFillPrice") else None
        price = _dec(row.get("price")) if row.get("price") else None
        return OrderData(
            id=str(row.get("orderId") or ""),
            client_id=row.get("clientId"),
            symbol=row.get("marketDisplayName") or symbol or "",
            side=OrderSide.BUY if str(row.get("side")).upper() == "BUY" else OrderSide.SELL,
            type=OrderType.MARKET if str(row.get("type")).upper() == "MARKET" else OrderType.LIMIT,
            amount=amount,
            price=price,
            filled=filled,
            remaining=remaining,
            cost=filled * (average or price or Decimal("0")),
            average=average,
            status=status,
            timestamp=_from_us(row.get("createdAt") or row.get("updatedAt")),
            updated=_from_us(row["updatedAt"]) if row.get("updatedAt") else None,
            fee=None,
            trades=[],
            params={},
            raw_data=row,
        )

    # === account ===

    async def get_balances(self) -> List[BalanceData]:
        data = await self._request("GET", "/v1/account", params=self._account_params())
        equity = _dec(data.get("equity"))
        free = _dec(data.get("freeCollateral"))
        return [
            BalanceData(
                currency="USDG",
                free=free,
                used=equity - free,
                total=equity,
                usd_value=equity,
                timestamp=datetime.now(),
                raw_data=data,
            )
        ]

    async def get_positions(self, symbols: Optional[List[str]] = None) -> List[PositionData]:
        symbol = symbols[0] if symbols and len(symbols) == 1 else None
        data = await self._request("GET", "/v1/positions", params=self._account_params(symbol))
        positions = [self._parse_position(row) for row in (data.get("positions") or {}).values()]
        positions = [p for p in positions if p.size != 0]
        if symbols:
            wanted = {self._market(s)["marketDisplayName"] for s in symbols}
            positions = [p for p in positions if p.symbol in wanted]
        return positions

    def _parse_position(self, row: Dict[str, Any]) -> PositionData:
        signed_size = _dec(row.get("size"))
        mark = _dec(row.get("markPx")) if row.get("markPx") else None
        return PositionData(
            symbol=row.get("marketDisplayName", ""),
            side=PositionSide.SHORT if signed_size < 0 else PositionSide.LONG,
            size=abs(signed_size),
            entry_price=_dec(row.get("averageEntryPrice")),
            mark_price=mark,
            current_price=mark,
            unrealized_pnl=_dec(row.get("unrealizedPnl")),
            realized_pnl=Decimal("0"),
            percentage=None,
            leverage=int(_dec(row.get("leverage"), "1")),
            margin_mode=(
                MarginMode.ISOLATED
                if str(row.get("marginMode")).upper() == "ISOLATED"
                else MarginMode.CROSS
            ),
            margin=_dec(row.get("marginUsed")),
            liquidation_price=None,
            timestamp=datetime.now(),
            raw_data=row,
        )

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        timestamp = time.time_ns()
        body = {
            "address": self._address,
            "accountIndex": self._account_index,
            "marketId": int(self._market(symbol)["marketId"]),
            "leverage": int(leverage),
        }
        message = f"{timestamp}setLeverage{canonical_json(body)}"
        return await self._request(
            "POST",
            "/v1/setLeverage",
            params={"address": self._address},
            body=body,
            headers=self._auth_headers(timestamp, self._sign(message)),
        )

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> Dict[str, Any]:
        raise NotImplementedError("Arcus margin mode changes go through setLeverage")

    # === market data ===

    async def _mark_price(self, market: Dict[str, Any]) -> Decimal:
        ticker = await self.get_ticker(market["marketDisplayName"])
        price = ticker.mark_price or ticker.last
        if not price:
            raise RuntimeError(f"No Arcus mark price for {market['marketDisplayName']}")
        return price

    async def get_exchange_info(self) -> ExchangeInfo:
        await self._load_markets()
        return ExchangeInfo(
            name="Arcus",
            id="arcus",
            type=ExchangeType.PERPETUAL,
            supported_features=["perpetual_trading", "websocket", "user_data"],
            rate_limits={},
            precision={},
            fees={},
            markets=self._markets,
            status="active",
            timestamp=datetime.now(),
        )

    async def get_ticker(self, symbol: str) -> TickerData:
        name = self._market(symbol)["marketDisplayName"]
        markets, bbo = await asyncio.gather(
            self._request("GET", "/v1/markets", params={"market": name}),
            self._request("GET", f"/v1/bbo/{name}"),
        )
        market = (markets.get("markets") or [{}])[0]
        mark = _dec(market.get("markPrice")) or None
        last = _dec(market.get("lastTradePrice")) or mark
        bid, ask = _bbo_side(bbo.get("bestBid")), _bbo_side(bbo.get("bestAsk"))
        return TickerData(
            symbol=name,
            timestamp=datetime.now(),
            bid=bid,
            ask=ask,
            last=last,
            close=last,
            mark_price=mark,
            oracle_price=_dec(market.get("oraclePrice")) or None,
            raw_data={"market": market, "bbo": bbo},
        )

    async def get_tickers(self, symbols: Optional[List[str]] = None) -> List[TickerData]:
        return list(await asyncio.gather(*(self.get_ticker(s) for s in symbols or [])))

    async def get_orderbook(self, symbol: str, limit: Optional[int] = None) -> OrderBookData:
        name = self._market(symbol)["marketDisplayName"]
        params = {"nLevels": int(limit)} if limit else None
        data = await self._request("GET", f"/v1/l2OrderBook/{name}", params=params)
        levels = lambda rows: [  # noqa: E731  levels are [price, size] string pairs
            OrderBookLevel(price=_dec(r[0]), size=_dec(r[1])) for r in rows or []
        ]
        return OrderBookData(
            symbol=name,
            bids=levels(data.get("bids")),
            asks=levels(data.get("asks")),
            timestamp=datetime.now(),
            nonce=data.get("lastSequenceId"),
            raw_data=data,
        )

    async def get_ohlcv(self, symbol, timeframe, since=None, limit=None) -> List[OHLCVData]:
        raise NotImplementedError("Arcus OHLCV is not used by the grid runtime")

    async def get_trades(self, symbol, since=None, limit=None) -> List[TradeData]:
        raise NotImplementedError("Arcus public trades are not used by the grid runtime")

    # === websocket ===

    async def subscribe_ticker(self, symbol: str, callback: Callable) -> None:
        name = self._market(symbol)["marketDisplayName"]
        self._ticker_callbacks.setdefault(name, []).append(callback)
        await self._subscribe({"channel": "bbo", "id": name})

    async def subscribe_orderbook(self, symbol: str, callback: Callable, depth: Optional[int] = None) -> None:
        raise NotImplementedError("Arcus orderbook streaming is not used by the grid runtime")

    async def subscribe_trades(self, symbol: str, callback: Callable) -> None:
        raise NotImplementedError("Arcus trade streaming is not used by the grid runtime")

    async def subscribe_user_data(self, callback: Callable) -> None:
        if callback not in self._user_callbacks:
            self._user_callbacks.append(callback)
        await self._subscribe_account_channel("orders")
        await self._subscribe_account_channel("positions")

    async def subscribe_position_updates(self, symbol: str, callback: Callable) -> None:
        self._position_callbacks.append(callback)
        await self._subscribe_account_channel("positions")

    async def unsubscribe(self, symbol: Optional[str] = None) -> None:
        self._ws_subscriptions.clear()
        self._ticker_callbacks.clear()
        self._user_callbacks.clear()
        self._position_callbacks.clear()

    async def _subscribe_account_channel(self, channel: str) -> None:
        await self._subscribe(
            {"channel": channel, "id": self._address.lower(), "accountIndex": self._account_index}
        )

    async def _subscribe(self, frame: Dict[str, Any]) -> None:
        key = (frame["channel"], frame["id"])
        if key in self._ws_subscriptions:
            return
        self._ws_subscriptions[key] = frame
        if self._ws_connected and self._ws is not None:
            await self._ws.send(json.dumps({"type": "subscribe", **frame}))

    async def _ws_loop(self) -> None:
        delay = 1.0
        while True:
            try:
                async with websockets.connect(self._ws_url, ping_interval=20, max_size=None) as ws:
                    self._ws = ws
                    self._ws_connected = True
                    delay = 1.0
                    for frame in list(self._ws_subscriptions.values()):
                        await ws.send(json.dumps({"type": "subscribe", **frame}))
                    async for message in ws:
                        self._last_heartbeat = datetime.now()
                        self._ws_queue.put_nowait(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(f"Arcus WebSocket dropped: {exc}; reconnecting in {delay:.0f}s")
            finally:
                self._ws_connected = False
                self._ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _dispatch_loop(self) -> None:
        # Callbacks run off the reader so slow handlers cannot stall the socket.
        while True:
            message = await self._ws_queue.get()
            try:
                await self._handle_ws_message(json.loads(message))
            except Exception as exc:
                self.logger.error(f"Arcus WebSocket message handling failed: {exc}")

    async def _handle_ws_message(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "error":
            self.logger.warning(f"Arcus WebSocket error frame: {frame}")
            return
        if kind not in {"subscribed", "channel_data"}:
            return
        channel = frame.get("channel")
        contents = frame.get("contents")
        if channel == "bbo" and isinstance(contents, dict):
            await self._emit_ticker(frame.get("id") or frame.get("market"), contents)
        elif channel == "orders" and kind == "channel_data" and isinstance(contents, dict):
            # Snapshots are skipped: the health checker reconciles via REST after gaps.
            order = self._parse_order(contents, frame.get("market"))
            for callback in list(self._user_callbacks):
                await _call(callback, order)
        elif channel == "positions" and isinstance(contents, dict):
            for row in (contents.get("positions") or {}).values():
                position = self._parse_position(row)
                signed = -position.size if position.side == PositionSide.SHORT else position.size
                self._position_cache[position.symbol] = {
                    "size": signed,
                    "entry_price": position.entry_price,
                    "unrealized_pnl": position.unrealized_pnl,
                }
                for callback in list(self._position_callbacks):
                    await _call(
                        callback,
                        {
                            "symbol": position.symbol,
                            "size": signed,
                            "entry_price": position.entry_price,
                            "unrealized_pnl": position.unrealized_pnl,
                        },
                    )

    async def _emit_ticker(self, name: Optional[str], contents: Dict[str, Any]) -> None:
        if not name:
            return
        bid = _bbo_side(contents.get("bestBid") or contents.get("bid"))
        ask = _bbo_side(contents.get("bestAsk") or contents.get("ask"))
        if bid is None or ask is None:
            return
        ticker = TickerData(
            symbol=name,
            timestamp=datetime.now(),
            bid=bid,
            ask=ask,
            last=(bid + ask) / 2,
            raw_data=contents,
        )
        for callback in list(self._ticker_callbacks.get(name, [])):
            await _call(callback, ticker)


def _bbo_side(level: Any) -> Optional[Decimal]:
    if isinstance(level, dict):
        value = level.get("price") or level.get("px")
    elif isinstance(level, (list, tuple)) and level:
        value = level[0]
    else:
        value = level
    return Decimal(str(value)) if value not in (None, "") else None


async def _call(callback: Callable, payload: Any) -> None:
    result = callback(payload)
    if inspect.isawaitable(result):
        await result

"""Opt-in RH read snapshots and a nonce-checked book; no transactions or retries."""

import asyncio
import json
import time
from decimal import Decimal

import websockets


class LighterReadStream:
    """One owned connection; book faults never disable healthy account proofs."""

    def __init__(self, url, account_index, market_id, auth_factory, *,
                 connect_factory=websockets.connect, clock=time.monotonic,
                 wall_clock=time.time, sleep=asyncio.sleep, timeout=5):
        self._url, self._account, self._market = url, account_index, market_id
        self._auth, self._connect, self._clock = auth_factory, connect_factory, clock
        self._wall_clock = wall_clock
        self._wall_quantum = max(0, min(time.get_clock_info("time").resolution, 0.02))
        self._sleep = sleep
        self._timeout = timeout
        self._socket = self._reader = self._pending = self._book_ready = None
        self._lock = asyncio.Lock()
        self._invalid = False
        self._book_invalid = False
        self._last_book_failure = None
        self._orders_subscribed = False
        self._book = None
        self._levels = {"bids": {}, "asks": {}}

    def _check(self):
        if self._invalid or self._socket is None:
            raise RuntimeError("read stream unavailable")

    @property
    def market_id(self):
        return self._market

    @property
    def transport_healthy(self):
        return self._socket is not None and not self._invalid

    @property
    def last_book_failure(self):
        return self._last_book_failure

    def _fail_book(self, stage, error):
        reasons = {"invalid book sequence": "invalid_sequence",
                   "stale or future source book timestamp": "source_time_out_of_bounds",
                   "wrong book channel": "wrong_channel", "unexpected book snapshot": "unexpected_snapshot",
                   "conflicting book offset": "conflicting_offset", "discontinuous book": "discontinuous_book",
                   "invalid book depth": "invalid_depth", "invalid book level": "invalid_level",
                   "book depth exceeds limit": "depth_limit"}
        candidate = error.args[0] if type(error) is ValueError and len(error.args) == 1 else None
        reason = reasons.get(candidate, "invalid_payload") if type(candidate) is str else "invalid_payload"
        self._last_book_failure = stage, reason
        self._book_invalid = True

    async def start(self):
        if self._invalid or self._socket is not None:
            raise RuntimeError("read stream cannot be restarted")
        try:
            self._socket = await asyncio.wait_for(self._connect(
                self._url, ping_interval=30, ping_timeout=10, close_timeout=1,
                max_size=8 * 1024 * 1024,
            ), self._timeout)
            self._book_ready = asyncio.get_running_loop().create_future()
            self._reader = asyncio.create_task(self._receive())
            await asyncio.wait_for(self._send_wait(
                {"type": "subscribe", "channel": f"order_book/{self._market}"},
                self._book_ready,
            ), self._timeout)
        except BaseException as exc:
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError("read stream startup unavailable") from None
        return self

    async def _send_wait(self, message, future):
        await self._socket.send(json.dumps(message))
        return await future

    async def _request_reply(self, action, channel):
        future = asyncio.get_running_loop().create_future()
        reply = "unsubscribed" if action == "unsubscribe" else f"subscribed/{channel}"
        self._pending = reply, channel, future
        message = {"type": action, "channel": f"{channel}/{self._account}"}
        if action == "subscribe" and channel == "account_all_orders":
            token = self._auth()
            if not isinstance(token, str) or not token:
                raise ValueError("authentication unavailable")
            message["auth"] = token
        return await self._send_wait(message, future)

    async def request_snapshot(self, channel):
        """Return only the requested subscription acknowledgement, never an update."""
        if channel not in {"account_all", "account_all_orders"}:
            raise ValueError("unsupported read snapshot channel")
        async with self._lock:
            self._check()
            try:
                async with asyncio.timeout(self._timeout):
                    if channel == "account_all_orders" and self._orders_subscribed:
                        await self._request_reply("unsubscribe", channel)
                    return await self._request_reply("subscribe", channel)
            except BaseException as exc:
                await self.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeError("read snapshot unavailable") from None
            finally:
                self._pending = None

    async def _receive(self):
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                if type(message) is not dict:
                    raise ValueError("invalid message")
                kind = message.get("type")
                if kind == "ping":
                    await self._socket.send(json.dumps({"type": "pong"}))
                elif kind == "connected":
                    continue
                elif kind in {"subscribed/order_book", "update/order_book"}:
                    if (message.get("channel") != f"order_book:{self._market}"
                            or (kind == "subscribed/order_book") == self._book_ready.done()):
                        raise ValueError("unexpected book protocol message")
                    if not self._book_invalid:
                        try:
                            received = self._clock()
                            age_ms = self._source_age_ms(self._integer(message["timestamp"]))
                            if (age_ms.is_finite()
                                    and -Decimal(str(self._wall_quantum)) * 1000 <= age_ms < 0):
                                # Wait once for a coarse host wall-clock tick, never relax the bounds.
                                await self._sleep(self._wall_quantum)
                            if not self._book_invalid:
                                self._update_book(message)
                                self._book["received_monotonic"] = received
                        except Exception as error:
                            # A bad book must not disable fresh account cleanup proofs.
                            self._fail_book("receive_book", error)
                    if not self._book_ready.done():
                        self._book_ready.set_result(None)
                elif kind in {"subscribed/account_all", "subscribed/account_all_orders"}:
                    self._accept_snapshot(message)
                elif kind == "unsubscribed":
                    self._check_account(message, "account_all_orders")
                    if (not self._orders_subscribed or self._pending is None
                            or self._pending[:2] != ("unsubscribed", "account_all_orders")
                            or self._pending[2].done()):
                        raise ValueError("unexpected unsubscribe acknowledgement")
                    self._orders_subscribed = False
                    self._pending[2].set_result(None)
                elif kind in {"update/account_all", "update/account_all_orders"}:
                    channel = kind.split("/", 1)[1]
                    self._check_account(message, channel)
                    # Updates are intentionally not merged into fresh snapshots.
                else:
                    raise ValueError("unexpected read stream message")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            await self.close()

    def _check_account(self, message, channel):
        if message.get("channel") != f"{channel}:{self._account}":
            raise ValueError("wrong account channel")
        account = message.get("account")
        if ((channel == "account_all" or account is not None)
                and (type(account) is not int or account != self._account)):
            raise ValueError("wrong account identity")

    def _accept_snapshot(self, message):
        channel = message["type"].split("/", 1)[1]
        self._check_account(message, channel)
        if (self._pending is None or self._pending[:2] != (message["type"], channel)
                or self._pending[2].done()):
            raise ValueError("unexpected snapshot acknowledgement")
        if channel == "account_all_orders":
            orders = message.get("orders")
            if (type(orders) is not dict or any(
                    not str(market).isdigit() or type(rows) is not list
                    or any(type(row) is not dict for row in rows)
                    for market, rows in orders.items())):
                raise ValueError("incomplete order snapshot")
            fields = {"orders", "account", "type", "channel"}
        else:
            fields = {"account", "assets", "positions", "shares", "funding_histories",
                      "trades", "type", "channel", "total_trades_count", "total_volume",
                      "daily_trades_count", "daily_volume", "weekly_trades_count",
                      "weekly_volume", "monthly_trades_count", "monthly_volume"}
        result = {key: value for key, value in message.items() if key in fields}
        result["_received_monotonic"] = self._clock()
        if channel == "account_all_orders":
            self._orders_subscribed = True
        self._pending[2].set_result(result)

    @staticmethod
    def _integer(value):
        if type(value) is not int or value < 0:
            raise ValueError("invalid book sequence")
        return value

    def _source_age_ms(self, timestamp):
        return Decimal(str(self._wall_clock())) * 1000 - timestamp

    def _check_source_time(self, timestamp):
        # No inferred clock correction: a lagging/jumping host clock fails closed.
        age_ms = self._source_age_ms(timestamp)
        if not age_ms.is_finite() or not 0 <= age_ms <= 3000:
            raise ValueError("stale or future source book timestamp")

    def _update_book(self, message):
        if message.get("channel") != f"order_book:{self._market}":
            raise ValueError("wrong book channel")
        book = message["order_book"]
        initial = message["type"] == "subscribed/order_book"
        if (initial != (self._book is None) or type(book.get("code")) is not int
                or book["code"] not in {0, 200}):
            raise ValueError("unexpected book snapshot")
        nonce, offset = self._integer(book["nonce"]), self._integer(book["offset"])
        timestamp = self._integer(message["timestamp"])
        self._check_source_time(timestamp)
        if "offset" in message and self._integer(message["offset"]) != offset:
            raise ValueError("conflicting book offset")
        if not initial and (self._integer(book["begin_nonce"]) != self._book["nonce"]
                            or nonce < self._book["nonce"] or offset <= self._book["offset"]
                            or timestamp < self._book["timestamp"]):
            raise ValueError("discontinuous book")
        for side in ("bids", "asks"):
            rows, seen = book[side], set()
            if type(rows) is not list or len(rows) > 20000:
                raise ValueError("invalid book depth")
            for row in rows:
                if type(row) is not dict or type(row.get("price")) is not str or type(row.get("size")) is not str:
                    raise ValueError("invalid book level")
                price, size = Decimal(row["price"]), Decimal(row["size"])
                if (not price.is_finite() or not size.is_finite() or price <= 0 or size < 0
                        or price in seen or (initial and size == 0)):
                    raise ValueError("invalid book level")
                seen.add(price)
                if size:
                    self._levels[side][price] = size
                else:
                    self._levels[side].pop(price, None)
        if sum(map(len, self._levels.values())) > 20000:
            raise ValueError("book depth exceeds limit")
        self._book = {"nonce": nonce, "offset": offset, "timestamp": timestamp,
                      "received_monotonic": self._clock(),
                      "last_updated_at": book.get("last_updated_at")}

    def book_snapshot(self):
        self._check()
        if self._book_invalid or self._book is None:
            raise RuntimeError("read book unavailable")
        try:
            self._check_source_time(self._book["timestamp"])
        except Exception as error:
            self._fail_book("book_snapshot", error)
            raise RuntimeError("read book source timestamp unavailable") from None
        return {**self._book, "bids": tuple(sorted(self._levels["bids"].items(), reverse=True)),
                "asks": tuple(sorted(self._levels["asks"].items()))}

    async def close(self):
        """Permanently invalidate before any awaited cleanup."""
        self._invalid = True
        for future in (self._book_ready, self._pending[2] if self._pending else None):
            if future is not None and not future.done():
                future.set_exception(RuntimeError("read stream unavailable"))
                future.exception()  # Also consume it if failure preceded its await.
        reader, socket = self._reader, self._socket
        self._socket = None
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if socket is not None:
            try:
                await asyncio.wait_for(socket.close(), 1)
            except Exception:
                pass

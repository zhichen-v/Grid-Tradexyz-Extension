"""Synthetic wire venue for the original MM CLI and shared Lighter stack.

Only the native signer, HTTP/WS transports and (for fast tests) the existing
session clock seam are replaced. No exchange DTO, manager or session result is
fabricated. This exercises Python SDK serialization, not the native ABI, TLS or
physical venue transport. Only stdlib asyncio's own Windows wakeup socketpair
may use its local loopback connection. All identities are deliberately synthetic.
"""

import asyncio
import ctypes
import json
import socket
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


REAL_SLEEP = asyncio.sleep
D = Decimal


class WireClock:
    def __init__(self, mode):
        self.mode = mode
        self.value = 100.0
        self.epoch = 1789257900.0
        self.started = time.perf_counter()
        self.venue = None

    def monotonic(self):
        return self.value if self.mode == "virtual" else time.perf_counter()

    def wall(self):
        return (self.epoch + self.value - 100 if self.mode == "virtual" else time.time())

    async def sleep(self, seconds):
        if self.mode == "real":
            await REAL_SLEEP(seconds)
        else:
            self.value += seconds
            if self.venue is not None:
                self.venue.broadcast_book()
            for _ in range(4):
                await REAL_SLEEP(0)


class _RawResponse:
    def __init__(self, payload, status=200):
        self.status, self.reason = status, "OK" if status == 200 else "Synthetic rejection"
        self.headers = {"Content-Type": "application/json"}
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    async def read(self):
        return self.payload


class _NativeSigner:
    """Own ctypes buffers so the *original* SDK decoder and Free calls run."""
    def __init__(self, venue):
        self.venue = venue
        self.buffers = {}

    def pointer(self, value):
        buffer = ctypes.create_string_buffer(value.encode())
        pointer = ctypes.addressof(buffer)
        self.buffers[pointer] = buffer
        return pointer

    def Free(self, pointer):
        self.buffers.pop(pointer, None)

    def CreateClient(self, url, key, chain, api_key, account):
        if (account != 7 or api_key != 0 or key != b"1" * 64 or chain != 300
                or url != b"https://api.rh-testnet.lighter.xyz"):
            raise AssertionError("synthetic signer identity required")
        return None

    def CheckClient(self, api_key, account):
        if account != 7 or api_key != 0:
            raise AssertionError("synthetic signer identity required")
        return None

    def CreateAuthToken(self, deadline, api_key, account):
        from lighter.signer_client import StrOrErr
        if account != 7 or api_key not in (0, 255):
            raise AssertionError("synthetic auth identity required")
        return StrOrErr(self.pointer("synthetic-wire-auth"), None)

    def signed(self, kind, payload):
        from lighter.signer_client import SignedTxResponse
        return SignedTxResponse(kind, self.pointer(json.dumps(payload)),
                                self.pointer("synthetic-wire-hash"), None, None)

    def SignCreateOrder(self, market, client, amount, price, ask, order_type,
                        tif, reduce_only, trigger, expiry, integrator,
                        taker_fee, maker_fee, behavior, equality, skip, nonce,
                        api_key, account):
        from lighter.signer_client import SignerClient
        supported_modes = ((skip, SignerClient.SKIP_NONCE_OFF),
                           (behavior, SignerClient.SELF_TRADE_BEHAVIOR_EXPIRE_MAKER),
                           (equality, SignerClient.SELF_TRADE_EQUALITY_ACCOUNT_INDEX))
        if any(type(value) is not int or value != supported for value, supported in supported_modes):
            raise AssertionError("unsupported synthetic native signing mode")
        return self.signed(14, {"AccountIndex": account, "OrderBookIndex": market,
            "ClientOrderIndex": client, "BaseAmount": amount, "Price": price,
            "IsAsk": ask, "OrderType": order_type, "TimeInForce": tif,
            "ReduceOnly": reduce_only, "TriggerPrice": trigger,
            "ExpiredAt": expiry, "Nonce": nonce, "ApiKeyIndex": api_key,
            "IntegratorAccountIndex": integrator, "IntegratorMakerFee": maker_fee,
            "IntegratorTakerFee": taker_fee, "Sig": "synthetic"})

    def SignCancelOrder(self, market, order, skip, nonce, api_key, account):
        from lighter.signer_client import SignerClient
        if type(skip) is not int or skip != SignerClient.SKIP_NONCE_OFF:
            raise AssertionError("unsupported synthetic native nonce mode")
        return self.signed(15, {"AccountIndex": account, "OrderBookIndex": market,
            "OrderNonce": order, "Nonce": nonce, "ApiKeyIndex": api_key,
            "Sig": "synthetic"})


class WireVenue:
    """Independent exchange state, reached exclusively via serialized I/O."""
    def __init__(self, clock, scenario="normal", state_path=None):
        self.clock, self.scenario = clock, scenario
        self.state_path = Path(state_path) if state_path else None
        self.position, self.cash, self.entry = D(0), D(1000), D(0)
        self.orders, self.trades = {}, []
        self.requests, self.events, self.sockets = [], [], []
        self.nonce, self.market_nonce = 0, 10
        self.cancel_count = 0
        self.delayed = {}
        self.published_terminal = set()
        self.fault_used = False
        self.paired_order_snapshots = 0
        self.native = _NativeSigner(self)

    def event(self, kind, **values):
        self.events.append({"kind": kind, "monotonic": self.clock.monotonic(), **values})
        self.persist()

    def snapshot(self):
        return {"scope": "synthetic", "scenario": self.scenario,
            "position": str(self.position), "cash": str(self.cash),
            "open_order_ids": [str(k) for k, v in self.orders.items() if v["status"] == "open"],
            "orders": [{"id": str(k), "side": "sell" if v["is_ask"] else "buy",
                "size": v["initial_base_amount"], "price": v["price"],
                "status": v["status"], "client_order_index": v["client_order_index"],
                "created_monotonic": v["_created"], "terminal_monotonic": v.get("_terminal")}
                for k, v in self.orders.items()],
            "requests": list(self.requests), "events": list(self.events),
            "trade_count": len(self.trades)}

    def persist(self):
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(self.snapshot()), encoding="utf-8")
            temporary.replace(self.state_path)

    def record(self, method, path, operation, **values):
        # Distinct receipt times model finite wire latency without moving the
        # process-wide monotonic clock or bypassing production freshness rules.
        if self.clock.mode == "virtual":
            self.clock.value += .001
        self.requests.append({"method": method, "path": path, "operation": operation,
                              "monotonic": self.clock.monotonic(), **values})

    @staticmethod
    def public_order(order):
        return {k: v for k, v in order.items() if not k.startswith("_")}

    def active(self):
        return [self.public_order(v) for v in self.orders.values() if v["status"] == "open"]

    def account(self):
        count = len(self.active())
        unrealized = self.position * (D("77000") - self.entry) if self.position else D(0)
        collateral = self.cash.quantize(D(".000001"), rounding=ROUND_DOWN)
        position = {"market_id": 1, "symbol": "BTC", "initial_margin_fraction": "100",
            "open_order_count": count, "pending_order_count": 0, "position_tied_order_count": 0,
            "sign": -1 if self.position < 0 else 1, "position": str(abs(self.position)),
            "avg_entry_price": str(self.entry), "position_value": str(abs(self.position) * D(77000)),
            "unrealized_pnl": str(unrealized), "realized_pnl": "0", "liquidation_price": "0",
            "total_funding_paid_out": "0", "margin_mode": 0, "allocated_margin": "0",
            "total_discount": "0"}
        asset = {"asset_id": 3, "symbol": "USDG", "balance": "0", "locked_balance": "0",
                 "margin_balance": str(self.cash), "margin_mode": "enabled"}
        return {"code": 200, "account_type": 0, "account_trading_mode": 1,
            "index": 7, "account_index": 7, "l1_address": "0x" + "1" * 40,
            "cancel_all_time": 0, "total_order_count": count, "total_isolated_order_count": 0,
            "pending_order_count": 0, "available_balance": str(collateral), "status": 1,
            "collateral": str(collateral), "name": "synthetic", "description": "synthetic",
            "can_invite": False, "referral_points_percentage": "0", "positions": [position],
            "assets": [asset], "total_asset_value": str(collateral + unrealized),
            "cross_asset_value": str(collateral + unrealized), "pool_info": None,
            "shares": [], "created_at": 0, "transaction_time": 0, "pending_unlocks": [],
            "approved_integrators": [], "can_rfq": False,
            "cross_initial_margin_requirement": "0", "cross_maintenance_margin_requirement": "0",
            "can_rfq_market_ids": []}

    def fill(self, identifier, *, maker, price=None):
        order = self.orders[identifier]
        if order["status"] != "open":
            raise AssertionError("cannot fill terminal synthetic order")
        size, execution = D(order["remaining_base_amount"]), D(price or order["price"])
        signed = -size if order["is_ask"] else size
        closed = min(abs(self.position), size) if self.position * signed < 0 else D(0)
        gross = closed * (execution - self.entry) * (1 if self.position > 0 else -1)
        final = self.position + signed
        if not self.position or self.position * signed > 0:
            self.entry = (abs(self.position) * self.entry + size * execution) / abs(final)
        elif not final:
            self.entry = D(0)
        elif final * self.position < 0:
            self.entry = execution
        self.position = final
        fee = size * execution * D(".00012" if maker else ".00035")
        self.cash += gross - fee
        order.update(status="filled", filled_base_amount=str(size), remaining_base_amount="0",
                     filled_quote_amount=str(size * execution), _terminal=self.clock.monotonic())
        own_ask = order["is_ask"]
        number = len(self.trades) + 1
        trade = {"trade_id": number, "trade_id_str": str(number), "tx_hash": "synthetic",
            "type": "trade", "market_id": 1, "size": str(size), "price": str(execution),
            "usd_amount": str(size * execution), "ask_id": identifier if own_ask else 999999,
            "bid_id": 999999 if own_ask else identifier, "ask_account_id": 7 if own_ask else 8,
            "bid_account_id": 8 if own_ask else 7, "is_maker_ask": own_ask if maker else not own_ask,
            "block_height": number, "timestamp": int(self.clock.wall() * 1000),
            "maker_fee": 120, "taker_fee": 350, "bid_account_pnl": str(gross),
            "ask_account_pnl": str(gross), "ask_client_id": order["client_order_index"] if own_ask else 0,
            "bid_client_id": 0 if own_ask else order["client_order_index"],
            "integrator_maker_fee": 0, "integrator_taker_fee": 0, "transaction_time": 0}
        self.trades.append(trade)
        self.market_nonce += 1
        self.event("fill", order_id=str(identifier), side="sell" if own_ask else "buy",
                   size=str(size), price=str(execution), fee=str(fee), gross=str(gross),
                   liquidity="maker" if maker else "taker")

    def accept_tx(self, tx_type, tx):
        if (type(tx_type) is not int
                or any(type(tx.get(key)) is not int for key in ("AccountIndex", "OrderBookIndex", "Nonce", "ApiKeyIndex"))
                or tx["AccountIndex"] != 7 or tx["OrderBookIndex"] != 1
                or tx["ApiKeyIndex"] != 0 or tx["Nonce"] != self.nonce):
            raise AssertionError("synthetic transaction identity/nonce mismatch")
        if tx_type == 14:
            if (any(type(tx.get(key)) is not int for key in ("ClientOrderIndex", "BaseAmount", "Price", "IsAsk",
                        "OrderType", "TimeInForce", "TriggerPrice", "IntegratorAccountIndex",
                        "IntegratorMakerFee", "IntegratorTakerFee"))
                    or tx["ClientOrderIndex"] < 0 or tx["IsAsk"] not in (0, 1)
                    or type(tx.get("ReduceOnly")) is not bool or tx["OrderType"] != 0
                    or tx["TimeInForce"] not in (0, 2)
                    or any(tx[key] != 0 for key in ("TriggerPrice", "IntegratorAccountIndex",
                                                    "IntegratorMakerFee", "IntegratorTakerFee"))):
                raise AssertionError("unsupported synthetic signed order fields")
            if any(order["client_order_index"] == tx["ClientOrderIndex"] for order in self.orders.values()):
                raise AssertionError("duplicate synthetic create client identity")
            identifier = 10001 + len(self.orders)
            amount, price = D(tx["BaseAmount"]) / 100000, D(tx["Price"]) / 10
            if amount <= 0 or price <= 0:
                raise AssertionError("invalid signed synthetic amount or price")
            ask = bool(tx["IsAsk"])
            if tx["ReduceOnly"] and (self.position * (-1 if ask else 1) >= 0 or amount > abs(self.position)):
                raise AssertionError("synthetic reduce-only order must strictly reduce")
            if tx["TimeInForce"] == 0 and not tx["ReduceOnly"]:
                raise AssertionError("synthetic IOC must be reduce-only")
            if tx["TimeInForce"] == 2:
                if amount * price < D("10"):
                    raise AssertionError("synthetic post-only order below advertised minimum notional")
                opposite_prices = [D(order["price"]) for order in self.orders.values()
                                   if order["status"] == "open" and order["is_ask"] != ask]
                best = (max([D("76999.9"), *opposite_prices]) if ask
                        else min([D("77000.1"), *opposite_prices]))
                if price <= best if ask else price >= best:
                    raise AssertionError("synthetic post-only order crosses visible liquidity")
            self.nonce += 1
            tif = {0: "immediate-or-cancel", 1: "good-till-time", 2: "post-only"}[tx["TimeInForce"]]
            order = {"order_index": identifier, "order_id": str(identifier),
                "client_order_index": tx["ClientOrderIndex"], "client_order_id": str(tx["ClientOrderIndex"]),
                "market_index": 1, "owner_account_index": 7, "initial_base_amount": str(amount),
                "price": str(price), "nonce": self.market_nonce, "remaining_base_amount": str(amount),
                "is_ask": bool(tx["IsAsk"]), "base_size": tx["BaseAmount"], "base_price": tx["Price"],
                "filled_base_amount": "0", "filled_quote_amount": "0", "side": "sell" if tx["IsAsk"] else "buy",
                "type": "limit", "time_in_force": tif, "reduce_only": tx["ReduceOnly"],
                "trigger_price": "0", "order_expiry": tx["ExpiredAt"], "status": "open",
                "trigger_status": "na", "trigger_time": 0, "parent_order_index": 0,
                "parent_order_id": "0", "to_trigger_order_id_0": "0", "to_trigger_order_id_1": "0",
                "to_cancel_order_id_0": "0", "block_height": 1, "timestamp": int(self.clock.wall() * 1000),
                "created_at": int(self.clock.wall() * 1000), "updated_at": int(self.clock.wall() * 1000),
                "transaction_time": 0, "integrator_fee_collector_index": "0",
                "integrator_maker_fee": "0", "integrator_taker_fee": "0", "_created": self.clock.monotonic()}
            self.orders[identifier] = order
            self.market_nonce += 1
            self.event("create_accepted", order_id=str(identifier), time_in_force=tif,
                       reduce_only=tx["ReduceOnly"], size=str(amount), price=str(price))
            if tif == "immediate-or-cancel":
                execution = D("76999.9" if ask else "77000.1")
                if price <= execution if ask else price >= execution:
                    self.fill(identifier, maker=False, price=str(execution))
                else:
                    order.update(status="canceled", _terminal=self.clock.monotonic())
                    self.event("ioc_unfilled", order_id=str(identifier))
            return identifier
        if tx_type != 15:
            raise AssertionError("unimplemented synthetic transaction")
        if type(tx.get("OrderNonce")) is not int or tx["OrderNonce"] not in self.orders:
            raise AssertionError("unknown synthetic cancellation identity")
        identifier = tx["OrderNonce"]
        order = self.orders[identifier]
        if order["status"] != "open":
            raise AssertionError("duplicate synthetic cancellation send")
        self.nonce += 1
        self.cancel_count += 1
        order.update(status="canceled", _terminal=self.clock.monotonic())
        self.market_nonce += 1
        self.event("cancel_accepted", order_id=str(identifier),
                   order_age_seconds=self.clock.monotonic() - order["_created"])
        if self.scenario == "late_cancel_fill" and not self.fault_used:
            self.fault_used = True
            self.delayed[identifier] = 5
            opposite = next((key for key, other in self.orders.items()
                             if other["status"] == "open" and other["is_ask"] != order["is_ask"]), None)
            if opposite is not None:
                self.fill(opposite, maker=True)
        return identifier

    async def http(self, **kwargs):
        url = urlsplit(kwargs["url"])
        if (url.scheme != "https" or url.netloc != "api.rh-testnet.lighter.xyz"
                or url.fragment):
            raise AssertionError("unexpected synthetic HTTP destination")
        op, method = url.path.rsplit("/", 1)[-1], kwargs["method"]
        self.record(method, url.path, op)
        query = parse_qs(url.query)
        if url.path != "/api/v1/" + op or method != ("POST" if op == "sendTx" else "GET"):
            raise AssertionError("synthetic HTTP method/path mismatch")
        if "account_index" in query and query["account_index"] != ["7"]:
            raise AssertionError("synthetic HTTP account mismatch")
        if "market_id" in query and query["market_id"] != ["1"]:
            raise AssertionError("synthetic HTTP market mismatch")
        if op == "account" and (query.get("by") != ["index"] or query.get("value") != ["7"]):
            raise AssertionError("synthetic HTTP account selector mismatch")
        if op == "nextNonce" and (query.get("account_index") != ["7"] or query.get("api_key_index") != ["0"]):
            raise AssertionError("synthetic HTTP nonce selector mismatch")
        if op in {"orderBookDetails", "positionFunding", "trades", "fundings"} and query.get("market_id") != ["1"]:
            raise AssertionError("synthetic HTTP market selector missing")
        if op in {"accountLimits", "positionFunding", "accountActiveOrders", "accountInactiveOrders", "trades"}:
            headers = {str(key).lower(): value for key, value in kwargs.get("headers", {}).items()}
            if headers.get("authorization") != "synthetic-wire-auth" or query.get("account_index") != ["7"]:
                raise AssertionError("synthetic private HTTP authentication missing")
        if op == "sendTx":
            data = kwargs.get("data")
            if hasattr(data, "_fields"):
                values = {field[0]["name"]: field[2] for field in data._fields}
            elif isinstance(data, (str, bytes)):
                values = json.loads(data)
            else:
                raise AssertionError("SDK wire transaction encoding missing")
            tx_type, tx = int(values["tx_type"]), json.loads(values["tx_info"])
            self.requests[-1].update(tx_type=tx_type, transaction_nonce=tx["Nonce"],
                **({"order_id": str(tx["OrderNonce"])} if tx_type == 15 else
                   {"client_order_index": tx["ClientOrderIndex"], "base_amount": tx["BaseAmount"],
                    "price": tx["Price"], "time_in_force": tx["TimeInForce"],
                    "reduce_only": tx["ReduceOnly"]}))
            identifier = self.accept_tx(tx_type, tx)
            if self.scenario in {"lost_cancel_response", "unresolved_cancel_response"} and tx_type == 15 and not self.fault_used:
                self.fault_used = True
                if self.scenario == "unresolved_cancel_response":
                    self.delayed[identifier] = -1  # No publication within any recovery read.
                self.event("response_lost", order_id=str(identifier))
                raise ConnectionResetError("synthetic accepted response lost")
            payload = {"code": 200, "message": "synthetic accepted", "tx_hash": "synthetic",
                       "predicted_execution_time_ms": 0, "volume_quota_remaining": 10000}
        elif op == "nextNonce":
            payload = {"code": 200, "nonce": self.nonce}
        elif op == "orderBooks":
            payload = {"code": 200, "order_books": [{"symbol": "BTC", "market_id": 1,
                "market_type": "perp", "status": "active", "supported_price_decimals": 1,
                "supported_size_decimals": 5, "min_base_amount": "0.00001", "min_quote_amount": "10"}]}
        elif op == "orderBookDetails":
            payload = {"code": 200, "order_book_details": [{"symbol": "BTC", "market_id": 1,
                "market_type": "perp", "status": "active", "supported_price_decimals": 1,
                "supported_size_decimals": 5, "min_base_amount": "0.00001", "min_quote_amount": "10",
                "price_decimals": 1, "size_decimals": 5, "last_trade_price": 77000.0}]}
        elif op == "account":
            payload = {"code": 200, "total": 1, "accounts": [self.account()]}
        elif op == "accountActiveOrders":
            payload = {"code": 200, "orders": self.active()}
        elif op == "accountInactiveOrders":
            rows = []
            for identifier, order in self.orders.items():
                if order["status"] == "open":
                    continue
                if self.delayed.get(identifier, 0):
                    if self.delayed[identifier] > 0:
                        self.delayed[identifier] -= 1
                    self.event("terminal_history_hidden", order_id=str(identifier))
                else:
                    rows.append(self.public_order(order))
                    if identifier not in self.published_terminal:
                        self.published_terminal.add(identifier)
                        self.event("terminal_history_visible", order_id=str(identifier))
            payload = {"code": 200, "orders": list(reversed(rows)), "next_cursor": ""}
        elif op == "trades":
            payload = {"code": 200, "trades": list(reversed(self.trades)), "next_cursor": ""}
        elif op == "accountLimits":
            payload = {"code": 200, "user_tier": "premium", "current_maker_fee_tick": 120,
                       "current_taker_fee_tick": 350}
        elif op == "positionFunding":
            payload = {"code": 200, "position_fundings": []}
        elif op == "fundings":
            payload = {"code": 200, "resolution": "1h", "fundings": []}
        elif op == "assetDetails":
            payload = {"code": 200, "asset_details": [{"asset_id": 3, "symbol": "USDG",
                "decimals": 6, "margin_mode": "enabled", "index_price": "1", "loan_to_value": "1"}]}
        else:
            raise AssertionError("unexpected synthetic HTTP operation: " + op)
        await REAL_SLEEP(0)
        return _RawResponse(payload)

    def broadcast_book(self):
        for sock in self.sockets:
            if sock.book_subscribed and not sock.closed:
                sock.book()

    async def connect(self, url, **kwargs):
        if url != "wss://api.rh-testnet.lighter.xyz/stream":
            raise AssertionError("unexpected synthetic WS destination")
        self.record("CONNECT", "/stream", "ws_connect")
        sock = _RawSocket(self)
        self.sockets.append(sock)
        return sock


class _RawSocket:
    def __init__(self, venue):
        self.venue, self.queue = venue, asyncio.Queue()
        self.closed, self.book_subscribed = False, False
        self.subscriptions = set()
        self.suppress_book = False
        self.alignment_pending = False
        self.wait_fault_clock = None
        self.book_nonce, self.offset = venue.market_nonce, 0
        self.protocol = SimpleNamespace(send_frame=lambda frame: None)
        self.ticker = asyncio.create_task(self._tick())

    async def _tick(self):
        while not self.closed:
            await REAL_SLEEP(.05)
            if self.wait_fault_clock is not None and self.venue.clock.mode == "virtual":
                started, clock_value = self.wait_fault_clock
                self.venue.clock.value = max(self.venue.clock.value, clock_value + time.perf_counter() - started)
            if self.book_subscribed:
                self.book()

    def push(self, message):
        self.queue.put_nowait(json.dumps(message))

    def book(self, initial=False):
        if self.suppress_book:
            return
        old = self.book_nonce
        self.book_nonce = old if self.wait_fault_clock is not None else max(old, self.venue.market_nonce)
        self.offset += 1
        timestamp = int(self.venue.clock.wall() * 1000)
        self.push({"type": "subscribed/order_book" if initial else "update/order_book",
            "channel": "order_book:1", "timestamp": timestamp, "offset": self.offset,
            "order_book": {"code": 200, "nonce": self.book_nonce, "begin_nonce": old,
                "offset": self.offset, "last_updated_at": timestamp,
                "bids": [{"price": "76999.9", "size": "2"}],
                "asks": [{"price": "77000.1", "size": "2"}]}})

    async def order_snapshot_fault(self):
        venue = self.venue
        if (venue.fault_used or not venue.scenario.startswith("book_")
                or len(venue.active()) != 2):
            return False
        venue.paired_order_snapshots += 1
        if venue.paired_order_snapshots < 2:
            return False  # The second order's original confirmation sees a healthy stream.
        venue.fault_used = True
        venue.event("book_fault", fault=venue.scenario, open_order_count=2)
        if venue.scenario.endswith("_fill"):
            opposite = next(key for key, row in venue.orders.items() if row["status"] == "open" and row["is_ask"])
            venue.fill(opposite, maker=True)
        if venue.scenario == "book_transport_close":
            await self.close()
            return True
        if venue.scenario == "book_alignment_recovers":
            old = self.book_nonce
            self.book_nonce += 1
            self.offset += 1
            self.alignment_pending = True
            # A valid public packet is one nonce ahead of this account bracket.
            # Only the next independent account bracket will cover that packet.
            self.push({"type": "update/order_book", "channel": "order_book:1",
                "timestamp": int(venue.clock.wall() * 1000), "offset": self.offset,
                "order_book": {"code": 200, "nonce": self.book_nonce, "begin_nonce": old,
                               "offset": self.offset, "bids": [], "asks": []}})
            return False
        if venue.scenario == "book_wait_timeout":
            # Account watermark advances; fresh public packets remain behind it.
            # Preserve the real 3s wait and include that wait in the fault clock.
            venue.market_nonce += 1
            self.wait_fault_clock = time.perf_counter(), venue.clock.monotonic()
        else:
            self.suppress_book = True
            self.offset += 1
            self.push({"type": "update/order_book", "channel": "order_book:1",
                "timestamp": int(venue.clock.wall() * 1000), "offset": self.offset,
                "order_book": {"code": 200, "nonce": self.book_nonce + 2,
                    "begin_nonce": self.book_nonce + 1, "offset": self.offset,
                    "bids": [], "asks": []}})
        return False

    async def send(self, raw):
        message = json.loads(raw)
        kind, channel = message["type"], message.get("channel", "")
        name = channel.split("/")[0]
        self.venue.record("WS", channel, kind)
        self.protocol.send_frame(SimpleNamespace(opcode=1))
        if name == "order_book":
            if kind != "subscribe" or channel != "order_book/1":
                raise AssertionError("unexpected synthetic book subscription")
            self.book_subscribed = True
            self.book(initial=True)
        elif name in {"account_all", "account_orders", "account_all_orders"}:
            if kind not in {"subscribe", "unsubscribe"}:
                raise AssertionError("unexpected synthetic account subscription action")
            expected = name + ("/1/7" if name == "account_orders" else "/7")
            if channel != expected:
                raise AssertionError("synthetic WS account identity mismatch")
            if kind == "subscribe" and name == "account_orders" and await self.order_snapshot_fault():
                return
            private_orders = name in {"account_orders", "account_all_orders"}
            if kind == "subscribe" and private_orders and name in self.subscriptions:
                self.push({"code": 30003})  # Duplicate subscription is not a fresh snapshot.
                return
            if kind == "unsubscribe":
                if not private_orders or name not in self.subscriptions:
                    raise AssertionError("synthetic unsubscribe lacks active subscription")
                self.subscriptions.remove(name)
            suffix = "1" if name == "account_orders" else "7"
            response = {"type": "unsubscribed" if kind == "unsubscribe" else "subscribed/" + name,
                        "channel": name + ":" + suffix, "account": 7}
            if kind == "subscribe":
                if name == "account_all":
                    account = self.venue.account()
                    response.update(assets={"3": account["assets"][0]}, positions={"1": account["positions"][0]},
                        shares=[], trades={}, funding_histories={}, total_trades_count=len(self.venue.trades))
                else:
                    if message.get("auth") != "synthetic-wire-auth":
                        raise AssertionError("synthetic WS auth missing")
                    self.subscriptions.add(name)
                    response.update(orders={"1": self.venue.active()}, nonce=self.venue.market_nonce)
                    self.venue.requests[-1]["response_nonce"] = self.venue.market_nonce
            self.push(response)
            if name == "account_all" and kind == "subscribe" and self.alignment_pending:
                self.alignment_pending = False
                self.venue.market_nonce = self.book_nonce
                self.venue.event("book_alignment_available", nonce=self.book_nonce)
            if kind == "subscribe":
                self.book()
        elif kind != "pong":
            raise AssertionError("unexpected synthetic WS frame")
        await REAL_SLEEP(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.queue.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def close(self):
        if not self.closed:
            self.closed = True
            self.ticker.cancel()
            self.queue.put_nowait(None)


class WireFixture:
    def __init__(self, scenario="normal", time_mode="virtual", state_path=None):
        if scenario not in {"normal", "late_cancel_fill", "lost_cancel_response", "unresolved_cancel_response",
                            "book_wait_timeout", "book_invalid_nonce", "book_transport_close", "book_invalid_nonce_fill",
                            "book_alignment_recovers"}:
            raise ValueError("unsupported synthetic wire scenario")
        if time_mode not in {"real", "virtual"}:
            raise ValueError("unsupported synthetic clock")
        self.scenario, self.time_mode = scenario, time_mode
        self.clock = WireClock(time_mode)
        self.venue = WireVenue(self.clock, scenario, state_path)
        self.clock.venue = self.venue

    @property
    def requests(self):
        return self.venue.requests

    def snapshot(self):
        return self.venue.snapshot()

    def write_synthetic_workspace(self, folder, duration_seconds=150):
        folder = Path(folder)
        paths = {"config": folder / "config/market_maker_v2/test_live_economics_60m.yaml",
                 "exchange_config": folder / "config/exchanges/lighter_config.yaml",
                 "env_file": folder / ".env", "output": folder / "logs/wire.jsonl"}
        for path in paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        paths["config"].write_text('''market_maker_v2:
  symbol: BTC
  profile: fee_neutral_volume_v1
  dry_run: false
  quote:
    order_size: "0.00040"
    target_net_edge_bps: "0"
    volatility_multiplier: "0"
    reprice_threshold_ticks: 5
    max_quote_age_ms: 60000
  inventory:
    soft_limit: "0.0008"
    hard_limit: "0.0012"
    skew_bps_at_hard: "0"
  flatten:
    max_hold_seconds: 10
    stop_loss_usdg: "0.10"
    passive_grace_seconds: 0
    ioc_slippage_ticks: 3
  session:
    duration_seconds: ''' + str(duration_seconds) + '''
    max_loss_usdg: "0.20"
    cooldown_seconds: 0
''', encoding="utf-8")
        paths["exchange_config"].write_text('''api_config:
  network: robinhood_testnet
  testnet: true
  auth:
    api_key_private_key: "''' + "1" * 64 + '''"
    account_index: 7
    api_key_index: 0
    expected_l1_address: "0x''' + "1" * 40 + '''"
''', encoding="utf-8")
        paths["env_file"].write_text("# Synthetic wire fixture; intentionally empty.\n", encoding="utf-8")
        return paths

    @contextmanager
    def install(self):
        """Install before CLI import; deny venue sockets/DNS, preserve loop wakeup."""
        import lighter.signer_client as signer
        import lighter.rest as rest
        import websockets
        # Capture the real default before patching websockets: a first import
        # inside the patch would otherwise leave this venue bound after restore.
        from core.adapters.exchanges.adapters.lighter_read_stream import LighterReadStream

        def denied(*args, **kwargs):
            raise AssertionError("physical network forbidden by synthetic wire fixture")

        venue = self.venue
        class Pool:
            request = staticmethod(venue.http)
            async def close(self):
                pass

        def rest_init(client, configuration):
            client.pool_manager = Pool()
            client.proxy = client.proxy_headers = client.retry_client = None

        with ExitStack() as stack:
            # Preserve the original Windows Proactor wakeup and Ctrl+C path.
            # Its stdlib socketpair fallback alone may connect synchronously to
            # its own newly bound literal-loopback port, on the same thread.
            # This is not an HTTP/WS venue connection or external network grant.
            pair_scope = threading.local()
            original_pair, original_bind = socket.socketpair, socket.socket.bind
            original_connect = socket.socket.connect
            pair_code = getattr(original_pair, "__code__", None)
            def pair(*args, **kwargs):
                if getattr(pair_scope, "active", False):
                    raise AssertionError("recursive synthetic socketpair scope")
                pair_scope.active, pair_scope.target, pair_scope.listener = True, None, None
                try:
                    return original_pair(*args, **kwargs)
                finally:
                    pair_scope.active, pair_scope.target, pair_scope.listener = False, None, None
            def bind(sock, address):
                caller = sys._getframe(1)
                if (not getattr(pair_scope, "active", False) or caller.f_code is not pair_code
                        or caller.f_locals.get("lsock") is not sock
                        or address[0] not in {"127.0.0.1", "::1"}
                        or address[1] != 0 or pair_scope.target is not None
                        or sock.type != socket.SOCK_STREAM or sock.proto != 0):
                    return denied()
                result = original_bind(sock, address)
                pair_scope.target, pair_scope.listener = sock.getsockname()[:2], sock
                return result
            def connect(sock, address):
                caller = sys._getframe(1)
                if (getattr(pair_scope, "active", False) and pair_scope.target is not None
                        and caller.f_code is pair_code and caller.f_locals.get("csock") is sock
                        and caller.f_locals.get("lsock") is pair_scope.listener
                        and sock.family == pair_scope.listener.family
                        and sock.type == socket.SOCK_STREAM and sock.proto == 0
                        and tuple(address[:2]) == pair_scope.target):
                    pair_scope.target = None
                    return original_connect(sock, address)
                return denied()
            stack.enter_context(patch.object(socket, "socketpair", pair))
            stack.enter_context(patch.object(socket.socket, "bind", bind))
            stack.enter_context(patch.object(socket.socket, "connect", connect))
            for name in ("connect_ex", "sendto"):
                stack.enter_context(patch.object(socket.socket, name, denied))
            stack.enter_context(patch.object(socket, "getaddrinfo", denied))
            stack.enter_context(patch.object(socket, "create_connection", denied))
            stack.enter_context(patch.object(signer, "get_signer", lambda: venue.native))
            stack.enter_context(patch.object(signer, "__signer", venue.native))
            stack.enter_context(patch.object(signer, "__get_shared_library", denied))
            stack.enter_context(patch.object(rest.RESTClientObject, "__init__", rest_init))
            stack.enter_context(patch.object(websockets, "connect", venue.connect))
            defaults = dict(LighterReadStream.__init__.__kwdefaults__)
            defaults.update(connect_factory=venue.connect, wall_clock=self.clock.wall)
            stack.enter_context(patch.object(LighterReadStream.__init__, "__kwdefaults__", defaults))
            if self.time_mode == "virtual":
                from core.services.market_maker_v2.orchestrator import VolumeSession
                original = VolumeSession.__init__
                def session_init(session, *args, **kwargs):
                    kwargs.update(clock=self.clock, sleep=self.clock.sleep)
                    original(session, *args, **kwargs)
                stack.enter_context(patch.object(VolumeSession, "__init__", session_init))
            yield self
            venue.persist()


def write_synthetic_workspace(folder, duration_seconds=150):
    return WireFixture().write_synthetic_workspace(folder, duration_seconds)

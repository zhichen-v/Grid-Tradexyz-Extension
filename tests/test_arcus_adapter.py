"""Offline Arcus adapter tests: signing, order mapping, uncertainty, WebSocket parsing."""

import asyncio
import json
import unittest
from decimal import Decimal

import aiohttp
from nacl.signing import SigningKey

from core.adapters.exchanges.adapters.arcus import ArcusAdapter, ArcusAPIError
from core.adapters.exchanges.exceptions import (
    OrderSubmissionNotSentError,
    OrderSubmissionRejectedError,
)
from core.adapters.exchanges.interface import ExchangeConfig
from core.adapters.exchanges.models import ExchangeType, OrderSide, OrderStatus, OrderType

SECRET = "11" * 32
ADDRESS = "0xAbCdEf0000000000000000000000000000000001"
MARKET = {
    "marketDisplayName": "BTC-USD",
    "marketId": 1,
    "tickSize": "0.5",
    "stepSize": "0.0001",
    "tickTiers": [{"upToPrice": "100000", "tick": "0.5"}, {"tick": "1"}],
    "minOrderSize": "0.0001",
    "minOrderNotional": "10",
}


def make_adapter() -> ArcusAdapter:
    adapter = ArcusAdapter(
        ExchangeConfig(
            exchange_id="arcus",
            name="Arcus",
            exchange_type=ExchangeType.PERPETUAL,
            api_key="",
            api_secret=SECRET,
            wallet_address=ADDRESS,
            enable_websocket=False,
            extra_params={"network": "testnet", "account_index": 2},
        )
    )
    adapter._markets = {"BTC-USD": MARKET}
    return adapter


class FakeRequests:
    def __init__(self, responses):
        self.calls = []
        self.responses = list(responses)

    async def __call__(self, method, path, params=None, body=None, headers=None):
        self.calls.append(
            {"method": method, "path": path, "params": params, "body": body, "headers": headers}
        )
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result(path, params, body) if callable(result) else result


class ArcusOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_place_order_signs_typed_payload(self):
        adapter = make_adapter()
        adapter._request = FakeRequests([{"orderId": "ab12", "status": "ACK", "side": "BUY",
                                          "quantity": "0.002", "price": "95000.5"}])

        order = await adapter.create_order(
            "BTC-USD", OrderSide.BUY, OrderType.LIMIT, Decimal("0.002"), Decimal("95000.5"),
            params={"time_in_force": "POST_ONLY"},
        )

        call = adapter._request.calls[0]
        body, headers = call["body"], call["headers"]
        self.assertEqual(call["path"], "/v1/placeOrder")
        self.assertEqual(body["timeInForce"], "ALO")
        self.assertEqual(body["accountIndex"], 2)
        self.assertEqual(headers["X-Timestamp"], str(body["timestamp"]))
        signed = (
            f'{{"ad":"{ADDRESS.lower()}","ai":2,"c":"{body["clientId"]}","ct":{body["timestamp"]},'
            f'"g":{int(body["goodTilTime"]) * 1000},"m":1,"op":1,"p":190001,"q":20,'
            f'"r":0,"s":0,"t":3,"v":1}}'
        )
        verify_key = SigningKey(bytes.fromhex(SECRET)).verify_key
        verify_key.verify(signed.encode(), bytes.fromhex(headers["X-Signature"]))
        self.assertEqual(headers["X-API-Key"], verify_key.encode().hex())
        self.assertEqual(order.id, "ab12")
        self.assertEqual(order.status, OrderStatus.OPEN)

    async def test_off_tick_price_is_rejected_before_sending(self):
        adapter = make_adapter()
        adapter._request = FakeRequests([])
        with self.assertRaisesRegex(ValueError, "not a multiple"):
            # 100000.5 sits in the 1.0 tick tier.
            await adapter.create_order(
                "BTC-USD", OrderSide.SELL, OrderType.LIMIT, Decimal("0.001"), Decimal("100000.5")
            )
        self.assertEqual(adapter._request.calls, [])

    async def test_market_order_uses_bounded_price_from_mark(self):
        adapter = make_adapter()
        adapter._request = FakeRequests([
            {"markets": [{"markPrice": "90000"}]},
            {"bestBid": ["89999", "1"], "bestAsk": ["90001", "1"]},
            {"orderId": "m1", "status": "ACK"},
        ])
        await adapter.create_order(
            "BTC", OrderSide.SELL, OrderType.MARKET, Decimal("0.001"), params={"reduce_only": True}
        )
        body = adapter._request.calls[-1]["body"]
        self.assertEqual(body["orderType"], "MARKET")
        self.assertEqual(body["timeInForce"], "IOC")
        self.assertEqual(Decimal(body["price"]), Decimal("85500"))
        self.assertTrue(body["reduceOnly"])

    async def test_definitive_and_unsent_failures_raise_typed_errors(self):
        adapter = make_adapter()
        adapter._request = FakeRequests([
            ArcusAPIError(400, {"error": {"message": "bad"}}),
            ArcusAPIError(503, {"error": {"errorType": "Transmission"}}),
        ])
        args = ("BTC-USD", OrderSide.BUY, OrderType.LIMIT, Decimal("0.001"), Decimal("90000"))
        with self.assertRaises(OrderSubmissionRejectedError):
            await adapter.create_order(*args)
        with self.assertRaises(OrderSubmissionNotSentError):
            await adapter.create_order(*args)

    async def test_lost_response_returns_uncertain_placeholder_then_resolves(self):
        adapter = make_adapter()
        adapter._request = FakeRequests([asyncio.TimeoutError()])
        order = await adapter.create_order(
            "BTC-USD", OrderSide.BUY, OrderType.LIMIT, Decimal("0.001"), Decimal("90000")
        )
        self.assertEqual(order.id, "")
        self.assertTrue(order.raw_data["submission_uncertain"])
        self.assertEqual(adapter.get_unresolved_submissions()[0]["client_order_id"], order.client_id)

        adapter._request = FakeRequests([
            {"orders": []},
            {"orders": [{"orderId": "late", "clientId": order.client_id, "status": "OPEN",
                         "side": "BUY", "originalSize": "0.001", "remainingSize": "0.001",
                         "price": "90000", "marketDisplayName": "BTC-USD"}]},
        ])
        resolved = await adapter.resolve_unresolved_submissions()
        self.assertEqual([o.id for o in resolved], ["late"])
        self.assertEqual(adapter.get_unresolved_submissions(), [])

    async def test_cancel_ack_is_nonterminal_and_lost_cancel_is_marked(self):
        adapter = make_adapter()
        adapter._request = FakeRequests([
            {"status": "CANCEL_ACKNOWLEDGED"},
            aiohttp.ClientConnectionError("reset"),
        ])
        ack = await adapter.cancel_order("o1", "BTC-USD")
        self.assertEqual(ack.params, {"cancel_terminal": False})
        self.assertIsNone(await adapter.cancel_order("o2", "BTC-USD"))
        self.assertIn(("BTC-USD", "o2"), adapter._uncertain_cancellations)

    async def test_cancel_all_uses_legacy_message_signature(self):
        adapter = make_adapter()
        adapter._request = FakeRequests([{"orders": []}, {"status": "CANCEL_ALL_ACKNOWLEDGED"}])
        await adapter.cancel_all_orders("BTC-USD")
        call = adapter._request.calls[1]
        body, headers = call["body"], call["headers"]
        message = headers["X-Timestamp"] + "cancelAllOrders" + json.dumps(
            body, separators=(",", ":"), sort_keys=True
        )
        SigningKey(bytes.fromhex(SECRET)).verify_key.verify(
            message.encode(), bytes.fromhex(headers["X-Signature"])
        )


class ArcusWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_and_position_frames(self):
        adapter = make_adapter()
        orders, tickers = [], []
        adapter._user_callbacks.append(lambda order: orders.append(order))
        adapter._ticker_callbacks["BTC-USD"] = [tickers.append]

        await adapter._handle_ws_message({
            "type": "channel_data", "channel": "orders", "id": ADDRESS.lower(),
            "contents": {"orderId": "o1", "clientId": "c1", "marketDisplayName": "BTC-USD",
                         "side": "SELL", "status": "PARTIALLY_FILLED", "price": "95000",
                         "originalSize": "0.002", "remainingSize": "0.001",
                         "avgFillPrice": "95000"},
        })
        await adapter._handle_ws_message({
            "type": "channel_data", "channel": "positions", "id": ADDRESS.lower(),
            "contents": {"positions": {"1": {"marketDisplayName": "BTC-USD", "size": "-0.003",
                                             "averageEntryPrice": "94000",
                                             "unrealizedPnl": "-1.5"}}},
        })
        await adapter._handle_ws_message({
            "type": "channel_data", "channel": "bbo", "id": "BTC-USD",
            "contents": {"bestBid": ["94999", "1"], "bestAsk": ["95001", "2"]},
        })

        self.assertEqual(orders[0].status, OrderStatus.OPEN)
        self.assertEqual(orders[0].filled, Decimal("0.001"))
        self.assertEqual(adapter._position_cache["BTC-USD"]["size"], Decimal("-0.003"))
        self.assertEqual(tickers[0].last, Decimal("95000"))

    async def test_expired_rejection_maps_to_expired(self):
        adapter = make_adapter()
        order = adapter._parse_order(
            {"orderId": "o", "status": "REJECTED", "rejectionReason": "EXPIRED", "side": "BUY",
             "originalSize": "1", "remainingSize": "1"}
        )
        self.assertEqual(order.status, OrderStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()

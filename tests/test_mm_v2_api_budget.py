"""Offline request-boundary tests; these are not live wire-traffic measurements."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.frames import Frame, Opcode
from websockets.protocol import Protocol, Side, State

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_read_stream import LighterReadStream
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.services.market_maker_v2.api_budget import ApiBudget, ApiBudgetUnavailable
from test_lighter_read_stream import Socket


ZERO = {"rest": 0, "ws": 0, "tx": 0}


class ApiBudgetTests(unittest.TestCase):
    def test_scheduled_exit_distinguishes_uniform_and_burst_expirations(self):
        def usage(times):
            now = [0.0]
            budget = ApiBudget(lambda: now[0])
            for index, timestamp in enumerate(times):
                now[0] = timestamp
                if index == 45:
                    continue  # Leave 300 weight for the exact funding round read.
                if index == 46:
                    for _ in range(2):
                        budget.observe("rest", "https://example.invalid/api/v1/accountInactiveOrders")
                else:
                    budget.observe("rest", "https://example.invalid/api/v1/account")
            now[0] = 60.0
            self.assertEqual(budget.snapshot()["used"]["rest"], 13700)
            return budget
        uniform = usage([1 + 58 * index / 46 for index in range(47)])
        expiring = usage([1.0] * 47)
        recent = usage([59.0] * 47)
        self.assertTrue(uniform.scheduled_live_available(ZERO))
        self.assertTrue(expiring.scheduled_live_available(ZERO))
        self.assertFalse(recent.scheduled_live_available(ZERO))
        # The peak at16s includes both the forced funding and coherent retry.
        self.assertTrue(uniform.scheduled_live_available(ZERO | {"rest": 94}))
        self.assertFalse(uniform.scheduled_live_available(ZERO | {"rest": 95}))
        with self.assertRaisesRegex(ApiBudgetUnavailable, "^API capacity reserved for bounded exit$"):
            uniform.require_normal(ZERO | {"rest": 300})
        self.assertIsNone(expiring.require_normal(ZERO | {"rest": 300}))

    def test_scheduled_exit_never_spends_future_expiry_credit_at_or_before_a_step(self):
        def burst(timestamp, accounts, histories=0):
            now = [timestamp]
            budget = ApiBudget(lambda: now[0])
            for endpoint, count in (("account", accounts), ("accountInactiveOrders", histories)):
                for _ in range(count):
                    budget.observe("rest", "https://example.invalid/api/v1/" + endpoint)
            now[0] = 60.0
            return budget
        # A request expiring soon remains charged at t=0.
        self.assertFalse(burst(0.5, 52).scheduled_live_available(ZERO))
        # 15100+8806 fits before t=.5. The new100-weight prefix at t=.5
        # fits only if that burst has expired by that exact boundary.
        self.assertTrue(burst(0.5, 50, 1).scheduled_live_available(ZERO))
        self.assertFalse(burst(0.500000001, 50, 1).scheduled_live_available(ZERO))

    def test_scheduled_exit_reserves_ws_tx_and_validates_all_cost_domains(self):
        budget = ApiBudget(lambda: 1.0)
        self.assertTrue(budget.scheduled_live_available({"rest": 8294, "ws": 133, "tx": 35}))
        for cost in ({"rest": 8295, "ws": 133, "tx": 35},
                     {"rest": 8294, "ws": 134, "tx": 35},
                     {"rest": 8294, "ws": 133, "tx": 36}):
            self.assertFalse(budget.scheduled_live_available(cost))
        for cost in ({"rest": 0}, ZERO | {"other": 0}, ZERO | {"rest": -1},
                     ZERO | {"ws": False}, ZERO | {"tx": 1.0}):
            with self.assertRaises(ValueError):
                budget.scheduled_live_available(cost)
        with self.assertRaisesRegex(RuntimeError, "unclassified API accounting endpoint"):
            budget.observe("rest", "https://example.invalid/api/v1/tokens/create?auth=test-secret")
        self.assertEqual(budget.snapshot()["attempts"], {})

    def test_refusal_diagnostic_keeps_only_numeric_usage_and_next_cost(self):
        from decimal import Decimal
        from core.services.market_maker_v2.telemetry import failure_diagnostic
        budget = ApiBudget(lambda: 1.0)
        for _ in range(130):
            budget.observe("ws", 1)
        with self.assertRaises(ApiBudgetUnavailable) as caught:
            budget.require_normal({"rest": 1900, "ws": 5, "tx": 0})
        diagnostic = failure_diagnostic("BTC", "authorizing_quotes", caught.exception)
        values = {row.name: row.value for row in diagnostic.values}
        self.assertEqual(values, {"api_used_rest": Decimal(0), "api_used_ws": Decimal(130),
            "api_used_tx": Decimal(0), "api_next_rest": Decimal(1900),
            "api_next_ws": Decimal(5), "api_next_tx": Decimal(0)})

    def test_weights_attempts_expiration_and_query_redaction(self):
        now = [1.0]
        budget = ApiBudget(lambda: now[0])
        for endpoint in ("account", "trades", "recentTrades", "accountInactiveOrders",
                         "nextNonce", "apikeys", "sendTx", "sendTxBatch"):
            budget.observe("rest", "https://example.invalid/api/v1/" + endpoint + "?auth=test-secret")
        for opcode in (0, 1, 2, 8, 9, 10):
            budget.observe("ws", opcode)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["used"], {"rest": 1756, "tx": 2, "ws": 6})
        self.assertEqual(snapshot["scope"], "owned_python_transports")
        self.assertNotIn("test-secret", repr(snapshot))
        now[0] = 60.999
        self.assertEqual(budget.snapshot()["used"], snapshot["used"])
        now[0] = 61.0
        expired = budget.snapshot()
        self.assertTrue(all(value == 0 for value in expired["used"].values()))
        self.assertEqual(expired["peaks"], snapshot["peaks"])
        self.assertEqual(expired["attempts"], snapshot["attempts"])

    def test_admission_reserves_every_bucket_and_rejects_invalid_cost_bounds(self):
        budget = ApiBudget(lambda: 1.0)
        self.assertTrue(budget.available(normal=ZERO, reserve=budget.LIMITS))
        budget.observe("rest", "https://example.invalid/api/v1/nextNonce")
        self.assertFalse(budget.available(normal=ZERO, reserve=budget.LIMITS))
        self.assertTrue(budget.available(normal=ZERO | {"rest": 23994}, reserve=ZERO))
        self.assertFalse(budget.available(normal=ZERO | {"rest": 23995}, reserve=ZERO))
        for costs in ({"rest": 0, "ws": 0}, ZERO | {"unknown": 0}, ZERO | {"tx": -1},
                      ZERO | {"tx": 0.0}, ZERO | {"tx": False}):
            with self.subTest(costs=costs):
                with self.assertRaises(ValueError):
                    budget.available(normal=costs, reserve=ZERO)
                with self.assertRaises(ValueError):
                    budget.available(normal=ZERO, reserve=costs)
        for _ in range(201):
            budget.observe("ws", 1)
        self.assertFalse(budget.available(normal=ZERO, reserve=ZERO))
        self.assertEqual(budget.snapshot()["peaks"]["ws"], 201)

    def test_invalid_transport_and_clock_discontinuity_cannot_produce_headroom(self):
        now = [42.0]
        budget = ApiBudget(lambda: now[0])
        budget.observe("ws", 1)
        for transport, target in (("http", "/account"), ("ws", True), ("ws", "1"), ("ws", 3)):
            with self.subTest(transport=transport, target=target):
                with self.assertRaises(RuntimeError):
                    budget.observe(transport, target)
        for value in (41.0, float("inf"), float("nan")):
            now[0] = value
            with self.assertRaises(RuntimeError):
                budget.available(normal=ZERO, reserve=ZERO)
        now[0] = 42.0
        self.assertEqual(budget.snapshot()["attempts"], {"ws:1": 1})


class OwnedRequestObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_execution_order_handoff_still_admits_following_terminal_reads(self):
        from core.services.market_maker_v2.lighter_runtime import LighterAccountPort
        from test_mm_v2_lighter_runtime import Adapter, Clock, ReadStream, ADDRESS

        adapter, clock = Adapter(), Clock()
        account = LighterAccountPort(adapter, "BTC", clock, account_index=7,
            expected_l1_address=ADDRESS, mutation_generation=lambda: 0)
        account.stream = ReadStream(adapter, clock)
        await account.snapshot()
        account.before_read = Mock(side_effect=ApiBudgetUnavailable("no headroom"))
        with self.assertRaises(ApiBudgetUnavailable):
            await account.read_execution_orders("BTC")
        account.before_read.assert_called_once()

    async def test_empty_normal_quote_plan_admits_cancellation_before_sending(self):
        from core.services.market_maker_v2.domain import QuotePlan
        from test_mm_v2_quote_execution import QuoteExecutionTests

        fixture = QuoteExecutionTests()
        fixture.setUp()
        await fixture.quote_both()
        fixture.adapter.cancel_order.reset_mock()
        fixture.port.before_mutation = Mock(side_effect=ApiBudgetUnavailable("no headroom"))
        try:
            await fixture.port.reconcile_quotes(QuotePlan("BTC"))
        except ApiBudgetUnavailable:
            pass
        fixture.port.before_mutation.assert_called_once()
        fixture.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(len(fixture.open), 2)

    async def test_quota_refusal_after_terminal_cancel_keeps_fresh_cleanup_possible(self):
        from core.services.market_maker_v2.domain import ExecutionHealth, ExecutionStatus
        from test_mm_v2_quote_execution import QuoteExecutionTests

        fixture = QuoteExecutionTests()
        fixture.setUp()
        await fixture.quote_both()
        read_account = fixture.account.snapshot
        fixture.account.snapshot = AsyncMock(side_effect=ApiBudgetUnavailable("no headroom"))
        with self.assertRaises(ApiBudgetUnavailable):
            await fixture.port.cancel_all_managed()
        self.assertIs(fixture.port.snapshot().health, ExecutionHealth.HEALTHY)
        self.assertEqual(fixture.open, {})
        self.assertEqual(fixture.adapter.cancel_order.await_count, 2)
        fixture.account.snapshot = read_account
        proved = await fixture.port.cancel_all_managed()
        self.assertIs(proved.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(proved.account_snapshot.open_order_ids, ())
        self.assertEqual(fixture.adapter.cancel_order.await_count, 2)

    async def test_account_budget_refusal_keeps_type_and_quote_refusal_does_not_mark_failure(self):
        from core.services.market_maker_v2.lighter_runtime import LighterAccountPort
        from test_mm_v2_lighter_runtime import Adapter, Clock, ReadStream, ADDRESS
        from test_mm_v2_quote_execution import QuoteExecutionTests

        adapter, clock = Adapter(), Clock()
        account = LighterAccountPort(adapter, "BTC", clock, account_index=7,
            expected_l1_address=ADDRESS, mutation_generation=lambda: 0)
        account.stream = ReadStream(adapter, clock)
        await account.snapshot()
        account.before_read = Mock(side_effect=ApiBudgetUnavailable("no headroom"))
        with self.assertRaises(ApiBudgetUnavailable):
            await account.snapshot()
        account.before_read = None
        self.assertTrue((await account.snapshot()).authenticated)

        fixture = QuoteExecutionTests()
        fixture.setUp()
        fixture.refresh.side_effect = ApiBudgetUnavailable("no headroom")
        fixture.port._on_failure = Mock()
        with self.assertRaises(ApiBudgetUnavailable):
            await fixture.port.reconcile_quotes(fixture.proposal)
        fixture.adapter.create_order.assert_not_awaited()
        fixture.port._on_failure.assert_not_called()

    @staticmethod
    def adapter():
        signer_transport = SimpleNamespace(request=AsyncMock(return_value="signer-response"))
        rest = object.__new__(LighterRest)
        rest.signer_client = SimpleNamespace(api_client=SimpleNamespace(rest_client=signer_transport),
                                             check_client=Mock(return_value=None))
        rest.base_url = "https://example.invalid"
        rest._load_markets = AsyncMock()
        adapter = object.__new__(LighterAdapter)
        adapter._rest, adapter._connected = rest, False
        return adapter, rest, signer_transport

    async def test_signer_and_initialized_rest_clients_observed_without_global_changes(self):
        adapter, rest, signer_transport = self.adapter()
        observer = Mock()
        unrelated = SimpleNamespace(request=AsyncMock(return_value="unrelated"))
        created_transport = SimpleNamespace(request=AsyncMock(return_value="account-response"))
        created_client = SimpleNamespace(rest_client=created_transport)
        adapter.set_market_maker_request_observer(observer)
        with patch("core.adapters.exchanges.adapters.lighter_rest.ApiClient", return_value=created_client):
            await rest.initialize()
        self.assertEqual(await signer_transport.request("GET", "https://example.invalid/api/v1/nextNonce"),
                         "signer-response")
        self.assertEqual(await created_transport.request("GET", "https://example.invalid/api/v1/account"),
                         "account-response")
        await unrelated.request("GET", "https://example.invalid/api/v1/account")
        self.assertEqual(observer.call_count, 2)
        self.assertEqual([call.args for call in observer.call_args_list], [
            ("rest", "https://example.invalid/api/v1/nextNonce"),
            ("rest", "https://example.invalid/api/v1/account")])

    async def test_repeated_or_connected_observer_registration_is_rejected(self):
        adapter, _, transport = self.adapter()
        first, second = Mock(), Mock()
        adapter.set_market_maker_request_observer(first)
        with self.assertRaises(ValueError):
            adapter.set_market_maker_request_observer(second)
        await transport.request("GET", "https://example.invalid/api/v1/account")
        first.assert_called_once()
        second.assert_not_called()
        adapter._connected = True
        with self.assertRaises(ValueError):
            adapter.set_market_maker_request_observer(Mock())

    async def test_read_429_retry_and_failed_attempt_are_both_counted(self):
        adapter, rest, transport = self.adapter()
        transport.request.side_effect = [RuntimeError("HTTP 429"), "confirmed"]
        budget = ApiBudget(lambda: 1.0)
        adapter.set_market_maker_request_observer(budget.observe)
        rest._safety_request_depth = 1  # Do not spend a real second on the existing cooldown.
        result = await rest._call_api("account query",
            lambda: transport.request("GET", "https://example.invalid/api/v1/account"))
        self.assertEqual(result, "confirmed")
        self.assertEqual(budget.snapshot()["used"], {"rest": 600})
        self.assertEqual(budget.snapshot()["attempts"], {"rest:account": 2})

    async def test_live_mm_admission_counts_and_stops_after_one_429_attempt(self):
        adapter, rest, transport = self.adapter()
        transport.request.side_effect = [RuntimeError("HTTP 429"), "must not retry"]
        budget = ApiBudget(lambda: 1.0)
        adapter.set_market_maker_request_observer(budget.observe, enforce_admission=True)
        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            await rest._call_api("account query",
                lambda: transport.request("GET", "https://example.invalid/api/v1/account"))
        self.assertEqual(budget.snapshot()["used"], {"rest": 300})
        self.assertEqual(budget.snapshot()["attempts"], {"rest:account": 1})

    async def test_protocol_text_ping_automatic_pong_and_close_are_counted_per_connection(self):
        class FramedSocket(Socket):
            def __init__(self):
                super().__init__()
                self.protocol = Protocol(Side.CLIENT)

            async def send(self, raw):
                self.protocol.send_text(raw.encode())
                await super().send(raw)

            async def close(self):
                if self.protocol.state is State.OPEN:
                    self.protocol.send_close(1000)
                await super().close()

        socket = FramedSocket()
        untouched = Protocol(Side.CLIENT)
        budget = ApiBudget(lambda: 42.5)
        stream = LighterReadStream("wss://example.invalid/stream", 7, 0, lambda: "test-auth",
            connect_factory=AsyncMock(return_value=socket), clock=lambda: 42.5,
            wall_clock=lambda: 1, timeout=0.1, request_observer=budget.observe)
        self.addAsyncCleanup(stream.close)
        await stream.start()
        await stream.request_snapshot("account_orders")
        await stream.request_snapshot("account_orders")
        socket.protocol.send_ping(b"client-keepalive")
        socket.protocol.receive_data(Frame(Opcode.PING, b"server-keepalive").serialize(mask=False))
        untouched.send_ping(b"unrelated")
        await stream.close()
        self.assertEqual(budget.snapshot()["attempts"], {"ws:1": 4, "ws:9": 1, "ws:10": 1, "ws:8": 1})
        self.assertEqual(budget.snapshot()["used"], {"ws": 7})


if __name__ == "__main__":
    unittest.main()

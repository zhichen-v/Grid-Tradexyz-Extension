"""Lighter SDK boundary and WebSocket lifecycle regressions; no account calls."""

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.adapters.exchanges.exceptions import OrderCancellationNotSentError, OrderSubmissionRejectedError
from core.adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket


def _order(
    order_id: str,
    side: OrderSide,
    price: Decimal,
    amount: Decimal,
    status: OrderStatus,
    params: dict | None = None,
) -> OrderData:
    return OrderData(
        id=order_id,
        client_id=f"client-{order_id}",
        symbol="BTC",
        side=side,
        type=OrderType.LIMIT,
        amount=amount,
        price=price,
        filled=Decimal("0"),
        remaining=Decimal("0") if status is OrderStatus.CANCELED else amount,
        cost=Decimal("0"),
        average=None,
        status=status,
        timestamp=datetime.now(timezone.utc),
        updated=None,
        fee=None,
        trades=[],
        params=params or {},
        raw_data=params or {},
    )


class LighterExactFundingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = []
        self.rounds = []
        rest = object.__new__(LighterRest)
        rest.network, rest.account_index, rest.api_key_index = "robinhood", 7, 0
        rest.get_market_index = Mock(return_value=1)
        rest.signer_client = SimpleNamespace(create_auth_token_with_expiry=Mock(
            return_value=("test-auth", None)))
        rest.account_api = SimpleNamespace(
            account_limits=AsyncMock(return_value=SimpleNamespace(code=200,
                current_maker_fee_tick=120, current_taker_fee_tick=350)),
            position_funding=AsyncMock(side_effect=lambda **kw:
                SimpleNamespace(code=200, position_fundings=self.rows)))
        rest.candlestick_api = SimpleNamespace(fundings=AsyncMock(side_effect=lambda **kw:
            SimpleNamespace(code=200, resolution="1h", fundings=self.rounds)))
        async def call_api(operation, factory):
            return await factory()
        rest._call_api = call_api
        self.adapter = object.__new__(LighterAdapter)
        self.adapter._rest = self.rest = rest
        self.adapter._normalize_symbol = Mock(return_value="BTC")

    @staticmethod
    def funding(**changes):
        data = dict(funding_id=67904, timestamp=1788692400, market_id=1,
            change="-0.000384", rate="0.000012", position_size="0.00040",
            position_side="long", discount="0.000000")
        data.update(changes)
        return SimpleNamespace(**data)

    @staticmethod
    def funding_round(**changes):
        data = dict(timestamp=1788692400, value="0.96002280", rate="0.0012", direction="long")
        data.update(changes)
        return SimpleNamespace(**data)

    async def start(self):
        self.adapter.enable_market_maker_exact_funding()
        return await self.adapter.get_account_fee_and_funding("BTC")

    async def test_default_funding_preserves_seconds_and_reported_amount(self):
        self.rows = [self.funding()]
        result = await self.adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(result["fundings"], ({"id": "67904", "timestamp": 1788692400,
            "change": Decimal("-0.000384")},))
        self.rest.candlestick_api.fundings.assert_not_awaited()

    async def test_mm_fee_terms_include_only_sanitized_account_tier(self):
        limits = self.rest.account_api.account_limits.return_value
        limits.user_tier = "premium"
        self.assertNotIn("account_tier", await self.adapter.get_account_fee_and_funding("BTC"))
        self.assertEqual((await self.start())["account_tier"], "premium")
        limits.user_tier = "untrusted-account-payload"
        self.assertIsNone((await self.adapter.get_account_fee_and_funding("BTC"))["account_tier"])

    async def test_baseline_only_normalizes_time_and_never_fetches_public_history(self):
        self.rows = [self.funding()]
        result = await self.start()
        self.assertEqual(result["fundings"], ({"id": "67904", "timestamp": 1788692400000,
            "change": Decimal("-0.000384")},))
        self.adapter.enable_market_maker_exact_funding()
        self.assertEqual(await self.adapter.get_account_fee_and_funding("BTC"), result)
        self.rest.candlestick_api.fundings.assert_not_awaited()

    async def test_new_funding_uses_independent_precise_round_and_caches_only_proven_event(self):
        await self.start()
        self.rows, self.rounds = [self.funding()], [self.funding_round()]
        result = await self.adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(result["fundings"], ({"id": "67904", "timestamp": 1788692400000,
            "change": Decimal("-0.0003840091200")},))
        self.assertEqual(await self.adapter.get_account_fee_and_funding("BTC"), result)
        self.rest.candlestick_api.fundings.assert_awaited_once_with(market_id=1,
            resolution="1h", start_timestamp=1788692400, end_timestamp=1788692401, count_back=100)

    async def test_public_payer_direction_and_authenticated_side_set_signed_amount(self):
        for side, direction, rate, change, expected in (
                ("long", "long", "0.000012", "-0.000384", "-0.00038400912"),
                ("short", "long", "0.000012", "0.000384", "0.00038400912"),
                ("long", "short", "-0.000012", "0.000384", "0.00038400912"),
                ("short", "short", "-0.000012", "-0.000384", "-0.00038400912")):
            with self.subTest(side=side, direction=direction):
                self.setUp()
                await self.start()
                self.rows = [self.funding(position_side=side, rate=rate, change=change)]
                self.rounds = [self.funding_round(direction=direction)]
                result = await self.adapter.get_account_fee_and_funding("BTC")
                self.assertEqual(result["fundings"][0]["change"], Decimal(expected))

    async def test_invalid_or_missing_round_fails_without_committing_new_id(self):
        mutations = [[], [self.funding_round(timestamp=1788692401)],
            [self.funding_round(rate="0.0013")], [self.funding_round(value="0.97002280")],
            [self.funding_round(direction="short")], [self.funding_round(value="NaN")],
            [self.funding_round(), self.funding_round()]]
        for rounds in mutations:
            with self.subTest(rounds=rounds):
                self.setUp()
                await self.start()
                self.rows, self.rounds = [self.funding()], rounds
                with self.assertRaisesRegex(RuntimeError, "^authenticated fee/funding read unavailable$"):
                    await self.adapter.get_account_fee_and_funding("BTC")
                self.rounds = [self.funding_round()]
                result = await self.adapter.get_account_fee_and_funding("BTC")
                self.assertEqual(result["fundings"][0]["change"], Decimal("-0.00038400912"))
                self.assertEqual(self.rest.candlestick_api.fundings.await_count, 2)

    async def test_public_endpoint_failure_does_not_use_rounded_amount(self):
        await self.start()
        self.rows = [self.funding()]
        self.rest.candlestick_api.fundings.side_effect = RuntimeError("test-public-unavailable")
        with self.assertRaisesRegex(RuntimeError, "^authenticated fee/funding read unavailable$"):
            await self.adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(self.adapter._mm_exact_funding[("robinhood", 7, 1)], {})

    async def test_invalid_authenticated_fields_reject_before_public_discovery(self):
        for changes in (dict(position_size="0"), dict(position_side="unknown"),
                        dict(discount="-0.000001"), dict(rate="NaN"),
                        dict(timestamp=1788692400000), dict(market_id=2)):
            with self.subTest(changes=changes):
                self.setUp()
                await self.start()
                self.rows = [self.funding(**changes)]
                with self.assertRaises(RuntimeError):
                    await self.adapter.get_account_fee_and_funding("BTC")
                self.rest.candlestick_api.fundings.assert_not_awaited()

    async def test_immutable_raw_conflict_is_not_hidden_by_precise_cache(self):
        await self.start()
        self.rows, self.rounds = [self.funding()], [self.funding_round()]
        await self.adapter.get_account_fee_and_funding("BTC")
        for changes in (dict(change="-0.000385"), dict(rate="0.000013"),
                        dict(position_size="0.00041"), dict(discount="0.000001")):
            with self.subTest(changes=changes):
                self.rows = [self.funding(**changes)]
                with self.assertRaises(RuntimeError):
                    await self.adapter.get_account_fee_and_funding("BTC")
        self.rest.candlestick_api.fundings.assert_awaited_once()

    async def test_multiple_new_rounds_use_one_bounded_read_and_atomic_cache_commit(self):
        await self.start()
        self.rows = [self.funding(), self.funding(funding_id=67905, timestamp=1788696000)]
        self.rounds = [self.funding_round()]
        with self.assertRaises(RuntimeError):
            await self.adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(self.adapter._mm_exact_funding[("robinhood", 7, 1)], {})
        self.rounds.append(self.funding_round(timestamp=1788696000))
        result = await self.adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(len(result["fundings"]), 2)
        self.assertEqual(self.rest.candlestick_api.fundings.await_count, 2)

    async def test_deferred_discount_is_validated_without_crediting_cash(self):
        await self.start()
        self.rows, self.rounds = [self.funding(discount="0.000020")], [self.funding_round()]
        result = await self.adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(result["fundings"][0]["change"], Decimal("-0.00038400912"))
        self.rows = [self.funding(funding_id=67905, discount="0.000385")]
        with self.assertRaises(RuntimeError):
            await self.adapter.get_account_fee_and_funding("BTC")

    async def test_exit_read_defers_new_precision_without_consuming_its_identity(self):
        self.rows = [self.funding(funding_id=67903, timestamp=1788688800)]
        baseline = await self.start()
        self.rows.append(self.funding())
        result = await self.adapter.get_account_fee_and_funding("BTC", allow_unsettled_funding=True)
        self.assertEqual(result, baseline)
        self.assertNotIn(67904, self.adapter._mm_exact_funding[("robinhood", 7, 1)])
        self.rest.candlestick_api.fundings.assert_not_awaited()
        self.rounds = [self.funding_round()]
        result = await self.adapter.get_account_fee_and_funding("BTC")
        self.assertEqual(result["fundings"][-1]["change"], Decimal("-0.00038400912"))
        self.rest.candlestick_api.fundings.assert_awaited_once()


class LighterRateLimitBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_premium_tier_gate_is_live_mm_only_and_fails_before_funding_read(self):
        for admission, tier, allowed in ((True, "premium", True), (True, "standard", False),
                                        (True, "Premium", False), (True, None, False),
                                        (False, "standard", True), (None, None, True)):
            with self.subTest(admission=admission, tier=tier):
                limits = SimpleNamespace(code=200, current_maker_fee_tick=120,
                                         current_taker_fee_tick=350)
                if tier is not None:
                    limits.user_tier = tier
                rest = object.__new__(LighterRest)
                if admission is not None:
                    rest._mm_budget_admission = admission
                rest.account_index, rest.api_key_index = 7, 0
                rest.get_market_index = Mock(return_value=1)
                rest.signer_client = SimpleNamespace(create_auth_token_with_expiry=Mock(
                    return_value=("test-auth", None)))
                rest.account_api = SimpleNamespace(account_limits=AsyncMock(return_value=limits),
                    position_funding=AsyncMock(return_value=SimpleNamespace(code=200, position_fundings=[])))
                async def call_api(operation, factory):
                    return await factory()
                rest._call_api = call_api
                adapter = object.__new__(LighterAdapter)
                adapter._rest = rest
                adapter._normalize_symbol = Mock(return_value="BTC")
                if allowed:
                    result = await adapter.get_account_fee_and_funding("BTC")
                    self.assertEqual(result, {"maker_fee_rate": Decimal("0.00012"),
                        "taker_fee_rate": Decimal("0.00035"), "fundings": ()})
                    rest.account_api.position_funding.assert_awaited_once()
                else:
                    with self.assertRaisesRegex(RuntimeError, "^authenticated fee/funding read unavailable$"):
                        await adapter.get_account_fee_and_funding("BTC")
                    rest.account_api.position_funding.assert_not_awaited()
                rest.account_api.account_limits.assert_awaited_once()

    @staticmethod
    def _ioc_rest(rows):
        rest = LighterRateLimitBoundaryTests._submission_rest(
            (object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
        rest.account_index = 7
        rest.get_market_index = Mock(return_value=1)
        rest._mm_confirmation_reader = AsyncMock(return_value=[])
        rest.get_order_history = AsyncMock(return_value=rows)
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.signer_client.create_order = AsyncMock(
            return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
        async def call_api(operation, factory, **kwargs):
            return await factory()
        rest._call_api = call_api
        return rest

    @staticmethod
    def _ioc_terminal():
        return replace(_order("987", OrderSide.SELL, Decimal("100.0"), Decimal("0.2"),
                              OrderStatus.FILLED), client_id="1", filled=Decimal("0.2"),
                       remaining=Decimal("0"), raw_data={"order_info": SimpleNamespace(
                           reduce_only=True, time_in_force="immediate-or-cancel",
                           owner_account_index=7, market_index=1)})

    async def test_mm_ioc_returns_exact_terminal_to_order_manager_without_active_or_duplicate_reads(self):
        from core.services.market_maker_v2.config import ExecutionSettings
        from core.services.market_maker_v2.execution_models import DesiredOrder, MarketMetadata
        from core.services.market_maker_v2.order_manager import MarketMakerOrderManager

        terminal = self._ioc_terminal()
        for row in (terminal, replace(terminal, status=OrderStatus.CANCELED, filled=Decimal("0.1")),
                    replace(terminal, status=OrderStatus.CANCELED, filled=Decimal("0"))):
            with self.subTest(status=row.status, filled=row.filled):
                rest = self._ioc_rest([row])
                async def create(symbol, side, kind, amount, price, params):
                    return await rest.place_order(symbol, side.value, kind.value, amount, price, **params)
                adapter = SimpleNamespace(create_order=AsyncMock(side_effect=create),
                    get_open_orders=AsyncMock(return_value=[]), get_order_history=AsyncMock(return_value=[]))
                manager = MarketMakerOrderManager(adapter,
                    ExecutionSettings("BTC", Decimal("0.2"), Decimal("1"), 1, False),
                    MarketMetadata("BTC", 1, 1, Decimal("0.1"), Decimal("0.1"), Decimal("0.1"), Decimal("0")))
                desired = DesiredOrder(OrderSide.SELL, Decimal("100.0"), Decimal("0.2"), True, "exit")
                await manager.execute_active_unwind(desired)
                result = await manager.execute_active_unwind(desired,
                    prepared_generation=manager.active_unwind_prepared_generation)
                self.assertFalse(result.errors)
                self.assertIn("987", manager.terminal_order_ids)
                self.assertFalse(manager.active_unwind_pending)
                rest.signer_client.create_order.assert_awaited_once()
                rest.get_order_history.assert_awaited_once_with("BTC", limit=100)
                rest.get_open_orders.assert_not_awaited()
                rest._mm_confirmation_reader.assert_not_awaited()
                adapter.get_order_history.assert_not_awaited()

    async def test_mm_ioc_missing_or_conflicting_terminal_remains_uncertain_without_resending(self):
        terminal = self._ioc_terminal()
        invalid = [replace(terminal, **changes) for changes in (
            {"client_id": "other"}, {"symbol": "ETH"}, {"side": OrderSide.BUY},
            {"type": OrderType.MARKET}, {"id": "0"}, {"id": str(1 << 60)},
            {"amount": Decimal("0.3")}, {"price": Decimal("100.1")},
            {"filled": Decimal("0.1")}, {"filled": Decimal("NaN")},
            {"remaining": Decimal("0.1")}, {"status": OrderStatus.OPEN},
        )]
        for field, value in (("reduce_only", False), ("time_in_force", "post-only"),
                             ("owner_account_index", 8), ("market_index", 0)):
            source = vars(terminal.raw_data["order_info"]) | {field: value}
            invalid.append(replace(terminal, raw_data={"order_info": SimpleNamespace(**source)}))
        for rows in ([], [terminal, terminal], *([row] for row in invalid)):
            with self.subTest(rows=[(row.id, row.status) for row in rows]):
                rest = self._ioc_rest(rows)
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
                    result = await rest.place_order("BTC", "sell", "limit", Decimal("0.2"),
                        Decimal("100.0"), time_in_force="IOC", reduce_only=True)
                self.assertIsNone(result.id)
                self.assertTrue(rest.get_unresolved_submissions())
                rest.signer_client.create_order.assert_awaited_once()
                self.assertLessEqual(rest.get_order_history.await_count, 10)
                rest.get_open_orders.assert_not_awaited()
                rest._mm_confirmation_reader.assert_not_awaited()

    async def test_ioc_terminal_lookup_is_mm_only_and_read_failure_is_not_retry_safe(self):
        for mm, tif, reducing in ((False, "IOC", True), (True, "POST_ONLY", True), (True, "IOC", False)):
            rest = self._ioc_rest([self._ioc_terminal()])
            if not mm:
                rest._mm_confirmation_reader = None
            rest._query_order_index = AsyncMock(return_value="987")
            result = await rest.place_order("BTC", "sell", "limit", Decimal("0.2"), Decimal("100.0"),
                time_in_force=tif, reduce_only=reducing)
            self.assertIs(result.status, OrderStatus.PENDING)
            rest._query_order_index.assert_awaited_once()
            rest.get_order_history.assert_not_awaited()
        for error in (TimeoutError(), RuntimeError("read unavailable")):
            rest = self._ioc_rest([])
            rest.get_order_history.side_effect = error
            result = await rest.place_order("BTC", "sell", "limit", Decimal("0.2"), Decimal("100.0"),
                time_in_force="IOC", reduce_only=True)
            self.assertIsNone(result.id)
            rest.signer_client.create_order.assert_awaited_once()
            rest.get_order_history.assert_awaited_once()

    async def test_mm_submission_confirmation_uses_owned_reader_and_close_detaches_it(self):
        rest = object.__new__(LighterRest)
        rest.enable_terminal_cancellation_outcomes()
        confirmed = replace(_order("987", OrderSide.BUY, Decimal("100"), Decimal("1"), OrderStatus.OPEN),
                            client_id="42")
        reader = AsyncMock(return_value=[confirmed])
        rest.get_open_orders = AsyncMock(return_value=[])
        rest.get_order_history = AsyncMock(return_value=[])
        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter._read_stream = SimpleNamespace(close=AsyncMock())
        adapter.set_market_maker_confirmation_reader(reader)
        with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
            identifier = await rest._query_order_index("BTC", "buy", Decimal("100"), Decimal("1"),
                                                      client_order_id=42)
        self.assertEqual(identifier, "987")
        reader.assert_awaited_once_with("BTC")
        rest.get_open_orders.assert_not_awaited()
        rest.get_order_history.assert_not_awaited()
        await adapter.close_read_stream()
        self.assertIsNone(rest._mm_confirmation_reader)
        with self.assertRaises(ValueError):
            adapter.set_market_maker_confirmation_reader(reader)

    async def test_mm_maker_confirmation_reaches_both_live_slots_without_another_read(self):
        from test_mm_v2_quote_execution import QuoteExecutionTests
        from core.services.market_maker_v2.domain import ExecutionHealth, ExecutionStatus

        fixture = QuoteExecutionTests()
        fixture.setUp()
        rest = object.__new__(LighterRest)
        rest.account_index = 7
        rest.get_market_index = Mock(return_value=1)
        rest._mm_confirmation_reader = AsyncMock(side_effect=lambda symbol: list(fixture.open.values()))
        rest.get_order_history = AsyncMock(return_value=[])

        async def create(symbol, side, kind, amount, price, params):
            row = await fixture.create(symbol, side, kind, amount, price, params)
            row.client_id = row.id
            row.raw_data = {"order_info": SimpleNamespace(owner_account_index=7, market_index=1,
                reduce_only=params["reduce_only"], time_in_force="post-only")}
            return await rest._handle_order_result(object(), SimpleNamespace(code=200, tx_hash="test-tx"),
                None, symbol, side.value, kind.value, amount, price, client_order_id=int(row.id), **params)

        fixture.adapter.create_order.side_effect = create
        with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
            result = await fixture.port.reconcile_quotes(fixture.proposal)
        self.assertIs(result.status, ExecutionStatus.CONFIRMED)
        self.assertIs(result.snapshot.health, ExecutionHealth.HEALTHY)
        self.assertEqual(result.submitted_count, 2)
        self.assertEqual(len(result.snapshot.orders), 2)
        self.assertEqual(fixture.adapter.create_order.await_count, 2)
        self.assertEqual(rest._mm_confirmation_reader.await_count, 2)
        rest.get_order_history.assert_not_awaited()

    async def test_mm_maker_preserves_partial_and_terminal_exchange_observations(self):
        initial = self._ioc_terminal()
        source = SimpleNamespace(owner_account_index=7, market_index=1, reduce_only=False,
                                 time_in_force="post-only")
        initial = replace(initial, status=OrderStatus.OPEN, filled=Decimal("0"), remaining=Decimal("0.2"),
                          raw_data={"order_info": source})
        for status, filled, remaining in ((OrderStatus.OPEN, "0", "0.2"),
                (OrderStatus.OPEN, "0.1", "0.1"), (OrderStatus.FILLED, "0.2", "0"),
                (OrderStatus.CANCELED, "0.1", "0")):
            with self.subTest(status=status, filled=filled):
                row = replace(initial, status=status, filled=Decimal(filled), remaining=Decimal(remaining))
                rest = self._ioc_rest([row])
                if status is OrderStatus.OPEN:
                    rest._mm_confirmation_reader.return_value = [row]
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
                    result = await rest.place_order("BTC", "sell", "limit", Decimal("0.2"), Decimal("100.0"),
                                                    time_in_force="POST_ONLY", reduce_only=False)
                self.assertEqual((result.id, result.status, result.filled, result.remaining),
                                 (row.id, row.status, row.filled, row.remaining))
                self.assertIs(result.raw_data["order_info"], source)
                self.assertEqual(result.params["time_in_force"], "POST_ONLY")
                self.assertIs(result.params["reduce_only"], False)
                rest.signer_client.create_order.assert_awaited_once()
                self.assertEqual(rest._mm_confirmation_reader.await_count, 1 if status is OrderStatus.OPEN else 3)
                self.assertEqual(rest.get_order_history.await_count, 0 if status is OrderStatus.OPEN else 1)

    async def test_mm_maker_conflicting_or_missing_confirmation_never_resends(self):
        row = replace(self._ioc_terminal(), status=OrderStatus.OPEN, filled=Decimal("0"),
            remaining=Decimal("0.2"), raw_data={"order_info": SimpleNamespace(owner_account_index=7,
                market_index=1, reduce_only=False, time_in_force="post-only")})
        invalid = [replace(row, **changes) for changes in (
            {"client_id": "other"}, {"symbol": "ETH"}, {"side": OrderSide.BUY},
            {"amount": Decimal("0.3")}, {"price": Decimal("100.1")},
            {"filled": Decimal("NaN")}, {"remaining": Decimal("0.1")},
            {"status": OrderStatus.PENDING}, {"id": "0"}, {"type": OrderType.MARKET})]
        for field, value in (("reduce_only", True), ("time_in_force", "good-till-time"),
                             ("owner_account_index", 8), ("market_index", 0)):
            invalid.append(replace(row, raw_data={"order_info": SimpleNamespace(
                **(vars(row.raw_data["order_info"]) | {field: value}))}))
        for rows in ([], [row, row], *([value] for value in invalid)):
            with self.subTest(rows=[(value.id, value.status) for value in rows]):
                rest = self._ioc_rest(rows)
                rest._mm_confirmation_reader.return_value = rows
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
                    result = await rest.place_order("BTC", "sell", "limit", Decimal("0.2"), Decimal("100.0"),
                                                    time_in_force="POST_ONLY", reduce_only=False)
                self.assertIsNone(result.id)
                self.assertTrue(rest.get_unresolved_submissions())
                rest.signer_client.create_order.assert_awaited_once()

    async def test_default_lighter_maker_receipt_and_index_lookup_remain_unchanged(self):
        row = self._ioc_terminal()
        rest = self._ioc_rest([])
        rest._mm_confirmation_reader = None
        rest.get_open_orders.return_value = [row]
        with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
            result = await rest.place_order("BTC", "sell", "limit", Decimal("0.2"), Decimal("100.0"),
                                            time_in_force="POST_ONLY", reduce_only=False)
        self.assertEqual(result.id, row.id)
        self.assertIs(result.status, OrderStatus.PENDING)
        rest.get_open_orders.assert_awaited_once_with("BTC")
        rest.get_order_history.assert_not_awaited()
        rest.signer_client.create_order.assert_awaited_once()

    async def test_mm_cancel_uses_positive_exact_history_without_redundant_active_reads(self):
        canceled = _order("987", OrderSide.BUY, Decimal("100"), Decimal("1"), OrderStatus.CANCELED)
        filled = replace(canceled, status=OrderStatus.FILLED, filled=Decimal("1"))
        impostor = replace(canceled, id="other", client_id="987")
        for rows, confirmed in (([canceled], True), ([filled], False), ([], False),
                                ([impostor], False), ([replace(canceled, symbol="ETH")], False)):
            with self.subTest(rows=[row.id for row in rows], confirmed=confirmed):
                rest = object.__new__(LighterRest)
                rest.enable_terminal_cancellation_outcomes()
                rest._mm_confirmation_reader = AsyncMock(return_value=[])
                rest.get_market_index = Mock(return_value=1)
                rest.signer_client = SimpleNamespace(cancel_order=AsyncMock(
                    return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None)))
                async def call_api(operation, factory, **kwargs):
                    return await factory()
                rest._call_api = call_api
                rest.get_open_orders = AsyncMock(return_value=[])
                rest.get_order_history = AsyncMock(return_value=rows)
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
                    self.assertIs(await rest.cancel_order("BTC", "987"), confirmed)
                    calls = rest.signer_client.cancel_order.await_count
                    if not confirmed:
                        self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
                        self.assertFalse(await rest.cancel_order("BTC", "987"))
                        self.assertEqual(rest.signer_client.cancel_order.await_count, calls)
                rest.get_open_orders.assert_not_awaited()
                if confirmed:
                    rest.get_order_history.assert_awaited_once()
                    self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
                    self.assertIs(rest.get_terminal_cancellation_outcome("BTC", "987"), canceled)
                    self.assertTrue(rest.confirm_terminal_cancellation_outcome("BTC", "987", canceled.status))
                    self.assertEqual(rest.get_unresolved_cancellations(), [])
                if rows == [filled]:
                    self.assertIs(rest.get_terminal_cancellation_outcome("BTC", "987"), filled)

    async def test_mm_exit_cancellation_stays_history_only_after_read_stream_closes(self):
        canceled = _order("987", OrderSide.BUY, Decimal("100"), Decimal("1"), OrderStatus.CANCELED)
        rest = self._ioc_rest([canceled])
        rest.enable_terminal_cancellation_outcomes()
        rest._mm_budget_admission = True
        rest.signer_client.cancel_order = AsyncMock(
            return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter._read_stream = SimpleNamespace(close=AsyncMock())
        await adapter.close_read_stream()
        self.assertIsNone(rest._mm_confirmation_reader)
        self.assertTrue(await rest.cancel_order("BTC", "987"))
        rest.get_open_orders.assert_not_awaited()
        rest.get_order_history.assert_awaited_once_with("BTC")
        rest.signer_client.cancel_order.assert_awaited_once()

    async def test_mm_adapter_preserves_full_cancel_terminal_receipt_for_order_manager(self):
        from core.services.market_maker_v2.config import ExecutionSettings
        from core.services.market_maker_v2.execution_models import (
            DesiredOrder, DesiredQuotes, MarketMetadata, RiskDecision, RuntimeState,
        )
        from core.services.market_maker_v2.order_manager import MarketMakerOrderManager

        for side in (OrderSide.BUY, OrderSide.SELL):
            for status, filled in ((OrderStatus.CANCELED, Decimal("0")),
                                   (OrderStatus.CANCELED, Decimal("0.1")),
                                   (OrderStatus.FILLED, Decimal("0.2")),
                                   (OrderStatus.EXPIRED, Decimal("0")),
                                   (OrderStatus.REJECTED, Decimal("0"))):
                with self.subTest(side=side, status=status, filled=filled):
                    live = _order("987", side, Decimal("99.9") if side is OrderSide.BUY else Decimal("100.1"),
                                  Decimal("0.2"), OrderStatus.OPEN)
                    terminal = replace(live, status=status, filled=filled, remaining=live.amount - filled)
                    rest = self._ioc_rest([terminal])
                    rest.signer_client.cancel_order = AsyncMock(
                        return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
                    adapter = object.__new__(LighterAdapter)
                    adapter._rest = rest
                    adapter._normalize_symbol = Mock(return_value="BTC")
                    adapter.enable_market_maker_cancellation_outcomes()
                    adapter.create_order = AsyncMock(return_value=live)
                    receipts = []
                    original_cancel = adapter.cancel_order

                    async def cancel(*args, **kwargs):
                        receipt = await original_cancel(*args, **kwargs)
                        receipts.append((receipt, rest.get_unresolved_cancellations(),
                            adapter.get_market_maker_cancellation_diagnostics("987", "BTC")))
                        return receipt

                    adapter.cancel_order = cancel
                    manager = MarketMakerOrderManager(adapter,
                        ExecutionSettings("BTC", Decimal("0.2"), Decimal("1"), 1, False),
                        MarketMetadata("BTC", 1, 1, Decimal("0.1"), Decimal("0.1"), Decimal("0.1"), Decimal("0")))
                    order = DesiredOrder(side, live.price, live.amount, False, "test")
                    desired = DesiredQuotes(order if side is OrderSide.BUY else None,
                        order if side is OrderSide.SELL else None, Decimal("100"), Decimal("100"),
                        Decimal("0.1"), Decimal("0"), RuntimeState.ACTIVE, "test")
                    risk = RiskDecision(Decimal("0.2"), Decimal("0.2"), False, False,
                        Decimal("1"), Decimal("1"), Decimal("0.2"), Decimal("-0.2"),
                        Decimal("0"), RuntimeState.ACTIVE, "test", True)
                    created = await manager.reconcile(desired, risk)
                    self.assertFalse(created.errors)

                    result = await manager.cancel_managed_orders("normal exact cancellation")

                    self.assertFalse(result.errors)
                    self.assertEqual(len(receipts), 1)
                    self.assertIs(receipts[0][0], terminal, "MM must receive the full DTO directly")
                    self.assertEqual(receipts[0][1], [("BTC", "987")], "only the consumer confirms the receipt")
                    self.assertEqual(receipts[0][2], dict(submission="acknowledged", stage="unavailable", error_kind="none",
                        history_attempts=1,
                        history_read_errors=0, exact_history_matches=1, captured_terminal=1))
                    self.assertIsNone(adapter.get_market_maker_cancellation_diagnostics("987", "BTC"))
                    self.assertEqual(manager.snapshot(), ())
                    self.assertEqual(manager.terminal_order_ids, frozenset({live.id}))
                    self.assertFalse(manager.has_uncertain_state)
                    self.assertEqual(result.fill_observed, filled > 0)
                    self.assertEqual(result.observed_fill_orders, (terminal,) if filled else ())
                    self.assertEqual(result.actions[0].success, status is not OrderStatus.FILLED)
                    self.assertEqual(rest.get_unresolved_cancellations(), [])
                    rest.signer_client.cancel_order.assert_awaited_once_with(market_index=1, order_index=987)
                    rest.get_order_history.assert_awaited_once_with("BTC")
                    rest.get_open_orders.assert_not_awaited()
                    rest._mm_confirmation_reader.assert_not_awaited()

    async def test_mm_adapter_delayed_cancel_terminal_keeps_identity_until_consumer_confirmation(self):
        terminal = _order("987", OrderSide.SELL, Decimal("100.1"), Decimal("0.2"), OrderStatus.CANCELED)
        for arrives in (True, False):
            with self.subTest(arrives=arrives):
                rest = self._ioc_rest([])
                rest.signer_client.cancel_order = AsyncMock(
                    return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
                adapter = object.__new__(LighterAdapter)
                adapter._rest = rest
                adapter._normalize_symbol = Mock(return_value="BTC")
                adapter.enable_market_maker_cancellation_outcomes()
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
                    first = await adapter.cancel_order("987", "BTC")
                    self.assertIs(first.status, OrderStatus.PENDING)
                    self.assertFalse(first.params["cancel_terminal"])
                    self.assertIsNone(adapter.get_terminal_cancellation_outcome("987", "BTC"))
                    self.assertEqual(rest.get_order_history.await_count, 4)
                    rest.get_order_history.return_value = [terminal] if arrives else []
                    second = await adapter.cancel_order("987", "BTC")
                rest.signer_client.cancel_order.assert_awaited_once()
                self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
                rest.get_open_orders.assert_not_awaited()
                if arrives:
                    self.assertIs(second, terminal)
                    self.assertEqual(rest.get_order_history.await_count, 5)
                    # Re-reading a retained receipt neither resends nor repolls.
                    self.assertIs(await adapter.cancel_order("987", "BTC"), terminal)
                    self.assertEqual(rest.get_order_history.await_count, 5)
                    self.assertTrue(adapter.confirm_terminal_cancellation_outcome(terminal))
                    self.assertEqual(rest.get_unresolved_cancellations(), [])
                    self.assertIsNone(adapter.get_terminal_cancellation_outcome("987", "BTC"))
                else:
                    self.assertIs(second.status, OrderStatus.PENDING)
                    self.assertFalse(second.params["cancel_terminal"])
                    self.assertIsNone(adapter.get_terminal_cancellation_outcome("987", "BTC"))
                    self.assertEqual(rest.get_order_history.await_count, 8)
                rest.signer_client.cancel_order.assert_awaited_once()

    async def test_default_adapter_cancel_keeps_existing_boolean_receipt_contract(self):
        terminal = _order("987", OrderSide.SELL, Decimal("100.1"), Decimal("0.2"), OrderStatus.CANCELED)
        rest = self._ioc_rest([terminal])
        rest.signer_client.cancel_order = AsyncMock(
            return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter._normalize_symbol = Mock(return_value="BTC")
        result = await adapter.cancel_order("987", "BTC")
        self.assertIs(result.status, OrderStatus.CANCELED)
        self.assertTrue(result.params["cancel_terminal"])
        self.assertEqual((result.side, result.amount, result.price, result.client_id),
                         (OrderSide.BUY, Decimal("0"), None, None))
        self.assertEqual(rest.get_unresolved_cancellations(), [])
        self.assertIsNone(adapter.get_terminal_cancellation_outcome("987", "BTC"))
        self.assertIsNone(adapter.get_market_maker_cancellation_diagnostics("987", "BTC"))
        rest.get_open_orders.assert_awaited_once_with("BTC")
        rest.get_order_history.assert_awaited_once_with("BTC")
        rest.signer_client.cancel_order.assert_awaited_once()

    async def test_mm_cancel_diagnostics_distinguish_missing_and_conflicting_terminal_receipts(self):
        live = _order("987", OrderSide.BUY, Decimal("99.9"), Decimal("0.2"), OrderStatus.OPEN)
        conflicting = replace(live, status=OrderStatus.CANCELED, price=Decimal("100.1"))
        for rows in ([], [conflicting]):
            with self.subTest(terminal_visible=bool(rows)):
                rest = self._ioc_rest(rows)
                rest.signer_client.cancel_order = AsyncMock(
                    return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
                adapter = object.__new__(LighterAdapter)
                adapter._rest = rest
                adapter._normalize_symbol = Mock(return_value="BTC")
                adapter.enable_market_maker_cancellation_outcomes()
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
                    receipt = await adapter.cancel_order("987", "BTC")
                diagnostic = adapter.get_market_maker_cancellation_diagnostics("987", "BTC")
                self.assertEqual(diagnostic, dict(submission="acknowledged", stage="unavailable", error_kind="none",
                    history_attempts=1 if rows else 4,
                    history_read_errors=0, exact_history_matches=int(bool(rows)), captured_terminal=int(bool(rows))))
                if rows:
                    self.assertIs(receipt, conflicting)
                    self.assertNotEqual(receipt.price, live.price)
                else:
                    self.assertIs(receipt.status, OrderStatus.PENDING)
                    # Fresh active evidence is not a cancellation rejection or terminal proof.
                    rest.get_open_orders.return_value = [live]
                    self.assertEqual(await adapter.get_open_orders("BTC"), [live])
                    self.assertIsNone(adapter.get_terminal_cancellation_outcome("987", "BTC"))
                self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
                diagnostic["history_attempts"] = 999
                self.assertEqual(adapter.get_market_maker_cancellation_diagnostics("987", "BTC")["history_attempts"],
                                 1 if rows else 4)
                rest.signer_client.cancel_order.assert_awaited_once()

    async def test_mm_cancel_diagnostics_never_store_provider_payloads_and_unproved_errors_stay_uncertain(self):
        secret = "fixture-private-provider-payload"
        success = (object(), SimpleNamespace(code=200, tx_hash="test-tx"), None)
        cases = (
            ("signer_or_provider_error", (None, None, secret), None, True, 4, 0),
            ("signer_or_provider_error", success, TimeoutError(secret), True, 4, 0),
            ("missing_response_code", (object(), None, None), None, True, 4, 0),
            ("missing_transaction_proof", (None, SimpleNamespace(code=200), None), None, True, 4, 0),
            ("response_rejected", (object(), SimpleNamespace(code=400, message=secret), None), None, True, 4, 0),
            ("signer_or_provider_error", success, RuntimeError("HTTP 429 " + secret), True, 4, 0),
            ("acknowledged", success, None, True, 4, 4),
        )
        for category, response, error, pending, attempts, read_errors in cases:
            with self.subTest(category=category, history_errors=read_errors):
                rest = self._ioc_rest([])
                rest.signer_client.cancel_order = AsyncMock(return_value=response, side_effect=error)
                if read_errors:
                    rest.get_order_history.side_effect = RuntimeError(secret)
                adapter = object.__new__(LighterAdapter)
                adapter._rest = rest
                adapter._normalize_symbol = Mock(return_value="BTC")
                adapter.enable_market_maker_cancellation_outcomes()
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()), \
                        patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
                    if pending:
                        self.assertIs((await adapter.cancel_order("987", "BTC")).status, OrderStatus.PENDING)
                    else:
                        with self.assertRaises(Exception):
                            await adapter.cancel_order("987", "BTC")
                kind = "timeout" if isinstance(error, TimeoutError) else (
                    "unknown" if category == "signer_or_provider_error" else "none")
                expected = dict(submission=category, stage="unavailable", error_kind=kind, history_attempts=attempts,
                    history_read_errors=read_errors, exact_history_matches=0, captured_terminal=0)
                if category == "response_rejected":
                    expected["api_code"] = 400
                self.assertEqual(adapter.get_market_maker_cancellation_diagnostics("987", "BTC"), expected)
                self.assertNotIn(secret, repr(rest._mm_cancellation_diagnostics))
                self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")] if pending else [])
                self.assertEqual(rest.get_order_history.await_count, attempts)
                rest.signer_client.cancel_order.assert_awaited_once()

    @staticmethod
    def _real_cancel_sdk(rows=()):
        """Use the installed nonce decorator and send method; native signing and HTTP are fake."""
        from lighter.signer_client import SignerClient
        from lighter.nonce_manager import OptimisticNonceManager
        rest = LighterRateLimitBoundaryTests._ioc_rest(list(rows))
        rest.api_key_index = 0
        rest.base_url = "https://fixture.invalid"
        rest._call_api = LighterRest._call_api.__get__(rest, LighterRest)
        signer = object.__new__(SignerClient)
        signer.account_index = 7
        signer.nonce_manager = OptimisticNonceManager(7, None, [0])
        signer.nonce_manager.nonce[0] = 40
        signer.signer = SimpleNamespace(SignCancelOrder=Mock(return_value=object()))
        signer._SignerClient__decode_tx_info = Mock(return_value=(15, '{"OrderNonce":987}', "fixture-hash", None))
        signer.tx_api = SimpleNamespace(send_tx=AsyncMock(return_value=SimpleNamespace(code=200, tx_hash="fixture-hash")))
        rest.signer_client = signer
        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter._normalize_symbol = Mock(return_value="BTC")
        adapter.enable_market_maker_cancellation_outcomes()
        return adapter, rest, signer

    async def test_mm_sdk_local_sign_error_requires_observed_completed_nonce_rollback(self):
        for rollback in (True, False):
            with self.subTest(rollback=rollback):
                adapter, rest, signer = self._real_cancel_sdk()
                native_sign = signer.sign_cancel_order
                signer._SignerClient__decode_tx_info.return_value = (None, None, None, "fixture-private-sign-error")
                if not rollback:
                    signer.nonce_manager.acknowledge_failure = Mock()
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()), \
                        patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
                    if rollback:
                        with self.assertRaises(OrderCancellationNotSentError) as caught:
                            await adapter.cancel_order("987", "BTC")
                        self.assertEqual((caught.exception.symbol, caught.exception.order_id), ("BTC", "987"))
                        self.assertNotIn("fixture-private", str(caught.exception))
                        self.assertIsNone(caught.exception.__cause__)
                    else:
                        self.assertIs((await adapter.cancel_order("987", "BTC")).status, OrderStatus.PENDING)
                self.assertEqual(signer.nonce_manager.nonce[0], 40 if rollback else 41)
                signer.tx_api.send_tx.assert_not_awaited()
                self.assertEqual(rest.get_order_history.await_count, 0 if rollback else 4)
                self.assertEqual(rest.get_unresolved_cancellations(), [] if rollback else [("BTC", "987")])
                diagnostic = adapter.get_market_maker_cancellation_diagnostics("987", "BTC")
                self.assertEqual((diagnostic["stage"], diagnostic["error_kind"]), ("sign", "local_sign_error"))
                self.assertNotIn("fixture-private", repr(diagnostic))
                self.assertEqual(signer.sign_cancel_order, native_sign)
                self.assertNotIn("send_tx", signer.__dict__)

    async def test_mm_sdk_cancel_dns_never_claims_no_send_even_for_configured_host(self):
        from aiohttp import ClientConnectorDNSError
        for mode in ("configured_host", "wrong_host", "multiple_keys", "redirect_after_post"):
            with self.subTest(mode=mode):
                adapter, rest, signer = self._real_cancel_sdk()
                if mode == "multiple_keys":
                    signer.nonce_manager.api_keys_list = [0, 1]
                    signer.nonce_manager.nonce[1] = 40
                host = "other.invalid" if mode == "wrong_host" else "fixture.invalid"
                dns_error = ClientConnectorDNSError(
                    SimpleNamespace(host=host, port=443, ssl=True), OSError(1, "fixture-private-dns-error"))
                intermediate_posts = []
                if mode == "redirect_after_post":
                    async def redirected_send(*args, **kwargs):
                        # aiohttp can send the initial POST before a 307/308
                        # redirect's DNS failure, even back to the configured host.
                        intermediate_posts.append(1)
                        raise dns_error
                    signer.tx_api.send_tx.side_effect = redirected_send
                else:
                    signer.tx_api.send_tx.side_effect = dns_error
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()), \
                        patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
                    self.assertIs((await adapter.cancel_order("987", "BTC")).status, OrderStatus.PENDING)
                key = 1 if mode == "multiple_keys" else 0
                self.assertEqual(signer.nonce_manager.nonce[key], 41)
                self.assertFalse(signer.nonce_manager.lock(key).locked())
                signer.tx_api.send_tx.assert_awaited_once()
                self.assertEqual(intermediate_posts, [1] if mode == "redirect_after_post" else [])
                self.assertEqual(rest.get_order_history.await_count, 4)
                diagnostic = adapter.get_market_maker_cancellation_diagnostics("987", "BTC")
                self.assertEqual((diagnostic["stage"], diagnostic["error_kind"]), ("send", "dns"))
                self.assertNotIn("fixture-private", repr(diagnostic))

    async def test_mm_cancel_sign_method_override_cannot_supply_no_send_proof(self):
        adapter, rest, signer = self._real_cancel_sdk()
        sends = []
        def overridden_sign(*args, **kwargs):
            # A custom signing hook need not be pure: it can bypass send_tx.
            sends.append(asyncio.create_task(signer.tx_api.send_tx(tx_type=15, tx_info="{}")))
            return None, None, None, "fixture-private-sign-error"
        signer.sign_cancel_order = overridden_sign
        with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()), \
                patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
            self.assertIs((await adapter.cancel_order("987", "BTC")).status, OrderStatus.PENDING)
            await asyncio.gather(*sends)
        self.assertEqual(signer.nonce_manager.nonce[0], 40)
        signer.tx_api.send_tx.assert_awaited_once()
        self.assertEqual(rest.get_order_history.await_count, 4)
        diagnostic = adapter.get_market_maker_cancellation_diagnostics("987", "BTC")
        self.assertEqual((diagnostic["stage"], diagnostic["error_kind"]), ("unavailable", "unknown"))
        self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
        self.assertIs(signer.sign_cancel_order, overridden_sign)

    async def test_mm_create_redirect_dns_and_unproved_429_keep_nonce_and_submission_quarantined(self):
        from aiohttp import ClientConnectorDNSError
        from lighter.exceptions import BadRequestException
        for mode in ("redirect_dns", "timeout_429", "bad_request_429", "returned_error_429"):
            with self.subTest(mode=mode):
                _, rest, signer = self._real_cancel_sdk()
                signer.signer.SignCreateOrder = Mock(return_value=object())
                rest._convert_limit_order_params.return_value = dict(market_index=1, client_order_index=1,
                    base_amount=200, price=1000, is_ask=False, order_type=0, time_in_force=2)
                rest._handle_ambiguous_order_submission = LighterRest._handle_ambiguous_order_submission.__get__(rest, LighterRest)
                intermediate_posts = []
                async def send(*args, **kwargs):
                    intermediate_posts.append(1)
                    if mode == "redirect_dns":
                        raise ClientConnectorDNSError(SimpleNamespace(host="fixture.invalid", port=443, ssl=True),
                            OSError(1, "fixture-private-dns"))
                    if mode == "timeout_429":
                        raise TimeoutError("fixture-private HTTP 429")
                    raise BadRequestException(status=400, reason="fixture-private HTTP 429")
                signer.tx_api.send_tx.side_effect = send
                if mode == "returned_error_429":
                    async def call_api(operation, factory, **kwargs):
                        return await factory()
                    rest._call_api = call_api
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()), \
                        patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
                    order = await rest.place_order("BTC", "buy", "limit", Decimal("0.2"), Decimal("100"),
                        time_in_force="POST_ONLY", _raise_on_definitive_pre_send_failure=True)
                    self.assertIs(order.status, OrderStatus.PENDING)
                    self.assertTrue(order.params["submission_uncertain"])
                    self.assertIsNone(order.id)
                    self.assertEqual(order.client_id, "1")
                    self.assertEqual(await rest.resolve_unresolved_submissions(), [])
                self.assertEqual(signer.nonce_manager.nonce[0],
                                 40 if mode in {"bad_request_429", "returned_error_429"} else 41)
                self.assertEqual(intermediate_posts, [1])
                signer.tx_api.send_tx.assert_awaited_once()
                self.assertEqual(len(rest.get_unresolved_submissions()), 1)
                self.assertNotIn("fixture-private", repr(order.raw_data))
                self.assertNotIn("_raise_on_definitive_pre_send_failure", rest._convert_limit_order_params.call_args.kwargs)

    async def test_mm_sdk_cancel_unproved_and_after_send_failures_reconcile_without_resending(self):
        from lighter.exceptions import BadRequestException
        for mode, stage, kind, sends, nonce in (
                ("before_sign", "before_sign", "unknown", 0, 40),
                ("native_raises", "sign", "unknown", 0, 41),
                ("timeout", "send", "timeout", 1, 41),
                ("parse", "after_send", "response_decode", 1, 41),
                ("bad_request", "send", "http_4xx", 1, 40),
                ("text_429", "send", "unknown", 1, 41)):
            with self.subTest(mode=mode):
                adapter, rest, signer = self._real_cancel_sdk()
                native_sign = signer.sign_cancel_order
                if mode == "before_sign":
                    signer.nonce_manager.async_next_nonce = AsyncMock(side_effect=ValueError("fixture-private"))
                elif mode == "native_raises":
                    signer.signer.SignCancelOrder.side_effect = TypeError("fixture-private HTTP 429")
                elif mode == "timeout":
                    signer.tx_api.send_tx.side_effect = TimeoutError("fixture-private")
                elif mode == "parse":
                    signer._SignerClient__decode_tx_info.return_value = (15, '{malformed-fixture-private', "fixture-hash", None)
                elif mode == "bad_request":
                    signer.tx_api.send_tx.side_effect = BadRequestException(status=400, reason="fixture-private",
                        body='{"code":1234,"message":"fixture-private","token":"fixture-private"}')
                elif mode == "text_429":
                    signer.tx_api.send_tx.side_effect = ValueError("fixture-private HTTP 429")
                with patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()), \
                        patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
                    self.assertIs((await adapter.cancel_order("987", "BTC")).status, OrderStatus.PENDING)
                    self.assertEqual(rest.get_order_history.await_count, 4)
                    # Another request for this same uncertain ID only reads proof.
                    self.assertIs((await adapter.cancel_order("987", "BTC")).status, OrderStatus.PENDING)
                self.assertEqual(rest.get_order_history.await_count, 8)
                self.assertEqual(signer.tx_api.send_tx.await_count, sends)
                self.assertEqual(signer.nonce_manager.nonce[0], nonce)
                diagnostic = adapter.get_market_maker_cancellation_diagnostics("987", "BTC")
                self.assertEqual((diagnostic["stage"], diagnostic["error_kind"]), (stage, kind))
                if mode == "bad_request":
                    self.assertEqual((diagnostic["http_status"], diagnostic["api_code"]), (400, 1234))
                self.assertNotIn("fixture-private", repr(diagnostic))
                self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
                self.assertEqual(signer.sign_cancel_order, native_sign)
                self.assertNotIn("send_tx", signer.__dict__)

    async def test_mm_sdk_after_send_error_tuple_can_return_exact_terminal(self):
        from lighter.exceptions import BadRequestException
        for status, remaining in ((OrderStatus.CANCELED, Decimal("0.1")),
                                  (OrderStatus.FILLED, Decimal("0"))):
            with self.subTest(status=status):
                terminal = replace(_order("987", OrderSide.SELL, Decimal("100"), Decimal("0.2"), status),
                    remaining=remaining, filled=Decimal("0.2") - remaining)
                adapter, rest, signer = self._real_cancel_sdk([terminal])
                signer.tx_api.send_tx.side_effect = BadRequestException(status=400, reason="fixture-private")
                with patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
                    self.assertIs(await adapter.cancel_order("987", "BTC"), terminal)
                self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
                rest.get_order_history.assert_awaited_once()
                signer.tx_api.send_tx.assert_awaited_once()
                self.assertTrue(adapter.confirm_terminal_cancellation_outcome(terminal))
                self.assertEqual(rest.get_unresolved_cancellations(), [])
                self.assertIsNone(adapter.get_market_maker_cancellation_diagnostics("987", "BTC"))

    async def test_mm_cancel_outer_proof_failure_preserves_uncertainty_without_another_loop(self):
        adapter, rest, signer = self._real_cancel_sdk()
        rest._reconcile_cancellation = AsyncMock(side_effect=ValueError("fixture-private HTTP 429"))
        with patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()) as log:
            self.assertIs((await adapter.cancel_order("987", "BTC")).status, OrderStatus.PENDING)
        self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "987")])
        rest._reconcile_cancellation.assert_awaited_once()
        signer.tx_api.send_tx.assert_awaited_once()
        self.assertNotIn("fixture-private", repr(log.mock_calls))

    async def test_mm_sdk_cancel_wrappers_restore_on_task_cancellation_and_pass_other_tasks(self):
        from lighter.signer_client import SignerClient
        adapter, rest, signer = self._real_cancel_sdk()
        native_sign = signer.sign_cancel_order
        class_send = SignerClient.send_tx
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return SimpleNamespace(code=200, tx_hash="fixture-hash")
        signer.tx_api.send_tx.side_effect = send
        pending = asyncio.create_task(adapter.cancel_order("987", "BTC"))
        await asyncio.wait_for(entered.wait(), 1)
        # An unrelated task on the same instance is passed through, not attributed.
        response = await signer.send_tx(15, "{}")
        self.assertEqual(response.code, 200)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(signer.tx_api.send_tx.await_count, 2)
        self.assertEqual(signer.sign_cancel_order, native_sign)
        self.assertNotIn("send_tx", signer.__dict__)
        self.assertIs(SignerClient.send_tx, class_send)
        self.assertFalse(signer.nonce_manager.lock(0).locked())

    async def test_mm_sdk_concurrent_cancel_observations_are_instance_scoped_and_restored(self):
        terminals = [_order(str(i), OrderSide.BUY, Decimal("100"), Decimal("0.2"), OrderStatus.CANCELED)
                     for i in (987, 988)]
        adapter, rest, signer = self._real_cancel_sdk(terminals)
        native_sign = signer.sign_cancel_order
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return SimpleNamespace(code=200, tx_hash="fixture-hash")
        signer.tx_api.send_tx.side_effect = send
        first = asyncio.create_task(adapter.cancel_order("987", "BTC"))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(adapter.cancel_order("988", "BTC"))
        await asyncio.sleep(0)
        self.assertEqual(signer.signer.SignCancelOrder.call_count, 1)
        release.set()
        with patch("core.adapters.exchanges.adapters.lighter_rest.logger", new=Mock()):
            self.assertEqual(await asyncio.gather(first, second), terminals)
        self.assertEqual(signer.tx_api.send_tx.await_count, 2)
        self.assertEqual(signer.nonce_manager.nonce[0], 42)
        for identifier in ("987", "988"):
            diagnostic = adapter.get_market_maker_cancellation_diagnostics(identifier, "BTC")
            self.assertEqual((diagnostic["stage"], diagnostic["error_kind"]), ("complete", "none"))
        self.assertEqual(signer.sign_cancel_order, native_sign)
        self.assertNotIn("send_tx", signer.__dict__)

    async def test_mm_cancel_diagnostic_storage_is_bounded_and_failure_is_isolated(self):
        rest = self._ioc_rest([])
        rest.enable_terminal_cancellation_outcomes()
        for identifier in range(10):
            rest._record_mm_cancellation_diagnostic("BTC", str(identifier), submission="acknowledged")
        self.assertEqual(len(rest._mm_cancellation_diagnostics), 2)
        self.assertIsNone(rest.get_market_maker_cancellation_diagnostics("BTC", "0"))
        terminal = _order("987", OrderSide.SELL, Decimal("100.1"), Decimal("0.2"), OrderStatus.CANCELED)
        rest.get_order_history.return_value = [terminal]
        rest.signer_client.cancel_order = AsyncMock(
            return_value=(object(), SimpleNamespace(code=200, tx_hash="test-tx"), None))
        rest._mm_cancellation_diagnostics = object()  # A broken diagnostic sink cannot affect proof.
        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter._normalize_symbol = Mock(return_value="BTC")
        self.assertIs(await adapter.cancel_order("987", "BTC"), terminal)
        self.assertIsNone(adapter.get_market_maker_cancellation_diagnostics("987", "BTC"))
        self.assertTrue(adapter.confirm_terminal_cancellation_outcome(terminal))
        self.assertEqual(rest.get_unresolved_cancellations(), [])
        rest.signer_client.cancel_order.assert_awaited_once()

    async def test_mm_cancel_confirmation_does_not_cross_exchange_and_client_namespaces(self):
        first = replace(_order("101", OrderSide.BUY, Decimal("99.9"), Decimal("0.2"), OrderStatus.CANCELED),
                        client_id="202")
        second = _order("202", OrderSide.SELL, Decimal("100.1"), Decimal("0.2"), OrderStatus.CANCELED)
        rest = self._ioc_rest([first, second])
        rest.enable_terminal_cancellation_outcomes()
        rest._uncertain_cancellations = {("BTC", "101"), ("BTC", "202")}
        rest._terminal_cancellation_outcomes = {("BTC", "101"): first, ("BTC", "202"): second}
        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter._normalize_symbol = Mock(return_value="BTC")

        self.assertTrue(adapter.confirm_terminal_cancellation_outcome(first))
        self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "202")])
        self.assertIsNone(adapter.get_terminal_cancellation_outcome("101", "BTC"))
        self.assertIs(adapter.get_terminal_cancellation_outcome("202", "BTC"), second)
        for identifier in (None, ""):
            self.assertFalse(adapter.confirm_terminal_cancellation_outcome(replace(first, id=identifier)))
            self.assertEqual(rest.get_unresolved_cancellations(), [("BTC", "202")])
            self.assertIs(adapter.get_terminal_cancellation_outcome("202", "BTC"), second)
        self.assertTrue(adapter.confirm_terminal_cancellation_outcome(second))
        self.assertEqual(rest.get_unresolved_cancellations(), [])
        rest.get_order_history.assert_not_awaited()
        rest.get_open_orders.assert_not_awaited()

    async def test_default_cancel_confirmation_preserves_client_id_fallback(self):
        terminal = replace(_order("101", OrderSide.BUY, Decimal("99.9"), Decimal("0.2"), OrderStatus.CANCELED),
                           id=None, client_id="202")
        rest = self._ioc_rest([])
        rest._uncertain_cancellations = {("BTC", "202")}
        adapter = object.__new__(LighterAdapter)
        adapter._rest = rest
        adapter._normalize_symbol = Mock(return_value="BTC")
        self.assertTrue(adapter.confirm_terminal_cancellation_outcome(terminal))
        self.assertEqual(rest.get_unresolved_cancellations(), [])

    @staticmethod
    def _submission_rest(result) -> LighterRest:
        rest = object.__new__(LighterRest)
        rest._validate_order_preconditions = Mock(return_value=True)
        rest._get_market_info = AsyncMock(
            return_value={"price_decimals": 1}
        )
        rest._validate_order_minimums = Mock()
        rest._next_client_order_index = Mock(return_value=1)
        rest._convert_limit_order_params = Mock(return_value={})
        rest._call_api = AsyncMock(return_value=result)
        rest._handle_ambiguous_order_submission = AsyncMock(
            return_value=SimpleNamespace(id="uncertain")
        )
        rest.signer_client = SimpleNamespace(create_order=Mock())
        return rest

    async def test_invalid_nonce_is_a_sanitized_definitive_rejection(
        self,
    ) -> None:
        rest = self._submission_rest(
            (
                None,
                None,
                "HTTP response body: code=21104 message='invalid nonce' "
                "additional_properties={}",
            )
        )

        with self.assertRaisesRegex(
            OrderSubmissionRejectedError,
            "invalid nonce",
        ) as raised:
            await rest.place_order(
                "BTC",
                "sell",
                "limit",
                Decimal("0.00020"),
                Decimal("77912.4"),
                reduce_only=True,
                time_in_force="POST_ONLY",
                _raise_on_definitive_submission_rejection=True,
            )

        self.assertEqual(
            str(raised.exception),
            "order submission rejected: invalid nonce",
        )
        rest._handle_ambiguous_order_submission.assert_not_awaited()

    async def test_invalid_nonce_rejection_requires_exact_tuple_and_code(
        self,
    ) -> None:
        cases = (
            (
                "default-off",
                None,
                None,
                "HTTP response body: code=21104 message='invalid nonce' "
                "additional_properties={}",
                False,
            ),
            (
                "wrong-code",
                None,
                None,
                "HTTP response body: code=21105 message='invalid nonce' "
                "additional_properties={}",
                True,
            ),
            (
                "wrong-message",
                None,
                None,
                "HTTP response body: code=21104 message='nonce unavailable' "
                "additional_properties={}",
                True,
            ),
            (
                "extra-payload",
                None,
                None,
                "HTTP response body: code=21104 message='invalid nonce' "
                "additional_properties={} extra",
                True,
            ),
        )
        for label, tx, response, error, opt_in in cases:
            with self.subTest(label=label):
                rest = self._submission_rest((tx, response, error))

                result = await rest.place_order(
                    "BTC",
                    "sell",
                    "limit",
                    Decimal("0.00020"),
                    Decimal("77912.4"),
                    reduce_only=True,
                    time_in_force="POST_ONLY",
                    _raise_on_definitive_submission_rejection=opt_in,
                )

                self.assertEqual(result.id, "uncertain")
                rest._handle_ambiguous_order_submission.assert_awaited_once()

    async def test_invalid_order_expiry_without_provenance_remains_ambiguous(
        self,
    ) -> None:
        rest = self._submission_rest((None, None, "OrderExpiry is invalid"))

        result = await rest.place_order(
            "BTC",
            "buy",
            "limit",
            Decimal("0.00020"),
            Decimal("78127.5"),
            reduce_only=True,
            time_in_force="IOC",
            _raise_on_definitive_pre_send_failure=True,
        )

        self.assertEqual(result.id, "uncertain")
        rest._handle_ambiguous_order_submission.assert_awaited_once()

    async def test_fast_fill_order_index_falls_back_to_exact_history(
        self,
    ) -> None:
        rest = object.__new__(LighterRest)
        rest.get_open_orders = AsyncMock(return_value=[])
        filled = replace(
            _order(
                "987",
                OrderSide.SELL,
                Decimal("72444.6"),
                Decimal("0.00020"),
                OrderStatus.FILLED,
            ),
            client_id="42",
            filled=Decimal("0.00020"),
            remaining=Decimal("0"),
        )
        rest.get_order_history = AsyncMock(return_value=[filled])

        with patch(
            "core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep",
            new=AsyncMock(),
        ):
            order_id = await rest._query_order_index(
                "BTC",
                "sell",
                Decimal("72444.6"),
                Decimal("0.00020"),
                client_order_id=42,
                max_retries=3,
                retry_delay=0,
            )

        self.assertEqual(order_id, "987")
        self.assertEqual(rest.get_open_orders.await_count, 3)
        rest.get_order_history.assert_awaited_once_with("BTC", limit=100)

    def test_post_only_maps_to_lighter_sdk_constant(self) -> None:
        import lighter

        rest = object.__new__(LighterRest)
        rest._convert_base_amount = Mock(return_value=2)
        rest._next_client_order_index = Mock(return_value=7)

        params = rest._convert_limit_order_params(
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            Decimal("0.00020"),
            Decimal("65000.0"),
            "buy",
            time_in_force="POST_ONLY",
            reduce_only=False,
        )

        self.assertEqual(
            params["time_in_force"],
            lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
        )
        self.assertEqual(
            params["order_type"], lighter.SignerClient.ORDER_TYPE_LIMIT
        )
        self.assertFalse(params["is_ask"])
        self.assertEqual(params["integrator_account_index"], 0)
        self.assertEqual(params["integrator_taker_fee"], 0)
        self.assertEqual(params["integrator_maker_fee"], 0)

    def test_active_ioc_reduce_only_maps_to_lighter_sdk_constant(self) -> None:
        import lighter

        rest = object.__new__(LighterRest)
        rest._convert_base_amount = Mock(return_value=2)
        rest._next_client_order_index = Mock(return_value=7)

        params = rest._convert_limit_order_params(
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            Decimal("0.00020"),
            Decimal("65000.0"),
            "buy",
            time_in_force="IOC",
            reduce_only=True,
        )

        self.assertEqual(
            params["time_in_force"],
            lighter.SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
        )
        self.assertEqual(
            params["order_expiry"], lighter.SignerClient.DEFAULT_IOC_EXPIRY
        )
        self.assertEqual(
            params["order_type"], lighter.SignerClient.ORDER_TYPE_LIMIT
        )
        self.assertTrue(params["reduce_only"])

        post_only_params = rest._convert_limit_order_params(
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            Decimal("0.00020"),
            Decimal("65000.0"),
            "sell",
            time_in_force="POST_ONLY",
        )
        self.assertNotIn("order_expiry", post_only_params)

    async def test_limit_submission_passes_explicit_zero_integrator_fees(self) -> None:
        import lighter

        rest = object.__new__(LighterRest)
        rest._convert_base_amount = Mock(return_value=2)
        rest._next_client_order_index = Mock(return_value=7)
        rest.signer_client = SimpleNamespace(
            create_order=AsyncMock(return_value=(None, None, None))
        )

        async def call_api(_operation, request, **_kwargs):
            return await request()

        rest._call_api = call_api
        rest._handle_order_result = AsyncMock(return_value=SimpleNamespace(id="1"))

        await rest._execute_limit_order(
            "BTC",
            "buy",
            Decimal("0.00020"),
            Decimal("65000.0"),
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            time_in_force="POST_ONLY",
        )

        submitted = rest.signer_client.create_order.await_args.kwargs
        self.assertEqual(submitted["integrator_account_index"], 0)
        self.assertEqual(submitted["integrator_taker_fee"], 0)
        self.assertEqual(submitted["integrator_maker_fee"], 0)
        self.assertNotIn("order_expiry", submitted)

        await rest._execute_limit_order(
            "BTC",
            "buy",
            Decimal("0.00020"),
            Decimal("65000.0"),
            {
                "price_decimals": 1,
                "price_multiplier": Decimal("10"),
                "market_index": 1,
            },
            time_in_force="IOC",
        )

        submitted = rest.signer_client.create_order.await_args.kwargs
        self.assertEqual(
            submitted["order_expiry"], lighter.SignerClient.DEFAULT_IOC_EXPIRY
        )

    async def test_order_submission_429_is_sanitized_and_propagated(self) -> None:
        rest = object.__new__(LighterRest)
        rest._validate_order_preconditions = Mock(return_value=True)
        rest._get_market_info = AsyncMock(
            return_value={"price_decimals": 1}
        )
        rest._validate_order_minimums = Mock()
        rest._next_client_order_index = Mock(return_value=1)
        rest._convert_limit_order_params = Mock(return_value={})
        rest._call_api = AsyncMock(
            side_effect=RuntimeError("HTTP 429 test-secret-payload")
        )
        rest.signer_client = SimpleNamespace(create_order=Mock())

        with self.assertRaisesRegex(RuntimeError, "HTTP 429") as raised:
            await rest.place_order(
                "BTC",
                "buy",
                "limit",
                Decimal("0.2"),
                Decimal("100"),
            )

        self.assertNotIn("test-secret-payload", str(raised.exception))

    async def test_order_cancellation_429_is_sanitized_and_propagated(self) -> None:
        rest = object.__new__(LighterRest)
        rest.signer_client = SimpleNamespace(cancel_order=Mock())
        rest._uncertain_cancellations = set()
        rest.get_market_index = Mock(return_value=1)
        rest._call_api = AsyncMock(
            side_effect=RuntimeError("HTTP 429 test-secret-payload")
        )

        with self.assertRaisesRegex(RuntimeError, "HTTP 429") as raised:
            await rest.cancel_order("BTC", "1")

        self.assertNotIn("test-secret-payload", str(raised.exception))


class LighterWebSocketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_ws_stop_cancels_connecting_async_client(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class Client:
            ws = None

            @staticmethod
            async def run_async() -> None:
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        websocket = object.__new__(LighterWebSocket)
        websocket.ws_client = Client()
        websocket._ws_task = asyncio.create_task(websocket._run_ws_client())
        websocket._stopping_sdk_ws = False
        websocket._connected = True
        websocket._explicit_stop = True
        await asyncio.wait_for(started.wait(), timeout=0.2)

        await asyncio.wait_for(
            websocket._stop_sdk_ws_client(), timeout=0.2
        )

        self.assertTrue(cancelled.is_set())
        self.assertIsNone(websocket._ws_task)
        self.assertIsNone(websocket.ws_client)

    async def test_sdk_ws_stop_awaits_connected_async_close(self) -> None:
        closed = asyncio.Event()

        class Connection:
            @staticmethod
            async def close() -> None:
                closed.set()

        class Client:
            ws = Connection()

            @staticmethod
            async def run_async() -> None:
                await closed.wait()

        websocket = object.__new__(LighterWebSocket)
        websocket.ws_client = Client()
        websocket._ws_task = asyncio.create_task(websocket._run_ws_client())
        websocket._stopping_sdk_ws = False
        websocket._connected = True
        websocket._explicit_stop = True

        await asyncio.wait_for(
            websocket._stop_sdk_ws_client(), timeout=0.2
        )

        self.assertTrue(closed.is_set())
        self.assertIsNone(websocket._ws_task)
        self.assertIsNone(websocket.ws_client)

    async def test_sdk_ws_stop_cancellation_does_not_orphan_stream(self) -> None:
        close_started = asyncio.Event()
        stream_cancelled = asyncio.Event()

        class Connection:
            @staticmethod
            async def close() -> None:
                close_started.set()
                await asyncio.Future()

        class Client:
            ws = Connection()

            @staticmethod
            async def run_async() -> None:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    stream_cancelled.set()
                    raise

        websocket = object.__new__(LighterWebSocket)
        websocket.ws_client = Client()
        stream_task = asyncio.create_task(websocket._run_ws_client())
        websocket._ws_task = stream_task
        websocket._stopping_sdk_ws = False
        websocket._connected = True
        websocket._explicit_stop = True
        stop_task = asyncio.create_task(websocket._stop_sdk_ws_client())

        try:
            await asyncio.wait_for(close_started.wait(), timeout=0.2)
            stop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task

            self.assertTrue(stream_task.done())
            self.assertTrue(stream_cancelled.is_set())
            self.assertIsNone(websocket._ws_task)
            self.assertIsNone(websocket.ws_client)
        finally:
            if not stream_task.done():
                stream_task.cancel()
                await stream_task



if __name__ == "__main__":
    unittest.main()

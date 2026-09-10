"""Lighter SDK boundary and WebSocket lifecycle regressions; no account calls."""

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.adapters.exchanges.exceptions import OrderSubmissionRejectedError
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

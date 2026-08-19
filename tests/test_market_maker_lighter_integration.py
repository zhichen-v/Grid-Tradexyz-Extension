from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import run_market_maker as entrypoint
from core.adapters.exchanges.models import (
    ExchangeInfo,
    ExchangeType,
    OrderBookData,
    OrderBookLevel,
    OrderData,
    OrderSide,
    OrderStatus,
    OrderType,
)
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.services.market_maker.config import MarketMakerConfig


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


class FakeLighterAdapter:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.create_calls: list[tuple] = []
        self.cancel_calls: list[tuple[str, str | None]] = []
        self.cancel_responses: list[OrderData] = []
        self.active: dict[str, OrderData] = {}
        self.history: list[OrderData] = []
        self._next_id = 0

    async def connect(self) -> bool:
        self.events.append(("connect",))
        return True

    async def authenticate(self) -> bool:
        self.events.append(("authenticate",))
        return True

    async def disconnect(self) -> None:
        self.events.append(("disconnect",))

    async def health_check(self) -> dict[str, bool]:
        self.events.append(("health",))
        return {"healthy": True}

    async def get_exchange_info(self) -> ExchangeInfo:
        self.events.append(("metadata",))
        return ExchangeInfo(
            name="Lighter",
            id="lighter",
            type=ExchangeType.PERPETUAL,
            supported_features=[],
            rate_limits={},
            precision={},
            fees={},
            markets={
                "BTC": {
                    "symbol": "BTC",
                    "price_decimals": 0,
                    "size_decimals": 3,
                    "min_base_amount": "0.001",
                    "min_quote_amount": "0.01",
                }
            },
            status="ok",
            timestamp=datetime.now(timezone.utc),
        )

    async def get_orderbook(self, symbol: str) -> OrderBookData:
        self.events.append(("book", symbol))
        return OrderBookData(
            symbol=symbol,
            bids=[OrderBookLevel(Decimal("99"), Decimal("1"))],
            asks=[OrderBookLevel(Decimal("101"), Decimal("1"))],
            timestamp=datetime.now(timezone.utc),
            nonce=1,
        )

    async def get_positions(self, symbols=None) -> list:
        self.events.append(("positions", tuple(symbols or ())))
        return []

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderData]:
        snapshot = list(self.active.values())
        self.events.append(("open_orders", symbol, tuple(self.active)))
        return snapshot

    async def get_order_history(self, symbol: str | None = None) -> list[OrderData]:
        self.events.append(("order_history", symbol))
        return list(self.history)

    def get_unresolved_submissions(self) -> list:
        return []

    async def resolve_unresolved_submissions(self) -> list:
        self.events.append(("resolve_unresolved",))
        return []

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: Decimal,
        price: Decimal,
        params: dict,
    ) -> OrderData:
        self._next_id += 1
        order_id = str(self._next_id)
        self.create_calls.append((symbol, side, order_type, amount, price, dict(params)))
        self.events.append(("create", order_id, side))
        order = _order(order_id, side, price, amount, OrderStatus.OPEN, dict(params))
        self.active[order_id] = order
        return order

    async def cancel_order(
        self, order_id: str, symbol: str | None = None
    ) -> OrderData:
        self.cancel_calls.append((order_id, symbol))
        self.events.append(("cancel", order_id, symbol))
        live = self.active.pop(order_id)
        result = _order(
            order_id,
            live.side,
            live.price or Decimal("0"),
            live.amount,
            OrderStatus.CANCELED,
            {"cancel_terminal": True},
        )
        self.cancel_responses.append(result)
        self.history.append(result)
        return result

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        self.events.append(("cancel_all", symbol))
        self.active.clear()

    async def subscribe_orderbook(self, symbol: str, callback) -> None:
        self.events.append(("subscribe_orderbook", symbol, callback))
        await callback(await self.get_orderbook(symbol))

    async def subscribe_user_data(self, callback) -> None:
        self.events.append(("subscribe_user", callback))

    async def subscribe_positions(self, callback) -> None:
        self.events.append(("subscribe_positions", callback))

    async def unsubscribe(self) -> None:
        self.events.append(("unsubscribe",))


class MarketMakerLighterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def config(self, *, dry_run: bool) -> MarketMakerConfig:
        return MarketMakerConfig(
            symbol="BTC",
            order_size=Decimal("0.001"),
            min_profit_buffer_bps=Decimal("0"),
            max_position=Decimal("0.010"),
            min_order_lifetime_ms=1,
            dry_run=dry_run,
        )

    async def run_fake(self, *, dry_run: bool):
        adapter = FakeLighterAdapter()
        factory_in_loop = False

        def factory(settings):
            nonlocal factory_in_loop
            asyncio.get_running_loop()
            factory_in_loop = True
            self.assertEqual(settings["network"], "robinhood_testnet")
            return adapter

        stop = asyncio.Event()
        stop.set()
        coordinator = await entrypoint.run_market_maker(
            self.config(dry_run=dry_run),
            {
                "network": "robinhood_testnet",
                "testnet": True,
                "api_key_private_key": "test-only",
                "account_index": 1,
                "api_key_index": 2,
                "expected_l1_address": None,
            },
            adapter_factory=factory,
            stop_event=stop,
        )
        return adapter, coordinator, factory_in_loop

    async def test_live_fake_covers_lifecycle_and_exact_order_contract(self) -> None:
        adapter, coordinator, factory_in_loop = await self.run_fake(dry_run=False)

        self.assertTrue(factory_in_loop)
        labels = [event[0] for event in adapter.events]
        for expected in (
            "connect",
            "authenticate",
            "metadata",
            "health",
            "book",
            "positions",
            "subscribe_orderbook",
            "subscribe_user",
            "subscribe_positions",
            "unsubscribe",
            "disconnect",
        ):
            self.assertIn(expected, labels)
        self.assertEqual(coordinator.position_snapshot.signed_size, Decimal("0"))

        self.assertEqual(len(adapter.create_calls), 2)
        self.assertEqual(
            {call[1] for call in adapter.create_calls},
            {OrderSide.BUY, OrderSide.SELL},
        )
        for symbol, _, order_type, amount, _, params in adapter.create_calls:
            self.assertEqual(symbol, "BTC")
            self.assertIs(order_type, OrderType.LIMIT)
            self.assertEqual(amount, Decimal("0.001"))
            self.assertEqual(
                params,
                {"time_in_force": "POST_ONLY", "reduce_only": False},
            )

        created_ids = {str(index) for index in range(1, 3)}
        self.assertEqual({order_id for order_id, _ in adapter.cancel_calls}, created_ids)
        self.assertTrue(
            all(response.status is OrderStatus.CANCELED for response in adapter.cancel_responses)
        )
        self.assertTrue(
            all(response.params["cancel_terminal"] for response in adapter.cancel_responses)
        )
        self.assertEqual(adapter.active, {})
        self.assertEqual(adapter.events[-2:], [("unsubscribe",), ("disconnect",)])
        last_cancel = max(
            index
            for index, event in enumerate(adapter.events)
            if event[0] == "cancel"
        )
        final_empty_query = max(
            index
            for index, event in enumerate(adapter.events)
            if event[0] == "open_orders" and not event[2]
        )
        unsubscribe = next(
            index
            for index, event in enumerate(adapter.events)
            if event[0] == "unsubscribe"
        )
        self.assertLess(last_cancel, final_empty_query)
        self.assertLess(final_empty_query, unsubscribe)

    async def test_dry_run_performs_zero_exchange_mutations(self) -> None:
        adapter, _, _ = await self.run_fake(dry_run=True)

        self.assertEqual(adapter.create_calls, [])
        self.assertEqual(adapter.cancel_calls, [])
        self.assertNotIn("cancel_all", [event[0] for event in adapter.events])
        self.assertEqual(adapter.events[-2:], [("unsubscribe",), ("disconnect",)])


class LighterRateLimitBoundaryTests(unittest.IsolatedAsyncioTestCase):
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


class CliAndWalletProfileTests(unittest.TestCase):
    def test_cli_accepts_wallet_alias_and_single_shortcut(self) -> None:
        self.assertEqual(
            entrypoint.parse_cli(["mm.yaml", "--walletname", "desk"]).wallet_name,
            "desk",
        )
        self.assertEqual(entrypoint.parse_cli(["mm.yaml", "--desk"]).wallet_name, "desk")

    def test_profile_shortcuts_are_not_parsed_as_abbreviated_flags(self) -> None:
        abbreviated_debug = entrypoint.parse_cli(["mm.yaml", "--deb"])
        abbreviated_dry_run = entrypoint.parse_cli(["mm.yaml", "--dry"])
        exact_flags = entrypoint.parse_cli(
            ["mm.yaml", "--debug", "--dry-run"]
        )

        self.assertEqual(abbreviated_debug.wallet_name, "deb")
        self.assertFalse(abbreviated_debug.debug)
        self.assertEqual(abbreviated_dry_run.wallet_name, "dry")
        self.assertFalse(abbreviated_dry_run.dry_run)
        self.assertTrue(exact_flags.debug)
        self.assertTrue(exact_flags.dry_run)

    def test_cli_rejects_shortcut_with_explicit_wallet(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            entrypoint.parse_cli(
                ["mm.yaml", "--wallet-name", "desk", "--another"]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_profile_names_reject_path_traversal(self) -> None:
        for invalid in ("", "../desk", "desk/name", ".desk", "a" * 65):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                entrypoint._validate_profile_name(invalid)

    def test_named_profile_is_complete_and_does_not_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "desk.env").write_text(
                "\n".join(
                    (
                        "LIGHTER_API_KEY_PRIVATE_KEY='profile-private-key'",
                        "LIGHTER_ACCOUNT_INDEX=7",
                        "LIGHTER_API_KEY_INDEX=3",
                        "LIGHTER_NETWORK=robinhood_testnet",
                        "LIGHTER_EXPECTED_L1_ADDRESS=0x1111111111111111111111111111111111111111",
                    )
                ),
                encoding="utf-8",
            )
            with patch.object(
                entrypoint, "load_settings", side_effect=AssertionError("fallback used")
            ):
                settings = entrypoint.load_lighter_settings(
                    "desk", profiles_dir=directory
                )

        self.assertEqual(settings["network"], "robinhood_testnet")
        self.assertTrue(settings["testnet"])
        self.assertEqual(settings["account_index"], 7)
        self.assertEqual(settings["api_key_index"], 3)
        self.assertEqual(settings["api_key_private_key"], "profile-private-key")

    def test_profile_errors_never_echo_private_key(self) -> None:
        secret = "never-print-this-private-key"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "broken.env").write_text(
                f"LIGHTER_API_KEY_PRIVATE_KEY={secret}\n",
                encoding="utf-8",
            )
            with self.assertRaises(entrypoint.WalletProfileError) as raised:
                entrypoint.load_wallet_profile("broken", profiles_dir=directory)
        self.assertNotIn(secret, str(raised.exception))

    def test_default_adapter_disables_credential_file_loading(self) -> None:
        settings = {
            "network": "robinhood_testnet",
            "testnet": True,
            "api_key_private_key": "test-only",
            "account_index": 1,
            "api_key_index": 2,
        }
        with patch(
            "core.adapters.exchanges.adapters.lighter.LighterAdapter"
        ) as adapter_class:
            entrypoint.build_adapter(settings)
        config = adapter_class.call_args.args[0]
        self.assertIs(config.extra_params["load_credentials_from_file"], False)

    def test_adapter_does_not_read_default_yaml_when_loading_is_disabled(
        self,
    ) -> None:
        from core.adapters.exchanges.adapters.lighter import LighterAdapter

        settings = {
            "network": "robinhood_testnet",
            "testnet": True,
            "api_key_private_key": "profile-only-test-key",
            "account_index": 1,
            "api_key_index": 2,
        }
        rest = SimpleNamespace(
            signer_client=None,
            base_url="https://example.invalid",
            ws_url="wss://example.invalid",
        )
        with (
            patch.object(
                LighterAdapter,
                "_load_lighter_config",
                side_effect=AssertionError("default YAML must not be read"),
            ) as load_config,
            patch(
                "core.adapters.exchanges.adapters.lighter.LighterRest",
                return_value=rest,
            ),
            patch("core.adapters.exchanges.adapters.lighter.LighterWebSocket"),
            patch.object(
                LighterAdapter, "_get_symbol_cache_service", return_value=None
            ),
            patch(
                "core.adapters.exchanges.adapters.lighter.create_subscription_manager",
                return_value=SimpleNamespace(),
            ) as create_subscriptions,
        ):
            entrypoint.build_adapter(settings)

        load_config.assert_not_called()
        self.assertEqual(
            create_subscriptions.call_args.kwargs["exchange_config"],
            {
                "exchange_id": "lighter",
                "subscription_mode": {
                    "mode": "predefined",
                    "predefined": {"symbols": [], "data_types": {}},
                },
            },
        )

    def test_log_redaction_covers_messages_and_tracebacks(self) -> None:
        secret = "traceback-secret-private-key"
        stream = io.StringIO()
        logger = logging.getLogger("market-maker-redaction-test")
        previous_handlers = list(logger.handlers)
        previous_propagate = logger.propagate
        logger.handlers = [logging.StreamHandler(stream)]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        previous_factory = entrypoint._install_log_redaction(
            {"api_key_private_key": secret}
        )
        try:
            try:
                raise RuntimeError(secret)
            except RuntimeError:
                logger.exception("adapter failed with %s", secret)
        finally:
            logging.setLogRecordFactory(previous_factory)
            logger.handlers = previous_handlers
            logger.propagate = previous_propagate

        output = stream.getvalue()
        self.assertNotIn(secret, output)
        self.assertIn("<redacted>", output)

    def test_operator_keyboard_interrupt_is_a_clean_exit(self) -> None:
        logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)

        async def runtime(*_args, **_kwargs):
            return None

        def interrupt(awaitable):
            awaitable.close()
            raise KeyboardInterrupt

        with (
            patch.object(
                entrypoint,
                "load_market_maker_config",
                return_value=MarketMakerConfig(),
            ),
            patch.object(
                entrypoint,
                "load_lighter_settings",
                return_value={"api_key_private_key": "test-only"},
            ),
        ):
            exit_code = entrypoint.main(
                ["config.yaml"],
                run=interrupt,
                runtime=runtime,
                logger_factory=lambda _debug: logger,
            )

        self.assertEqual(exit_code, 0)

    def test_main_forces_dry_run_and_redacts_runtime_errors(self) -> None:
        secret = "runtime-secret-private-key"
        captured = {}

        class Logger:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def info(self, message: str, **kwargs) -> None:
                self.messages.append(message)

            def warning(self, message: str, **kwargs) -> None:
                self.messages.append(message)

            def error(self, message: str, **kwargs) -> None:
                self.messages.append(message)

        logger = Logger()

        async def failing_runtime(config, settings, **kwargs):
            captured["config"] = config
            raise RuntimeError(f"adapter failed with {settings['api_key_private_key']}")

        live_config = MarketMakerConfig(dry_run=False)
        settings = {
            "network": "robinhood",
            "testnet": False,
            "api_key_private_key": secret,
            "account_index": 1,
            "api_key_index": 2,
        }
        stderr = io.StringIO()
        with (
            patch.object(entrypoint, "load_market_maker_config", return_value=live_config),
            patch.object(entrypoint, "load_lighter_settings", return_value=settings),
            redirect_stderr(stderr),
        ):
            code = entrypoint.main(
                ["ignored.yaml", "--dry-run"],
                runtime=failing_runtime,
                logger_factory=lambda debug: logger,
            )

        self.assertEqual(code, 1)
        self.assertTrue(captured["config"].dry_run)
        combined = stderr.getvalue() + "\n".join(logger.messages)
        self.assertIn("<redacted>", combined)
        self.assertNotIn(secret, combined)

    def test_main_preserves_config_mode_and_uses_injected_run_seams(self) -> None:
        captured = {}

        class Logger:
            def info(self, message: str, **kwargs) -> None:
                pass

            def warning(self, message: str, **kwargs) -> None:
                pass

            def error(self, message: str, **kwargs) -> None:
                pass

        async def successful_runtime(config, settings, **kwargs):
            captured["config"] = config
            captured["adapter_factory"] = kwargs["adapter_factory"]

        def factory(settings):
            return None
        runner_calls = 0

        def runner(awaitable):
            nonlocal runner_calls
            runner_calls += 1
            return asyncio.run(awaitable)

        live_config = MarketMakerConfig(dry_run=False)
        with (
            patch.object(entrypoint, "load_market_maker_config", return_value=live_config),
            patch.object(entrypoint, "load_lighter_settings", return_value={}),
        ):
            code = entrypoint.main(
                ["ignored.yaml"],
                adapter_factory=factory,
                run=runner,
                runtime=successful_runtime,
                logger_factory=lambda debug: Logger(),
            )

        self.assertEqual(code, 0)
        self.assertFalse(captured["config"].dry_run)
        self.assertIs(captured["adapter_factory"], factory)
        self.assertEqual(runner_calls, 1)


if __name__ == "__main__":
    unittest.main()

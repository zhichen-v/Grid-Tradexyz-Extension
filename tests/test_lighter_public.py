"""Read-only Lighter adapter smoke test; no credentials or orders required."""

import argparse
import asyncio
from decimal import Decimal
from datetime import datetime
from importlib.metadata import version
from inspect import signature
from types import SimpleNamespace

import lighter
from lighter.endpoint_profiles import ENDPOINT_PROFILES, get_endpoint_profile

from core.adapters.exchanges.factory import ExchangeFactory
from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket
from core.adapters.exchanges.interface import ExchangeConfig
from core.adapters.exchanges.models import ExchangeType


async def check_client_lifecycle() -> None:
    created = 0
    closed = 0
    signer_kwargs = None
    real_signer = lighter.SignerClient
    robinhood_profile = get_endpoint_profile("robinhood")

    class FakeSigner:
        def __init__(self, **kwargs):
            nonlocal created, signer_kwargs
            created += 1
            signer_kwargs = kwargs

        async def close(self):
            nonlocal closed
            closed += 1

    signed_config = ExchangeConfig(
        exchange_id="lighter",
        name="Lighter",
        exchange_type=ExchangeType.PERPETUAL,
        api_key="",
        api_secret="",
        testnet=False,
        base_url="https://lighter-proxy.example",
        ws_url="",
        extra_params={"network": "robinhood"},
    )
    signed_config.api_key_private_key = "test-only"
    lighter.SignerClient = FakeSigner
    try:
        signed_client = LighterAdapter(signed_config)
        assert signed_client._rest.signer_client is signed_client._websocket.signer_client
        assert signed_client._base is signed_client._rest
        assert signer_kwargs["chain_id"] == robinhood_profile.chain_id
        assert signer_kwargs["url"] == "https://lighter-proxy.example"
        assert signed_client._websocket.ws_url == robinhood_profile.ws_url
        await signed_client.disconnect()
    finally:
        lighter.SignerClient = real_signer
    assert created == 1 and closed == 1

    blank_override_client = LighterWebSocket({
        "network": "robinhood",
        "ws_url": "",
    })
    assert blank_override_client.ws_url == robinhood_profile.ws_url

    started = asyncio.Event()
    stopped = asyncio.Event()

    class FakeConnection:
        async def close(self):
            stopped.set()

    class BlockingWsClient:
        def __init__(self):
            self.ws = FakeConnection()

        async def run_async(self):
            started.set()
            await stopped.wait()

    ws_client = LighterWebSocket({"testnet": False})
    await ws_client.connect()
    ws_client.ws_client = BlockingWsClient()
    ws_client._ws_task = asyncio.create_task(ws_client._run_ws_client())
    await asyncio.wait_for(started.wait(), timeout=2)
    await ws_client.disconnect()
    assert stopped.is_set() and ws_client._ws_task is None

    order_fill_subscription_started = False

    async def mark_order_fill_subscription():
        nonlocal order_fill_subscription_started
        order_fill_subscription_started = True

    ws_client._subscribe_account_all_orders = mark_order_fill_subscription
    await ws_client.subscribe_order_fills(lambda order: None)
    assert order_fill_subscription_started


async def check_collateral_currency() -> None:
    class FakeAccountApi:
        async def account(self, **kwargs):
            return SimpleNamespace(
                code=200,
                accounts=[SimpleNamespace(
                    available_balance="90",
                    collateral="100",
                )],
            )

    for network, expected_currency in (
        ("mainnet", "USDC"),
        ("robinhood", "USDG"),
    ):
        rest = LighterRest({"network": network})
        rest.signer_client = object()
        rest.account_api = FakeAccountApi()
        balances = await rest.get_account_balance()
        assert balances and balances[0].currency == expected_currency
        assert balances[0].free == Decimal("90")
        assert balances[0].used == Decimal("10")


async def main(network: str = "mainnet", symbol: str | None = None) -> None:
    assert "api_private_keys" in signature(lighter.SignerClient).parameters
    assert "authorization" in signature(
        lighter.OrderApi.account_active_orders
    ).parameters
    await check_client_lifecycle()
    await check_collateral_currency()

    profile = get_endpoint_profile(network)
    symbol = (symbol or "ETH").upper()

    config = ExchangeConfig(
        exchange_id="lighter",
        name="Lighter",
        exchange_type=ExchangeType.PERPETUAL,
        api_key="",
        api_secret="",
        testnet=profile.name.endswith("testnet"),
        extra_params={
            "network": profile.name,
            "load_credentials_from_file": False,
        },
    )
    client = ExchangeFactory().create_adapter("lighter", config)

    try:
        assert client._rest.signer_client is None
        assert client._rest.base_url == profile.api_url
        assert client._websocket.ws_url == profile.ws_url
        assert await client.connect()

        robinhood_ticker = None
        if profile.name == "robinhood":
            assert client._rest.get_market_index("PLTR") is not None
            robinhood_ticker = await client.get_ticker("PLTR-USD")
            assert robinhood_ticker and robinhood_ticker.last > 0

        market_id = client._rest.get_market_index(f"{symbol}-USD")
        assert market_id is not None
        assert market_id == client._rest.get_market_index(f"{symbol}_USDC_PERP")

        ticker = await client.get_ticker(f"{symbol}-USD")
        assert ticker and ticker.last and ticker.last > 0
        assert ticker.bid and ticker.ask and ticker.bid <= ticker.ask

        orderbook = await client.get_orderbook(f"{symbol}-USD", limit=3)
        assert orderbook and orderbook.bids and orderbook.asks

        ws_orderbook = None
        orderbook_update = asyncio.Event()

        async def on_orderbook(update) -> None:
            nonlocal ws_orderbook
            ws_orderbook = update
            orderbook_update.set()

        await client.subscribe_orderbook(f"{symbol}-USD", on_orderbook)
        await asyncio.wait_for(orderbook_update.wait(), timeout=15)
        assert ws_orderbook and ws_orderbook.bids and ws_orderbook.asks

        recent_trades = await client.get_recent_trades(f"{symbol}-USD", limit=2)
        assert recent_trades and recent_trades[0].order_id
        assert isinstance(recent_trades[0].timestamp, datetime)

        market_info = await client._rest._get_market_info(symbol)
        assert market_info is not None
        limit_params = client._rest._convert_limit_order_params(
            market_info,
            quantity=Decimal("0.005"),
            price=ticker.last,
            side="buy",
            client_order_id=1,
        )
        assert limit_params["base_amount"] == int(
            Decimal("0.005") * market_info["size_multiplier"]
        )
        try:
            client._rest._convert_base_amount(
                market_info, Decimal(1) / market_info["size_multiplier"] / 10
            )
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported size precision was silently truncated")

        ws_ticker = None
        ws_update = asyncio.Event()

        async def on_ticker(update) -> None:
            nonlocal ws_ticker
            ws_ticker = update
            ws_update.set()

        await client.subscribe_ticker(f"{symbol}-USD", on_ticker)
        await asyncio.wait_for(ws_update.wait(), timeout=15)
        assert ws_ticker and ws_ticker.last and ws_ticker.last > 0
        assert ws_ticker.bid and ws_ticker.ask and ws_ticker.bid <= ws_ticker.ask

        ws_trade = None
        trade_update = asyncio.Event()

        async def on_trade(update) -> None:
            nonlocal ws_trade
            ws_trade = update
            trade_update.set()

        await client.subscribe_trades(f"{symbol}-USD", on_trade)
        await asyncio.wait_for(trade_update.wait(), timeout=15)
        assert ws_trade and ws_trade.price > 0 and ws_trade.amount > 0
        assert isinstance(ws_trade.timestamp, datetime)

        parsed_order = client._websocket._parse_order_from_direct_ws({
            "market_index": market_id,
            "order_index": 1,
            "client_order_index": 2,
            "initial_base_amount": "0.1",
            "remaining_base_amount": "0.1",
            "filled_base_amount": "0",
            "filled_quote_amount": "0",
            "price": "100",
            "is_ask": False,
            "status": "canceled-post-only",
            "type": "stop-loss",
            "timestamp": 0,
        })
        assert parsed_order.status.value == "canceled"
        assert parsed_order.type.value == "stop"

        parsed_positions = client._websocket._parse_positions({
            str(market_id): {
                "position": "0.1",
                "avg_entry_price": "100",
                "unrealized_pnl": "1",
                "realized_pnl": "0",
                "liquidation_price": "50",
                "initial_margin_fraction": "2.00",
                "margin_mode": 0,
                "allocated_margin": "2",
            }
        })
        assert parsed_positions and parsed_positions[0].leverage == 50

        await client._websocket.unsubscribe_ticker(symbol)
        await client._websocket.unsubscribe_trades(symbol)
        await client._websocket.unsubscribe_orderbook(symbol)
        assert market_id not in client._websocket._subscribed_market_stats
        assert market_id not in client._websocket._subscribed_trades
        assert market_id not in client._websocket._subscribed_markets

        print(f"lighter-sdk={version('lighter-sdk')}")
        print(
            f"network={profile.name} {symbol} market_id={market_id} REST={ticker.last} "
            f"WS={ws_ticker.last} bid={ws_ticker.bid} ask={ws_ticker.ask}"
        )
        print(
            f"depth={len(orderbook.bids)}x{len(orderbook.asks)} "
            f"price_decimals={market_info['price_decimals']} "
            f"size_decimals={market_info['size_decimals']}"
        )
        print(
            f"recent_trade={recent_trades[0].price} "
            f"ws_trade={ws_trade.price} side={ws_trade.side.value}"
        )
        if robinhood_ticker:
            print(f"Robinhood market check: PLTR={robinhood_ticker.last}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        choices=sorted(ENDPOINT_PROFILES),
        default="mainnet",
    )
    parser.add_argument("--symbol")
    args = parser.parse_args()
    asyncio.run(main(network=args.network, symbol=args.symbol))

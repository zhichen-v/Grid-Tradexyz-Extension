"""Opt-in live BTC post-only place/cancel smoke test for Lighter Robinhood."""

import argparse
import asyncio
import sys
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any

from lighter_preflight import DEFAULT_CONFIG, build_adapter, load_settings

SYMBOL = "BTC"


def _step(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-decimals)


def _round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def build_passive_buy_plan(
    best_bids: list[Decimal],
    market_info: dict[str, Any],
    max_notional: Decimal,
    price_ratio: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return price, amount and notional for one minimum-size passive BTC bid."""
    if not best_bids or any(not bid.is_finite() or bid <= 0 for bid in best_bids):
        raise ValueError("best bids must be finite positive values")
    if not Decimal("0") < price_ratio < Decimal("1"):
        raise ValueError("price ratio must be between 0 and 1")
    if not max_notional.is_finite() or max_notional <= 0:
        raise ValueError("max notional must be a finite positive value")

    tick = _step(int(market_info["price_decimals"]))
    size_step = _step(int(market_info["size_decimals"]))
    price = _round_to_step(min(best_bids) * price_ratio, tick, ROUND_FLOOR)
    if price <= 0:
        raise ValueError("calculated limit price is not positive")

    min_base = Decimal(str(market_info["min_base_amount"]))
    min_quote = Decimal(str(market_info["min_quote_amount"]))
    amount = _round_to_step(max(min_base, min_quote / price), size_step, ROUND_CEILING)
    notional = price * amount
    if notional < min_quote or amount < min_base:
        raise ValueError("calculated order does not meet Lighter market minimums")
    if notional > max_notional:
        raise ValueError(
            f"minimum legal order notional {notional} exceeds cap {max_notional}"
        )
    return price, amount, notional


async def _read_book(adapter: Any) -> tuple[Decimal, Decimal]:
    book = await adapter.get_orderbook(SYMBOL, limit=20)
    if not book or not book.best_bid or not book.best_ask:
        raise RuntimeError("BTC order book has no best bid/ask")
    bid, ask = book.best_bid.price, book.best_ask.price
    if (
        not bid.is_finite()
        or not ask.is_finite()
        or bid <= 0
        or ask <= 0
        or bid >= ask
    ):
        raise RuntimeError(f"invalid BTC order book: bid={bid}, ask={ask}")
    return bid, ask


def _matching_orders(orders: list[Any], client_id: int) -> list[Any]:
    client_id_text = str(client_id)
    return [
        order
        for order in orders
        if str(getattr(order, "client_id", "")) == client_id_text
    ]


async def _wait_for_test_order(
    adapter: Any,
    client_id: int,
    timeout: float,
) -> Any | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        orders = await adapter.get_open_orders(SYMBOL)
        matches = _matching_orders(orders, client_id)
        if len(matches) > 1:
            raise RuntimeError("multiple active orders matched the unique smoke-test ID")
        if matches:
            return matches[0]
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(1.0)


async def _wait_until_absent(
    adapter: Any,
    client_id: int,
    timeout: float,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    consecutive_empty = 0
    while True:
        orders = await adapter.get_open_orders(SYMBOL)
        matches = _matching_orders(orders, client_id)
        consecutive_empty = consecutive_empty + 1 if not matches else 0
        if consecutive_empty >= 2:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(1.0)


async def run_live_smoke(
    settings: dict[str, Any],
    *,
    max_notional: Decimal = Decimal("15"),
    price_ratio: Decimal = Decimal("0.50"),
    samples: int = 3,
    sample_interval: float = 1.0,
) -> tuple[Decimal, Decimal, Decimal]:
    """Submit exactly one passive order, cancel it, and verify no BTC exposure."""
    if settings["network"] != "robinhood":
        raise ValueError("live order smoke test requires api_config.network=robinhood")
    if samples < 2:
        raise ValueError("at least two order-book samples are required")

    adapter = build_adapter(settings)
    attempted = False
    verified_removed = False
    client_id = int(time.time_ns() // 1_000_000)
    price = amount = notional = Decimal("0")

    try:
        if not await adapter.connect() or not await adapter.authenticate():
            raise RuntimeError("Lighter connection or signer authentication failed")
        rest = adapter._rest
        if rest.network != "robinhood" or rest.chain_id != 466324:
            raise RuntimeError("adapter is not connected to Lighter Robinhood mainnet")
        expected_wallet = settings.get("expected_l1_address")
        if not expected_wallet:
            raise RuntimeError("expected_l1_address is required for a live order test")
        account_response = await rest.account_api.account(
            by="index", value=str(rest.account_index)
        )
        rest._require_success_response(account_response, "live smoke account query")
        accounts = getattr(account_response, "accounts", None) or []
        returned_wallet = str(getattr(accounts[0], "l1_address", "")).lower() if accounts else ""
        if returned_wallet != expected_wallet:
            raise RuntimeError("configured account index belongs to a different L1 wallet")

        positions = await adapter.get_positions([SYMBOL])
        if positions:
            raise RuntimeError("BTC position must be zero before the smoke test")
        baseline_orders = await adapter.get_open_orders(SYMBOL)
        if baseline_orders:
            raise RuntimeError("BTC active orders must be empty before the smoke test")

        balances = await adapter.get_balances()
        usdg = next((balance for balance in balances if balance.currency == "USDG"), None)
        if usdg is None or Decimal(str(usdg.free)) < max_notional:
            raise RuntimeError("insufficient free USDG for the capped smoke test")

        bids = []
        for index in range(samples):
            bid, ask = await _read_book(adapter)
            bids.append(bid)
            print(f"price_sample={index + 1}/{samples} bid={bid} ask={ask}")
            if index + 1 < samples:
                await asyncio.sleep(sample_interval)

        market_info = await rest._get_market_info(SYMBOL)
        if not market_info:
            raise RuntimeError("BTC market metadata is unavailable")
        price, amount, notional = build_passive_buy_plan(
            bids, market_info, max_notional, price_ratio
        )

        final_bid, final_ask = await _read_book(adapter)
        if price >= final_bid:
            raise RuntimeError("planned price is no longer strictly below the best bid")
        print(
            f"plan=BUY {amount} BTC @ {price} POST_ONLY "
            f"notional={notional} final_bid={final_bid} final_ask={final_ask}"
        )

        attempted = True
        await rest.place_order(
            SYMBOL,
            "buy",
            "limit",
            amount,
            price,
            skip_order_index_query=True,
            client_order_id=client_id,
            time_in_force="POST_ONLY",
            reduce_only=False,
        )

        order = await _wait_for_test_order(adapter, client_id, timeout=12)
        if order is None:
            raise RuntimeError(
                "submitted order was not found in active orders; it was not resubmitted"
            )
        canonical_id = str(order.id)
        print(f"open_order=PASS client_id={client_id} order_index={canonical_id}")

        if not await rest.cancel_order(SYMBOL, canonical_id):
            raise RuntimeError(f"cancel transaction was rejected for order {canonical_id}")
        verified_removed = await _wait_until_absent(
            adapter, client_id, timeout=10
        )
        if not verified_removed:
            raise RuntimeError(f"order {canonical_id} is still active after cancellation")
        if await adapter.get_positions([SYMBOL]):
            raise RuntimeError("BTC position changed during the post-only smoke test")

        print(f"cancel_order=PASS order_index={canonical_id}")
        print("position_check=PASS BTC position remains zero")
        return price, amount, notional
    finally:
        cleanup_error = None
        if attempted and not verified_removed:
            try:
                order = await _wait_for_test_order(adapter, client_id, timeout=12)
                if order is not None:
                    canonical_id = str(order.id)
                    await adapter._rest.cancel_order(SYMBOL, canonical_id)
                    if not await _wait_until_absent(
                        adapter, client_id, timeout=10
                    ):
                        cleanup_error = RuntimeError(
                            f"MANUAL ACTION REQUIRED: BTC order {canonical_id} may remain active"
                        )
            except Exception as exc:
                cleanup_error = RuntimeError(
                    "MANUAL ACTION REQUIRED: unable to prove the BTC test order is absent"
                )
                cleanup_error.__cause__ = exc
        if attempted:
            try:
                if await adapter.get_open_orders(SYMBOL):
                    cleanup_error = RuntimeError(
                        "MANUAL ACTION REQUIRED: unable to prove BTC active orders are empty"
                    )
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = RuntimeError(
                        "MANUAL ACTION REQUIRED: unable to prove BTC active orders are empty"
                    )
                    cleanup_error.__cause__ = exc
            try:
                if await adapter.get_positions([SYMBOL]):
                    cleanup_error = RuntimeError(
                        "MANUAL ACTION REQUIRED: BTC position changed during the smoke test"
                    )
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = RuntimeError(
                        "MANUAL ACTION REQUIRED: unable to prove the BTC position is zero"
                    )
                    cleanup_error.__cause__ = exc
        await adapter.disconnect()
        if cleanup_error:
            raise cleanup_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--confirm-live-order",
        action="store_true",
        help="Required opt-in: submit one real Robinhood BTC post-only order",
    )
    parser.add_argument("--max-notional", type=Decimal, default=Decimal("15"))
    parser.add_argument("--price-ratio", type=Decimal, default=Decimal("0.50"))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_live_order:
        print("FAIL: pass --confirm-live-order to authorize the one-order live test")
        return 2
    try:
        settings = load_settings(args.config)
        asyncio.run(
            run_live_smoke(
                settings,
                max_notional=args.max_notional,
                price_ratio=args.price_ratio,
                samples=args.samples,
                sample_interval=args.sample_interval,
            )
        )
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

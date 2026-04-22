"""
Order verification utilities.

Provides reusable helpers for validating order cancellation, order existence,
and other exchange-side order state checks.
"""

import asyncio
from typing import Callable

from ....logging import get_logger
from ..models import GridOrderSide


class OrderVerificationUtils:
    """
    Reusable order-verification helper methods.

    Responsibilities:
    1. Verify that orders have been canceled.
    2. Verify that orders exist on the exchange.
    3. Support batch order-cancel validation.
    4. Provide reusable exchange verification logic.
    """

    def __init__(self, exchange, symbol: str):
        """
        Initialize the verification utilities.

        Args:
            exchange: Exchange adapter.
            symbol: Trading symbol.
        """
        self.logger = get_logger(__name__)
        self.exchange = exchange
        self.symbol = symbol

    async def verify_no_orders_by_filter(
        self,
        order_filter: Callable,
        filter_description: str,
        max_retries: int = 3
    ) -> bool:
        """
        Verify that no exchange open orders match the given filter.

        Args:
            order_filter: Filter callback. Orders returning `True` are treated
                as orders that should no longer exist.
            filter_description: Human-readable description used in logs.
            max_retries: Maximum verification attempts.

        Returns:
            True if no matching orders remain, otherwise False.
        """
        for retry in range(max_retries):
            try:
                exchange_orders = await self.exchange.get_open_orders(
                    symbol=self.symbol
                )

                filtered_orders = [
                    order for order in exchange_orders
                    if order_filter(order)
                ]

                if len(filtered_orders) == 0:
                    self.logger.info(
                        f"Verification passed: exchange confirms no {filter_description}"
                    )
                    return True

                self.logger.warning(
                    f"Verification failed (attempt {retry + 1}/{max_retries}): "
                    f"exchange still has {len(filtered_orders)} {filter_description}"
                )
                for order in filtered_orders:
                    self.logger.warning(
                        f"   Remaining order: {order.id}, price=${order.price}"
                    )

                if retry < max_retries - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                self.logger.error(f"Failed to verify {filter_description}: {e}")
                if retry < max_retries - 1:
                    await asyncio.sleep(0.5)

        return False

    async def verify_no_sell_orders(self, max_retries: int = 3) -> bool:
        """
        Verify that no sell orders remain.

        Useful for long-grid scalping flows that should only keep buy orders.

        Args:
            max_retries: Maximum verification attempts.

        Returns:
            True if no sell orders remain, otherwise False.
        """
        return await self.verify_no_orders_by_filter(
            order_filter=lambda order: order.side == GridOrderSide.SELL,
            filter_description="sell orders",
            max_retries=max_retries
        )

    async def verify_no_buy_orders(self, max_retries: int = 3) -> bool:
        """
        Verify that no buy orders remain.

        Useful for short-grid scalping flows that should only keep sell orders.

        Args:
            max_retries: Maximum verification attempts.

        Returns:
            True if no buy orders remain, otherwise False.
        """
        return await self.verify_no_orders_by_filter(
            order_filter=lambda order: order.side == GridOrderSide.BUY,
            filter_description="buy orders",
            max_retries=max_retries
        )

    async def verify_all_orders_cancelled(self, max_retries: int = 3) -> bool:
        """
        Verify that all exchange open orders have been canceled.

        Args:
            max_retries: Maximum verification attempts.

        Returns:
            True if no open orders remain, otherwise False.
        """
        for retry in range(max_retries):
            try:
                exchange_orders = await self.exchange.get_open_orders(
                    symbol=self.symbol
                )

                if len(exchange_orders) == 0:
                    self.logger.info("Verification passed: exchange confirms no open orders")
                    return True

                self.logger.warning(
                    f"Verification failed (attempt {retry + 1}/{max_retries}): "
                    f"exchange still has {len(exchange_orders)} open orders"
                )

                if retry < max_retries - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                self.logger.error(f"Failed to verify order cancellation: {e}")
                if retry < max_retries - 1:
                    await asyncio.sleep(0.5)

        return False

    async def verify_order_exists(
        self,
        expected_order_id: str,
        max_retries: int = 3
    ) -> bool:
        """
        Verify that an expected order exists on the exchange.

        Args:
            expected_order_id: Expected order ID.
            max_retries: Maximum verification attempts.

        Returns:
            True if the order is found, otherwise False.
        """
        for retry in range(max_retries):
            try:
                exchange_orders = await self.exchange.get_open_orders(
                    symbol=self.symbol
                )

                found = False
                for order in exchange_orders:
                    if order.id == expected_order_id:
                        found = True
                        self.logger.info(
                            f"Verification passed: order exists "
                            f"{order.side.value} {order.amount}@${order.price}"
                        )
                        break

                if found:
                    return True

                self.logger.warning(
                    f"Verification failed (attempt {retry + 1}/{max_retries}): "
                    f"exchange did not return order {expected_order_id}"
                )

                if retry < max_retries - 1:
                    await asyncio.sleep(0.5)

            except Exception as e:
                self.logger.error(f"Failed to verify order existence: {e}")
                if retry < max_retries - 1:
                    await asyncio.sleep(0.5)

        return False

    async def get_open_orders_count(self) -> int:
        """
        Return the current number of open orders.

        Returns:
            Open order count, or `-1` on failure.
        """
        try:
            open_orders = await self.exchange.get_open_orders(self.symbol)
            return len(open_orders)
        except Exception as e:
            self.logger.error(f"Failed to get open order count: {e}")
            return -1

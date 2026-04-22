"""
Grid strategy implementation.

This module builds the initial grid order layout and calculates reverse orders
for:
- long grid mode
- short grid mode
- follow and martingale variants
"""

from typing import List, Tuple
from decimal import Decimal
from datetime import datetime

from ....logging import get_logger
from ..interfaces.grid_strategy import IGridStrategy
from ..models import (
    GridConfig, GridOrder, GridOrderSide, GridOrderStatus,
    GridType
)


class GridStrategyImpl(IGridStrategy):
    """
    Grid strategy implementation.

    Core behavior:
    1. Build the initial grid order set from the configured price range.
    2. Calculate reverse orders after fills.
    3. A filled buy order places a sell order above the fill.
    4. A filled sell order places a buy order below the fill.
    """

    def __init__(self):
        self.logger = get_logger(__name__)
        self.config: GridConfig = None
        self.grid_prices: List[Decimal] = []

    def initialize(self, config: GridConfig, current_price: Decimal = None) -> List[GridOrder]:
        """
        Initialize the strategy and build the initial order set.

        In long grid mode, only buy orders below the current price are placed.
        In short grid mode, only sell orders above the current price are placed.

        Args:
            config: Grid configuration.
            current_price: Current market price. When set, orders that would
                cross the market are skipped to avoid taker execution.

        Returns:
            Initial grid orders.
        """
        self.config = config
        self.current_price = current_price
        self.grid_prices = self._calculate_grid_prices()

        # In follow mode the range is dynamic, so log the grid shape only.
        if config.is_follow_mode():
            self.logger.info(
                f"Initialized {config.grid_type.value}: "
                f"dynamic range, interval={config.grid_interval}, "
                f"grids={config.grid_count}"
            )
        else:
            self.logger.info(
                f"Initialized {config.grid_type.value}: "
                f"range=[{config.lower_price}, {config.upper_price}], "
                f"interval={config.grid_interval}, grids={config.grid_count}"
            )

        all_orders = self._create_all_initial_orders()

        self.logger.info(f"Built {len(all_orders)} initial grid orders")

        return all_orders

    def _calculate_grid_prices(self) -> List[Decimal]:
        """
        Calculate all configured grid prices.

        Returns:
            Price list ordered by grid ID.
        """
        prices = []
        for grid_id in range(1, self.config.grid_count + 1):
            price = self.config.get_grid_price(grid_id)
            prices.append(price)

        return prices

    def _create_all_initial_orders(self) -> List[GridOrder]:
        """
        Create all initial grid orders.

        In long grid mode, initial orders are buys.
        In short grid mode, initial orders are sells.

        Returns:
            Initial grid orders.
        """
        all_orders = []
        skipped_count = 0

        if self.config.grid_type in [GridType.LONG, GridType.MARTINGALE_LONG, GridType.FOLLOW_LONG]:
            # In long mode, place only buy orders below the live price.
            for grid_id in range(1, self.config.grid_count + 1):
                price = self.config.get_grid_price(grid_id)

                # Skip orders that would cross the market and execute as takers.
                if self.current_price is not None and price >= self.current_price:
                    skipped_count += 1
                    self.logger.debug(
                        f"Skip Grid {grid_id} buy @{price}: "
                        f"current_price={self.current_price}, "
                        f"would cross the market and act as taker"
                    )
                    continue

                amount = self.config.get_formatted_grid_order_amount(grid_id)

                order = GridOrder(
                    order_id="",
                    grid_id=grid_id,
                    side=GridOrderSide.BUY,
                    price=price,
                    amount=amount,
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now()
                )
                all_orders.append(order)

            if all_orders:
                self.logger.info(
                    f"Long grid created {len(all_orders)} initial buy orders, "
                    f"price range ${all_orders[0].price:,.2f} - ${all_orders[-1].price:,.2f}"
                    + (
                        f", skipped {skipped_count} market-crossing levels"
                        if skipped_count > 0 else ""
                    )
                )
            else:
                self.logger.info(
                    f"Long grid created no initial orders below current price "
                    f"${self.current_price:,.2f}; skipped {skipped_count} levels"
                )

        else:  # SHORT, MARTINGALE_SHORT, FOLLOW_SHORT
            # In short mode, place only sell orders above the live price.
            for grid_id in range(1, self.config.grid_count + 1):
                price = self.config.get_grid_price(grid_id)

                # Skip orders that would cross the market and execute as takers.
                if self.current_price is not None and price <= self.current_price:
                    skipped_count += 1
                    self.logger.debug(
                        f"Skip Grid {grid_id} sell @{price}: "
                        f"current_price={self.current_price}, "
                        f"would cross the market and act as taker"
                    )
                    continue

                amount = self.config.get_formatted_grid_order_amount(grid_id)

                order = GridOrder(
                    order_id="",
                    grid_id=grid_id,
                    side=GridOrderSide.SELL,
                    price=price,
                    amount=amount,
                    status=GridOrderStatus.PENDING,
                    created_at=datetime.now()
                )
                all_orders.append(order)

            if all_orders:
                self.logger.info(
                    f"Short grid created {len(all_orders)} initial sell orders, "
                    f"price range ${all_orders[0].price:,.2f} - ${all_orders[-1].price:,.2f}"
                    + (
                        f", skipped {skipped_count} market-crossing levels"
                        if skipped_count > 0 else ""
                    )
                )
            else:
                self.logger.info(
                    f"Short grid created no initial orders above current price "
                    f"${self.current_price:,.2f}; skipped {skipped_count} levels"
                )

        return all_orders

    def calculate_reverse_order(
        self,
        filled_order: GridOrder,
        grid_interval: Decimal,
        distance: int = 1
    ) -> Tuple[GridOrderSide, Decimal, int]:
        """
        Calculate the reverse order parameters.

        Rules:
        - A filled buy order places a sell order N grids above.
        - A filled sell order places a buy order N grids below.

        Args:
            filled_order: Filled order.
            grid_interval: Grid interval.
            distance: Reverse-order grid distance. Default is 1.

        Returns:
            (side, price, grid_id)
        """
        if filled_order.is_buy_order():
            # Buy fill -> place sell above.
            new_side = GridOrderSide.SELL
            # Use the configured order price as the base grid anchor so the
            # reverse order stays aligned to the intended grid layout.
            new_price = filled_order.price + (grid_interval * distance)
            # Keep the same logical grid id so state remains paired to the fill.
            new_grid_id = filled_order.grid_id

            self.logger.debug(
                f"Buy fill (order_price={filled_order.price}, "
                f"filled_price={filled_order.filled_price}), "
                f"reverse sell -> {new_price} "
                f"(distance={distance}, delta={grid_interval * distance})"
            )
        else:
            # Sell fill -> place buy below.
            new_side = GridOrderSide.BUY
            new_price = filled_order.price - (grid_interval * distance)
            new_grid_id = filled_order.grid_id

            self.logger.debug(
                f"Sell fill (order_price={filled_order.price}, "
                f"filled_price={filled_order.filled_price}), "
                f"reverse buy -> {new_price} "
                f"(distance={distance}, delta={grid_interval * distance})"
            )

        return (new_side, new_price, new_grid_id)

    def calculate_batch_reverse_orders(
        self,
        filled_orders: List[GridOrder],
        grid_interval: Decimal,
        distance: int = 1
    ) -> List[Tuple[GridOrderSide, Decimal, int, Decimal]]:
        """
        Calculate reverse orders for a batch of fills.

        Each filled order produces one reverse order entry.

        Args:
            filled_orders: Filled orders.
            grid_interval: Grid interval.
            distance: Reverse-order grid distance. Default is 1.

        Returns:
            [(side, price, grid_id, amount), ...]
        """
        reverse_orders = []

        for order in filled_orders:
            side, price, grid_id = self.calculate_reverse_order(
                order, grid_interval, distance)
            # Reuse the actual filled amount when available.
            amount = order.filled_amount or order.amount
            reverse_orders.append((side, price, grid_id, amount))

        self.logger.info(
            f"Processed {len(filled_orders)} fills into "
            f"{len(reverse_orders)} reverse orders with distance={distance}"
        )

        return reverse_orders

    def get_grid_prices(self) -> List[Decimal]:
        """Return all configured grid prices."""
        return self.grid_prices.copy()

    def validate_price_range(self, current_price: Decimal) -> bool:
        """
        Validate whether the current price is inside the configured range.

        Args:
            current_price: Current market price.

        Returns:
            Whether the price is in range.
        """
        in_range = self.config.is_price_in_range(current_price)

        if not in_range:
            self.logger.warning(
                f"Current price {current_price} is outside grid range "
                f"[{self.config.lower_price}, {self.config.upper_price}]"
            )

        return in_range

    def get_grid_id_by_price(self, price: Decimal) -> int:
        """
        Map a price to its grid ID.

        Args:
            price: Price.

        Returns:
            Grid ID.
        """
        return self.config.get_grid_index_by_price(price)

    def __repr__(self) -> str:
        if self.config:
            return (
                f"GridStrategy({self.config.grid_type.value}, "
                f"{self.config.grid_count} grids)"
            )
        return "GridStrategy(not initialized)"

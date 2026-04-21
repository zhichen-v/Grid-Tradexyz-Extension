"""
Grid trading terminal UI.

This module uses Rich to render a live monitoring dashboard.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ...logging import get_logger, restore_console_log_level, set_console_log_level
from .coordinator import GridCoordinator
from .models import GridStatistics, GridType
from .models.grid_order import GridOrderSide, GridOrderStatus


class GridTerminalUI:
    """
    Rich-based terminal dashboard for the grid trading system.

    The UI presents runtime status, order statistics, position information,
    profit and loss, and recent trades.
    """

    DISPLAY_TIMEZONE = timezone(timedelta(hours=8))

    def __init__(self, coordinator: GridCoordinator):
        """Initialize the terminal UI."""
        self.logger = get_logger(__name__)
        self.coordinator = coordinator
        self.console = Console()
        self.live_console_level = "WARNING"
        self.refresh_rate = 2
        self.history_limit = 10
        self._running = False

        symbol = self.coordinator.config.symbol
        self.base_currency = symbol.split("_")[0] if "_" in symbol else symbol

    @staticmethod
    def _append_field(
        content: Text,
        label: str,
        value: str,
        *,
        label_style: str = "white",
        value_style: str = "white",
    ) -> None:
        """Append an aligned key-value row to a Rich text block."""
        content.append(f"{label:<20}", style=label_style)
        content.append(value, style=value_style)
        content.append("\n")

    @staticmethod
    def _format_grid_mode(grid_type: GridType) -> str:
        """Return a human-readable grid mode string."""
        mapping = {
            GridType.LONG: "Long Grid (Standard)",
            GridType.SHORT: "Short Grid (Standard)",
            GridType.MARTINGALE_LONG: "Long Grid (Martingale)",
            GridType.MARTINGALE_SHORT: "Short Grid (Martingale)",
            GridType.FOLLOW_LONG: "Long Grid (Follow)",
            GridType.FOLLOW_SHORT: "Short Grid (Follow)",
        }
        return mapping.get(grid_type, grid_type.value)

    @staticmethod
    def _format_order_range(grid_ids: list[int]) -> str:
        """Format a list of grid ids into a compact range string."""
        if not grid_ids:
            return "None"
        min_grid = min(grid_ids)
        max_grid = max(grid_ids)
        return f"Grid {min_grid}" if min_grid == max_grid else f"Grid {min_grid}-{max_grid}"

    def create_header(self, stats: GridStatistics) -> Panel:
        """Create the header panel."""
        title = Text()
        title.append("Grid Trading Monitor", style="bold cyan")
        title.append("  |  ", style="white")
        title.append(
            f"{self.coordinator.config.exchange.upper()}/{self.coordinator.config.symbol}",
            style="bold yellow",
        )
        title.append("  |  ", style="white")
        title.append(
            self._format_grid_mode(self.coordinator.config.grid_type),
            style="bold green",
        )
        title.append("  |  ", style="white")
        title.append(f"Price ${stats.current_price:,.2f}", style="bold magenta")
        return Panel(title, style="bold white on blue")

    def create_status_panel(self, stats: GridStatistics) -> Panel:
        """Create the runtime status panel."""
        content = Text()
        running_time = str(stats.running_time).split(".")[0]

        self._append_field(
            content,
            "Grid mode",
            self._format_grid_mode(self.coordinator.config.grid_type),
            value_style="bold cyan",
        )
        self._append_field(
            content,
            "Status",
            self.coordinator.get_status_text(),
            value_style="bold green",
        )
        self._append_field(
            content,
            "Price range",
            f"${stats.price_range[0]:,.2f} - ${stats.price_range[1]:,.2f}",
        )
        self._append_field(content, "Grid interval", f"{stats.grid_interval}", value_style="cyan")
        self._append_field(
            content,
            "Reverse distance",
            f"{self.coordinator.config.reverse_order_grid_distance} grids",
            value_style="magenta",
        )
        self._append_field(
            content,
            "Order amount",
            f"{self.coordinator.config.order_amount} {self.base_currency}",
            value_style="bold cyan",
        )
        self._append_field(
            content,
            "Precision",
            f"{self.coordinator.config.quantity_precision} dp",
        )
        self._append_field(
            content,
            "Current price",
            f"${stats.current_price:,.2f}",
            value_style="bold yellow",
        )
        self._append_field(
            content,
            "Current grid",
            f"Grid {stats.current_grid_id}/{stats.grid_count}",
        )

        if self.coordinator.config.martingale_increment and self.coordinator.config.martingale_increment > 0:
            self._append_field(
                content,
                "Martingale",
                f"Enabled | +{self.coordinator.config.martingale_increment} {self.base_currency}",
                value_style="bold green",
            )

        if self.coordinator.config.scalping_enabled:
            scalping_active = bool(
                self.coordinator.scalping_manager and self.coordinator.scalping_manager.is_active()
            )
            trigger_grid = self.coordinator.config.get_scalping_trigger_grid()
            trigger_price = self.coordinator.config.get_grid_price(trigger_grid)
            self._append_field(
                content,
                "Scalping",
                f"{'Active' if scalping_active else 'Pending'} | triggers {stats.scalping_trigger_count}",
                value_style="bold red" if scalping_active else "bold cyan",
            )
            self._append_field(
                content,
                "Scalp trigger",
                f"Grid {trigger_grid} @ ${trigger_price:,.4f}",
            )

        if self.coordinator.config.capital_protection_enabled:
            self._append_field(
                content,
                "Capital protect",
                f"{'Triggered' if stats.capital_protection_active else 'Pending'} | triggers {stats.capital_protection_trigger_count}",
                value_style="bold green" if stats.capital_protection_active else "bold cyan",
            )

        if stats.take_profit_enabled:
            if stats.take_profit_active:
                tp_text = f"Triggered | count {stats.take_profit_trigger_count}"
                tp_style = "bold red"
            else:
                tp_text = (
                    f"Pending | current {float(stats.take_profit_profit_rate):+.2f}% | "
                    f"threshold {float(stats.take_profit_threshold):.2f}%"
                )
                tp_style = "bold cyan"
            self._append_field(content, "Take profit", tp_text, value_style=tp_style)

        if stats.price_lock_enabled:
            lock_text = (
                "Active (locked)"
                if stats.price_lock_active
                else f"Pending | threshold ${stats.price_lock_threshold:,.2f}"
            )
            lock_style = "bold yellow" if stats.price_lock_active else "bold cyan"
            self._append_field(content, "Price lock", lock_text, value_style=lock_style)

        if stats.price_escape_active:
            direction_text = "DOWN" if stats.price_escape_direction == "down" else "UP"
            self._append_field(
                content,
                "Price escape",
                f"{direction_text} | {stats.price_escape_remaining}s left | triggers {stats.price_escape_trigger_count}",
                value_style="bold yellow",
            )
        elif self.coordinator.config.is_follow_mode():
            self._append_field(
                content,
                "Price escape",
                f"Normal | historical triggers {stats.price_escape_trigger_count}",
                value_style="bold green",
            )

        self._append_field(content, "Running time", running_time)
        return Panel(content, title="Runtime Status", border_style="green")

    def _get_display_pending_order_counts(self, stats: GridStatistics) -> tuple[int, int, int]:
        """Return display-adjusted pending counts without mutating engine state."""
        display_buy_orders = stats.pending_buy_orders
        display_sell_orders = stats.pending_sell_orders
        position_order_units = self._get_position_order_units(stats.current_position)

        if position_order_units <= 0:
            return display_buy_orders, display_sell_orders, 0

        grid_type = self.coordinator.config.grid_type
        if grid_type in [GridType.LONG, GridType.MARTINGALE_LONG, GridType.FOLLOW_LONG] and stats.current_position > 0:
            display_buy_orders = max(display_buy_orders - position_order_units, 0)
        elif grid_type in [GridType.SHORT, GridType.MARTINGALE_SHORT, GridType.FOLLOW_SHORT] and stats.current_position < 0:
            display_sell_orders = max(display_sell_orders - position_order_units, 0)
        return display_buy_orders, display_sell_orders, position_order_units

    def _get_position_order_units(self, current_position: Decimal) -> int:
        """Convert current position size into whole order units for display."""
        order_amount = self.coordinator.config.order_amount
        if order_amount <= 0:
            return 0
        position_units = (abs(current_position) / order_amount).quantize(
            Decimal("1"),
            rounding=ROUND_DOWN,
        )
        return int(position_units)

    def create_orders_panel(self, stats: GridStatistics) -> Panel:
        """Create the raw order statistics panel."""
        content = Text()
        monitoring_mode = getattr(stats, "monitoring_mode", "WebSocket")
        mode_icon = "WS" if monitoring_mode == "WebSocket" else "REST"
        mode_style = "bold cyan" if monitoring_mode == "WebSocket" else "bold yellow"

        buy_grid_ids: list[int] = []
        sell_grid_ids: list[int] = []
        if hasattr(self.coordinator, "state") and hasattr(self.coordinator.state, "active_orders"):
            for order in self.coordinator.state.active_orders.values():
                if not getattr(order, "grid_id", None):
                    continue
                if order.side == GridOrderSide.BUY:
                    buy_grid_ids.append(order.grid_id)
                elif order.side == GridOrderSide.SELL:
                    sell_grid_ids.append(order.grid_id)

        self._append_field(content, "Monitoring", f"{mode_icon} {monitoring_mode}", value_style=mode_style)
        self._append_field(
            content,
            "Pending buys",
            f"{stats.pending_buy_orders} ({self._format_order_range(buy_grid_ids)})",
            value_style="green",
        )
        self._append_field(
            content,
            "Pending sells",
            f"{stats.pending_sell_orders} ({self._format_order_range(sell_grid_ids)})",
            value_style="red",
        )

        if self.coordinator.config.is_scalping_enabled():
            if self.coordinator.scalping_manager and self.coordinator.scalping_manager.is_active():
                tp_order = self.coordinator.scalping_manager.get_current_take_profit_order()
                if tp_order:
                    tp_text = (
                        f"sell {abs(tp_order.amount):.5f} @ ${tp_order.price:,.2f} "
                        f"(Grid {tp_order.grid_id})"
                    )
                    tp_style = "bold yellow"
                else:
                    tp_text = "Missing"
                    tp_style = "red"
            else:
                tp_text = "Pending"
                tp_style = "yellow"
            self._append_field(content, "Scalping TP", tp_text, value_style=tp_style)

        self._append_field(
            content,
            "Total pending",
            str(stats.total_pending_orders),
            value_style="bold white",
        )
        return Panel(content, title="Order Statistics", border_style="blue")

    def _calculate_liquidation_price(self, stats: GridStatistics) -> tuple:
        """
        Estimate liquidation risk for display purposes only.

        The estimate assumes an extreme case where all directional pending orders
        fill, then computes the final position, average cost, and liquidation level.
        """
        try:
            open_orders = [
                order
                for order in self.coordinator.state.active_orders.values()
                if order.status == GridOrderStatus.PENDING
            ]

            if stats.current_position == 0 and not open_orders:
                return (None, 0.0, "N/A")

            current_equity = stats.collateral_balance
            current_position = stats.current_position
            average_cost = stats.average_cost
            current_price = stats.current_price

            if current_position > 0:
                is_long = True
            elif current_position < 0:
                is_long = False
            else:
                buy_orders = [o for o in open_orders if o.side == GridOrderSide.BUY]
                is_long = len(buy_orders) > 0

            if is_long:
                liquidation_price = self._calculate_long_liquidation(
                    current_equity, current_position, average_cost, open_orders
                )
            else:
                liquidation_price = self._calculate_short_liquidation(
                    current_equity, current_position, average_cost, open_orders
                )

            if liquidation_price is None:
                return (None, 0.0, "safe")

            distance_percent = float(
                (liquidation_price - current_price) / current_price * 100
            )
            abs_distance = abs(distance_percent)
            if abs_distance > 20:
                risk_level = "safe"
            elif abs_distance > 10:
                risk_level = "warning"
            else:
                risk_level = "danger"
            return (liquidation_price, distance_percent, risk_level)
        except Exception as exc:
            self.logger.error(f"Failed to calculate liquidation price: {exc}")
            import traceback

            self.logger.error(traceback.format_exc())
            return (None, 0.0, "N/A")

    def _calculate_long_liquidation(
        self,
        equity: Decimal,
        position: Decimal,
        avg_cost: Decimal,
        open_orders: list,
    ) -> Optional[Decimal]:
        """Estimate long-grid liquidation after all pending buy orders fill."""
        buy_orders = [o for o in open_orders if o.side == GridOrderSide.BUY]

        if not buy_orders:
            if position == 0:
                return None
            liquidation_price = avg_cost - equity / position
            return liquidation_price if liquidation_price > 0 else None

        total_buy_amount = sum(o.amount for o in buy_orders)
        total_buy_cost = sum(o.amount * o.price for o in buy_orders)
        final_position = position + total_buy_amount

        if position > 0:
            final_avg_cost = (position * avg_cost + total_buy_cost) / final_position
        else:
            final_avg_cost = total_buy_cost / final_position

        liquidation_price = final_avg_cost - equity / final_position
        return liquidation_price if liquidation_price > 0 else None

    def _calculate_short_liquidation(
        self,
        equity: Decimal,
        position: Decimal,
        avg_cost: Decimal,
        open_orders: list,
    ) -> Optional[Decimal]:
        """Estimate short-grid liquidation after all pending sell orders fill."""
        sell_orders = [o for o in open_orders if o.side == GridOrderSide.SELL]

        if not sell_orders:
            if position == 0:
                return None
            return avg_cost + equity / abs(position)

        total_sell_amount = sum(o.amount for o in sell_orders)
        total_sell_cost = sum(o.amount * o.price for o in sell_orders)
        position_abs = abs(position)
        final_position_abs = position_abs + total_sell_amount

        if position_abs > 0:
            final_avg_cost = (position_abs * avg_cost + total_sell_cost) / final_position_abs
        else:
            final_avg_cost = total_sell_cost / final_position_abs

        return final_avg_cost + equity / final_position_abs

    def create_position_panel(self, stats: GridStatistics) -> Panel:
        """Create the position information panel."""
        content = Text()
        position_color = (
            "green"
            if stats.current_position > 0
            else "red"
            if stats.current_position < 0
            else "white"
        )
        position_type = (
            "Long"
            if stats.current_position > 0
            else "Short"
            if stats.current_position < 0
            else "Flat"
        )

        mark_price = stats.current_price if stats.current_price > 0 else stats.average_cost
        position_value = abs(stats.current_position) * mark_price
        self._append_field(
            content,
            "Position",
            f"{stats.current_position:+.5f} {self.base_currency} ({position_type})",
            value_style=f"bold {position_color}",
        )
        self._append_field(content, "Average Cost", f"${stats.average_cost:,.2f}")
        self._append_field(
            content,
            "Position Value",
            f"${position_value:,.2f}",
            value_style="bold cyan",
        )

        exchange_liquidation_price = getattr(stats, "liquidation_price", None)
        risk_label = "Liquidation risk"
        if stats.current_position == 0:
            risk_text = "No live position"
            risk_style = "cyan"
        elif exchange_liquidation_price is not None and exchange_liquidation_price > 0:
            risk_reference_price = (
                stats.current_price if stats.current_price > 0 else stats.average_cost
            )
            if risk_reference_price > 0:
                distance_percent = float(
                    (exchange_liquidation_price - risk_reference_price)
                    / risk_reference_price
                    * 100
                )
            else:
                distance_percent = 0.0
            direction = "DOWN" if stats.current_position > 0 else "UP"
            if abs(distance_percent) > 20:
                risk_prefix = "OK"
                risk_style = "bold green"
            elif abs(distance_percent) > 10:
                risk_prefix = "WARN"
                risk_style = "bold yellow"
            else:
                risk_prefix = "HIGH"
                risk_style = "bold red"
            risk_text = (
                f"{risk_prefix} ${exchange_liquidation_price:,.2f} "
                f"({direction} {abs(distance_percent):.1f}%)"
            )
        else:
            risk_label = "Liquidation risk (est.)"
            liquidation_price, distance_percent, risk_level = self._calculate_liquidation_price(
                stats
            )
            if risk_level == "N/A":
                risk_text = "Unavailable"
                risk_style = "cyan"
            elif liquidation_price is None:
                risk_text = "Safe inside current grid range"
                risk_style = "bold green"
            else:
                direction = "DOWN" if stats.current_position > 0 else "UP"
                if risk_level == "safe":
                    risk_prefix = "OK"
                    risk_style = "bold green"
                elif risk_level == "warning":
                    risk_prefix = "WARN"
                    risk_style = "bold yellow"
                else:
                    risk_prefix = "HIGH"
                    risk_style = "bold red"
                risk_text = (
                    f"{risk_prefix} ${liquidation_price:,.2f} "
                    f"({direction} {abs(distance_percent):.1f}%)"
                )
        self._append_field(content, risk_label, risk_text, value_style=risk_style)

        return Panel(content, title="Position Information", border_style="yellow")

    def create_pnl_panel(self, stats: GridStatistics) -> Panel:
        """Create the profit and loss panel."""
        content = Text()

        realized_style = (
            "bold green"
            if stats.realized_profit > 0
            else "bold red"
            if stats.realized_profit < 0
            else "white"
        )
        unrealized_style = "cyan" if stats.unrealized_profit >= 0 else "red"
        total_style = (
            "bold green"
            if stats.total_profit > 0
            else "bold red"
            if stats.total_profit < 0
            else "white"
        )

        self._append_field(
            content,
            "Realized",
            f"{'+' if stats.realized_profit >= 0 else ''}${stats.realized_profit:,.2f}",
            value_style=realized_style,
        )
        self._append_field(
            content,
            "Unrealized",
            f"{'+' if stats.unrealized_profit >= 0 else ''}${stats.unrealized_profit:,.2f}",
            value_style=unrealized_style,
        )
        self._append_field(
            content,
            "Fees",
            f"-${stats.total_fees:,.2f}",
            value_style="red",
        )
        self._append_field(
            content,
            "Total PnL",
            f"{'+' if stats.total_profit >= 0 else ''}${stats.total_profit:,.2f}",
            value_style=total_style,
        )
        self._append_field(
            content,
            "Profit rate",
            f"{'+' if stats.profit_rate >= 0 else ''}{stats.profit_rate:.2f}%",
            value_style=total_style,
        )
        self._append_field(
            content,
            "Net profit",
            f"{'+' if stats.net_profit >= 0 else ''}${stats.net_profit:,.2f}",
            value_style=total_style,
        )

        return Panel(content, title="PnL Statistics", border_style="magenta")

    def create_trigger_panel(self, stats: GridStatistics) -> Panel:
        """Create the trigger statistics panel."""
        content = Text()
        avg_cycle_profit = getattr(
            stats,
            "avg_cycle_profit",
            (
                stats.realized_profit / stats.completed_cycles
                if stats.completed_cycles > 0
                else Decimal("0")
            ),
        )

        self._append_field(content, "Buy fills", str(stats.filled_buy_count), value_style="green")
        self._append_field(content, "Sell fills", str(stats.filled_sell_count), value_style="red")
        self._append_field(
            content,
            "Completed cycles",
            f"{stats.completed_cycles} (1 buy + 1 sell)",
            value_style="yellow",
        )
        self._append_field(
            content,
            "Grid utilization",
            f"{stats.grid_utilization:.1f}%",
            value_style="cyan",
        )
        self._append_field(
            content,
            "Avg cycle profit",
            f"${avg_cycle_profit:,.4f}",
            value_style="green" if avg_cycle_profit > 0 else "white",
        )

        return Panel(content, title="Trigger Statistics", border_style="cyan")

    def create_recent_trades_table(self, stats: GridStatistics) -> Panel:
        """Create the recent trades panel."""
        table = Table(show_header=True, header_style="bold magenta", box=None, expand=True)
        table.add_column("Time", style="cyan", width=10)
        table.add_column("Side", width=6)
        table.add_column("Price", style="yellow", justify="right")
        table.add_column("Amount", style="white", justify="right")
        table.add_column("Grid", style="blue", justify="right")

        trades = self.coordinator.tracker.get_trade_history(self.history_limit)
        for trade in reversed(trades[-5:]):
            time_str = self._format_display_time(trade.get("time"))
            side = trade["side"]
            side_style = "green" if side == "buy" else "red"
            table.add_row(
                time_str,
                f"[{side_style}]{side.upper()}[/{side_style}]",
                f"${trade['price']:,.2f}",
                f"{trade['amount']:.5f} {self.base_currency}",
                f"Grid {trade['grid_id']}",
            )

        if not trades:
            table.add_row("--", "--", "--", "--", "--")

        return Panel(table, title="Recent Trades (Last 5)", border_style="green")

    def _format_display_time(self, trade_time: Optional[datetime]) -> str:
        """Render recent-trade timestamps in UTC+8."""
        if not isinstance(trade_time, datetime):
            return "--"
        if trade_time.tzinfo is None:
            trade_time = trade_time.replace(tzinfo=timezone.utc)
        return trade_time.astimezone(self.DISPLAY_TIMEZONE).strftime("%H:%M:%S")

    def create_orders_panel_display(self, stats: GridStatistics) -> Panel:
        """Create the display-adjusted order status panel."""
        content = Text()
        monitoring_mode = getattr(stats, "monitoring_mode", "WebSocket")
        mode_icon = "WS" if monitoring_mode == "WebSocket" else "REST"
        mode_style = "bold cyan" if monitoring_mode == "WebSocket" else "bold yellow"

        buy_grid_ids: list[int] = []
        sell_grid_ids: list[int] = []
        if hasattr(self.coordinator, "state") and hasattr(self.coordinator.state, "active_orders"):
            for order in self.coordinator.state.active_orders.values():
                grid_id = getattr(order, "grid_id", None)
                if not grid_id:
                    continue
                if order.side == GridOrderSide.BUY:
                    buy_grid_ids.append(grid_id)
                elif order.side == GridOrderSide.SELL:
                    sell_grid_ids.append(grid_id)

        display_buy_orders, display_sell_orders, position_order_units = self._get_display_pending_order_counts(
            stats
        )

        self._append_field(content, "Monitoring", f"{mode_icon} {monitoring_mode}", value_style=mode_style)
        self._append_field(
            content,
            "Display buys",
            f"{display_buy_orders} ({self._format_order_range(buy_grid_ids)})",
            value_style="green",
        )
        self._append_field(
            content,
            "Display sells",
            f"{display_sell_orders} ({self._format_order_range(sell_grid_ids)})",
            value_style="red",
        )

        if position_order_units > 0:
            self._append_field(
                content,
                "Position offset",
                f"{position_order_units} units ({abs(stats.current_position):.5f} {self.base_currency})",
                value_style="yellow",
            )

        if self.coordinator.config.is_scalping_enabled():
            if self.coordinator.scalping_manager and self.coordinator.scalping_manager.is_active():
                tp_order = self.coordinator.scalping_manager.get_current_take_profit_order()
                if tp_order:
                    tp_text = (
                        f"sell {abs(tp_order.amount):.5f} @ ${tp_order.price:,.2f} "
                        f"(Grid {tp_order.grid_id})"
                    )
                    tp_style = "bold yellow"
                else:
                    tp_text = "Missing order"
                    tp_style = "red"
            else:
                tp_text = "Disabled"
                tp_style = "yellow"
            self._append_field(content, "Scalping TP", tp_text, value_style=tp_style)

        self._append_field(
            content,
            "Total pending",
            str(stats.total_pending_orders),
            value_style="bold white",
        )
        return Panel(content, title="Order Status", border_style="blue")

    def create_layout(self, stats: GridStatistics) -> Layout:
        """Create the full dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(self.create_header(stats), size=3),
            Layout(name="summary", ratio=7),
            Layout(self.create_recent_trades_table(stats), ratio=4),
        )

        layout["summary"].split_row(
            Layout(name="left", ratio=7),
            Layout(name="right", ratio=5),
        )

        layout["left"].split_column(
            Layout(self.create_status_panel(stats), ratio=5),
            Layout(self.create_position_panel(stats), ratio=6),
        )
        layout["right"].split_column(
            Layout(self.create_orders_panel_display(stats), ratio=4),
            Layout(self.create_pnl_panel(stats), ratio=3),
            Layout(self.create_trigger_panel(stats), ratio=3),
        )
        return layout

    async def run(self) -> None:
        """Run the terminal UI."""
        self._running = True

        self.console.print("\n[bold green]Grid trading terminal UI started[/bold green]")
        self.console.print("[cyan]Tip: use Ctrl+C to stop the system[/cyan]\n")
        await asyncio.sleep(1)
        self.console.clear()

        self.console.print("[cyan]Loading initial statistics...[/cyan]")
        try:
            initial_stats = await self.coordinator.get_statistics()
            self.console.print("[green]Initial statistics loaded successfully[/green]")
        except Exception as exc:
            self.console.print(f"[red]Failed to load initial statistics: {exc}[/red]")
            import traceback

            self.console.print(f"[yellow]{traceback.format_exc()}[/yellow]")
            from .models import GridStatistics

            initial_stats = GridStatistics()

        self.console.print("[cyan]Starting Rich terminal UI...[/cyan]")

        import os

        use_fullscreen = os.getenv("GRID_UI_FULLSCREEN", "true").lower() == "true"
        self.live_console_level = os.getenv("GRID_UI_CONSOLE_LOG_LEVEL", "WARNING").upper()

        try:
            self.console.print(
                f"[yellow]Creating Live display object (fullscreen: {use_fullscreen})...[/yellow]"
            )
            live_display = Live(
                self.create_layout(initial_stats),
                refresh_per_second=self.refresh_rate,
                console=self.console,
                screen=use_fullscreen,
                transient=False,
            )
            self.console.print("[green]Live object created successfully[/green]")
        except Exception as exc:
            self.console.print(f"[red]Failed to create Live object: {exc}[/red]")
            import traceback

            self.console.print(f"[yellow]{traceback.format_exc()}[/yellow]")
            if use_fullscreen:
                self.console.print("[yellow]Trying windowed mode...[/yellow]")
                try:
                    live_display = Live(
                        self.create_layout(initial_stats),
                        refresh_per_second=self.refresh_rate,
                        console=self.console,
                        screen=False,
                        transient=False,
                    )
                    self.console.print("[green]Windowed mode started successfully[/green]")
                except Exception as fallback_exc:
                    self.console.print(f"[red]Windowed mode also failed: {fallback_exc}[/red]")
                    return
            else:
                return

        self.console.print("[cyan]Entering Live context...[/cyan]")
        self.logger.info("Entering Live context manager...")
        console_log_state = set_console_log_level(self.live_console_level)

        try:
            with live_display as live:
                self.logger.info("Rich Live context started; entering main loop")
                loop_started = False
                try:
                    while self._running:
                        try:
                            if not loop_started:
                                self.logger.info("First main-loop iteration started...")
                            try:
                                stats = await asyncio.wait_for(
                                    self.coordinator.get_statistics(),
                                    timeout=5.0,
                                )
                                if not loop_started:
                                    self.logger.info("Initial statistics fetched successfully")
                            except asyncio.TimeoutError:
                                self.logger.error(
                                    "Statistics fetch timed out after 5 seconds; skipping this update"
                                )
                                continue

                            live.update(self.create_layout(stats))
                            if not loop_started:
                                self.logger.info("First UI refresh completed successfully")
                                loop_started = True
                        except Exception as exc:
                            self.logger.error(f"UI update failed: {exc}")
                            import traceback

                            self.logger.error(f"Detailed error: {traceback.format_exc()}")
                        await asyncio.sleep(1 / self.refresh_rate)
                except KeyboardInterrupt:
                    self.console.print("\n[yellow]Exit signal received...[/yellow]")
                finally:
                    self._running = False
        finally:
            restore_console_log_level(console_log_state)

    def stop(self) -> None:
        """Stop the terminal UI."""
        self._running = False

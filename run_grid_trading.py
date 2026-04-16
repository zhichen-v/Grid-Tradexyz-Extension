#!/usr/bin/env python3
"""
Grid trading system startup script.

Use this entrypoint to start the grid trading runtime directly.
"""

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

import yaml

from core.adapters.exchanges import ExchangeConfig, ExchangeFactory
from core.adapters.exchanges.models import ExchangeType
from core.logging import get_system_logger, initialize
from core.logging.logger import LineLimitedFileHandler
from core.services.grid.coordinator import GridCoordinator
from core.services.grid.implementations import (
    GridEngineImpl,
    GridStrategyImpl,
    PositionTrackerImpl,
)
from core.services.grid.models import GridConfig, GridState, GridType
from core.services.grid.reserve import (
    ReserveMonitor,
    SpotReserveManager,
    check_spot_reserve_on_startup,
)
from core.services.grid.terminal_ui import GridTerminalUI


# Add the project root to the import path.
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


async def load_config(config_path: str) -> dict:
    """
    Load a YAML configuration file.

    Args:
        config_path: Configuration file path.

    Returns:
        Parsed configuration dictionary.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
        return config
    except Exception as exc:
        print(f"Failed to load config file: {exc}")
        raise


def create_grid_config(config_data: dict) -> GridConfig:
    """
    Build the grid configuration object from raw config data.

    Args:
        config_data: Parsed configuration data.

    Returns:
        Grid configuration object.
    """
    grid_config = config_data["grid_system"]
    grid_type = GridType(grid_config["grid_type"])

    # Base parameters.
    params = {
        "exchange": grid_config["exchange"],
        "symbol": grid_config["symbol"],
        "grid_type": grid_type,
        "grid_interval": Decimal(str(grid_config["grid_interval"])),
        "order_amount": Decimal(str(grid_config["order_amount"])),
        "max_position": (
            Decimal(str(grid_config.get("max_position")))
            if grid_config.get("max_position")
            else None
        ),
        "enable_notifications": grid_config.get("enable_notifications", False),
        "order_health_check_interval": grid_config.get(
            "order_health_check_interval", 600
        ),
        "fee_rate": Decimal(str(grid_config.get("fee_rate", "0.0001"))),
        "quantity_precision": int(grid_config.get("quantity_precision", 3)),
    }

    # Follow grid settings.
    if grid_type in [GridType.FOLLOW_LONG, GridType.FOLLOW_SHORT]:
        params["follow_grid_count"] = grid_config["follow_grid_count"]
        params["follow_timeout"] = grid_config.get("follow_timeout", 300)
        params["follow_distance"] = grid_config.get("follow_distance", 1)
        params["price_offset_grids"] = grid_config.get("price_offset_grids", 0)
        # lower_price and upper_price stay at their default None values.
    else:
        # Support both flat and nested price range config formats.
        if "price_range" in grid_config:
            params["lower_price"] = Decimal(
                str(grid_config["price_range"]["lower_price"])
            )
            params["upper_price"] = Decimal(
                str(grid_config["price_range"]["upper_price"])
            )
        else:
            params["lower_price"] = Decimal(str(grid_config["lower_price"]))
            params["upper_price"] = Decimal(str(grid_config["upper_price"]))

    # Martingale settings.
    if "martingale_increment" in grid_config:
        params["martingale_increment"] = Decimal(
            str(grid_config["martingale_increment"])
        )

    # Scalping settings.
    if "scalping_enabled" in grid_config:
        params["scalping_enabled"] = grid_config["scalping_enabled"]
    if "scalping_trigger_percent" in grid_config:
        params["scalping_trigger_percent"] = grid_config["scalping_trigger_percent"]
    if "scalping_take_profit_grids" in grid_config:
        params["scalping_take_profit_grids"] = grid_config[
            "scalping_take_profit_grids"
        ]

    # Capital protection settings.
    if "capital_protection_enabled" in grid_config:
        params["capital_protection_enabled"] = grid_config[
            "capital_protection_enabled"
        ]
    if "capital_protection_trigger_percent" in grid_config:
        params["capital_protection_trigger_percent"] = grid_config[
            "capital_protection_trigger_percent"
        ]

    # Take-profit settings.
    if "take_profit_enabled" in grid_config:
        params["take_profit_enabled"] = grid_config["take_profit_enabled"]
    if "take_profit_percentage" in grid_config:
        params["take_profit_percentage"] = Decimal(
            str(grid_config["take_profit_percentage"])
        )

    # Price-lock settings.
    if "price_lock_enabled" in grid_config:
        params["price_lock_enabled"] = grid_config["price_lock_enabled"]
    if "price_lock_threshold" in grid_config:
        params["price_lock_threshold"] = Decimal(
            str(grid_config["price_lock_threshold"])
        )
    if "price_lock_start_at_threshold" in grid_config:
        params["price_lock_start_at_threshold"] = grid_config[
            "price_lock_start_at_threshold"
        ]

    # Reverse-order settings.
    if "reverse_order_grid_distance" in grid_config:
        params["reverse_order_grid_distance"] = int(
            grid_config["reverse_order_grid_distance"]
        )

    # Spot reserve settings.
    if "spot_reserve" in grid_config:
        params["spot_reserve"] = grid_config["spot_reserve"]

    # Health-check tolerance settings.
    if "position_tolerance" in grid_config:
        params["position_tolerance"] = grid_config["position_tolerance"]

    return GridConfig(**params)


def detect_market_type(symbol: str, exchange_name: str) -> ExchangeType:
    """
    Detect the market type from the trading symbol and exchange.

    Args:
        symbol: Trading pair symbol.
        exchange_name: Exchange name.

    Returns:
        ExchangeType: Spot or perpetual market type.
    """
    symbol_upper = symbol.upper()
    exchange_lower = exchange_name.lower()

    # Hyperliquid exchange.
    if exchange_lower == "hyperliquid":
        # Hyperliquid symbol formats:
        # - Spot: BTC/USDC
        # - Perpetual: BTC/USDC:USDC
        if ":USDC" in symbol_upper or ":PERP" in symbol_upper or ":SPOT" in symbol_upper:
            if ":SPOT" in symbol_upper:
                return ExchangeType.SPOT
            return ExchangeType.PERPETUAL
        return ExchangeType.SPOT

    # Backpack exchange.
    if exchange_lower == "backpack":
        if "_PERP" in symbol_upper or "PERP" in symbol_upper:
            return ExchangeType.PERPETUAL
        if "_SPOT" in symbol_upper or "SPOT" in symbol_upper:
            return ExchangeType.SPOT
        return ExchangeType.PERPETUAL

    # Lighter exchange.
    if exchange_lower == "lighter":
        return ExchangeType.PERPETUAL

    # TradeXYZ exchange.
    if exchange_lower == "tradexyz":
        return ExchangeType.PERPETUAL

    # Default for unknown exchanges.
    return ExchangeType.PERPETUAL


def _load_dotenv() -> None:
    """Load environment variables from `.env` without overwriting existing values."""
    import os

    env_path = Path(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


async def create_exchange_adapter(config_data: dict):
    """
    Create and connect the exchange adapter.

    Args:
        config_data: Parsed configuration data.

    Returns:
        Connected exchange adapter.
    """
    import os

    grid_config = config_data["grid_system"]
    exchange_name = grid_config["exchange"].lower()
    symbol = grid_config["symbol"]

    # Load additional credentials from .env, such as agent wallet keys.
    _load_dotenv()

    # Detect spot vs perpetual automatically.
    market_type = detect_market_type(symbol, exchange_name)
    print(f"   - Market type: {market_type.value}")

    # Resolution order: environment variables > exchange config file > empty string.
    api_key = os.getenv(f"{exchange_name.upper()}_API_KEY")
    api_secret = os.getenv(f"{exchange_name.upper()}_API_SECRET")
    wallet_address = os.getenv(f"{exchange_name.upper()}_WALLET_ADDRESS")

    if exchange_name in {"hyperliquid", "tradexyz"}:
        api_key = api_key or api_secret
        api_secret = api_secret or api_key

    # TradeXYZ additionally supports HL_AGENT_KEY / HL_WALLET_ADDRESS.
    if exchange_name == "tradexyz":
        hl_agent_key = os.getenv("HL_AGENT_KEY")
        hl_wallet_address = os.getenv("HL_WALLET_ADDRESS")
        if hl_agent_key and not api_key:
            api_key = hl_agent_key
            api_secret = hl_agent_key
            print("   - Using HL_AGENT_KEY (Agent Wallet)")
        if hl_wallet_address and not wallet_address:
            wallet_address = hl_wallet_address

    # Fall back to the exchange config file if env vars are not set.
    if not api_key or not api_secret:
        try:
            exchange_config_path = Path(
                f"config/exchanges/{exchange_name}_config.yaml"
            )
            if exchange_config_path.exists():
                with open(exchange_config_path, "r", encoding="utf-8") as file:
                    exchange_config_data = yaml.safe_load(file)

                auth_config = exchange_config_data.get(exchange_name, {}).get(
                    "authentication", {}
                )

                if exchange_name in {"hyperliquid", "tradexyz"}:
                    api_key = api_key or auth_config.get("private_key", "")
                    api_secret = api_secret or auth_config.get("private_key", "")
                    wallet_address = wallet_address or auth_config.get(
                        "wallet_address", ""
                    )
                elif exchange_name == "lighter":
                    api_config = exchange_config_data.get("api_config", {})
                    auth_config = api_config.get("auth", {})
                    api_key = api_key or auth_config.get("api_key_private_key", "")
                    api_secret = api_secret or auth_config.get(
                        "api_key_private_key", ""
                    )
                else:
                    api_key = api_key or auth_config.get("api_key", "")
                    api_secret = api_secret or auth_config.get(
                        "private_key", ""
                    ) or auth_config.get("api_secret", "")
                    wallet_address = wallet_address or auth_config.get(
                        "wallet_address", ""
                    )

                if api_key and api_secret:
                    print(f"   - Loaded API credentials from: {exchange_config_path}")
                    if (
                        exchange_name in {"hyperliquid", "tradexyz"}
                        and wallet_address
                    ):
                        print(
                            f"   - Wallet address: {wallet_address[:10]}...{wallet_address[-6:]}"
                        )
        except Exception as exc:
            print(f"   - Warning: failed to read exchange config file: {exc}")

    # Print a warning if credentials are still missing.
    if not api_key or not api_secret:
        print("   - Warning: API credentials were not found")
        print(
            f"   - Hint: set environment variables or configure config/exchanges/{exchange_name}_config.yaml"
        )

        if exchange_name in {"hyperliquid", "tradexyz"}:
            display_name = "Hyperliquid" if exchange_name == "hyperliquid" else "TradeXYZ"
            print(f"   - {display_name} requires:")
            print("      - private_key: wallet private key")
            print("      - wallet_address: wallet address")
        elif exchange_name == "lighter":
            print("   - Lighter requires:")
            print("      - api_key_private_key: API key private key")
            print("      - account_index: account index")
            print("      - api_key_index: API key index (default 0)")

    # Build the exchange configuration.
    if exchange_name == "lighter":
        try:
            lighter_config_path = Path("config/exchanges/lighter_config.yaml")
            if lighter_config_path.exists():
                with open(lighter_config_path, "r", encoding="utf-8") as file:
                    lighter_config_data = yaml.safe_load(file)
                api_config = lighter_config_data.get("api_config", {})

                exchange_config = ExchangeConfig(
                    exchange_id="lighter",
                    name="Lighter",
                    exchange_type=market_type,
                    api_key="",
                    api_secret="",
                    testnet=api_config.get("testnet", False),
                    enable_websocket=True,
                    enable_auto_reconnect=True,
                )
                print(
                    "   - Lighter config loaded successfully (adapter will read API credentials automatically)"
                )
            else:
                print(f"   - Warning: Lighter config file not found: {lighter_config_path}")
                exchange_config = ExchangeConfig(
                    exchange_id="lighter",
                    name="Lighter",
                    exchange_type=market_type,
                    api_key="",
                    api_secret="",
                    testnet=False,
                    enable_websocket=True,
                    enable_auto_reconnect=True,
                )
        except Exception as exc:
            print(f"   - Warning: failed to load Lighter config: {exc}")
            exchange_config = ExchangeConfig(
                exchange_id="lighter",
                name="Lighter",
                exchange_type=market_type,
                api_key="",
                api_secret="",
                testnet=False,
                enable_websocket=True,
                enable_auto_reconnect=True,
            )
    else:
        display_name = "TradeXYZ" if exchange_name == "tradexyz" else exchange_name.capitalize()
        exchange_config = ExchangeConfig(
            exchange_id=exchange_name,
            name=display_name,
            exchange_type=market_type,
            api_key=api_key or "",
            api_secret=api_secret or "",
            wallet_address=wallet_address,
            testnet=False,
            enable_websocket=True,
            enable_auto_reconnect=True,
        )

    # Create and connect the adapter through the factory.
    factory = ExchangeFactory()
    adapter = factory.create_adapter(exchange_id=exchange_name, config=exchange_config)
    await adapter.connect()
    return adapter


async def main(
    config_path: str = "config/grid/default_grid.yaml", debug: bool = False
) -> None:
    """
    Main entrypoint.

    Args:
        config_path: Configuration file path.
        debug: Whether to enable debug mode.
    """
    initialize(clear_existing=True)

    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

        for module in ["core.services.grid", "core.adapters.exchanges", "ExchangeAdapter"]:
            logging.getLogger(module).setLevel(logging.DEBUG)

        lighter_ws_logger = logging.getLogger(
            "core.adapters.exchanges.adapters.lighter_websocket"
        )
        lighter_ws_logger.setLevel(logging.DEBUG)

        exchange_log_path = Path("logs/ExchangeAdapter.log").resolve()
        has_exchange_log_handler = any(
            isinstance(handler, logging.FileHandler)
            and Path(getattr(handler, "baseFilename", "")).resolve() == exchange_log_path
            for handler in lighter_ws_logger.handlers
        )

        if not has_exchange_log_handler:
            ws_handler = LineLimitedFileHandler(
                str(exchange_log_path),
                max_lines=1000,
                encoding="utf-8",
            )
            ws_handler.setLevel(logging.DEBUG)
            ws_formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
            )
            ws_handler.setFormatter(ws_formatter)
            lighter_ws_logger.addHandler(ws_handler)

        lighter_ws_logger.propagate = False

        print("=" * 70)
        print("Grid Trading System Startup - DEBUG Mode")
        print("=" * 70)
        print("Detailed debug logs will be written during startup and runtime")
        print("=" * 70)
    else:
        print("=" * 70)
        print("Grid Trading System Startup")
        print("=" * 70)

    logger = get_system_logger()

    try:
        # 1. Load configuration.
        print("\nStep 1/6: Loading configuration...")
        config_data = await load_config(config_path)
        grid_config = create_grid_config(config_data)
        print("Configuration loaded successfully")
        print(f"   - Exchange: {grid_config.exchange}")
        print(f"   - Symbol: {grid_config.symbol}")
        print(f"   - Grid type: {grid_config.grid_type.value}")

        # Spot markets support only long-side grid modes.
        symbol = grid_config.symbol
        exchange_name = grid_config.exchange.lower()
        is_spot = False

        if exchange_name == "hyperliquid":
            is_spot = ":SPOT" in symbol.upper()
        elif exchange_name == "backpack":
            is_spot = "_SPOT" in symbol.upper() or "SPOT" in symbol.upper()

        if is_spot and grid_config.grid_type.value in [
            "short",
            "martingale_short",
            "follow_short",
        ]:
            print("\nError: spot markets do not support short grid modes")
            print(f"   - Current symbol: {symbol} (spot)")
            print(f"   - Current grid type: {grid_config.grid_type.value} (short)")
            print(
                "   - Suggested grid types: long, martingale_long, follow_long"
            )
            sys.exit(1)

        if is_spot:
            print("   - Spot market detected: only long grid modes are supported")

        if grid_config.is_follow_mode():
            print("   - Price range: dynamic follow mode (set at runtime)")
        else:
            print(
                f"   - Price range: ${grid_config.lower_price:,.2f} - ${grid_config.upper_price:,.2f}"
            )

        print(f"   - Grid interval: ${grid_config.grid_interval}")
        print(f"   - Grid count: {grid_config.grid_count}")
        print(f"   - Order amount: {grid_config.order_amount}")

        if grid_config.is_martingale_mode():
            print(
                f"   - Martingale increment: {grid_config.martingale_increment} per grid"
            )
        if grid_config.is_follow_mode():
            print(f"   - Follow timeout: {grid_config.follow_timeout}s")
            print(f"   - Follow distance: {grid_config.follow_distance} grids")

        # 2. Connect the exchange adapter.
        print("\nStep 2/6: Connecting exchange...")
        exchange_adapter = await create_exchange_adapter(config_data)
        print(f"Exchange connected successfully: {grid_config.exchange}")

        # 3. Create core components.
        print("\nStep 3/6: Initializing core components...")

        strategy = GridStrategyImpl()
        print("   - Grid strategy created")

        engine = GridEngineImpl(exchange_adapter)
        print("   - Execution engine created")

        grid_state = GridState()

        tracker = PositionTrackerImpl(grid_config, grid_state)
        print("   - Position tracker created")

        # Create reserve management only for spot mode when enabled.
        reserve_manager = None
        reserve_monitor = None

        if exchange_adapter.config.exchange_type == ExchangeType.SPOT:
            spot_reserve_config = getattr(grid_config, "spot_reserve", None)

            if spot_reserve_config and spot_reserve_config.get("enabled", False):
                print("   - Spot reserve management enabled")

                reserve_manager = SpotReserveManager(
                    reserve_config=spot_reserve_config,
                    exchange_adapter=exchange_adapter,
                    symbol=grid_config.symbol,
                    quantity_precision=grid_config.quantity_precision,
                )

                reserve_monitor = ReserveMonitor(
                    reserve_manager=reserve_manager,
                    exchange_adapter=exchange_adapter,
                    symbol=grid_config.symbol,
                    check_interval=60,
                )
                print("   - Reserve monitor created")

        # 4. Create the coordinator.
        print("\nStep 4/6: Creating system coordinator...")
        coordinator = GridCoordinator(
            config=grid_config,
            strategy=strategy,
            engine=engine,
            tracker=tracker,
            grid_state=grid_state,
            reserve_manager=reserve_manager,
        )
        print("Coordinator created successfully")

        # Run the startup reserve check only for spot mode with reserve management enabled.
        if reserve_manager:
            print("\nStartup check: validating spot reserve balance...")
            if not await check_spot_reserve_on_startup(
                grid_config, exchange_adapter, reserve_manager
            ):
                print("Startup check failed, exiting")
                await exchange_adapter.disconnect()
                sys.exit(1)
            print("Reserve validation passed")

        # 5. Start the grid system.
        print("\nStep 5/6: Starting grid system...")
        print(f"   - Preparing batch placement for {grid_config.grid_count} orders")

        if not grid_config.is_follow_mode():
            print(
                f"   - Target price range: ${grid_config.lower_price:,.2f} - ${grid_config.upper_price:,.2f}"
            )
        else:
            print("   - Price range: dynamic follow mode (set from current price)")

        await coordinator.start()
        print("Grid system started")
        print(f"   - Active startup order count: {grid_config.grid_count}")

        if reserve_monitor:
            await reserve_monitor.start()
            print("Reserve monitor started")

        if grid_config.is_follow_mode():
            print(
                f"   - Effective price range: ${grid_config.lower_price:,.2f} - ${grid_config.upper_price:,.2f}"
            )

        print("   - All grids are in place, waiting for fills...")

        # 6. Start the terminal UI.
        print("\nStep 6/6: Starting terminal UI...")
        terminal_ui = GridTerminalUI(coordinator)

        print("=" * 70)
        print("Grid Trading System Fully Started")
        print("=" * 70)
        print()

        await terminal_ui.run()

    except KeyboardInterrupt:
        print("\n\nExit signal received, stopping the system...")

    except Exception as exc:
        logger.error(f"System error: {exc}", exc_info=True)
        print(f"\nSystem error: {exc}")

    finally:
        print("\nCleaning up resources...")
        try:
            if "reserve_monitor" in locals() and reserve_monitor:
                await reserve_monitor.stop()
                print("   - Reserve monitor stopped")

            if "coordinator" in locals():
                await coordinator.stop()
                print("   - Grid system stopped")

            if "exchange_adapter" in locals():
                await exchange_adapter.disconnect()
                print("   - Exchange disconnected")

            print("\nSystem exited safely")

        except Exception as exc:
            print(f"Cleanup error: {exc}")


def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Grid trading system for multiple exchanges",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start in normal mode
  python3 run_grid_trading.py config/grid/lighter_btc_perp_long.yaml

  # Start in DEBUG mode with detailed logs
  python3 run_grid_trading.py config/grid/lighter_btc_perp_long.yaml --debug

  # Backpack exchange
  python3 run_grid_trading.py config/grid/backpack_capital_protection_long_btc.yaml

  # Hyperliquid exchange
  python3 run_grid_trading.py config/grid/hyperliquid_btc_perp_long.yaml

  # Lighter exchange
  python3 run_grid_trading.py config/grid/lighter_btc_perp_long.yaml --debug

Supported exchanges:
  Backpack    - Perpetual markets (long / short)
  Hyperliquid - Perpetual markets (long / short), spot (long only)
  Lighter     - Perpetual markets (long / short)

Notes:
  1. Make sure API credentials are configured correctly.
  2. Make sure the account has enough capital for grid trading.
  3. Start with small size before using larger capital.
  4. Spot markets support long grids only.
  5. The grid system runs continuously until it is stopped manually.
  6. Use Ctrl+C or Q to exit safely.
  7. DEBUG mode prints detailed logs for troubleshooting.
        """,
    )

    parser.add_argument(
        "config",
        type=str,
        help="Grid config file path (for example: config/grid/lighter_btc_perp_long.yaml)",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG mode and print detailed diagnostic logs",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="Grid Trading System v2.0.0",
    )

    return parser.parse_args()


if __name__ == "__main__":
    try:
        # Parse command-line arguments.
        args = parse_arguments()

        # Resolve the config path.
        config_path = args.config

        # Ensure the config file exists before startup.
        if not Path(config_path).exists():
            print(f"Config file does not exist: {config_path}")
            print("\nUse -h or --help to view usage")
            sys.exit(1)

        # Print a short debug summary before startup.
        if args.debug:
            print("\n" + "=" * 70)
            print("DEBUG mode enabled")
            print("=" * 70)
            print("Detailed logs will include:")
            print("   - WebSocket message details")
            print("   - REST API call parameters")
            print("   - Order matching behavior")
            print("   - Position and balance updates")
            print("=" * 70)
            print()

        # Run the application.
        asyncio.run(main(config_path, debug=args.debug))

    except KeyboardInterrupt:
        print("\nProgram exited")
    except SystemExit:
        # argparse --help and --version trigger SystemExit intentionally.
        pass
    except Exception as exc:
        print(f"\nStartup failed: {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

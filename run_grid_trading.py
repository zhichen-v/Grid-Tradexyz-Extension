#!/usr/bin/env python3
"""
Grid trading system startup script.

Use this entrypoint to start the grid trading runtime directly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from decimal import Decimal
from pathlib import Path

import yaml

from core.logging import get_system_logger, initialize
from core.logging.logger import build_rotating_file_handler


# Add the project root to the import path.
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

GRID_CONFIG_DIR = Path("config/grid")
WALLET_PROFILES_DIR = Path("config/exchanges/wallets")
WALLET_PROFILE_EXCHANGES = {"hyperliquid", "tradexyz"}


class WalletProfileError(RuntimeError):
    """Raised when a named wallet profile cannot be loaded."""


def _sanitize_path_segment(value: str, fallback: str = "default") -> str:
    """Return a filesystem-safe single path segment."""
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return sanitized or fallback


def _validate_wallet_name(wallet_name: str) -> str:
    """Validate a wallet profile name before using it as a file name."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", wallet_name):
        raise ValueError(
            "Wallet name must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens."
        )
    return wallet_name


def _available_wallet_profiles() -> list[str]:
    """Return wallet profile names available under config/exchanges/wallets."""
    if not WALLET_PROFILES_DIR.exists():
        return []
    return sorted(path.stem for path in WALLET_PROFILES_DIR.glob("*.yaml"))


def _wallet_profile_path(wallet_name: str) -> Path:
    """Return the YAML path for a validated wallet profile name."""
    return WALLET_PROFILES_DIR / f"{_validate_wallet_name(wallet_name)}.yaml"


def _apply_wallet_shortcut(
    args: argparse.Namespace,
    unknown_args: list[str],
    parser: argparse.ArgumentParser,
) -> argparse.Namespace:
    """Treat one unknown --<name> argument as a wallet profile shortcut."""
    if not unknown_args:
        return args

    if len(unknown_args) != 1:
        parser.error(f"unrecognized arguments: {' '.join(unknown_args)}")

    wallet_arg = unknown_args[0]
    if not wallet_arg.startswith("--") or wallet_arg == "--" or "=" in wallet_arg:
        parser.error(f"unrecognized argument: {wallet_arg}")

    shortcut_name = _validate_wallet_name(wallet_arg[2:])
    if args.wallet_name and args.wallet_name != shortcut_name:
        parser.error(
            f"wallet profile was provided twice: {args.wallet_name} and {shortcut_name}"
        )

    args.wallet_name = shortcut_name
    return args


def resolve_config_path(config_path: str) -> str:
    """Resolve a config path, accepting bare file names from config/grid."""
    requested_path = Path(config_path)
    if requested_path.exists():
        return str(requested_path)

    if not requested_path.is_absolute() and len(requested_path.parts) == 1:
        grid_path = GRID_CONFIG_DIR / requested_path
        if grid_path.exists():
            return str(grid_path)

    return str(requested_path)


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


def build_grid_log_dir(config_data: dict, wallet_name: str | None = None) -> str:
    """Return the per-symbol log directory for one grid runtime."""
    grid_config = config_data.get("grid_system", {})
    raw_symbol = str(grid_config.get("symbol", "")).strip()
    symbol_folder = _sanitize_path_segment(raw_symbol)

    if wallet_name:
        wallet_folder = _sanitize_path_segment(_validate_wallet_name(wallet_name))
        return str(Path("logs") / wallet_folder / symbol_folder)

    return str(Path("logs") / symbol_folder)


def create_grid_config(config_data: dict) -> GridConfig:
    """
    Build the grid configuration object from raw config data.

    Args:
        config_data: Parsed configuration data.

    Returns:
        Grid configuration object.
    """
    from core.services.grid.models import GridConfig, GridType

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
            if grid_config.get("max_position") is not None
            else None
        ),
        "enable_notifications": grid_config.get("enable_notifications", False),
        "order_health_check_interval": grid_config.get(
            "order_health_check_interval", 600
        ),
        "position_monitor_interval": int(
            grid_config.get("position_monitor_interval", 10)
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

    # Stop-loss settings.
    if "stop_loss_enabled" in grid_config:
        params["stop_loss_enabled"] = grid_config["stop_loss_enabled"]
    if "stop_loss_price" in grid_config and grid_config["stop_loss_price"] is not None:
        params["stop_loss_price"] = Decimal(str(grid_config["stop_loss_price"]))
    if "stop_loss_check_interval" in grid_config:
        params["stop_loss_check_interval"] = int(
            grid_config["stop_loss_check_interval"]
        )

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
    if "position_repair_mode" in grid_config:
        params["position_repair_mode"] = str(grid_config["position_repair_mode"])

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
    from core.adapters.exchanges.models import ExchangeType

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


def _load_wallet_profile(wallet_name: str) -> dict[str, str]:
    """Load a named Hyperliquid/TradeXYZ agent wallet profile."""
    profile_path = _wallet_profile_path(wallet_name)
    if not profile_path.exists():
        available = ", ".join(_available_wallet_profiles()) or "(none)"
        raise WalletProfileError(
            f"Wallet profile not found: {wallet_name}. "
            f"Expected file: {profile_path}. Available profiles: {available}"
        )

    try:
        with open(profile_path, "r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
    except yaml.YAMLError:
        # Parser errors echo the offending line, which may hold the key.
        raise WalletProfileError(f"Invalid YAML in wallet profile: {profile_path}") from None

    auth = data.get("authentication") if isinstance(data, dict) else None
    auth = auth if isinstance(auth, dict) else {}
    missing = sorted(
        key for key in ("private_key", "wallet_address") if not str(auth.get(key) or "").strip()
    )
    if missing:
        raise WalletProfileError(
            f"Wallet profile {wallet_name} is missing authentication keys: {', '.join(missing)}"
        )
    return {key: str(value).strip() for key, value in auth.items() if value is not None}


def _read_arcus_settings() -> dict:
    """Return non-secret Arcus settings (network, account index) from its config."""
    settings: dict = {}
    path = Path("config/exchanges/arcus_config.yaml")
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as file:
                settings = (yaml.safe_load(file) or {}).get("arcus", {}) or {}
        except yaml.YAMLError:
            raise RuntimeError(f"Invalid YAML in {path}") from None
    auth = settings.get("authentication", {}) or {}
    return {
        "network": str(settings.get("network", "mainnet")).strip().lower(),
        "account_index": int(auth.get("account_index", 0)),
    }


async def create_exchange_adapter(config_data: dict, wallet_name: str | None = None):
    """
    Create and connect the exchange adapter.

    Args:
        config_data: Parsed configuration data.

    Returns:
        Connected exchange adapter.
    """
    import os
    from core.adapters.exchanges import ExchangeConfig, ExchangeFactory

    grid_config = config_data["grid_system"]
    exchange_name = grid_config["exchange"].lower()
    symbol = grid_config["symbol"]

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

    # A named wallet profile is explicit and overrides any stale shell vars.
    if wallet_name:
        if exchange_name not in WALLET_PROFILE_EXCHANGES:
            raise WalletProfileError(
                f"Wallet profiles only apply to {', '.join(sorted(WALLET_PROFILE_EXCHANGES))}"
            )
        profile = _load_wallet_profile(wallet_name)
        api_key = api_secret = profile["private_key"]
        wallet_address = profile["wallet_address"]
        print(f"   - Wallet profile: {wallet_name} ({_wallet_profile_path(wallet_name)})")

    # Fall back to the exchange config file if env vars are not set.
    # Lighter's adapter reads config/exchanges/lighter_config.yaml itself.
    if exchange_name != "lighter" and (not api_key or not api_secret):
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
                elif exchange_name == "arcus":
                    api_secret = api_secret or auth_config.get("api_signing_key", "")
                    api_key = api_key or api_secret
                    wallet_address = wallet_address or auth_config.get(
                        "wallet_address", ""
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
        except yaml.YAMLError:
            print("   - Warning: exchange config contains invalid YAML")
        except Exception as exc:
            print(
                "   - Warning: failed to read exchange config file "
                f"({type(exc).__name__})"
            )

    # Print a warning if credentials are still missing.
    if exchange_name != "lighter" and (not api_key or not api_secret):
        print("   - Warning: API credentials were not found")
        print(
            f"   - Hint: set environment variables or configure config/exchanges/{exchange_name}_config.yaml"
        )

        if exchange_name in {"hyperliquid", "tradexyz"}:
            display_name = "Hyperliquid" if exchange_name == "hyperliquid" else "TradeXYZ"
            print(f"   - {display_name} requires:")
            print("      - private_key: wallet private key")
            print("      - wallet_address: wallet address")
        elif exchange_name == "arcus":
            print("   - Arcus requires (config/exchanges/arcus_config.yaml):")
            print("      - wallet_address: master Ethereum address that registered the key")
            print("      - api_signing_key: Ed25519 API signing key (hex)")
            print("      - account_index: subaccount index (default 0)")

    # Build the exchange configuration.
    if exchange_name == "lighter":
        from core.adapters.exchanges.adapters.lighter_base import (
            LIGHTER_CONFIG_PATH,
            read_lighter_config,
        )

        try:
            lighter_settings = read_lighter_config()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        if not lighter_settings.get("api_key_private_key"):
            raise RuntimeError(
                f"Lighter credentials missing: set lighter.authentication in {LIGHTER_CONFIG_PATH} "
                "(copy lighter_config_example.yaml)"
            )
        lighter_network = str(lighter_settings.get("network") or "mainnet").strip().lower()
        exchange_config = ExchangeConfig(
            exchange_id="lighter",
            name="Lighter",
            exchange_type=market_type,
            api_key="",
            api_secret="",
            enable_websocket=True,
            enable_auto_reconnect=True,
            extra_params={"network": lighter_network},
        )
        print(f"   - Lighter network: {lighter_network} (credentials from {LIGHTER_CONFIG_PATH})")
    elif exchange_name == "arcus":
        arcus_settings = _read_arcus_settings()
        exchange_config = ExchangeConfig(
            exchange_id="arcus",
            name="Arcus",
            exchange_type=market_type,
            api_key="",
            api_secret=api_secret or "",
            wallet_address=wallet_address,
            enable_websocket=True,
            enable_auto_reconnect=True,
            extra_params=arcus_settings,
        )
        print(f"   - Arcus network: {arcus_settings['network']}")
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
    connected = await adapter.connect()
    if not connected:
        try:
            await adapter.disconnect()
        except Exception:
            pass
        raise RuntimeError(f"Failed to connect to exchange: {exchange_name}")
    return adapter


async def main(
    config_path: str = "config/grid/default_grid.yaml",
    debug: bool = False,
    wallet_name: str | None = None,
) -> None:
    """
    Main entrypoint.

    Args:
        config_path: Configuration file path.
        debug: Whether to enable debug mode.
        wallet_name: Optional wallet profile name from config/exchanges/wallets.
    """
    print("\nStep 1/6: Loading configuration...")
    config_data = await load_config(config_path)
    if wallet_name:
        _load_wallet_profile(wallet_name)
    log_dir = build_grid_log_dir(config_data, wallet_name=wallet_name)
    # Append across restarts; size rotation bounds disk use and keeps history.
    initialize(log_dir=log_dir, clear_existing=False)
    from core.services.grid.coordinator import GridCoordinator
    from core.services.grid.implementations import (
        GridEngineImpl,
        GridStrategyImpl,
        PositionTrackerImpl,
    )
    from core.services.grid.models import GridState
    from core.services.grid.terminal_ui import GridTerminalUI

    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

        for module in ["core.services.grid", "core.adapters.exchanges", "ExchangeAdapter"]:
            logging.getLogger(module).setLevel(logging.DEBUG)

        lighter_ws_logger = logging.getLogger(
            "core.adapters.exchanges.adapters.lighter_websocket"
        )
        lighter_ws_logger.setLevel(logging.DEBUG)

        exchange_log_path = (Path(log_dir) / "ExchangeAdapter.log").resolve()
        has_exchange_log_handler = any(
            isinstance(handler, logging.FileHandler)
            and Path(getattr(handler, "baseFilename", "")).resolve() == exchange_log_path
            for handler in lighter_ws_logger.handlers
        )

        if not has_exchange_log_handler:
            ws_handler = build_rotating_file_handler(str(exchange_log_path))
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
    from core.adapters.exchanges.models import ExchangeType

    runtime_error = None
    unsafe_shutdown_incident = None
    try:
        # 1. Load configuration.
        grid_config = create_grid_config(config_data)
        print("Configuration loaded successfully")
        print(f"   - Exchange: {grid_config.exchange}")
        print(f"   - Symbol: {grid_config.symbol}")
        print(f"   - Grid type: {grid_config.grid_type.value}")
        if wallet_name:
            print(f"   - Wallet profile: {wallet_name}")
        print(f"   - Log directory: {Path(log_dir)}")

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
        exchange_adapter = await create_exchange_adapter(
            config_data,
            wallet_name=wallet_name,
        )
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
            from core.services.grid.reserve import (
                ReserveMonitor,
                SpotReserveManager,
                check_spot_reserve_on_startup,
            )

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
        active_startup_orders = len(engine.get_pending_orders())
        print(f"   - Active startup order count: {active_startup_orders}")

        if reserve_monitor:
            await reserve_monitor.start()
            print("Reserve monitor started")

        if grid_config.is_follow_mode():
            print(
                f"   - Effective price range: ${grid_config.lower_price:,.2f} - ${grid_config.upper_price:,.2f}"
            )

        print("   - Initial grid placement verified, waiting for fills...")

        # 6. Start the terminal UI.
        print("\nStep 6/6: Starting terminal UI...")
        terminal_ui = GridTerminalUI(coordinator)

        print("=" * 70)
        print("Grid Trading System Fully Started")
        print("=" * 70)
        print()

        await terminal_ui.run()
        fatal_reason_getter = getattr(coordinator, "get_fatal_stop_reason", None)
        fatal_reason = fatal_reason_getter() if callable(fatal_reason_getter) else None
        if fatal_reason:
            raise RuntimeError(f"Grid stopped automatically: {fatal_reason}")

    except (KeyboardInterrupt, asyncio.CancelledError):
        # asyncio.run turns SIGINT into task cancellation. Handle it here so
        # cleanup failures below can still produce a non-zero process exit.
        print("\n\nExit signal received, stopping the system...")

    except Exception as exc:
        runtime_error = exc
        logger.error(f"System error: {exc}", exc_info=True)
        print(f"\nSystem error: {exc}")

    finally:
        print("\nCleaning up resources...")
        cleanup_errors = []

        if "reserve_monitor" in locals() and reserve_monitor:
            try:
                await reserve_monitor.stop()
                print("   - Reserve monitor stopped")
            except Exception as exc:
                cleanup_errors.append(f"reserve monitor: {exc}")

        if "coordinator" in locals():
            try:
                await coordinator.stop()
                print("   - Grid system stopped")
            except Exception as exc:
                cleanup_errors.append(f"grid stop: {exc}")
            finally:
                incident_getter = getattr(
                    coordinator,
                    "get_unsafe_shutdown_incident",
                    None,
                )
                if callable(incident_getter):
                    unsafe_shutdown_incident = incident_getter()

        if "exchange_adapter" in locals():
            try:
                await exchange_adapter.disconnect()
                print("   - Exchange disconnected")
            except Exception as exc:
                cleanup_errors.append(f"exchange disconnect: {exc}")

        if unsafe_shutdown_incident and not any(
            error.startswith("grid stop:") for error in cleanup_errors
        ):
            cleanup_errors.append(
                "grid recovered from an earlier unsafe shutdown incident: "
                f"{unsafe_shutdown_incident}"
            )

        if cleanup_errors:
            print("\nCleanup completed with errors:")
            for error in cleanup_errors:
                print(f"   - {error}")
        elif runtime_error is None:
            print("\nSystem exited safely")
        else:
            print("\nResources cleaned up after system failure")

    if cleanup_errors:
        cleanup_error = RuntimeError("; ".join(cleanup_errors))
        if runtime_error is not None:
            raise cleanup_error from runtime_error
        raise cleanup_error
    if runtime_error is not None:
        raise runtime_error


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

  # TradeXYZ with a config filename and named agent wallet profile
  python3 run_grid_trading.py tradexyz_NVDA_long.yaml --nvda01

Supported exchanges:
  Backpack    - Perpetual markets (long / short)
  Hyperliquid - Perpetual markets (long / short), spot (long only)
  Lighter     - Perpetual markets (long / short)
  TradeXYZ    - Perpetual markets through Hyperliquid DEX support
  Arcus       - Perpetual markets (long / short), symbols like BTC-USD

Notes:
  1. Make sure API credentials are configured correctly.
  2. Make sure the account has enough capital for grid trading.
  3. Start with small size before using larger capital.
  4. Spot markets support long grids only.
  5. The grid system runs continuously until it is stopped manually.
  6. Use Ctrl+C or Q to exit safely.
  7. DEBUG mode prints detailed logs for troubleshooting.
  8. Named wallet profiles can be selected with --<name>, for example --main.
        """,
    )

    parser.add_argument(
        "config",
        type=str,
        help="Grid config file path or filename under config/grid",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG mode and print detailed diagnostic logs",
    )

    parser.add_argument(
        "--wallet-name",
        "--walletname",
        dest="wallet_name",
        type=_validate_wallet_name,
        help="Use a named wallet profile from config/exchanges/wallets/<name>.yaml (legacy form; prefer --<name>)",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="Grid Trading System v2.0.0",
    )

    args, unknown_args = parser.parse_known_args()
    return _apply_wallet_shortcut(args, unknown_args, parser)


if __name__ == "__main__":
    try:
        # Parse command-line arguments.
        args = parse_arguments()

        # Resolve the config path.
        config_path = resolve_config_path(args.config)
        wallet_name = args.wallet_name

        # Ensure the config file exists before startup.
        if not Path(config_path).exists():
            print(f"Config file does not exist: {args.config}")
            requested_path = Path(args.config)
            if not requested_path.is_absolute() and len(requested_path.parts) == 1:
                print(f"Also checked: {GRID_CONFIG_DIR / requested_path}")
            elif args.config == config_path:
                print("Tried the provided path directly.")
            else:
                print(f"Resolved path was: {config_path}")
            print("\nUse -h or --help to view usage")
            sys.exit(1)

        if config_path != args.config:
            print(f"Resolved config file: {config_path}")

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
        asyncio.run(main(config_path, debug=args.debug, wallet_name=wallet_name))

    except KeyboardInterrupt:
        print("\nProgram exited")
    except SystemExit:
        # argparse --help and --version trigger SystemExit intentionally.
        raise
    except WalletProfileError as exc:
        print(f"\nStartup failed: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"\nProgram failed: {exc}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

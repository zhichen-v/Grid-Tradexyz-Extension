"""Authenticated, read-only preflight for Lighter's Robinhood instances."""

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Callable

import yaml
from lighter.endpoint_profiles import get_endpoint_profile

DEFAULT_CONFIG = Path("config/exchanges/lighter_config.yaml")
DEFAULT_ENV = Path(".env")
ROBINHOOD_NETWORKS = {"robinhood", "robinhood_testnet"}

# .env 欄位名稱 → load_settings 的 settings 欄位名稱
_ENV_FIELD_NAMES = (
    ("LIGHTER_NETWORK", "network"),
    ("LIGHTER_API_KEY_PRIVATE_KEY", "api_key_private_key"),
    ("LIGHTER_ACCOUNT_INDEX", "account_index"),
    ("LIGHTER_API_KEY_INDEX", "api_key_index"),
    ("LIGHTER_EXPECTED_L1_ADDRESS", "expected_l1_address"),
)


def _read_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file (skips comments and blank lines)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _env_fill_in(env_path: Path) -> dict[str, Any]:
    """Non-empty LIGHTER_* values from .env, keyed by settings field name.

    只回傳非空的欄位；空值代表「未配置」，不會覆蓋 YAML。
    """
    env_values = _read_env_file(env_path)
    return {
        field: env_values[env_key]
        for env_key, field in _ENV_FIELD_NAMES
        if env_values.get(env_key, "").strip()
    }


def _wallet_address(value: Any) -> str | None:
    if value in (None, ""):
        return None
    address = str(value).strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("expected L1 wallet address must be a 20-byte 0x EVM address")
    return address.lower()


def load_settings(path: Path, env_path: Path = DEFAULT_ENV) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(
            f"Config not found: {path}. Copy lighter_config_example.yaml first."
        )

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        raise ValueError(f"Invalid YAML in config: {path}") from None

    # .env 填空：YAML 留空的欄位由 .env 的非空值補上（YAML 有值時以 YAML 為準）。
    fill_in = _env_fill_in(env_path)
    if fill_in:
        api_config = data.get("api_config") or {}
        auth = dict(api_config.get("auth") or {})
        for field, value in fill_in.items():
            if field == "network":
                if not str(api_config.get("network") or "").strip():
                    api_config = {**api_config, "network": value}
            elif not str(auth.get(field) or "").strip():
                auth[field] = value
        data = {**data, "api_config": {**api_config, "auth": auth}}

    api_config = data.get("api_config") or {}
    auth = api_config.get("auth") or {}
    network = str(api_config.get("network") or "").strip().lower()

    if network not in ROBINHOOD_NETWORKS:
        raise ValueError("api_config.network must be robinhood or robinhood_testnet")

    expected_testnet = network.endswith("_testnet")
    configured_testnet = api_config.get("testnet")
    if configured_testnet is not None:
        if not isinstance(configured_testnet, bool):
            raise ValueError("api_config.testnet must be true or false")
        if configured_testnet != expected_testnet:
            raise ValueError("api_config.testnet conflicts with api_config.network")

    private_key = auth.get("api_key_private_key")
    if not isinstance(private_key, str) or not private_key.strip():
        raise ValueError("api_config.auth.api_key_private_key is required")

    settings = {
        "network": network,
        "testnet": expected_testnet,
        "api_key_private_key": private_key.strip(),
        "expected_l1_address": _wallet_address(auth.get("expected_l1_address")),
    }
    for field in ("account_index", "api_key_index"):
        value = auth.get(field)
        if isinstance(value, bool):
            raise ValueError(f"api_config.auth.{field} must be a non-negative integer")
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"api_config.auth.{field} must be a non-negative integer"
            ) from None
        if value < 0:
            raise ValueError(f"api_config.auth.{field} must be a non-negative integer")
        settings[field] = value

    if settings["api_key_index"] > 254:
        raise ValueError("api_config.auth.api_key_index must be between 0 and 254")

    return settings


def build_adapter(settings: dict[str, Any]) -> Any:
    from core.adapters.exchanges.adapters.lighter import LighterAdapter
    from core.adapters.exchanges.interface import ExchangeConfig
    from core.adapters.exchanges.models import ExchangeType

    config = ExchangeConfig(
        exchange_id="lighter",
        name="Lighter",
        exchange_type=ExchangeType.PERPETUAL,
        api_key="",
        api_secret="",
        testnet=settings["testnet"],
        extra_params={
            "network": settings["network"],
            "load_credentials_from_file": False,
        },
    )
    config.api_key_private_key = settings["api_key_private_key"]
    config.account_index = settings["account_index"]
    config.api_key_index = settings["api_key_index"]
    return LighterAdapter(config)


def _display(value: Any) -> str:
    if value is None:
        return "-"
    return str(getattr(value, "value", value))


async def run_read_only_preflight(
    adapter: Any,
    settings: dict[str, Any],
    symbol: str,
    emit: Callable[[str], None] = print,
) -> None:
    """Run authenticated reads only. This function never submits or cancels orders."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol must not be empty")

    try:
        if not await adapter.connect():
            raise RuntimeError("adapter connection or SignerClient check failed")
        if not await adapter.authenticate():
            raise RuntimeError("SignerClient is not configured")

        rest = adapter._rest
        profile = get_endpoint_profile(settings["network"])
        if rest.network != profile.name:
            raise RuntimeError(
                f"adapter network mismatch: expected {profile.name}, got {rest.network}"
            )
        if rest.chain_id != profile.chain_id:
            raise RuntimeError(
                f"adapter chain mismatch: expected {profile.chain_id}, got {rest.chain_id}"
            )
        if rest.base_url.rstrip("/") != profile.api_url.rstrip("/"):
            raise RuntimeError("adapter API URL does not match the official endpoint profile")
        if rest.account_index != settings["account_index"]:
            raise RuntimeError("adapter account index does not match the config")
        if rest.api_key_index != settings["api_key_index"]:
            raise RuntimeError("adapter API key index does not match the config")
        if rest.signer_client is None:
            raise RuntimeError("SignerClient was not created")

        account_response = await rest.account_api.account(
            by="index", value=str(rest.account_index)
        )
        rest._require_success_response(account_response, "preflight account query")
        accounts = getattr(account_response, "accounts", None) or []
        if not accounts:
            raise RuntimeError("configured account index was not returned by Lighter")
        account = accounts[0]
        returned_index = getattr(
            account, "account_index", getattr(account, "index", None)
        )
        if returned_index is None or int(returned_index) != rest.account_index:
            raise RuntimeError("Lighter returned a different account index")
        returned_l1_address = _wallet_address(getattr(account, "l1_address", None))
        if returned_l1_address is None:
            raise RuntimeError("Lighter account response did not include an L1 address")
        expected_l1_address = settings.get("expected_l1_address")
        if expected_l1_address and returned_l1_address != expected_l1_address:
            raise RuntimeError("configured account index belongs to a different L1 wallet")

        native_symbol = rest.normalize_symbol(symbol)
        market = rest.markets.get(native_symbol)
        if not market:
            raise ValueError(f"active market not found: {symbol}")

        balances = await adapter.get_balances()
        positions = await adapter.get_positions()
        orders = await adapter.get_open_orders()
        usdg = next((item for item in balances if item.currency == "USDG"), None)
        if usdg is None:
            raise RuntimeError("balance query returned no USDG record")

        emit("Lighter authenticated preflight (READ ONLY)")
        emit(
            f"profile={profile.name} chain_id={profile.chain_id} "
            f"api_url={profile.api_url}"
        )
        emit(
            f"signer_check=PASS account_index={rest.account_index} "
            f"api_key_index={rest.api_key_index}"
        )
        emit(
            "wallet_check=PASS"
            if expected_l1_address
            else "wallet_check=SKIPPED (set expected_l1_address or use the CLI flag)"
        )
        emit(
            "USDG "
            f"total={_display(getattr(usdg, 'total', 0))} "
            f"free={_display(getattr(usdg, 'free', 0))} "
            f"used={_display(getattr(usdg, 'used', 0))}"
        )
        emit(
            f"market={native_symbol} market_id={market.get('market_id')} "
            f"status={market.get('status')} "
            f"price_decimals={market.get('supported_price_decimals')} "
            f"size_decimals={market.get('supported_size_decimals')} "
            f"min_base={market.get('min_base_amount')} "
            f"min_quote={market.get('min_quote_amount')}"
        )

        emit(f"positions={len(positions)}")
        for position in positions:
            emit(
                f"  {_display(position.symbol)} {_display(position.side)} "
                f"size={_display(position.size)} entry={_display(position.entry_price)} "
                f"leverage={_display(position.leverage)} "
                f"margin_mode={_display(position.margin_mode)}"
            )

        emit(f"open_orders={len(orders)}")
        for order in orders:
            order_type = getattr(order, "order_type", getattr(order, "type", None))
            emit(
                f"  id={_display(order.id)} {_display(order.symbol)} "
                f"{_display(order.side)} {_display(order_type)} "
                f"amount={_display(order.amount)} price={_display(order.price)} "
                f"remaining={_display(order.remaining)} status={_display(order.status)}"
            )

        emit("PASS: authenticated reads completed; no orders were submitted or cancelled")
    finally:
        await adapter.disconnect()


async def preflight_from_config(
    config_path: Path,
    symbol: str,
    emit: Callable[[str], None] = print,
    adapter_factory: Callable[[dict[str, Any]], Any] = build_adapter,
    expected_wallet_address: str | None = None,
) -> None:
    settings = load_settings(config_path)
    await _preflight_with_settings(
        settings,
        symbol,
        emit=emit,
        adapter_factory=adapter_factory,
        expected_wallet_address=expected_wallet_address,
    )


async def _preflight_with_settings(
    settings: dict[str, Any],
    symbol: str,
    emit: Callable[[str], None] = print,
    adapter_factory: Callable[[dict[str, Any]], Any] = build_adapter,
    expected_wallet_address: str | None = None,
) -> None:
    """Build the SDK adapter inside the active event loop and run read-only checks."""
    settings = dict(settings)
    cli_address = _wallet_address(expected_wallet_address)
    configured_address = settings.get("expected_l1_address")
    if cli_address and configured_address and cli_address != configured_address:
        raise ValueError("CLI wallet address conflicts with expected_l1_address in config")
    settings["expected_l1_address"] = cli_address or configured_address
    adapter = adapter_factory(settings)
    await run_read_only_preflight(adapter, settings, symbol, emit)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authenticated read-only preflight for Lighter Robinhood instances"
    )
    parser.add_argument("--symbol", default="ETH", help="Target perp symbol (default: ETH)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--expected-wallet-address",
        help="Optional public EVM/L1 address that must own the configured account index",
    )
    args = parser.parse_args()

    private_key = ""
    try:
        settings = load_settings(args.config)
        private_key = settings["api_key_private_key"]
        asyncio.run(
            _preflight_with_settings(
                settings,
                args.symbol,
                expected_wallet_address=args.expected_wallet_address,
                adapter_factory=build_adapter,
            )
        )
    except Exception as exc:
        message = str(exc)
        if private_key:
            message = message.replace(private_key, "<redacted>")
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

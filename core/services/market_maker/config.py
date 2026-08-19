from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import MarketMetadata


_FORBIDDEN_SECRET_FIELDS = {
    "api_key",
    "api_secret",
    "private_key",
    "secret_key",
    "wallet_private_key",
}


def parse_decimal(value: Any, field_name: str = "value") -> Decimal:
    """Convert a scalar to a finite Decimal without binary-float arithmetic."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError(f"{field_name} must be a decimal scalar")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _validate_step_inputs(value: Decimal, step: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("value must be a finite Decimal")
    if not isinstance(step, Decimal) or not step.is_finite() or step <= 0:
        raise ValueError("step must be a finite positive Decimal")


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    _validate_step_inputs(value, step)
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    _validate_step_inputs(value, step)
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def is_step_aligned(value: Decimal, step: Decimal) -> bool:
    _validate_step_inputs(value, step)
    return value % step == 0


@dataclass(frozen=True)
class MarketMakerConfig:
    exchange: str = "lighter"
    symbol: str = "BTC"
    order_size: Decimal = Decimal("0.00020")
    base_half_spread_ticks: int = 1
    max_inventory_skew_ticks: int = 4
    reprice_threshold_ticks: int = 1
    maker_fee_rate: Decimal = Decimal("0")
    min_profit_buffer_bps: Decimal = Decimal("0.5")
    max_position: Decimal = Decimal("0.00200")
    soft_position_ratio: Decimal = Decimal("0.50")
    hard_position_ratio: Decimal = Decimal("0.80")
    refresh_interval_ms: int = 1000
    min_order_lifetime_ms: int = 1000
    stale_book_seconds: int = 3
    stale_position_seconds: int = 10
    position_poll_interval_seconds: int = 3
    order_sync_interval_seconds: int = 10
    health_check_interval_seconds: int = 60
    max_consecutive_errors: int = 5
    error_cooldown_seconds: int = 5
    max_mutations_per_minute: int = 30
    post_only: bool = True
    exclusive_symbol_control: bool = True
    startup_open_order_policy: str = "abort"
    unknown_order_policy: str = "pause"
    cancel_on_shutdown: bool = True
    dry_run: bool = True
    log_status_interval_seconds: int = 10

    def __post_init__(self) -> None:
        for name in (
            "order_size",
            "maker_fee_rate",
            "min_profit_buffer_bps",
            "max_position",
            "soft_position_ratio",
            "hard_position_ratio",
        ):
            object.__setattr__(self, name, parse_decimal(getattr(self, name), name))

        if self.exchange != "lighter":
            raise ValueError("exchange must be 'lighter' for the MVP")
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if self.order_size <= 0:
            raise ValueError("order_size must be positive")
        if self.max_position <= 0:
            raise ValueError("max_position must be positive")
        if self.order_size > self.max_position:
            raise ValueError("order_size cannot exceed max_position")

        self._validate_int("base_half_spread_ticks", minimum=1)
        self._validate_int("max_inventory_skew_ticks", minimum=0)
        self._validate_int("reprice_threshold_ticks", minimum=1)
        if self.maker_fee_rate < 0:
            raise ValueError("maker_fee_rate cannot be negative")
        if self.min_profit_buffer_bps < 0:
            raise ValueError("min_profit_buffer_bps cannot be negative")
        if not (
            Decimal("0")
            < self.soft_position_ratio
            < self.hard_position_ratio
            <= Decimal("1")
        ):
            raise ValueError(
                "position ratios must satisfy 0 < soft < hard <= 1"
            )

        for name in (
            "refresh_interval_ms",
            "min_order_lifetime_ms",
            "stale_book_seconds",
            "stale_position_seconds",
            "position_poll_interval_seconds",
            "order_sync_interval_seconds",
            "health_check_interval_seconds",
            "max_consecutive_errors",
            "error_cooldown_seconds",
            "max_mutations_per_minute",
            "log_status_interval_seconds",
        ):
            self._validate_int(name, minimum=1)

        for name in (
            "post_only",
            "exclusive_symbol_control",
            "cancel_on_shutdown",
            "dry_run",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if not self.post_only:
            raise ValueError("post_only must be true")
        if (
            not isinstance(self.startup_open_order_policy, str)
            or self.startup_open_order_policy not in {"abort", "cancel_all"}
        ):
            raise ValueError("unsupported startup_open_order_policy")
        if (
            self.startup_open_order_policy == "cancel_all"
            and not self.exclusive_symbol_control
        ):
            raise ValueError(
                "cancel_all startup policy requires exclusive_symbol_control"
            )
        if (
            not isinstance(self.unknown_order_policy, str)
            or self.unknown_order_policy != "pause"
        ):
            raise ValueError("unsupported unknown_order_policy")

    def _validate_int(self, name: str, minimum: int) -> None:
        value = getattr(self, name)
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")

    def validate_order_size(
        self,
        metadata: MarketMetadata,
        target_price: Decimal | None = None,
    ) -> None:
        """Fail startup if configured size violates exchange metadata."""
        if metadata.symbol != self.symbol:
            raise ValueError("market metadata symbol does not match config")
        if not is_step_aligned(self.order_size, metadata.quantity_step):
            raise ValueError("order_size is not aligned to quantity_step")
        if self.order_size < metadata.min_base_amount:
            raise ValueError("order_size is below min_base_amount")
        if target_price is not None:
            price = parse_decimal(target_price, "target_price")
            if price <= 0:
                raise ValueError("target_price must be positive")
            if self.order_size * price < metadata.min_quote_amount:
                raise ValueError("order notional is below min_quote_amount")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "MarketMakerConfig":
        if not isinstance(values, Mapping):
            raise ValueError("market_maker must be a mapping")
        if any(not isinstance(key, str) for key in values):
            raise ValueError("market_maker field names must be strings")
        known_fields = {field.name for field in fields(cls)}
        unknown = set(values) - known_fields
        forbidden = {str(key).lower() for key in values} & _FORBIDDEN_SECRET_FIELDS
        if forbidden:
            raise ValueError("secret fields are not allowed in market maker config")
        if unknown:
            raise ValueError(f"unknown market_maker fields: {sorted(unknown)}")
        return cls(**dict(values))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "MarketMakerConfig":
        return load_market_maker_config(path)


def load_market_maker_config(path: str | Path) -> MarketMakerConfig:
    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("invalid market maker YAML") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("config root must be a mapping")
    if set(loaded) != {"market_maker"}:
        raise ValueError("config must contain only a market_maker block")
    block = loaded["market_maker"]
    if not isinstance(block, Mapping):
        raise ValueError("market_maker must be a mapping")
    return MarketMakerConfig.from_mapping(block)

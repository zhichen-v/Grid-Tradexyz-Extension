"""The 18-field V2 schema; safety constants and per-run authorization are not YAML."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

import yaml


class ConfigError(ValueError):
    """Sanitized configuration or authorization failure; no connection was needed."""


@dataclass(frozen=True, slots=True)
class LighterVolumeRuntimeProfile:
    stale_book_seconds: int = field(default=3, init=False)
    stale_position_seconds: int = field(default=10, init=False)
    max_flatten_attempts: int = field(default=3, init=False)
    max_flatten_seconds: int = field(default=30, init=False)


LIGHTER_VOLUME_RUNTIME_PROFILE = LighterVolumeRuntimeProfile()
_FINANCIAL_FIELDS = frozenset({
    "order_size", "target_net_edge_bps", "volatility_multiplier", "soft_limit",
    "hard_limit", "skew_bps_at_hard", "stop_loss_usdg", "max_loss_usdg",
})


def _money(value, name, *, positive=False):
    if (type(value) is not Decimal or not value.is_finite()
            or value < 0 or (positive and value == 0)):
        raise ConfigError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} Decimal")


def _integer(value, name, *, minimum=1):
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True, slots=True)
class QuoteConfig:
    order_size: Decimal
    target_net_edge_bps: Decimal
    volatility_multiplier: Decimal
    reprice_threshold_ticks: int
    max_quote_age_ms: int

    def __post_init__(self):
        _money(self.order_size, "order_size", positive=True)
        _money(self.target_net_edge_bps, "target_net_edge_bps")
        _money(self.volatility_multiplier, "volatility_multiplier")
        _integer(self.reprice_threshold_ticks, "reprice_threshold_ticks")
        _integer(self.max_quote_age_ms, "max_quote_age_ms")
        if self.target_net_edge_bps >= Decimal("20000"):
            raise ConfigError("target_net_edge_bps cannot eliminate the positive bid")


@dataclass(frozen=True, slots=True)
class InventoryConfig:
    soft_limit: Decimal
    hard_limit: Decimal
    skew_bps_at_hard: Decimal

    def __post_init__(self):
        _money(self.soft_limit, "soft_limit", positive=True)
        _money(self.hard_limit, "hard_limit", positive=True)
        _money(self.skew_bps_at_hard, "skew_bps_at_hard")
        if not self.soft_limit < self.hard_limit:
            raise ConfigError("inventory requires soft_limit < hard_limit")
        if self.skew_bps_at_hard >= Decimal("10000"):
            raise ConfigError("skew_bps_at_hard must keep reservation positive")


@dataclass(frozen=True, slots=True)
class FlattenConfig:
    max_hold_seconds: int
    stop_loss_usdg: Decimal
    passive_grace_seconds: int
    ioc_slippage_ticks: int

    def __post_init__(self):
        _integer(self.max_hold_seconds, "max_hold_seconds")
        _money(self.stop_loss_usdg, "stop_loss_usdg", positive=True)
        _integer(self.passive_grace_seconds, "passive_grace_seconds", minimum=0)
        _integer(self.ioc_slippage_ticks, "ioc_slippage_ticks", minimum=0)
        if self.passive_grace_seconds >= LIGHTER_VOLUME_RUNTIME_PROFILE.max_flatten_seconds:
            raise ConfigError("passive grace must leave time inside the fixed flatten deadline")


@dataclass(frozen=True, slots=True)
class SessionConfig:
    duration_seconds: int
    max_loss_usdg: Decimal
    cooldown_seconds: int

    def __post_init__(self):
        _integer(self.duration_seconds, "duration_seconds")
        _money(self.max_loss_usdg, "max_loss_usdg", positive=True)
        _integer(self.cooldown_seconds, "cooldown_seconds", minimum=0)


def _mapping(values, expected, *, optional=()):
    if (not isinstance(values, Mapping) or any(type(key) is not str for key in values)
            or set(values) - set(expected) or set(expected) - set(values) - set(optional)):
        raise ConfigError("configuration has unknown, missing, or invalid fields")
    return dict(values)


def _section(cls, values):
    parsed = _mapping(values, {field.name for field in fields(cls)})
    for name in parsed.keys() & _FINANCIAL_FIELDS:
        if type(parsed[name]) is not str:
            raise ConfigError(f"{name} must be a quoted decimal string")
        try:
            parsed[name] = Decimal(parsed[name])
        except InvalidOperation:
            raise ConfigError(f"{name} must be a valid decimal string") from None
    return cls(**parsed)


@dataclass(frozen=True, slots=True)
class MarketMakerV2Config:
    symbol: str
    profile: str
    quote: QuoteConfig
    inventory: InventoryConfig
    flatten: FlattenConfig
    session: SessionConfig
    dry_run: bool = True

    def __post_init__(self):
        if type(self.symbol) is not str or re.fullmatch(r"[A-Z0-9][A-Z0-9_.:-]{0,31}", self.symbol) is None:
            raise ConfigError("symbol must be an uppercase market identifier")
        if type(self.profile) is not str or self.profile != "fee_neutral_volume_v1":
            raise ConfigError("only fee_neutral_volume_v1 is supported")
        if type(self.dry_run) is not bool:
            raise ConfigError("dry_run must be a literal boolean")
        for name, cls in (("quote", QuoteConfig), ("inventory", InventoryConfig),
                          ("flatten", FlattenConfig), ("session", SessionConfig)):
            if type(getattr(self, name)) is not cls:
                raise ConfigError("typed V2 config sections required")
        if self.quote.order_size > self.inventory.hard_limit:
            raise ConfigError("order_size cannot exceed hard inventory limit")
        if self.flatten.stop_loss_usdg > self.session.max_loss_usdg:
            raise ConfigError("inventory stop loss cannot exceed session loss limit")

    @classmethod
    def from_mapping(cls, values: Mapping) -> "MarketMakerV2Config":
        """Parse the inner market_maker_v2 block; only dry_run may be omitted."""
        parsed = _mapping(values, {field.name for field in fields(cls)}, optional=("dry_run",))
        for name, section in (("quote", QuoteConfig), ("inventory", InventoryConfig),
                              ("flatten", FlattenConfig), ("session", SessionConfig)):
            parsed[name] = _section(section, parsed[name])
        return cls(**parsed)


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node):
    if not isinstance(node, yaml.MappingNode):
        raise ConfigError("YAML config sections must be mappings")
    values = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if type(key) is not str or key in values:
            raise ConfigError("YAML requires unique string keys")
        if key in _FINANCIAL_FIELDS and (value_node.tag != "tag:yaml.org,2002:str"
                                             or value_node.style not in {"'", '"'}):
            raise ConfigError("financial YAML values must be quoted decimal strings")
        values[key] = loader.construct_object(value_node, deep=True)
    return values


def _construct_boolean(loader, node):
    if node.value not in {"true", "false"}:
        raise ConfigError("YAML booleans must be literal true or false")
    return node.value == "true"


_StrictLoader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)
_StrictLoader.add_constructor("tag:yaml.org,2002:bool", _construct_boolean)


def load_config(path: str | Path) -> MarketMakerV2Config:
    try:
        loaded = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_StrictLoader)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("unable to read valid V2 YAML") from None
    root = _mapping(loaded, {"market_maker_v2"})
    return MarketMakerV2Config.from_mapping(root["market_maker_v2"])


def require_authorization(config: MarketMakerV2Config, authorized: bool = False) -> None:
    """Call before constructing an adapter or connecting; authorization is per run."""
    if type(config) is not MarketMakerV2Config or type(authorized) is not bool:
        raise ConfigError("typed config and literal per-run authorization required")
    if not config.dry_run and not authorized:
        raise ConfigError("live startup requires --authorize-bounded-flatten")

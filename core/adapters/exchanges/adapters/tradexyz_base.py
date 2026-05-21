"""
TradeXYZ base helpers.

TradeXYZ reuses Hyperliquid's API endpoints, while XYZ assets are exposed
through the `dex="xyz"` namespace and use `xyz:`-prefixed coin identifiers.
"""

from pathlib import Path
from typing import List, Set

import yaml

from .hyperliquid_base import HyperliquidBase


class TradeXYZBase(HyperliquidBase):
    """Base utilities for symbol normalization and config loading."""

    XYZ_DEX = "xyz"
    XYZ_COIN_PREFIX = "xyz:"
    XYZ_QUOTE_SUFFIXES = {"USD", "USDC", "USDT", "PERP"}

    XYZ_ASSET_CLASSES = {
        "us_stocks": [
            "AAPL",
            "AMZN",
            "GOOGL",
            "META",
            "MSFT",
            "NVDA",
            "TSLA",
            "AMD",
            "MU",
            "PLTR",
            "COIN",
            "MSTR",
            "NFLX",
            "CRM",
            "UBER",
            "SQ",
            "SHOP",
            "SNOW",
            "ABNB",
            "RBLX",
            "HOOD",
            "BA",
            "DIS",
            "JPM",
            "V",
            "MA",
            "WMT",
            "KO",
            "PEP",
            "MCD",
            "NKE",
            "PYPL",
            "INTC",
            "QCOM",
            "AVGO",
        ],
        "indices": ["SP500", "XYZ100"],
        "commodities": [
            "GOLD",
            "SILVER",
            "PLATINUM",
            "PALLADIUM",
            "COPPER",
            "WTIOIL",
            "BRENTOIL",
            "NATGAS",
        ],
        "fx": ["EUR/USD", "USD/JPY"],
        "korean_equities": ["SMSN", "SKHX", "HYUNDAI", "EWY"],
    }

    def __init__(self, config=None):
        super().__init__(config)
        self.xyz_market_enabled = True
        self.xyz_config = {}
        self._load_xyz_config()

    def _setup_urls(self):
        if self.config:
            self.base_url = self.config.base_url or self.DEFAULT_REST_URL
            self.ws_url = self.config.ws_url or self.DEFAULT_WS_URL
        else:
            self.base_url = self.DEFAULT_REST_URL
            self.ws_url = self.DEFAULT_WS_URL

    def _load_xyz_config(self):
        config_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "config"
            / "exchanges"
            / "tradexyz_config.yaml"
        )

        try:
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as file:
                    self.xyz_config = yaml.safe_load(file) or {}

                xyz_settings = self.xyz_config.get("tradexyz", {})
                xyz_market = xyz_settings.get("xyz_market", {})
                self.xyz_market_enabled = xyz_market.get("enabled", True)
            else:
                self.xyz_config = {}
        except Exception as exc:
            self.xyz_config = {}
            if self.logger:
                self.logger.error(f"Failed to load TradeXYZ config: {exc}")

    def is_xyz_symbol(self, symbol: str) -> bool:
        normalized_symbol = self._normalize_symbol_text(symbol)
        if not normalized_symbol:
            return False

        if normalized_symbol.startswith(self.XYZ_COIN_PREFIX.upper()):
            return True

        xyz_assets = self._get_xyz_asset_set()
        base_symbol = self._extract_base_symbol(normalized_symbol)
        return normalized_symbol in xyz_assets or base_symbol in xyz_assets

    def to_xyz_coin(self, symbol: str) -> str:
        normalized_symbol = self._normalize_symbol_text(symbol)
        if normalized_symbol.startswith(self.XYZ_COIN_PREFIX.upper()):
            return f"{self.XYZ_COIN_PREFIX}{normalized_symbol.split(':', 1)[1]}"

        base = self._extract_base_symbol(normalized_symbol)
        return f"{self.XYZ_COIN_PREFIX}{base}"

    def from_xyz_coin(self, xyz_coin: str) -> str:
        normalized_coin = self._normalize_symbol_text(xyz_coin)
        if normalized_coin.startswith(self.XYZ_COIN_PREFIX.upper()):
            return normalized_coin[len(self.XYZ_COIN_PREFIX) :]
        return normalized_coin

    def _extract_base_symbol(self, symbol: str) -> str:
        symbol = self._normalize_symbol_text(symbol)

        if symbol.startswith(self.XYZ_COIN_PREFIX.upper()):
            symbol = symbol[len(self.XYZ_COIN_PREFIX) :]

        if ":" in symbol:
            symbol = symbol.split(":", 1)[0]

        if "/" in symbol:
            symbol = symbol.split("/", 1)[0]
        elif "-" in symbol:
            parts = symbol.split("-")
            while len(parts) > 1 and parts[-1] in self.XYZ_QUOTE_SUFFIXES:
                parts.pop()
            symbol = "-".join(parts)

        return symbol.upper()

    @staticmethod
    def _normalize_symbol_text(symbol: str) -> str:
        return str(symbol or "").strip().upper()

    def _get_xyz_asset_set(self) -> Set[str]:
        assets: Set[str] = set()
        self._add_xyz_assets(assets, self.get_xyz_asset_list())
        return assets

    def _get_configured_xyz_assets(self) -> List[str]:
        if not isinstance(self.xyz_config, dict):
            return []

        tradexyz_config = self.xyz_config.get("tradexyz", {})
        if not isinstance(tradexyz_config, dict):
            return []

        symbols_config = tradexyz_config.get("symbols", {})
        if not isinstance(symbols_config, dict):
            return []

        xyz_symbols = symbols_config.get("xyz", [])
        if not isinstance(xyz_symbols, list):
            return []

        return [str(symbol) for symbol in xyz_symbols if symbol]

    def _add_xyz_assets(self, asset_set: Set[str], assets: List[str]) -> None:
        for asset in assets:
            normalized_asset = self._normalize_symbol_text(asset)
            if not normalized_asset:
                continue

            asset_set.add(normalized_asset)
            asset_set.add(self._extract_base_symbol(normalized_asset))

    def to_tradexyz_symbol(self, symbol: str) -> str:
        if self.is_xyz_symbol(symbol):
            base = self._extract_base_symbol(symbol)
            return f"{base}/USD:PERP"
        return symbol

    def map_symbol(self, symbol: str) -> str:
        if self.is_xyz_symbol(symbol):
            return self.to_tradexyz_symbol(symbol)
        return super().map_symbol(symbol)

    def reverse_map_symbol(self, exchange_symbol: str) -> str:
        normalized_symbol = self._normalize_symbol_text(exchange_symbol)

        if normalized_symbol.startswith(self.XYZ_COIN_PREFIX.upper()):
            return self.from_xyz_coin(exchange_symbol)

        if normalized_symbol.endswith("/USD:PERP"):
            base_symbol = normalized_symbol.split("/")[0]
            if self.is_xyz_symbol(base_symbol):
                return base_symbol

        return super().reverse_map_symbol(exchange_symbol)

    def get_xyz_asset_list(self) -> List[str]:
        all_assets: List[str] = []
        seen: Set[str] = set()

        for assets in list(self.XYZ_ASSET_CLASSES.values()) + [
            self._get_configured_xyz_assets()
        ]:
            for asset in assets:
                normalized_asset = self._normalize_symbol_text(asset)
                if not normalized_asset or normalized_asset in seen:
                    continue
                all_assets.append(normalized_asset)
                seen.add(normalized_asset)

        return all_assets

    def get_supported_xyz_symbols(self) -> List[str]:
        return [asset for asset in self.get_xyz_asset_list() if "/" not in asset]

    def get_xyz_assets_by_class(self, asset_class: str) -> List[str]:
        return self.XYZ_ASSET_CLASSES.get(asset_class, [])

    def get_market_type_from_symbol(self, symbol: str) -> str:
        if self.is_xyz_symbol(symbol):
            return "xyz_perpetual"
        return super().get_market_type_from_symbol(symbol)

"""
   交易所適配器子模組

   包含各個具體交易所的適配器實現，每個適配器都基於統一的ExchangeInterface
   介面，提供標準化的交易所功能。

   支援的交易所:
   - Hyperliquid: 永續合約交易所
   - Backpack: 永續合約交易所  
   - Binance: 期貨交易所
   - OKX: 現貨、永續合約、期貨、期權交易所
   - EdgeX: 永續合約交易所
   - Lighter: 永續合約交易所

   每個適配器都包含:
   - 完整的交易功能實現
   - WebSocket即時資料流支援
   - 自動重連和錯誤處理
   - 符合MESA事件驅動架構
   """

from .hyperliquid import HyperliquidAdapter
from .backpack import BackpackAdapter
from .binance import BinanceAdapter
from .okx import OKXAdapter
from .edgex import EdgeXAdapter
from .lighter import LighterAdapter
from .tradexyz import TradeXYZAdapter
from . import lighter_selective_cancel as _lighter_selective_cancel
from .lighter_selective_cancel import install_lighter_selective_cancel
from .lighter_selective_cancel_v3 import (
    PATCH_VERSION as LIGHTER_SELECTIVE_CANCEL_VERSION,
    install_lighter_selective_cancel_v3,
)

install_lighter_selective_cancel()
install_lighter_selective_cancel_v3()
# Prevent a later compatibility installer call from downgrading v3 back to the
# original selective-cancel implementation.
_lighter_selective_cancel.PATCH_VERSION = LIGHTER_SELECTIVE_CANCEL_VERSION

__all__ = [
    'HyperliquidAdapter',
    'BackpackAdapter',
    'BinanceAdapter',
    'OKXAdapter',
    'EdgeXAdapter',
    'LighterAdapter',
    'TradeXYZAdapter'
]
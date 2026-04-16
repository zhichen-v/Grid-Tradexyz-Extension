"""Public logging API with lazy initialization."""

from __future__ import annotations

import os
from typing import Any, Dict

from .logger import (
    BaseLogger,
    DataLogger,
    ErrorLogger,
    ExchangeLogger,
    LogConfig,
    PerformanceLogger,
    SystemLogger,
    TradingLogger,
    get_config,
    get_data_logger as _get_data_logger,
    get_error_logger as _get_error_logger,
    get_exchange_logger as _get_exchange_logger,
    get_health_status,
    get_logger as _get_logger,
    get_performance_logger as _get_performance_logger,
    get_system_logger as _get_system_logger,
    get_trading_logger as _get_trading_logger,
    initialize_logging as _initialize_logging,
    override_console_level,
    restore_console_level,
    set_config,
    shutdown_logging as _shutdown_logging,
)

_auto_initialized = False


def _ensure_initialized() -> None:
    """Initialize logging on first use."""
    if _auto_initialized:
        return

    config_path = "config/logging.yaml"
    if os.path.exists(config_path):
        initialize_logging()
    else:
        initialize_logging()


def get_logger(name: str) -> BaseLogger:
    """Return a generic managed logger."""
    _ensure_initialized()
    return _get_logger(name)


def get_system_logger(name: str = "system") -> SystemLogger:
    """Return a managed system logger."""
    _ensure_initialized()
    return _get_system_logger(name)


def get_trading_logger() -> TradingLogger:
    """Return the shared trading logger."""
    _ensure_initialized()
    return _get_trading_logger()


def get_data_logger(name: str = "data") -> DataLogger:
    """Return a managed data logger."""
    _ensure_initialized()
    return _get_data_logger(name)


def get_error_logger() -> ErrorLogger:
    """Return the shared error logger."""
    _ensure_initialized()
    return _get_error_logger()


def get_exchange_logger(exchange_name: str) -> ExchangeLogger:
    """Return a logger scoped to one exchange adapter."""
    _ensure_initialized()
    return _get_exchange_logger(exchange_name)


def get_performance_logger() -> PerformanceLogger:
    """Return the shared performance logger."""
    _ensure_initialized()
    return _get_performance_logger()


def set_console_log_level(level: str = "WARNING") -> Dict[str, Any]:
    """Temporarily change console verbosity for all managed loggers."""
    _ensure_initialized()
    return override_console_level(level)


def restore_console_log_level(state: Dict[str, Any]) -> None:
    """Restore console verbosity after set_console_log_level()."""
    _ensure_initialized()
    restore_console_level(state)


def initialize_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    enable_console: bool = True,
    clear_existing: bool = True,
) -> bool:
    """Initialize managed logging and mark the package as ready."""
    global _auto_initialized
    success = _initialize_logging(
        log_dir=log_dir,
        level=level,
        enable_console=enable_console,
        clear_existing=clear_existing,
    )
    if success:
        _auto_initialized = True
    return success


def shutdown_logging() -> None:
    """Shutdown managed logging and reset lazy-init state."""
    global _auto_initialized
    _shutdown_logging()
    _auto_initialized = False


initialize = initialize_logging
shutdown = shutdown_logging


__all__ = [
    "LogConfig",
    "BaseLogger",
    "SystemLogger",
    "TradingLogger",
    "DataLogger",
    "ErrorLogger",
    "ExchangeLogger",
    "PerformanceLogger",
    "get_logger",
    "get_system_logger",
    "get_trading_logger",
    "get_data_logger",
    "get_error_logger",
    "get_exchange_logger",
    "get_performance_logger",
    "initialize_logging",
    "shutdown_logging",
    "initialize",
    "shutdown",
    "get_config",
    "set_config",
    "set_console_log_level",
    "restore_console_log_level",
    "get_health_status",
]

"""Unified logging utilities for the trading system."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional


class LogConfig:
    """Central logging configuration."""

    def __init__(
        self,
        log_dir: str = "logs",
        level: str = "INFO",
        console_level: str = "INFO",
        file_level: str = "DEBUG",
        max_log_lines: int = 1000,
        max_file_size: int = 5 * 1024 * 1024,
        backup_count: int = 3,
        enable_console: bool = True,
    ) -> None:
        self.log_dir = log_dir
        self.level = getattr(logging, level.upper())
        self.console_level = getattr(logging, console_level.upper())
        self.file_level = getattr(logging, file_level.upper())
        self.max_log_lines = max_log_lines
        self.max_file_size = max_file_size
        self.backup_count = backup_count
        self.enable_console = enable_console

        Path(log_dir).mkdir(parents=True, exist_ok=True)


def clear_log_files(log_dir: str = "logs") -> None:
    """Remove existing `.log` files so each runtime starts fresh."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    for file_path in log_path.rglob("*.log"):
        if file_path.is_file():
            file_path.unlink(missing_ok=True)


class LineLimitedFileHandler(logging.FileHandler):
    """Reset a log file once it reaches the configured line cap."""

    def __init__(self, filename: str, max_lines: int = 1000, encoding: str = "utf-8"):
        self.max_lines = max(1, int(max_lines))
        super().__init__(filename, mode="a", encoding=encoding)
        self._line_count = self._read_existing_line_count()
        if self._line_count >= self.max_lines:
            self._reset_file()

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        message_line_count = self._count_lines(f"{message}{self.terminator}")

        if self._line_count + message_line_count > self.max_lines:
            self._reset_file()

        super().emit(record)
        self._line_count += message_line_count

    def _reset_file(self) -> None:
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None

        with open(
            self.baseFilename,
            "w",
            encoding=self.encoding or "utf-8",
        ) as file:
            file.write("")

        self._line_count = 0
        self.stream = self._open()

    def _read_existing_line_count(self) -> int:
        if not os.path.exists(self.baseFilename):
            return 0

        with open(
            self.baseFilename,
            "r",
            encoding=self.encoding or "utf-8",
            errors="ignore",
        ) as file:
            return sum(1 for _ in file)

    @staticmethod
    def _count_lines(message: str) -> int:
        if not message:
            return 0
        return len(message.splitlines()) or 1


class BaseLogger:
    """Base logger wrapper with shared formatting."""

    def __init__(self, name: str, config: Optional[LogConfig] = None):
        self.name = name
        self.config = config or LogConfig()
        self.logger = logging.getLogger(name)
        self._setup_logger()

    def _setup_logger(self) -> None:
        self.logger.setLevel(self.config.level)
        self.logger.propagate = False
        self.logger.handlers.clear()

        if self.config.enable_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(self.config.console_level)
            console_handler.setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
            self.logger.addHandler(console_handler)

        log_file = os.path.join(self.config.log_dir, f"{self.name}.log")
        file_handler = LineLimitedFileHandler(
            log_file,
            max_lines=self.config.max_log_lines,
            encoding="utf-8",
        )
        file_handler.setLevel(self.config.file_level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
            )
        )
        self.logger.addHandler(file_handler)

    def debug(self, message: str, **kwargs: Any) -> None:
        extra_info = f" | {self._format_extra(**kwargs)}" if kwargs else ""
        self.logger.debug(f"{message}{extra_info}")

    def info(self, message: str, **kwargs: Any) -> None:
        extra_info = f" | {self._format_extra(**kwargs)}" if kwargs else ""
        self.logger.info(f"{message}{extra_info}")

    def warning(self, message: str, **kwargs: Any) -> None:
        extra_info = f" | {self._format_extra(**kwargs)}" if kwargs else ""
        self.logger.warning(f"{message}{extra_info}")

    def error(self, message: str, **kwargs: Any) -> None:
        extra_info = f" | {self._format_extra(**kwargs)}" if kwargs else ""
        self.logger.error(f"{message}{extra_info}")

    def critical(self, message: str, **kwargs: Any) -> None:
        extra_info = f" | {self._format_extra(**kwargs)}" if kwargs else ""
        self.logger.critical(f"{message}{extra_info}")

    def _format_extra(self, **kwargs: Any) -> str:
        return " | ".join(f"{key}={value}" for key, value in kwargs.items())


class SystemLogger(BaseLogger):
    """Logger for system lifecycle events."""

    def __init__(self, config: Optional[LogConfig] = None):
        super().__init__("system", config)

    def startup(self, component: str, version: str = "", **kwargs: Any) -> None:
        self.info(
            f"Component started: {component} {version}".strip(),
            component=component,
            version=version,
            **kwargs,
        )

    def shutdown(self, component: str, reason: str = "", **kwargs: Any) -> None:
        self.info(
            f"Component stopped: {component} ({reason})" if reason else f"Component stopped: {component}",
            component=component,
            reason=reason,
            **kwargs,
        )

    def config_change(self, component: str, key: str, old_value: Any, new_value: Any) -> None:
        self.info(f"Config changed: {component}.{key} {old_value} -> {new_value}")


class TradingLogger(BaseLogger):
    """Logger for trading actions and fills."""

    def __init__(self, config: Optional[LogConfig] = None):
        super().__init__("trading", config)

    def order_placed(
        self,
        exchange: str,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        **kwargs: Any,
    ) -> None:
        self.info(
            f"Order placed: {exchange} {symbol} {side} {amount}@{price}",
            exchange=exchange,
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            **kwargs,
        )

    def order_filled(
        self,
        exchange: str,
        symbol: str,
        order_id: str,
        filled_amount: float,
        **kwargs: Any,
    ) -> None:
        self.info(
            f"Order filled: {exchange} {symbol} {order_id} {filled_amount}",
            exchange=exchange,
            symbol=symbol,
            order_id=order_id,
            filled_amount=filled_amount,
            **kwargs,
        )

    def arbitrage_opportunity(
        self,
        buy_exchange: str,
        sell_exchange: str,
        symbol: str,
        profit: float,
        **kwargs: Any,
    ) -> None:
        self.info(
            f"Arbitrage opportunity: {symbol} {buy_exchange}->{sell_exchange} profit={profit:.4f}",
            symbol=symbol,
            buy_exchange=buy_exchange,
            sell_exchange=sell_exchange,
            profit=profit,
            **kwargs,
        )

    def trade(self, action: str, symbol: str, amount: float, **kwargs: Any) -> None:
        self.info(
            f"Trade event: {action} {symbol} {amount}",
            action=action,
            symbol=symbol,
            amount=amount,
            **kwargs,
        )


class DataLogger(BaseLogger):
    """Logger for market data and websocket state."""

    def __init__(self, config: Optional[LogConfig] = None):
        super().__init__("data", config)

    def price_update(self, exchange: str, symbol: str, bid: float, ask: float, **kwargs: Any) -> None:
        self.debug(
            f"Price update: {exchange} {symbol} bid={bid} ask={ask}",
            exchange=exchange,
            symbol=symbol,
            bid=bid,
            ask=ask,
            **kwargs,
        )

    def websocket_connected(self, exchange: str, **kwargs: Any) -> None:
        self.info(f"WebSocket connected: {exchange}", exchange=exchange, **kwargs)

    def websocket_disconnected(self, exchange: str, reason: str = "", **kwargs: Any) -> None:
        message = (
            f"WebSocket disconnected: {exchange} ({reason})"
            if reason
            else f"WebSocket disconnected: {exchange}"
        )
        self.warning(message, exchange=exchange, reason=reason, **kwargs)


class ErrorLogger(BaseLogger):
    """Logger for failures and API errors."""

    def __init__(self, config: Optional[LogConfig] = None):
        super().__init__("error", config)

    def exception(self, error: Exception, context: str = "", **kwargs: Any) -> None:
        self.error(
            f"Exception: {context} {type(error).__name__}: {error}",
            error_type=type(error).__name__,
            error_message=str(error),
            context=context,
            **kwargs,
        )

    def api_error(
        self,
        exchange: str,
        endpoint: str,
        status_code: int,
        error_message: str,
        **kwargs: Any,
    ) -> None:
        self.error(
            f"API error: {exchange} {endpoint} {status_code} {error_message}",
            exchange=exchange,
            endpoint=endpoint,
            status_code=status_code,
            error_message=error_message,
            **kwargs,
        )

    def connection_error(
        self,
        exchange: str,
        error_type: str,
        error_message: str,
        **kwargs: Any,
    ) -> None:
        self.error(
            f"Connection error: {exchange} {error_type} {error_message}",
            exchange=exchange,
            error_type=error_type,
            error_message=error_message,
            **kwargs,
        )


class ExchangeLogger(BaseLogger):
    """Logger for a specific exchange adapter."""

    def __init__(self, exchange_name: str, config: Optional[LogConfig] = None):
        super().__init__(f"exchange.{exchange_name}", config)
        self.exchange_name = exchange_name

    def adapter_start(self, **kwargs: Any) -> None:
        self.info(f"Exchange adapter started: {self.exchange_name}", exchange=self.exchange_name, **kwargs)

    def adapter_stop(self, reason: str = "", **kwargs: Any) -> None:
        message = (
            f"Exchange adapter stopped: {self.exchange_name} ({reason})"
            if reason
            else f"Exchange adapter stopped: {self.exchange_name}"
        )
        self.info(message, exchange=self.exchange_name, reason=reason, **kwargs)

    def rate_limit(self, endpoint: str, wait_time: float, **kwargs: Any) -> None:
        self.warning(
            f"Rate limit hit: {self.exchange_name} endpoint={endpoint} wait={wait_time}s",
            exchange=self.exchange_name,
            endpoint=endpoint,
            wait_time=wait_time,
            **kwargs,
        )


class PerformanceLogger(BaseLogger):
    """Logger for expensive operations and memory usage."""

    def __init__(self, config: Optional[LogConfig] = None):
        super().__init__("performance", config)

    def execution_time(self, function_name: str, duration: float, **kwargs: Any) -> None:
        if duration > 1.0:
            self.info(
                f"Execution time: {function_name} {duration:.3f}s",
                function=function_name,
                duration=duration,
                **kwargs,
            )

    def memory_usage(self, component: str, memory_mb: float, **kwargs: Any) -> None:
        if memory_mb > 100:
            self.info(
                f"Memory usage: {component} {memory_mb:.1f}MB",
                component=component,
                memory_mb=memory_mb,
                **kwargs,
            )


_loggers: Dict[str, BaseLogger] = {}
_config: Optional[LogConfig] = None


def _close_managed_handlers() -> None:
    """Close and detach all managed logger handlers."""
    for logger_wrapper in _loggers.values():
        for handler in list(logger_wrapper.logger.handlers):
            handler.close()
            logger_wrapper.logger.removeHandler(handler)


def _iter_console_handlers():
    """Yield console-only handlers from managed loggers."""
    seen = set()

    for logger_wrapper in _loggers.values():
        for handler in logger_wrapper.logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler_id = id(handler)
                if handler_id in seen:
                    continue
                seen.add(handler_id)
                yield handler


def get_config() -> LogConfig:
    """Return the global logging configuration."""
    global _config
    if _config is None:
        _config = LogConfig()
    return _config


def set_config(config: LogConfig) -> None:
    """Replace the global logging configuration."""
    global _config
    _config = config


def override_console_level(level: str = "WARNING") -> Dict[str, Any]:
    """Temporarily change console verbosity without affecting file logs."""
    config = get_config()
    level_no = getattr(logging, level.upper())
    state = {
        "config_console_level": config.console_level,
        "handler_levels": [],
    }

    config.console_level = level_no

    for handler in _iter_console_handlers():
        state["handler_levels"].append((handler, handler.level))
        handler.setLevel(level_no)

    return state


def restore_console_level(state: Optional[Dict[str, Any]]) -> None:
    """Restore console levels after override_console_level()."""
    if not state:
        return

    config = get_config()
    config.console_level = state.get("config_console_level", config.console_level)

    for handler, level in state.get("handler_levels", []):
        handler.setLevel(level)


def get_logger(name: str) -> BaseLogger:
    """Return a generic managed logger."""
    if name not in _loggers:
        _loggers[name] = BaseLogger(name, get_config())
    return _loggers[name]


def get_system_logger(name: str = "system") -> SystemLogger:
    """Return a managed system logger."""
    logger_key = f"system.{name}" if name != "system" else "system"
    if logger_key not in _loggers:
        _loggers[logger_key] = SystemLogger(get_config())
        if name != "system":
            _loggers[logger_key].logger.name = logger_key
    return _loggers[logger_key]


def get_trading_logger() -> TradingLogger:
    """Return the shared trading logger."""
    if "trading" not in _loggers:
        _loggers["trading"] = TradingLogger(get_config())
    return _loggers["trading"]


def get_data_logger(name: str = "data") -> DataLogger:
    """Return a managed data logger."""
    logger_key = f"data.{name}" if name != "data" else "data"
    if logger_key not in _loggers:
        _loggers[logger_key] = DataLogger(get_config())
        if name != "data":
            _loggers[logger_key].logger.name = logger_key
    return _loggers[logger_key]


def get_error_logger() -> ErrorLogger:
    """Return the shared error logger."""
    if "error" not in _loggers:
        _loggers["error"] = ErrorLogger(get_config())
    return _loggers["error"]


def get_exchange_logger(exchange_name: str) -> ExchangeLogger:
    """Return a managed logger for a single exchange adapter."""
    key = f"exchange.{exchange_name}"
    if key not in _loggers:
        _loggers[key] = ExchangeLogger(exchange_name, get_config())
    return _loggers[key]


def get_performance_logger() -> PerformanceLogger:
    """Return the shared performance logger."""
    if "performance" not in _loggers:
        _loggers["performance"] = PerformanceLogger(get_config())
    return _loggers["performance"]


def initialize_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    enable_console: bool = True,
    clear_existing: bool = True,
) -> bool:
    """Initialize the managed logging system."""
    try:
        config = LogConfig(log_dir=log_dir, level=level, enable_console=enable_console)
        _close_managed_handlers()
        if clear_existing:
            clear_log_files(config.log_dir)
        set_config(config)
        _loggers.clear()
        get_system_logger().startup("UnifiedLoggingSystem", "v3.0")
        return True
    except Exception as exc:
        print(f"Failed to initialize logging: {exc}")
        return False


def shutdown_logging() -> None:
    """Shutdown all managed loggers and close handlers."""
    try:
        get_system_logger().shutdown("UnifiedLoggingSystem", "normal shutdown")
        _close_managed_handlers()
        _loggers.clear()
    except Exception as exc:
        print(f"Failed to shutdown logging: {exc}")


def get_health_status() -> Dict[str, Any]:
    """Return basic health information for the logging system."""
    config = get_config()
    return {
        "status": "healthy",
        "version": "v3.0",
        "active_loggers": len(_loggers),
        "config": {
            "log_dir": config.log_dir,
            "level": logging.getLevelName(config.level),
        },
    }

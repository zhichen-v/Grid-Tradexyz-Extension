"""One append-only JSONL stream of explicit V2 DTOs; no raw exchange payloads."""

from dataclasses import fields
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path

from .domain import (
    AccountSnapshot, CashflowEvent, ExecutionResult, ExecutionSnapshot,
    FillAccounting, FillEvent, MarkEvent, QuoteIntent, QuotePlan, SessionReport,
    TelemetryEvent, WorkingOrder, BoundedExitReport, InventoryDecision, FlattenIntent, FailureDiagnostic, DiagnosticValue,
)


_EVENTS = {
    AccountSnapshot: "account_snapshot", QuotePlan: "quote_plan",
    ExecutionResult: "execution_result", FillAccounting: "fill",
    MarkEvent: "mark", CashflowEvent: "cashflow", SessionReport: "session_report",
    BoundedExitReport: "bounded_exit",
    InventoryDecision: "inventory_decision",
    FailureDiagnostic: "failure_diagnostic",
}
_MODELS = {*_EVENTS, ExecutionSnapshot, QuoteIntent, FillEvent, WorkingOrder, FlattenIntent, DiagnosticValue}


def failure_diagnostic(symbol, stage, error=None, *, execution=None, manager=None):
    """Extract only code locations and local state; never format an exception."""
    error_type, source, seen = "blocked", [], set()
    values = {}
    if error is not None:
        module = type(error).__module__
        error_type = (type(error).__name__ if module in ("builtins", "asyncio.exceptions")
                      or module.startswith("core.services.market_maker_v2.") else "ExternalError")
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if type(error).__module__ in {"core.services.market_maker_v2.lighter_runtime",
                                     "core.services.market_maker_v2.api_budget",
                                     "core.services.market_maker_v2.execution_port"}:
            allowed = {"cash", "collateral", "unrealized", "total", "cross",
                       "ledger_position", "account_position", "stream_count", "previous_count", "history_count",
                       "expected_equity", "account_equity",
                       "exit_book_age_ms", "exit_book_after_prepare_ms", "exit_bid", "exit_ask", "exit_limit",
                       "api_used_rest", "api_used_ws", "api_used_tx", "api_next_rest", "api_next_ws", "api_next_tx"}
            for name, value in error.diagnostic_values.items():
                if name in allowed and type(value) is Decimal and value.is_finite():
                    values.setdefault(name, DiagnosticValue(name, value))
        trace = error.__traceback__
        while trace is not None:
            module = trace.tb_frame.f_globals.get("__name__", "")
            if module.startswith("core.services.market_maker_v2."):
                source.append(f"{module.rsplit('.', 1)[-1]}:{trace.tb_lineno}")
            trace = trace.tb_next
        error = error.__cause__ or error.__context__
    health, states, uncertain, unknown = None, (), None, None
    try:
        if manager is not None:
            states = tuple(f"{slot.side.value}:{slot.state.value}:"
                           f"{slot.order_id if slot.order_id in manager.known_order_ids else 'unconfirmed'}"
                           for slot in manager.snapshot())
            uncertain, unknown = manager.has_uncertain_state, manager.has_unknown_order_state
        if execution is not None:
            health = execution.snapshot().health
    except Exception:
        pass  # Unavailable state is left unknown; evidence must never block cleanup.
    return FailureDiagnostic(symbol, stage, error_type, tuple(source[-12:]),
                             health, states, uncertain, unknown, tuple(values.values()))


class TelemetryError(RuntimeError):
    """A stream cannot be recorded; underlying payloads/errors are not logged."""


def _encode(value):
    if type(value) in _MODELS:
        return {item.name: _encode(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TelemetryError("non-finite telemetry value")
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if type(value) is tuple:
        return [_encode(item) for item in value]
    if value is None or type(value) in (str, int, bool, float):
        return value
    raise TelemetryError("unsupported telemetry payload")


class JsonlTelemetrySink:
    """Create one new file; never overwrite or mix sessions in an existing file.

    ponytail: single writer with per-event flush; crash durability/restart recovery
    belongs to Phase 11. A failed write disables this stream, not the financial ledger.
    """

    def __init__(self, path):
        try:
            self._stream = Path(path).open("x", encoding="utf-8", newline="\n")
        except (OSError, ValueError):
            raise TelemetryError("cannot create new telemetry stream") from None
        self._failed = False

    def emit(self, event: TelemetryEvent) -> None:
        if type(event) not in _EVENTS:
            raise TelemetryError("unsupported telemetry event")
        if self._failed or self._stream.closed:
            raise TelemetryError("telemetry stream unavailable")
        try:
            record = {"schema": "mm_v2_event_v1", "event": _EVENTS[type(event)], "data": _encode(event)}
            line = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:
            self._failed = True
            raise TelemetryError("telemetry append failed") from None

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError:
            raise TelemetryError("telemetry close failed") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

"""One append-only JSONL stream of explicit V2 DTOs; no raw exchange payloads."""

from dataclasses import fields
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path

from .domain import (
    AccountSnapshot, CashflowEvent, ExecutionResult, ExecutionSnapshot,
    FillAccounting, FillEvent, MarkEvent, QuoteIntent, QuotePlan, SessionReport,
    TelemetryEvent, WorkingOrder, BoundedExitReport, InventoryDecision, FlattenIntent,
)


_EVENTS = {
    AccountSnapshot: "account_snapshot", QuotePlan: "quote_plan",
    ExecutionResult: "execution_result", FillAccounting: "fill",
    MarkEvent: "mark", CashflowEvent: "cashflow", SessionReport: "session_report",
    BoundedExitReport: "bounded_exit",
    InventoryDecision: "inventory_decision",
}
_MODELS = {*_EVENTS, ExecutionSnapshot, QuoteIntent, FillEvent, WorkingOrder, FlattenIntent}


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

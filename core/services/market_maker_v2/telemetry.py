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
    OrderEvidence, PublicBookObservation, MarketStateSnapshot, GovernorDiagnostic,
)
from .order_manager import ReconcileAction, ReconcileResult, CANCELLATION_DIAGNOSTIC_NAMES
from .lighter_runtime import LighterReadError, MARKET_DIAGNOSTIC_NAMES


_EVENTS = {
    AccountSnapshot: "account_snapshot", QuotePlan: "quote_plan",
    ExecutionResult: "execution_result", FillAccounting: "fill",
    MarkEvent: "mark", CashflowEvent: "cashflow", SessionReport: "session_report",
    BoundedExitReport: "bounded_exit",
    InventoryDecision: "inventory_decision",
    FailureDiagnostic: "failure_diagnostic",
    OrderEvidence: "order_evidence", PublicBookObservation: "public_book_observation",
    GovernorDiagnostic: "governor_diagnostic",
}
_MODELS = {*_EVENTS, ExecutionSnapshot, QuoteIntent, FillEvent, WorkingOrder, FlattenIntent, DiagnosticValue,
           MarketStateSnapshot}

_CANCEL_DIAGNOSTIC_STAGES = frozenset({
    "reconciling_quotes", "cancel_managed_orders", "exit_order_sync", "exit_health",
})
_CANCEL_ERROR_FLAGS = {
    "cancel outcome is not terminal": "cancel_nonterminal_response",
    "exact cancellation terminal proof could not be confirmed": "cancel_terminal_unconfirmed",
    "cancel rejected: http_429": "cancel_http_429",
    "cancel outcome uncertain: http_429": "cancel_http_429_text",
    "cancel definitively not sent": "cancel_not_sent",
    "cancel outcome uncertain: TimeoutError": "cancel_timeout",
    "cancel outcome uncertain: ConnectionError": "cancel_network_error",
    "cancel outcome uncertain: ConnectionResetError": "cancel_network_error",
    "cancel outcome uncertain: ConnectionAbortedError": "cancel_network_error",
    "cancel outcome uncertain: ConnectionRefusedError": "cancel_network_error",
    "cancel outcome uncertain: ClientConnectionError": "cancel_network_error",
    "cancel outcome uncertain: ServerDisconnectedError": "cancel_network_error",
    "cancel outcome uncertain: RuntimeError": "cancel_other_error",
    "cancel outcome uncertain: ValueError": "cancel_other_error",
    "cancel outcome uncertain: TypeError": "cancel_other_error",
    "cancel outcome uncertain: OSError": "cancel_other_error",
}


def _cancel_values(manager, slots, stage):
    """Classify the matching unresolved cancel batch, never an individual provider payload."""
    if stage not in _CANCEL_DIAGNOSTIC_STAGES:
        return ()
    pending = [slot for slot in slots if getattr(slot, "cancellation_uncertain", False) is True]
    result = getattr(manager, "last_result", None)
    not_sent = [slot for slot in slots if type(result) is ReconcileResult and any(
        type(action) is ReconcileAction and action.operation == "cancel"
        and action.cancellation_not_sent is True and action.success is False
        and action.order_id == slot.order_id and action.side is slot.side
        for action in result.actions)]
    relevant = pending + [slot for slot in not_sent if slot not in pending]
    if not relevant:
        return ()
    matched = set()
    details = {}
    protocol_results = set()
    if type(result) is ReconcileResult:
        for slot in relevant:
            if slot.order_id is None or slot.order_id not in manager.known_order_ids:
                continue
            for action in result.actions:
                if (type(action) is not ReconcileAction or action.operation != "cancel"
                        or action.success is True or action.side is not slot.side
                        or action.order_id != slot.order_id):
                    continue
                matched.add(slot.order_id)
                protocol_values = {}
                if type(action.diagnostic_values) is tuple:
                    for item in action.diagnostic_values:
                        if type(item) is not tuple or len(item) != 2:
                            continue
                        name, value = item
                        if (type(name) is str and name in CANCELLATION_DIAGNOSTIC_NAMES
                                and type(value) is int and 0 <= value <= 2147483647):
                            if name in {"cancel_http_status", "cancel_api_code"}:
                                if name != "cancel_http_status" or 100 <= value <= 599:
                                    protocol_values.setdefault(name, set()).add(value)
                            else:
                                details[name] = max(details.get(name, 0), value)
                protocol_results.add(tuple(sorted((name, next(iter(codes)))
                    for name, codes in protocol_values.items() if len(codes) == 1)))
    # Only merge identical complete results, never pair different orders' codes.
    if len(protocol_results) == 1:
        details.update(next(iter(protocol_results)))
    # last_result may concern a prior order or another operation. Its errors do
    # not explain today's pending slot unless an exact cancel action still matches.
    flags = set()
    if matched:
        for category in result.errors:
            flags.add(_CANCEL_ERROR_FLAGS.get(category, "cancel_reason_unknown")
                      if type(category) is str else "cancel_reason_unknown")
    if not flags or len(matched) < len(relevant):
        flags.add("cancel_reason_unknown")
    counts = {"cancel_pending_count": len(pending), "cancel_action_matched_count": len(matched)}
    if not_sent:
        counts["cancel_not_sent_count"] = len(not_sent)
    return tuple(DiagnosticValue(name, Decimal(value)) for name, value in (counts | details).items()) + tuple(
        DiagnosticValue(name, Decimal(1)) for name in sorted(flags))


_READ_ERROR_CATEGORIES = {
    ("builtins", "TimeoutError"): "read_cause_timeout",
    ("builtins", "ConnectionError"): "read_cause_connection",
    ("builtins", "ValueError"): "read_cause_value",
    ("builtins", "TypeError"): "read_cause_type",
    ("builtins", "RuntimeError"): "read_cause_runtime",
    ("aiohttp.client_exceptions", "ClientConnectionError"): "read_cause_connection",
    ("aiohttp.client_exceptions", "ClientResponseError"): "read_cause_http",
    ("lighter.exceptions", "ApiException"): "read_cause_http",
    ("pydantic_core._pydantic_core", "ValidationError"): "read_cause_schema",
    ("json.decoder", "JSONDecodeError"): "read_cause_json",
}
_READ_SOURCE_MODULES = {
    "core.adapters.exchanges.adapters.lighter": "lighter_adapter",
    "core.adapters.exchanges.adapters.lighter_rest": "lighter_rest",
    "core.adapters.exchanges.adapters.lighter_base": "lighter_base",
    "lighter.api_client": "lighter_api_client",
    "lighter.api.account_api": "lighter_account_api",
    "lighter.rest": "lighter_sdk_rest",
}


def failure_diagnostic(symbol, stage, error=None, *, execution=None, manager=None, market_values=None):
    """Extract only code locations and local state; never format an exception."""
    error_type, source, seen = "blocked", [], set()
    values = {}
    read_failure = isinstance(error, LighterReadError)
    if error is not None:
        module = type(error).__module__
        error_type = (type(error).__name__ if module in ("builtins", "asyncio.exceptions")
                      or module.startswith("core.services.market_maker_v2.") else "ExternalError")
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        # Fixed categories and numeric HTTP status preserve the underlying read
        # failure without serializing SDK bodies, URLs, headers or error text.
        if read_failure and not type(error).__module__.startswith("core.services.market_maker_v2."):
            for cls in type(error).__mro__:
                category = _READ_ERROR_CATEGORIES.get((cls.__module__, cls.__name__))
                if category is not None:
                    values.setdefault(category, DiagnosticValue(category, Decimal(1)))
                    if category == "read_cause_http":
                        status = getattr(error, "status", None)
                        if type(status) is int and 100 <= status <= 599:
                            values.setdefault("read_http_status", DiagnosticValue("read_http_status", Decimal(status)))
                    break
        if type(error).__module__ in {"core.services.market_maker_v2.lighter_runtime",
                                     "core.services.market_maker_v2.api_budget",
                                     "core.services.market_maker_v2.execution_port"}:
            allowed = {"cash", "collateral", "unrealized", "total", "cross",
                       "ledger_position", "account_position", "stream_count", "previous_count", "history_count",
                       "expected_equity", "account_equity",
                       "exit_book_age_ms", "exit_book_after_prepare_ms", "exit_bid", "exit_ask", "exit_limit",
                       "api_used_rest", "api_used_ws", "api_used_tx", "api_next_rest", "api_next_ws", "api_next_tx"}
            allowed |= {"recovery_reads", "recovery_pending_count", "recovery_deadline_remaining_ms",
                        "recovery_ineligible", "recovery_scope_changed", "recovery_terminal_pending",
                        "recovery_registry_pending", "recovery_deadline_exhausted", "recovery_budget_refused",
                        "recovery_read_failed", "recovery_unhealthy"}
            allowed |= MARKET_DIAGNOSTIC_NAMES
            for name, value in error.diagnostic_values.items():
                if name in allowed and type(value) is Decimal and value.is_finite():
                    values.setdefault(name, DiagnosticValue(name, value))
        trace = error.__traceback__
        while trace is not None:
            module = trace.tb_frame.f_globals.get("__name__", "")
            if module.startswith("core.services.market_maker_v2."):
                source.append(f"{module.rsplit('.', 1)[-1]}:{trace.tb_lineno}")
            elif module in _READ_SOURCE_MODULES:
                source.append(f"{_READ_SOURCE_MODULES[module]}:{trace.tb_lineno}")
            trace = trace.tb_next
        error = error.__cause__ or error.__context__
    if type(market_values) is dict:
        for name, value in market_values.items():
            if name in MARKET_DIAGNOSTIC_NAMES and type(value) is Decimal and value.is_finite():
                values.setdefault(name, DiagnosticValue(name, value))
    health, states, uncertain, unknown = None, (), None, None
    try:
        if manager is not None:
            slots = manager.snapshot()
            states = tuple(f"{slot.side.value}:{slot.state.value}:"
                           f"{slot.order_id if slot.order_id in manager.known_order_ids else 'unconfirmed'}"
                           for slot in slots)
            uncertain, unknown = manager.has_uncertain_state, manager.has_unknown_order_state
            for value in _cancel_values(manager, slots, stage):
                values.setdefault(value.name, value)
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

"""Durable, sanitized submission evidence; never restores or resubmits orders."""

import json
import os
import re
import threading
import uuid
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit


_RUN_ID = uuid.uuid4().hex
_WRITE_LOCK = threading.Lock()
_FAILED_PATHS = set()
_SYMBOLIC = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIELDS = frozenset({
    "client_order_id", "symbol", "grid_id", "logical_client_id", "market_index",
    "nonce", "base_amount", "price", "is_ask", "order_type", "time_in_force",
    "reduce_only", "quantity", "limit_price", "elapsed_ms", "http_status",
    "error_type", "trace_headers", "response_code", "order_id", "order_status",
    "source", "tx_status", "tx_found", "reason", "api_key_index",
})
_TRACE_HEADERS = frozenset({"x-amz-cf-id", "x-amz-cf-pop", "x-request-id", "cf-ray"})


def _trace_headers(headers):
    if not hasattr(headers, "items"):
        return {}
    return {
        name.lower(): "".join(c if c.isprintable() else " " for c in value)[:256]
        for name, value in headers.items()
        if isinstance(name, str) and name.lower() in _TRACE_HEADERS
        and isinstance(value, str)
    }


def safe_error_fields(exc):
    """Do not inspect str(exc), response bodies, request headers or SDK objects."""
    result = {"error_type": type(exc).__name__}
    status = getattr(exc, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        result["http_status"] = status
    traces = _trace_headers(getattr(exc, "headers", None))
    if traces:
        result["trace_headers"] = traces
    return result


def _safe_fields(fields):
    result = {}
    for key in _FIELDS.intersection(fields):
        value = fields[key]
        if key == "trace_headers":
            result[key] = _trace_headers(value)
        elif key in {"reason", "source", "error_type"}:
            # These are codes, never exception messages or response bodies.
            if isinstance(value, str) and _SYMBOLIC.fullmatch(value):
                result[key] = value
        elif isinstance(value, (str, int, float, bool, Decimal)) or value is None:
            if isinstance(value, (str, Decimal)):
                value = "".join(c if c.isprintable() else " " for c in str(value))[:256]
            result[key] = value
    return result


def _fsync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SubmissionJournal:
    def __init__(self, base_url, account_index, api_key_index,
                 directory=Path("logs/lighter_submission_evidence")):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Submission journal requires an HTTP(S) API origin")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        self.base_url = f"{parsed.scheme}://{host}"
        if parsed.port:
            self.base_url += f":{parsed.port}"
        self.account_index = int(account_index)
        self.api_key_index = int(api_key_index)
        self.run_id = _RUN_ID
        self.path = Path(directory) / f"{self.run_id}.jsonl"

    def append(self, event, tx_hash, **fields):
        """Return only after fsync succeeds; callers must not send on failure."""
        if not isinstance(event, str) or not _SYMBOLIC.fullmatch(event):
            raise ValueError("Invalid submission journal event")
        if not isinstance(tx_hash, str) or not _SYMBOLIC.fullmatch(tx_hash):
            raise ValueError("Invalid submission transaction hash")
        record = {
            **_safe_fields(fields),
            "event": event, "tx_hash": tx_hash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id, "base_url": self.base_url,
            "account_index": self.account_index,
            # The actual signed key can differ from a configured default key.
            "api_key_index": int(fields.get("api_key_index", self.api_key_index)),
        }
        encoded = (json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        with _WRITE_LOCK:
            journal_path = self.path.resolve()
            if journal_path in _FAILED_PATHS:
                raise OSError("Submission journal blocked after a prior write failure")
            try:
                new_directories = []
                parent = self.path.parent
                while not parent.exists():
                    new_directories.append(parent)
                    parent = parent.parent
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                for directory in new_directories:
                    _fsync_directory(directory.parent)
                new_file = not self.path.exists()
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    remaining = memoryview(encoded)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("Submission journal write made no progress")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if new_file:
                    _fsync_directory(self.path.parent)
            except BaseException:
                # Never append behind a potentially torn record or repair evidence.
                _FAILED_PATHS.add(journal_path)
                raise


def read_records(path):
    """Read complete events; never hide corruption except an interrupted tail."""
    path = Path(path)
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    records = []
    for journal_path in files:
        lines = journal_path.read_bytes().splitlines(keepends=True)
        for index, raw in enumerate(lines):
            try:
                record = json.loads(raw)
                required = ("run_id", "base_url", "account_index", "api_key_index",
                            "tx_hash", "event", "timestamp")
                if not isinstance(record, dict) or any(key not in record for key in required):
                    raise ValueError("Missing event context")
                if not all(isinstance(record[key], (str, int)) for key in required):
                    raise ValueError("Invalid event context")
            except (ValueError, KeyError, TypeError):
                if index == len(lines) - 1 and not raw.endswith(b"\n"):
                    warnings.warn(f"Ignoring interrupted journal tail: {journal_path.name}", RuntimeWarning)
                    continue
                raise ValueError(f"Invalid submission journal record: {journal_path.name}:{index + 1}") from None
            records.append({**record, "journal_path": str(journal_path)})
    return records


def read_pending(path):
    """Read prior runs without changing files or deciding an absent order failed.

    A successful HTTP response is not final exchange-order evidence. Only explicit
    rejection or an exact observed order resolves a record. A torn final write is
    warned about; corruption in the middle fails closed instead of hiding records.
    """
    pending = {}
    for record in read_records(path):
        identity = tuple(record[key] for key in (
            "run_id", "base_url", "account_index", "api_key_index", "tx_hash",
        ))
        event = record["event"]
        if event == "pre_send":
            pending[identity] = record.copy()
        elif event in {"order_observed", "rejected"}:
            pending.pop(identity, None)
        elif identity in pending:
            fields = _safe_fields(record)
            if event.startswith("tx_"):
                # Lookup failures must not replace the original submission's
                # HTTP status, gateway trace IDs or request elapsed time.
                fields = {key: value for key, value in fields.items()
                          if key in {"tx_found", "tx_status"}}
            pending[identity].update(fields)
        if identity in pending:
            pending[identity]["last_event"] = event
            pending[identity]["last_timestamp"] = record["timestamp"]
    return list(pending.values())

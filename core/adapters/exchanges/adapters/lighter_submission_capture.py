"""Capture create-order evidence without replacing the SDK nonce protocol."""

import asyncio
from contextvars import ContextVar
import inspect
import logging
import time

from core.lighter_submission_journal import SubmissionJournal, safe_error_fields
from ..exceptions import OrderSubmissionNotSentError
from .lighter_selective_cancel import _is_invalid_nonce_rejection


logger = logging.getLogger(
    "core.adapters.exchanges.adapters.lighter_rest.submission_evidence"
)
_NOT_SENT = object()
_SIGNED_FIELDS = (
    "market_index", "nonce", "api_key_index", "base_amount", "price",
    "is_ask", "order_type", "time_in_force", "reduce_only",
)


class SubmissionCapture:
    def __init__(self, signer, journal: SubmissionJournal):
        self.journal = journal
        self.pending = {}
        self._context = ContextVar("lighter_submission", default=None)
        original_sign = signer.sign_create_order
        original_send = signer.send_tx
        # Use the class signature even when a test replaces native signing.
        signature = inspect.signature(type(signer).sign_create_order)

        def sign_create_order(*args, **kwargs):
            result = original_sign(*args, **kwargs)
            context = self._context.get()
            if context is None or result[3] is not None:
                return result
            try:
                bound = signature.bind(signer, *args, **kwargs)
                bound.apply_defaults()
                values = bound.arguments
                tx_hash = result[2]
                if not isinstance(tx_hash, str) or not tx_hash:
                    raise ValueError("missing signed transaction hash")
                record = {**context["intent"], **{
                    key: values[key] for key in _SIGNED_FIELDS
                }, "client_order_id": str(values["client_order_index"])}
                # This synchronous write is inside the SDK's nonce lock, before
                # it can send. The signed transaction/signature is never stored.
                self.journal.append("pre_send", tx_hash, **record)
            except Exception as exc:
                logger.error("Submission evidence unavailable before send: %s", type(exc).__name__)
                # A signing-error result lets the SDK roll back its allocated
                # nonce while still holding its own lock. Do not raise here.
                return None, None, None, _NOT_SENT
            context["tx_hash"] = tx_hash
            context["api_key_index"] = record["api_key_index"]
            context["client_order_id"] = record["client_order_id"]
            self.pending[record["client_order_id"]] = {**record, "tx_hash": tx_hash}
            return result

        async def send_tx(*args, **kwargs):
            context = self._context.get()
            if context is None or "tx_hash" not in context:
                return await original_send(*args, **kwargs)
            tx_hash = context["tx_hash"]
            started = time.monotonic()
            try:
                response = await original_send(*args, **kwargs)
            except BaseException as exc:
                # Includes task cancellation after send: still not safe to retry.
                rejected = isinstance(exc, Exception) and _is_invalid_nonce_rejection(exc)
                self.record("rejected" if rejected else "uncertain", tx_hash,
                            api_key_index=context["api_key_index"],
                            response_code=21104 if rejected else None,
                            elapsed_ms=round((time.monotonic() - started) * 1000),
                            **safe_error_fields(exc))
                if rejected:
                    self.pending.pop(context["client_order_id"], None)
                raise
            self.record("acknowledged" if getattr(response, "code", None) == 200 else "uncertain",
                        tx_hash, response_code=getattr(response, "code", None),
                        api_key_index=context["api_key_index"],
                        elapsed_ms=round((time.monotonic() - started) * 1000))
            return response

        signer.sign_create_order = sign_create_order
        signer.send_tx = send_tx

    async def run(self, request, **intent):
        token = self._context.set({"intent": intent})
        try:
            result = await request()
            if isinstance(result, tuple) and len(result) == 3 and result[2] is _NOT_SENT:
                raise OrderSubmissionNotSentError(
                    "Submission evidence could not be persisted; order was not sent"
                )
            return result
        finally:
            self._context.reset(token)

    def record(self, event, tx_hash, **fields):
        try:
            self.journal.append(event, tx_hash, **fields)
            return True
        except Exception as exc:
            # A disk failure AFTER send cannot change the mutation outcome.
            # The fsynced pre_send record remains available after restart.
            logger.error("Submission evidence append failed: tx_hash=%s error=%s",
                         tx_hash, type(exc).__name__)
            return False

    def observe_order(self, client_id, order_id, status=None, source="exact_client_lookup"):
        record = self.pending.get(str(client_id))
        if record is None or order_id in (None, ""):
            return
        if self.record("order_observed", record["tx_hash"],
                       api_key_index=record["api_key_index"],
                       client_order_id=str(client_id), order_id=str(order_id),
                       order_status=getattr(status, "value", status), source=source):
            self.pending.pop(str(client_id), None)

    async def probe_transaction(self, client_id, rest):
        record = self.pending.get(str(client_id))
        if record is None:
            return
        tx_hash = record["tx_hash"]
        try:
            # One diagnostic GET only; its total budget includes shared cooldown.
            response = await asyncio.wait_for(rest._call_api(
                "submission transaction evidence",
                lambda: rest.transaction_api.tx(by="hash", value=tx_hash,
                                                _request_timeout=3.0),
                retry_on_429=False,
            ), timeout=3.0)
            found = (
                getattr(response, "code", None) == 200
                and getattr(response, "hash", None) == tx_hash
                and getattr(response, "account_index", None) == self.journal.account_index
                and getattr(response, "api_key_index", None) == record["api_key_index"]
                and getattr(response, "nonce", None) == record["nonce"]
            )
            self.record("tx_observed", tx_hash, tx_found=found,
                        api_key_index=record["api_key_index"],
                        tx_status=getattr(response, "status", None) if found else None,
                        response_code=getattr(response, "code", None))
            logger.warning("Submission transaction lookup: client_order_id=%s tx_hash=%s matched=%s journal=%s",
                           client_id, tx_hash, found, self.journal.path)
        except Exception as exc:
            self.record("tx_lookup_failed", tx_hash,
                        api_key_index=record["api_key_index"], **safe_error_fields(exc))
            logger.warning("Submission transaction lookup unresolved: client_order_id=%s tx_hash=%s error=%s journal=%s",
                           client_id, tx_hash, type(exc).__name__, self.journal.path)
        # Transaction status is evidence only, never an exchange order ID or
        # proof that an absent order was rejected. Existing order reads decide.

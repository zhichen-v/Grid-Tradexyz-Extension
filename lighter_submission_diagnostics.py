"""Inspect durable submission evidence; network queries are opt-in and read-only."""

import argparse
import asyncio
import json
import math
from pathlib import Path

from core.lighter_submission_journal import read_pending, read_records, safe_error_fields


DEFAULT_JOURNAL = Path("logs/lighter_submission_evidence")
DISPLAY_FIELDS = (
    "run_id", "tx_hash", "client_order_id", "grid_id", "base_url", "account_index",
    "api_key_index", "market_index", "symbol", "nonce", "timestamp", "event",
    "is_ask", "quantity", "base_amount", "price", "limit_price", "time_in_force", "reduce_only",
    "last_event", "last_timestamp", "journal_path", "http_status", "error_type", "trace_headers",
    "elapsed_ms", "logical_client_id",
    "response_code", "tx_found", "tx_status", "order_id", "order_status", "events",
)


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _display(record):
    return {name: record[name] for name in DISPLAY_FIELDS if name in record}


def _scope_matches(record, settings, base_url):
    return (
        str(record.get("base_url", "")).rstrip("/") == base_url.rstrip("/")
        and record.get("account_index") == settings["account_index"]
        and record.get("api_key_index") == settings["api_key_index"]
    )


def _order_match(order, record):
    client_ids = [str(_field(order, name)) for name in
                  ("client_order_id", "client_order_index") if _field(order, name) is not None]
    return (
        bool(client_ids) and all(value == str(record["client_order_id"]) for value in client_ids)
        and _field(order, "market_index") == record["market_index"]
        and _field(order, "owner_account_index") == record["account_index"]
    )


def _order_evidence(order):
    return {name: _field(order, name) for name in (
        "order_index", "client_order_id", "client_order_index", "market_index",
        "owner_account_index", "status", "remaining_base_amount", "filled_base_amount",
    )}


def select_records(path, tx_hash=None):
    if not path.exists():
        return []
    if not tx_hash:
        records = read_pending(path)
    else:
        # Explicit hash lookup includes resolved history, without changing it.
        merged = {}
        for record in read_records(path):
            if record["tx_hash"] != tx_hash:
                continue
            identity = tuple(record[key] for key in
                             ("run_id", "base_url", "account_index", "api_key_index", "tx_hash"))
            previous = merged.get(identity, {})
            update = record
            if record["event"].startswith("tx_"):
                update = {key: record[key] for key in ("tx_found", "tx_status") if key in record}
            merged[identity] = {**previous, **update,
                                "timestamp": previous.get("timestamp", record["timestamp"]),
                                "last_event": record["event"], "last_timestamp": record["timestamp"],
                                "events": [*previous.get("events", []), _display(record)]}
        records = list(merged.values())
    return sorted(records, key=lambda record: record.get("timestamp", ""))


async def query_record(record, settings, base_url, tx_api, order_api, auth, *, pages=3, timeout=5.0):
    """Return observations only; absent or failed reads never prove rejection."""
    result = _display(record)
    if not _scope_matches(record, settings, base_url):
        return {**result, "query_state": "scope_mismatch_no_queries"}
    if not record.get("tx_hash") or not record.get("client_order_id") or any(
        type(record.get(name)) is not int for name in ("market_index", "nonce")
    ):
        return {**result, "query_state": "invalid_evidence_no_queries"}
    if not 1 <= pages <= 10 or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("invalid query bounds")

    result["query_state"] = "observations_only"
    try:
        tx = await asyncio.wait_for(tx_api.tx(
            by="hash", value=record["tx_hash"], _request_timeout=float(timeout)), timeout)
        if _field(tx, "code") != 200:
            result["transaction"] = {"state": "not_confirmed", "response_code": _field(tx, "code")}
        elif any(_field(tx, name) != record.get(key) for name, key in (
            ("hash", "tx_hash"), ("account_index", "account_index"),
            ("api_key_index", "api_key_index"), ("nonce", "nonce"),
        )):
            result["transaction"] = {"state": "identity_mismatch"}
        else:
            result["transaction"] = {"state": "found", **{
                name: _field(tx, name) for name in
                ("hash", "status", "nonce", "transaction_index", "block_height", "executed_at")}}
    except Exception as exc:
        result["transaction"] = {"state": "query_failed", **safe_error_fields(exc)}
    if _rate_limited(result):
        return {**result, "query_state": "rate_limited"}

    params = dict(authorization=auth, account_index=record["account_index"],
                  market_id=record["market_index"], _request_timeout=float(timeout))
    try:
        await asyncio.sleep(0.25)
        active = await asyncio.wait_for(order_api.account_active_orders(**params), timeout)
        if _field(active, "code") != 200 or not isinstance(_field(active, "orders"), list):
            result["active_orders"] = {"state": "query_failed", "response_code": _field(active, "code")}
        else:
            matches = [_order_evidence(order) for order in (_field(active, "orders") or [])
                       if _order_match(order, record)]
            result["active_orders"] = {"state": "found" if matches else "not_found", "matches": matches}
    except Exception as exc:
        result["active_orders"] = {"state": "query_failed", **safe_error_fields(exc)}
    if _rate_limited(result):
        return {**result, "query_state": "rate_limited"}

    cursor, seen = None, set()
    for page in range(1, pages + 1):
        try:
            await asyncio.sleep(0.25)
            history = await asyncio.wait_for(order_api.account_inactive_orders(
                **params, limit=100, cursor=cursor), timeout)
            if _field(history, "code") != 200 or not isinstance(_field(history, "orders"), list):
                result["history"] = {"state": "query_failed", "pages": page,
                                     "response_code": _field(history, "code")}
                break
            matches = [_order_evidence(order) for order in (_field(history, "orders") or [])
                       if _order_match(order, record)]
            if matches:
                result["history"] = {"state": "found", "pages": page, "matches": matches}
                break
            cursor = _field(history, "next_cursor")
            if not cursor:
                result["history"] = {"state": "not_found", "pages": page}
                break
            if cursor in seen:
                result["history"] = {"state": "incomplete_repeated_cursor", "pages": page}
                break
            seen.add(cursor)
            result["history"] = {"state": "incomplete_page_limit", "pages": page}
        except Exception as exc:
            result["history"] = {"state": "query_failed", "pages": page, **safe_error_fields(exc)}
            break
    if _rate_limited(result):
        result["query_state"] = "rate_limited"
    return result


def _rate_limited(result):
    return any(isinstance(result.get(name), dict) and any(result[name].get(field) == 429
               for field in ("http_status", "response_code"))
               for name in ("transaction", "active_orders", "history"))


async def query_records(records, settings, *, pages, timeout):
    import lighter
    from lighter.endpoint_profiles import get_endpoint_profile
    from lighter.nonce_manager import NonceManagerType

    profile = get_endpoint_profile(settings["network"])
    results, signer = [], None
    try:
        for record in records:
            if results and results[-1].get("query_state") in {"rate_limited", "skipped_rate_limited"}:
                results.append({**_display(record), "query_state": "skipped_rate_limited"})
                continue
            if not _scope_matches(record, settings, profile.api_url):
                results.append({**_display(record), "query_state": "scope_mismatch_no_queries"})
                continue
            if signer is None:
                # No strategy/adapter startup and no nonce fetch; sign auth tokens only.
                signer = lighter.SignerClient(
                    url=profile.api_url, account_index=settings["account_index"],
                    api_private_keys={settings["api_key_index"]: settings["api_key_private_key"]},
                    nonce_management_type=NonceManagerType.NONE, chain_id=profile.chain_id,
                )
                signer.api_client.configuration.debug = False
            auth, error = signer.create_auth_token_with_expiry(api_key_index=settings["api_key_index"])
            if error or not auth:
                results.append({**_display(record), "query_state": "authentication_failed"})
                continue
            results.append(await query_record(
                record, settings, profile.api_url, signer.tx_api, signer.order_api, auth,
                pages=pages, timeout=timeout))
            await asyncio.sleep(0.25)
    finally:
        if signer is not None:
            await signer.close()
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--query", action="store_true", help="Opt in to bounded authenticated GET queries")
    parser.add_argument("--tx-hash", help="Select one transaction hash")
    parser.add_argument("--config", type=Path, default=Path("config/exchanges/lighter_config.yaml"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--limit", type=int, default=10, help="Maximum records (1-100)")
    parser.add_argument("--pages", type=int, default=3, help="History page budget per record (1-10)")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout seconds (0-30)")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100 or not 1 <= args.pages <= 10 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
        parser.error("limit must be 1-100, pages 1-10, timeout > 0 and <= 30")
    try:
        records = select_records(args.journal, args.tx_hash)
        selected = records[-args.limit:]
        if args.query and selected:
            from lighter_preflight import load_settings
            results = asyncio.run(query_records(selected, load_settings(args.config, args.env),
                                                pages=args.pages, timeout=args.timeout))
        else:
            results = [_display(record) for record in selected]
        print(json.dumps({"read_only": True, "queried": bool(args.query and selected),
                          "matching_records": len(records), "displayed_records": len(results),
                          "notice": "Absence or failed reads do not prove rejection; no journal or order state is changed.",
                          "records": results}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"read_only": True, "state": "diagnostics_failed", **safe_error_fields(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

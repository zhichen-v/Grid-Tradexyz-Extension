"""Read-only Lighter mapping. Execution and credential ownership stay outside V2."""

from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from dataclasses import replace
from copy import deepcopy
import asyncio
import re

from .domain import (
    AccountSnapshot, CashflowEvent, CashflowKind, FillEvent, LiquidityRole,
    Side, WorkingOrder, ZERO,
)
from .market_state import MarketState
from .api_budget import ApiBudgetUnavailable


class LighterReadError(RuntimeError):
    """Sanitized failure: an incomplete read cannot authorize execution."""

    def __init__(self, message, *, values=None):
        super().__init__(message)
        self.diagnostic_values = values or {}


class AccountReadRace(LighterReadError):
    """Observations straddle activity; accepted fills remain deduplicated by ID."""


class _AccountCashRace(AccountReadRace):
    """Cash may settle before its independently identified funding evidence."""


class UnattributedCashflow(_AccountCashRace):
    """Exact equity bridge failed; only identified cashflows can explain it."""


def _number(value):
    if type(value) not in (str, int, Decimal):
        raise LighterReadError("invalid financial data")
    value = Decimal(value)
    if not value.is_finite():
        raise LighterReadError("invalid financial data")
    return value


def _value(value):
    return getattr(value, "value", value)


def _trade_key(trade):
    fee, raw = trade.fee, trade.raw_data
    return (str(trade.id), str(trade.order_id), trade.symbol, _value(trade.side),
            trade.amount, trade.price, trade.cost, fee.get("role"), fee.get("cost"),
            fee.get("rate"), fee.get("tick"), fee.get("currency"),
            raw.get("timestamp"), raw.get("trade_sequence"),
            raw.get("integrator_fee_tick"), raw.get("realized_pnl"))


def _trade_map(trades):
    result = {}
    for trade in trades:
        key = _trade_key(trade)
        if key[0] in result:
            raise LighterReadError("duplicate trade identity in account read")
        result[key[0]] = key
    return result


def _funding_map(rows):
    result = {}
    for row in rows:
        key = str(row["id"])
        if key in result:
            raise LighterReadError("duplicate funding identity in account read")
        timestamp = row["timestamp"]
        if type(timestamp) is not int or timestamp < 0:
            raise LighterReadError("invalid funding source time")
        result[key] = (timestamp, _number(row["change"]))
    return result


def _account_state(assets, positions, shares, *, include_valuation=False):
    """Financial/ownership fields shared by REST and fresh account_all snapshots."""
    if shares != []:
        raise LighterReadError("nonexclusive pool shares")
    def rows(values, identity, integers, decimals, strings=()):
        result = []
        for row in values:
            get = row.__getitem__ if type(row) is dict else lambda key: getattr(row, key)
            whole = tuple(get(key) for key in integers)
            if any(type(value) is not int for value in whole):
                raise LighterReadError("invalid account state counters")
            result.append((get(identity), *whole, *(_number(get(key)) for key in decimals),
                           *(get(key) for key in strings)))
        if len({row[0] for row in result}) != len(result):
            raise LighterReadError("duplicate account state identity")
        return tuple(sorted(result))
    return (rows(assets, "asset_id", ("asset_id",), ("balance", "locked_balance", "margin_balance"),
                 ("symbol", "margin_mode")),
            rows(positions, "market_id", ("market_id", "sign", "margin_mode", "open_order_count",
                 "pending_order_count"), ("position", "avg_entry_price", "initial_margin_fraction",
                 "allocated_margin") + (("unrealized_pnl",) if include_valuation else ()), ("symbol",)))


class LighterAccountPort:
    """Audit one exclusive account; attach a ledger only after the flat baseline.

    REST discovery times are monotonic receipt times, not reconstructed exchange
    times. Source sequence detects gaps/reordering; accepted ids retain their
    original events. Unattributed transfers are rejected by the equity bridge,
    never relabelled funding. A separate transfer workflow is not supported.
    """

    def __init__(self, adapter, symbol, clock, *, account_index,
                 expected_l1_address, known_order_ids=lambda: frozenset(),
                 flatten_id_for=lambda order_id: None,
                 terminal_order_ids=lambda: frozenset(), mutation_generation=lambda: None):
        if (type(account_index) is not int or account_index < 0
                or not isinstance(expected_l1_address, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{40}", expected_l1_address)):
            raise LighterReadError("explicit account and expected wallet required")
        self.adapter, self.symbol, self.clock = adapter, symbol, clock
        self._index, self._address = account_index, expected_l1_address.lower()
        self._known, self._flatten = known_order_ids, flatten_id_for
        self._terminal = terminal_order_ids
        self._generation = mutation_generation
        self._opening_orders = self._audited_orders = self._confirmation_orders = None
        self._audited_book = None
        self._terminal_proofs = {}
        self.latest_orders = ()
        self._ledger = self._baseline = None
        self._trades, self._fundings = {}, {}
        self._sequence = -1
        self._funding_time = -1
        self._mode = self._settlement = None
        self.stream = None
        self._stream_count = None
        self._stream_ids = frozenset()
        self._stream_trade_ahead = 0
        self._trade_state = None
        self._fees_cache = None
        self._fees_source = self._cash_cache = None
        self._fees_at = self._settlement_at = float("-inf")
        self._metadata_stream = None
        self.account_tier = None
        self._risk_position = ZERO
        self._risk_opened_at = self._verified_cash_at = None
        self.before_read = None

    def attach_ledger(self, ledger):
        if self._baseline is None or self._ledger is not None:
            raise LighterReadError("one ledger requires a completed flat baseline")
        report = ledger.snapshot(now=self._baseline.observed_monotonic)
        if report.ledger_position != ZERO or report.duration_seconds != ZERO:
            raise LighterReadError("ledger must start at the account baseline")
        self._ledger = ledger

    @property
    def accepted_funding_ids(self):
        return frozenset(self._fundings)

    def begin_quote_cycle(self):
        """Order observations are single-use handoffs, never a cross-cycle cache."""
        self._opening_orders = self._audited_orders = self._confirmation_orders = None
        self._audited_book = None

    def inventory_age_bound(self, now):
        """Conservative risk age; ledger analytics retain actual observation times."""
        if self._risk_opened_at is None:
            return ZERO
        age = _number(str(now)) - _number(str(self._risk_opened_at))
        if age < ZERO:
            raise LighterReadError("inventory risk clock moved backwards")
        return age

    @property
    def aligned_book(self):
        if self._audited_book is not None and self._audited_book[0] == self._generation():
            return self._audited_book[1]
        return None

    def _take_orders(self, name):
        observation = getattr(self, name)
        setattr(self, name, None)
        if (observation is not None and self.stream is not None and self.stream.transport_healthy
                and observation[0] is not None and observation[0] == self._generation()
                and 0 <= self.clock.monotonic() - observation[1] <= 3):
            return observation
        return None

    async def _read_orders(self, *, confirmation=False):
        if self.before_read is not None and not confirmation:
            self.before_read("orders")
        generation, started = self._generation(), self.clock.monotonic()
        rows = deepcopy(tuple(await self.adapter.get_open_orders(self.symbol)))
        if generation != self._generation():
            raise LighterReadError("execution changed during order read")
        return generation, started, rows, getattr(self.stream, "order_nonce", None)

    async def read_execution_orders(self, symbol):
        """OM consumes the completed account audit, or supplies its first bookend."""
        if symbol != self.symbol:
            raise LighterReadError("order observation symbol mismatch")
        if self.before_read is not None:
            self.before_read("sync")  # Includes up to two missing-slot history reads, even on handoff.
        audited = self._take_orders("_audited_orders")
        if audited is not None:
            return deepcopy(audited[2])
        self._opening_orders = None
        self._opening_orders = self._take_orders("_confirmation_orders") or await self._read_orders()
        return deepcopy(self._opening_orders[2])

    async def read_confirmation_orders(self, symbol):
        """A create lookup may supply the next OM sync and account opening proof."""
        self.begin_quote_cycle()
        if symbol != self.symbol:
            raise LighterReadError("confirmation observation symbol mismatch")
        self._confirmation_orders = await self._read_orders(confirmation=True)
        return deepcopy(self._confirmation_orders[2])

    def _orders(self, rows):
        result = []
        known = self._known()
        for row in rows:
            if row.symbol != self.symbol or str(row.id) not in known:
                raise LighterReadError("unknown or nonexclusive open order")
            info = row.raw_data.get("order_info")
            reduce_only = getattr(info, "reduce_only", None)
            if type(reduce_only) is not bool:
                raise LighterReadError("working order lacks reduce-only evidence")
            if _value(row.status) not in {"open", "partially_filled"}:
                raise LighterReadError("uncertain open order status")
            result.append(WorkingOrder(str(row.id), Side(_value(row.side)),
                                       _number(row.remaining), _number(row.price), reduce_only))
        if len({row.order_id for row in result}) != len(result):
            raise LighterReadError("duplicate open order identity")
        return tuple(sorted(result, key=lambda row: row.order_id))

    def _account(self, balances, orders, fees, now):
        if len(balances) != 1 or balances[0].currency != "USDG":
            raise LighterReadError("exclusive USDG collateral account required")
        balance = balances[0]
        account = balance.raw_data.get("account")
        if (getattr(account, "account_index", None) != self._index
                or getattr(account, "index", None) != self._index
                or str(getattr(account, "l1_address", "")).lower() != self._address):
            raise LighterReadError("account identity mismatch")
        # Only exclusive cross USDG: never reinterpret multi-asset collateral.
        mode = getattr(account, "account_trading_mode", None)
        if (type(mode) is not int or mode not in {0, 1}
                or self._mode is not None and mode != self._mode):
            raise LighterReadError("unsupported or changed account trading mode")
        if (getattr(account, "shares", None) != []
                or getattr(account, "pending_order_count", None) != 0
                or getattr(account, "total_order_count", None) != len(orders)):
            raise LighterReadError("unsupported or inconsistent account state")
        for asset in getattr(account, "assets", ()):
            if _number(asset.balance) != ZERO:
                raise LighterReadError("nonexclusive spot collateral")
        position, entry, unrealized = ZERO, None, ZERO
        matched = False
        for row in account.positions:
            size = _number(row.position)
            if size < ZERO:
                raise LighterReadError("negative position magnitude")
            if mode == 1 and (_number(row.allocated_margin) != ZERO
                    or not size and _number(row.unrealized_pnl) != ZERO):
                raise LighterReadError("unsupported Unified position accounting")
            if row.symbol != self.symbol:
                if size or row.open_order_count or row.pending_order_count:
                    raise LighterReadError("nonexclusive account position")
                continue
            if matched:
                raise LighterReadError("duplicate account position")
            matched = True
            if (row.margin_mode != 0 or _number(row.initial_margin_fraction) != Decimal("100")
                    or row.pending_order_count != 0 or row.open_order_count != len(orders)):
                raise LighterReadError("cross 1x and coherent account orders required")
            if size:
                if type(row.sign) is not int or row.sign not in {-1, 1}:
                    raise LighterReadError("invalid position direction")
                position, entry = size * row.sign, _number(row.avg_entry_price)
                unrealized = _number(row.unrealized_pnl)
        if not matched:
            raise LighterReadError("target market cross 1x settings not proven")
        collateral = _number(account.collateral)
        if collateral != _number(balance.total) or collateral < ZERO:
            raise LighterReadError("collateral mismatch")
        if mode == 1:
            collateral = self._unified_cash(account, unrealized, flat=position == ZERO)
        with localcontext() as context:
            context.traps[Inexact] = True
            equity = collateral + unrealized
        self._mode = mode
        return AccountSnapshot(self.symbol, now, position, equity,
                               _number(fees["maker_fee_rate"]), _number(fees["taker_fee_rate"]),
                               len(orders), True, entry, unrealized,
                               open_order_ids=tuple(row.order_id for row in orders))

    def _unified_cash(self, account, unrealized, *, flat):
        meta, assets = self._settlement, account.assets
        if (not meta or len(assets) != 1 or meta["symbol"] != "USDG"
                or type(meta["asset_id"]) is not int or meta["asset_id"] < 0
                or type(meta["decimals"]) is not int or meta["decimals"] != 6
                or _number(meta["index_price"]) != 1 or _number(meta["loan_to_value"]) != 1
                or account.total_isolated_order_count != 0 or account.pending_unlocks != []):
            raise LighterReadError("exclusive unit-valued Unified USDG required")
        asset = assets[0]
        if (asset.symbol != "USDG" or type(asset.asset_id) is not int
                or asset.asset_id != meta["asset_id"] or asset.margin_mode != "enabled"
                or _number(asset.balance) != ZERO or _number(asset.locked_balance) != ZERO):
            raise LighterReadError("unsupported Unified collateral asset")
        cash = _number(asset.margin_balance)
        quantum = Decimal(1).scaleb(-meta["decimals"])
        # Full-precision settlement cash is the ledger authority. Valuation
        # summaries and serialized position PnL have no common rounding/mark
        # contract; they cannot prove or disprove a nonflat cash reconciliation.
        truncated_cash = cash.quantize(quantum, rounding=ROUND_DOWN)
        values = {"cash": cash, "collateral": _number(account.collateral), "unrealized": unrealized,
                  "total": _number(account.total_asset_value), "cross": _number(account.cross_asset_value)}
        if cash < ZERO or truncated_cash != _number(account.collateral):
            raise LighterReadError("Unified cash and collateral mismatch", values=values)
        total, cross = map(_number, (account.total_asset_value, account.cross_asset_value))
        if total != cross or flat and (unrealized != ZERO or total != truncated_cash):
            raise LighterReadError("Unified exclusive valuation summary mismatch", values=values)
        return cash

    def _fill(self, trade, now):
        fee, raw = trade.fee, trade.raw_data
        order_id = str(trade.order_id)
        if trade.symbol != self.symbol or order_id not in self._known():
            raise LighterReadError("unattributed account fill")
        size, price, turnover = map(_number, (trade.amount, trade.price, trade.cost))
        tick, rate, cost = fee.get("tick"), _number(fee.get("rate")), _number(fee.get("cost"))
        if (type(tick) is not int or tick < 0 or rate != Decimal(tick) / 1000000
                or turnover != size * price or cost != turnover * rate
                or fee.get("currency") != "USDG"):
            raise LighterReadError("fill actual fee/notional proof mismatch")
        integrator = raw.get("integrator_fee_tick")
        if integrator is None:
            integrator = self.adapter.managed_order_integrator_fee_tick
        if integrator != ZERO:
            raise LighterReadError("unsupported integrator fees")
        return FillEvent(str(trade.id), order_id, self.symbol, Side(_value(trade.side)),
                         size, price, cost, LiquidityRole(fee["role"]), now,
                         flatten_id=self._flatten(order_id), source_timestamp_ms=raw["timestamp"],
                         realized_pnl=_number(raw.get("realized_pnl")))

    async def _check_terminal_fills(self):
        identifiers = set(self._terminal()) | self._terminal_proofs.keys()
        pending = identifiers - self._terminal_proofs.keys()
        history = {}
        if pending:
            if self.before_read is not None:
                self.before_read("terminal_history")
            rows = await self.adapter.get_order_history(self.symbol, limit=100)
            history = {str(row.id): row for row in rows}
            if len(rows) > 100 or len(history) != len(rows):
                raise LighterReadError("invalid terminal history window")
        for identifier in sorted(identifiers):
            accepted = sum((_number(row[4]) for row in self._trades.values()
                            if row[1] == identifier), ZERO)
            if identifier in self._terminal_proofs:
                if accepted != self._terminal_proofs[identifier][1]:
                    raise LighterReadError("late fill conflicts with immutable terminal proof")
                continue
            if identifier not in self._known():
                raise LighterReadError("unattributed terminal order")
            order = history.get(identifier)
            if order is None:
                raise AccountReadRace("exact terminal order proof unavailable")
            status = _value(order.status)
            if (str(order.id) != identifier or order.symbol != self.symbol
                    or status not in {"filled", "canceled", "expired", "rejected"}):
                raise LighterReadError("exact terminal order proof unavailable")
            filled, amount = _number(order.filled), _number(order.amount)
            if (not ZERO <= filled <= amount or accepted > filled
                    or (status == "filled" and filled != amount)):
                raise LighterReadError("terminal fills not reflected in account ledger")
            if accepted < filled:
                raise AccountReadRace("terminal fills not reflected in account ledger")
            self._terminal_proofs[identifier] = (status, filled, amount)

    @property
    def has_complete_empty_order_proof(self):
        """Only a completed, current audit can rule out further owned fills."""
        cached = self._cash_cache
        return bool(self.stream is not None and self.stream is self._metadata_stream
            and self.stream.transport_healthy and cached is not None
            and cached[0] is not None and cached[0] == self._generation()
            and 0 <= self.clock.monotonic() - cached[1] <= 10
            and cached[4] == () and self.latest_orders == ()
            and set(self._known()) <= self._terminal_proofs.keys()
            and set(self._terminal()) <= self._terminal_proofs.keys())

    def normal_terms_refresh_cost(self, horizon_seconds=0):
        """Read-only cost forecast; never extends the actual metadata TTL."""
        now = self.clock.monotonic() + horizon_seconds
        if self.stream is not self._metadata_stream:
            return 1200
        return (900 if not 0 <= now - self._fees_at < 28 else 0) + (
            300 if not 0 <= now - self._settlement_at < 28 else 0)

    async def _read_fees(self, *, allow_unreconciled_cash=False):
        if self.before_read is not None:
            self.before_read("fees")
        options = ({"allow_unsettled_funding": True} if allow_unreconciled_cash
                   and callable(getattr(self.adapter, "enable_market_maker_exact_funding", None)) else {})
        value = await self.adapter.get_account_fee_and_funding(self.symbol, limit=100, **options)
        tier = value.get("account_tier")
        self.account_tier = tier if tier in {"standard", "premium", "plus"} else None
        return value

    async def _read_trades(self):
        if self.before_read is not None:
            self.before_read("trades")
        return list(await self.adapter.get_account_trades(self.symbol, limit=100))

    async def _read_settlement(self):
        if self.before_read is not None:
            self.before_read("settlement")
        started = self.clock.monotonic()
        value = await self.adapter.get_settlement_asset()
        self._settlement, self._settlement_at = value, started

    async def _stream_read(self, *, allow_cash_reuse=False, allow_metadata_cache=False, force_trades=False,
                           force_funding=False, allow_unreconciled_cash=False):
        # Separate bounded fee/asset terms from fast account truth. A new
        # transport cannot inherit metadata or cash evidence from the old one.
        if self.stream is not self._metadata_stream:
            self._fees_at = self._settlement_at = float("-inf")
            self._cash_cache = None
            self._metadata_stream = self.stream
        terms_age = 28 if allow_metadata_cache else 8  # Keep 2s before the 30s proof limit.
        # Cash is read inside exact order bookends, or a prior complete cash proof
        # is revalidated by current full state/counters without restamping its age.
        opening = (self._take_orders("_opening_orders") or self._take_orders("_confirmation_orders")
                   or await self._read_orders())
        orders = self._orders(opening[2])
        book = None
        if opening[3] is not None:
            try:
                book = await self.stream.book_at_or_after(opening[3], after=opening[1])
            except (RuntimeError, TimeoutError):
                pass  # An unusable book must not prevent authenticated flat cleanup.
        cached = self._cash_cache
        unified_cash = (cached is not None
                        and cached[2][0].raw_data["account"].account_trading_mode == 1
                        and any(_number(row.position) != ZERO
                                for row in cached[2][0].raw_data["account"].positions))
        reuse = (allow_cash_reuse is True and cached is not None and cached[0] is not None
                 # A due terms query can consume most of this audit's 10s.
                 # Take new cash first rather than carry an older proof into it.
                 and 0 <= self.clock.monotonic() - self._fees_at < terms_age
                 and 0 <= self.clock.monotonic() - self._settlement_at < terms_age
                 and cached[0] == opening[0] and orders == cached[4]
                 # Unified account_all proves exact margin cash independently
                 # of moving valuation. Classic has no equivalent cash field.
                 and (unified_cash or all(_number(row.position) == ZERO
                         for row in cached[2][0].raw_data["account"].positions))
                 and 0 <= self.clock.monotonic() - cached[1] < 8)
        cash_at = cached[1] if reuse else self.clock.monotonic()
        balances = deepcopy(cached[2] if reuse else list(await self.adapter.get_balances()))
        closing = await self._read_orders()
        confirmed = self._orders(closing[2])
        account = await self.stream.request_snapshot("account_all")
        count = account["total_trades_count"]
        if (type(count) is not int or count < 0 or account["account"] != self._index
                or self._stream_count is not None and count < self._stream_count):
            raise LighterReadError("invalid account activity counter")
        funding_source = account["funding_histories"]
        if type(funding_source) not in (dict, list):
            raise LighterReadError("invalid account funding snapshot")
        # Retain the original cash/valuation and cash_at; current WS must still
        # prove all settlement, position, ownership and activity fields equal.
        include_valuation = reuse and not unified_cash
        def state(snapshot):
            if type(snapshot["assets"]) is not dict or type(snapshot["positions"]) is not dict:
                raise LighterReadError("incomplete account activity snapshot")
            return _account_state(snapshot["assets"].values(), snapshot["positions"].values(),
                                  snapshot["shares"], include_valuation=include_valuation)
        if len(balances) != 1:
            raise LighterReadError("exclusive collateral account required")
        raw = balances[0].raw_data["account"]
        if opening[0] != closing[0] or closing[0] != self._generation():
            raise LighterReadError("execution changed during account audit")
        if orders != confirmed:
            raise AccountReadRace("account changed during stream/REST bracket")
        if (reuse and count != cached[3]
                or state(account) != _account_state(raw.assets, raw.positions, raw.shares,
                                                   include_valuation=include_valuation)):
            if not reuse:
                raise AccountReadRace("account changed during stream/REST bracket")
            # Equal current order bookends can invalidate an older cash cache
            # without proving a concurrent fresh-read race. Start fresh cash
            # inside new order bookends and retain the one real race retry.
            # Even closing may predate the activity now visible in account_all.
            # This remains inside snapshot's original 10s wait; admit every
            # additional proof before its reads (normal only; exit uses fresh).
            self.begin_quote_cycle()
            self._cash_cache = None
            if self.before_read is not None:
                self.before_read("audit")
            return await self._stream_read(allow_metadata_cache=allow_metadata_cache,
                force_trades=force_trades, force_funding=force_funding,
                allow_unreconciled_cash=allow_unreconciled_cash)
        now = self.clock.monotonic()
        if (self.before_read is not None and 0 <= now - self._fees_at < terms_age
                and (force_funding or funding_source != self._fees_source)):
            self.before_read("funding_refresh")
        fees_refreshed = (force_funding or not 0 <= now - self._fees_at < terms_age
                          or funding_source != self._fees_source)
        if fees_refreshed:
            self._fees_cache = deepcopy(await self._read_fees(allow_unreconciled_cash=allow_unreconciled_cash))
            self._fees_at = now  # Request start, never restamp cached inputs.
            self._fees_source = deepcopy(funding_source)
        if ((force_funding and allow_metadata_cache)
                or not 0 <= self.clock.monotonic() - self._settlement_at < terms_age):
            await self._read_settlement()
        financial = _account_state(account["assets"].values(), account["positions"].values(), account["shares"])
        # Order counts change on quote revisions; mark PnL changes without fills.
        # Cash, size and entry changes must discover trades even if stats lag.
        classic_cash = _number(raw.collateral) if raw.account_trading_mode == 0 else None
        trade_state = (classic_cash, financial[0], tuple(row[:4] + row[6:] for row in financial[1]))
        fetch_trades = (force_trades or self._stream_count is None or count != self._stream_count
                        or trade_state != self._trade_state)
        trades = await self._read_trades() if fetch_trades else []
        if self._baseline is not None and not fees_refreshed and any(str(row.id) not in self._trades
               and row.fee.get("role") in {"maker", "taker"}
               and _number(row.fee["rate"]) > self._fees_cache[row.fee["role"] + "_fee_rate"]
               for row in trades):
            # A fill can reveal a fee increase before the terms TTL. Reuse the
            # bounded cash-race refresh, not an unmetered extra query or retry.
            # After refresh, actual fill fees remain authoritative: an older
            # fill can legitimately predate a subsequent fee discount.
            raise _AccountCashRace("new fill exceeds observed fee terms")
        if book is not None and not opening[3] <= book["nonce"] <= closing[3]:
            book = None
        mapped_trades = _trade_map(trades)
        trade_ahead = 0
        if self._stream_count is not None:
            delta = count - self._stream_count
            if delta >= 100:
                raise LighterReadError("account history window exhausted")
            trade_ahead = self._stream_trade_ahead + len(mapped_trades.keys() - self._stream_ids) - delta
            if trade_ahead < 0:
                raise AccountReadRace("account trade count and history disagree", values={
                    "stream_count": Decimal(count), "previous_count": Decimal(self._stream_count),
                    "history_count": Decimal(len(mapped_trades.keys() - self._stream_ids))})
        elif count < len(trades):
            raise AccountReadRace("account history exceeds activity counter")
        self._check_active_fills(closing[2], mapped_trades)
        return balances, orders, self._fees_cache, trades, cash_at, count, (opening, closing, book), trade_state, trade_ahead

    def _check_active_fills(self, rows, trades):
        observed_trades = self._trades | trades
        with localcontext() as context:
            context.traps[Inexact] = True
            for row in rows:
                filled, amount, remaining = map(_number, (row.filled, row.amount, row.remaining))
                accepted = sum((_number(fill[4]) for fill in observed_trades.values()
                                if fill[1] == str(row.id)), ZERO)
                if not ZERO <= filled <= amount or amount != filled + remaining:
                    raise LighterReadError("invalid active order fill quantities")
                if accepted != filled:
                    raise AccountReadRace("active order fills not reflected in account history")

    async def snapshot(self, *, allow_cash_reuse=False, allow_unreconciled_cash=False,
                       allow_metadata_cache=False):
        try:
            started, generation = self.clock.monotonic(), self._generation()
            force_funding = False
            # One shared retry/deadline covers the complete proof, not only its reads.
            for attempt in range(2 if self.stream is not None else 1):
                remaining = 10 - (self.clock.monotonic() - started)
                if remaining <= 0 or generation != self._generation():
                    raise LighterReadError("account audit deadline or generation changed")
                try:
                    if self.stream is not None and self.before_read is not None:
                        if attempt:
                            self.before_read("retry")
                        self.before_read("audit")
                    return await asyncio.wait_for(self._snapshot_once(started,
                        allow_cash_reuse=allow_cash_reuse and not attempt, force_trades=bool(attempt),
                        allow_metadata_cache=allow_metadata_cache,
                        force_funding=force_funding, allow_unreconciled_cash=allow_unreconciled_cash), remaining)
                except AccountReadRace as error:
                    if attempt or self.stream is None:
                        raise
                    force_funding = isinstance(error, _AccountCashRace)
                    self.begin_quote_cycle()
        except asyncio.CancelledError:
            self.begin_quote_cycle()
            self._cash_cache = None
            raise
        except ApiBudgetUnavailable:
            self.begin_quote_cycle()
            self._cash_cache = None
            raise  # Local backpressure is recoverable; it is not bad account data.
        except LighterReadError:
            self.begin_quote_cycle()
            self._cash_cache = None
            self._fees_at = self._settlement_at = float("-inf")
            raise  # These are code-owned messages, never raw SDK/account payloads.
        except Exception:
            self.begin_quote_cycle()
            self._cash_cache = None
            self._fees_at = self._settlement_at = float("-inf")
            raise LighterReadError("authenticated account audit unavailable") from None

    async def _snapshot_once(self, started, *, allow_cash_reuse, allow_metadata_cache=False, force_trades=False,
                             force_funding=False, allow_unreconciled_cash=False):
        if self.stream is not None:
            values = await self._stream_read(allow_cash_reuse=allow_cash_reuse, force_trades=force_trades,
                allow_metadata_cache=allow_metadata_cache,
                force_funding=force_funding, allow_unreconciled_cash=allow_unreconciled_cash)
            balances, orders, fees, second, cash_at, count, bookends, trade_state, trade_ahead = values
            first, confirmed_fees, confirmed_orders = second, fees, orders
        else:  # Explicit slow read-only forensic/preflight path, never the runner loop.
            first = await self._read_trades()
            fees = await self._read_fees(allow_unreconciled_cash=allow_unreconciled_cash)
            orders = self._orders(await self.adapter.get_open_orders())
            cash_at = self.clock.monotonic()
            balances = list(await self.adapter.get_balances())
            if len(balances) == 1 and getattr(balances[0].raw_data.get("account"),
                                            "account_trading_mode", None) == 1:
                await self._read_settlement()
            second = await self._read_trades()
            confirmed_fees = await self._read_fees(allow_unreconciled_cash=allow_unreconciled_cash)
            confirmed_rows = deepcopy(tuple(await self.adapter.get_open_orders()))
            confirmed_orders = self._orders(confirmed_rows)
        now = self.clock.monotonic()
        trades, funding = _trade_map(second), _funding_map(fees["fundings"])
        if (not 0 <= now - started <= 10 or _trade_map(first) != trades
                or fees != confirmed_fees or orders != confirmed_orders):
            raise LighterReadError("account changed during consistent read")
        if self.stream is None:
            self._check_active_fills(confirmed_rows, trades)
        account = self._account(balances, orders, fees, cash_at)
        if self.stream is not None:
            account = replace(account, inputs_observed_monotonic=bookends[0][1],
                terms_observed_monotonic=min(self._fees_at, self._settlement_at))
        if not account.fresh(now):
            raise LighterReadError("account or financial inputs stale")
        if self._baseline is None:
            if account.position or account.open_order_count:
                raise LighterReadError("authenticated flat and empty start required")
            self._baseline = account
            self._trades, self._fundings = trades, funding
            self._sequence = max((row[13] for row in trades.values()), default=-1)
            self._funding_time = max((row[0] for row in funding.values()), default=-1)
        else:
            new_trades = [row for row in second if str(row.id) not in self._trades]
            new_funding = {key: row for key, row in funding.items() if key not in self._fundings}
            if len(new_trades) >= 100 or len(new_funding) >= 100:
                raise LighterReadError("account history window exhausted")
            if any(key in self._trades and self._trades[key] != row for key, row in trades.items()):
                raise LighterReadError("conflicting trade identity")
            if any(key in self._fundings and self._fundings[key] != row for key, row in funding.items()):
                raise LighterReadError("conflicting funding identity")
            for row in new_trades:
                sequence = row.raw_data.get("trade_sequence")
                if type(sequence) is not int or sequence <= self._sequence:
                    raise LighterReadError("out-of-order account trade")
            if any(row[0] < self._funding_time for row in new_funding.values()):
                raise LighterReadError("out-of-order funding")
            if self._ledger is None:
                if (new_trades or new_funding or account.position or orders
                        or account.equity != self._baseline.equity):
                    raise LighterReadError("ledger required before account activity")
            else:
                pending = [(row.raw_data["timestamp"], row.raw_data["trade_sequence"],
                            self._fill(row, now)) for row in new_trades]
                pending += [(row[0], -1, CashflowEvent("funding:" + key, self.symbol,
                            now, row[1], CashflowKind.FUNDING)) for key, row in new_funding.items()]
                report = self._ledger.snapshot(now=now)
                expected = report.ledger_position + sum((event.size if event.side == Side.BUY
                    else -event.size for _, _, event in pending if isinstance(event, FillEvent)), ZERO)
                if expected != account.position:
                    raise AccountReadRace("account fills and position disagree", values={
                        "ledger_position": expected, "account_position": account.position})
                for _, _, event in sorted(pending, key=lambda row: (row[0], row[1])):
                    if isinstance(event, FillEvent):
                        self._ledger.ingest_fill(event)
                        self._trades[event.fill_id] = trades[event.fill_id]
                        self._sequence = max(self._sequence, trades[event.fill_id][13])
                    else:
                        self._ledger.ingest_cashflow(event)
                        key = event.event_id.removeprefix("funding:")
                        self._fundings[key] = funding[key]
                        self._funding_time = max(self._funding_time, funding[key][0])
                report = self._ledger.snapshot(now=now)
                if not allow_unreconciled_cash:
                    with localcontext() as context:
                        context.traps[Inexact] = True
                        expected_equity = (self._baseline.equity + report.realized_net_pnl
                                           + report.external_transfers + account.unrealized_pnl)
                    if expected_equity != account.equity:
                        raise UnattributedCashflow("unattributed account cashflow or equity mismatch", values={
                            "expected_equity": expected_equity, "account_equity": account.equity})
            self._trades.update(trades)
            self._fundings.update(funding)
            self._sequence = max((row[13] for row in trades.values()), default=self._sequence)
            self._funding_time = max((row[0] for row in funding.values()), default=self._funding_time)
        await self._check_terminal_fills()
        if not 0 <= self.clock.monotonic() - started <= 10:
            raise LighterReadError("account audit exceeded freshness bound")
        if self.stream is not None:
            if bookends[1][0] != self._generation():
                raise LighterReadError("execution changed during account audit")
            self._stream_count = count
            self._stream_ids = frozenset(self._trades)
            self._trade_state, self._stream_trade_ahead = trade_state, trade_ahead
            self._audited_orders = bookends[1]
            self._audited_book = (bookends[1][0], bookends[2])
            self._cash_cache = (bookends[1][0], cash_at, deepcopy(balances), count, orders)
        if account.position == ZERO:
            self._risk_opened_at = None
        elif (self._risk_position == ZERO
                or (account.position > ZERO) != (self._risk_position > ZERO)):
            # A delayed fill could open just after the prior cash proof began.
            self._risk_opened_at = self._verified_cash_at
        self._risk_position, self._verified_cash_at = account.position, cash_at
        self.latest_orders = orders
        return account


class LighterMarketData:
    """Fresh nonce-checked stream depth; bounded REST only for explicit diagnostics."""

    def __init__(self, adapter, symbol, clock, *, working_orders=lambda: (), aligned_book=None):
        self.adapter, self.symbol, self.clock = adapter, symbol, clock
        self._working = working_orders
        self._aligned_book = aligned_book
        self._state = None
        self._valid = False
        self._last_book = None
        self.min_quote_amount = ZERO
        self.stream = None

    async def initialize(self):
        try:
            info = await self.adapter.get_exchange_info()
            rows = [row for row in info.symbols if row["symbol"] == self.symbol]
            if len(rows) != 1 or rows[0]["status"] != "active":
                raise LighterReadError("active exclusive market required")
            row = rows[0]
            for key in ("price_decimals", "size_decimals"):
                if type(row[key]) is not int or not 0 <= row[key] <= 18:
                    raise LighterReadError("unsupported market precision")
            self.min_quote_amount = _number(row["min_quote_amount"])
            if self.min_quote_amount <= ZERO:
                raise LighterReadError("missing market minimum notional")
            self._state = MarketState(self.symbol, tick_size=Decimal(1).scaleb(-row["price_decimals"]),
                size_step=Decimal(1).scaleb(-row["size_decimals"]),
                min_order_size=_number(row["min_base_amount"]))
        except Exception:
            raise LighterReadError("market metadata unavailable") from None

    async def refresh(self):
        self._valid = False
        try:
            if self._state is None:
                raise LighterReadError("market metadata not initialized")
            started = self.clock.monotonic()
            own = self._working()
            if self.stream is not None and self._aligned_book is not None and hasattr(self.stream, "book_at_or_after"):
                book = self._aligned_book()
                if book is None or not self.stream.transport_healthy:
                    raise LighterReadError("book lacks coherent order watermarks")
                self.stream.check_book_source(book["timestamp"])
            else:
                book = (self.stream.book_snapshot() if self.stream is not None
                        else await self.adapter.get_orderbook(self.symbol, limit=20))
            now = self.clock.monotonic()
            if self.stream is not None:
                observed = book["received_monotonic"]
                bids, asks = book["bids"], book["asks"]
            else:
                observed = started
                if book.symbol != self.symbol:
                    raise LighterReadError("book symbol mismatch")
                bids, asks = book.bids, book.asks
            if not 0 <= now - observed <= 3 or own != self._working():
                raise LighterReadError("incoherent or slow book read")
            source = (observed, tuple(bids), tuple(asks), own)
            if self.stream is not None and source == self._last_book:
                result = self._state.snapshot()
                self._valid = True
                return result  # Same source, same own sizes; never advance its timestamp.
            def levels(rows):
                # REST returns individual orders, so equal prices are aggregated.
                result = {}
                for row in rows:
                    price, size = map(_number, row) if self.stream is not None else (_number(row.price), _number(row.size))
                    if price <= ZERO or size <= ZERO:
                        raise LighterReadError("invalid book level")
                    result[price] = result.get(price, ZERO) + size
                return tuple(result.items())
            result = self._state.update(bids=levels(bids), asks=levels(asks),
                own_bids=tuple((row.price, row.remaining_size) for row in own if row.side == Side.BUY),
                own_asks=tuple((row.price, row.remaining_size) for row in own if row.side == Side.SELL),
                observed_monotonic=observed, trusted=True)
            self._valid = True
            self._last_book = source
            return result
        except Exception:
            raise LighterReadError("trusted market read unavailable") from None

    def snapshot(self):
        if not self._valid:
            raise LighterReadError("trusted market snapshot unavailable")
        result = self._state.snapshot()
        if not 0 <= self.clock.monotonic() - result.observed_monotonic <= 3:
            raise LighterReadError("market snapshot stale")
        return result

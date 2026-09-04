"""Read-only Lighter mapping. Execution and credential ownership stay outside V2."""

from decimal import Decimal
import re

from .domain import (
    AccountSnapshot, CashflowEvent, CashflowKind, FillEvent, LiquidityRole,
    Side, WorkingOrder, ZERO,
)
from .market_state import MarketState


class LighterReadError(RuntimeError):
    """Sanitized failure: an incomplete read cannot authorize execution."""


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
            raw.get("integrator_fee_tick"))


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
                 terminal_order_ids=lambda: frozenset()):
        if (type(account_index) is not int or account_index < 0
                or not isinstance(expected_l1_address, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{40}", expected_l1_address)):
            raise LighterReadError("explicit account and expected wallet required")
        self.adapter, self.symbol, self.clock = adapter, symbol, clock
        self._index, self._address = account_index, expected_l1_address.lower()
        self._known, self._flatten = known_order_ids, flatten_id_for
        self._terminal = terminal_order_ids
        self._terminal_proofs = {}
        self.latest_orders = ()
        self._ledger = self._baseline = None
        self._trades, self._fundings = {}, {}
        self._sequence = -1
        self._funding_time = -1

    def attach_ledger(self, ledger):
        if self._baseline is None or self._ledger is not None:
            raise LighterReadError("one ledger requires a completed flat baseline")
        report = ledger.snapshot(now=self._baseline.observed_monotonic)
        if report.ledger_position != ZERO or report.duration_seconds != ZERO:
            raise LighterReadError("ledger must start at the account baseline")
        self._ledger = ledger

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
        # Cross/classic USDG accounting only: alternate collateral, pools and
        # isolated margin need explicit accounting support, never assumptions.
        if (getattr(account, "account_trading_mode", None) != 0
                or getattr(account, "shares", None) != []
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
        return AccountSnapshot(self.symbol, now, position, collateral + unrealized,
                               _number(fees["maker_fee_rate"]), _number(fees["taker_fee_rate"]),
                               len(orders), True, entry, unrealized,
                               open_order_ids=tuple(row.order_id for row in orders))

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
                         flatten_id=self._flatten(order_id))

    async def _check_terminal_fills(self):
        for identifier in sorted(set(self._terminal()) | self._terminal_proofs.keys()):
            accepted = sum((_number(row[4]) for row in self._trades.values()
                            if row[1] == identifier), ZERO)
            if identifier in self._terminal_proofs:
                if accepted != self._terminal_proofs[identifier][1]:
                    raise LighterReadError("late fill conflicts with immutable terminal proof")
                continue
            if identifier not in self._known():
                raise LighterReadError("unattributed terminal order")
            order = await self.adapter.get_order(identifier, self.symbol)
            status = _value(order.status)
            if (str(order.id) != identifier or order.symbol != self.symbol
                    or status not in {"filled", "canceled", "expired", "rejected"}):
                raise LighterReadError("exact terminal order proof unavailable")
            filled, amount = _number(order.filled), _number(order.amount)
            if (not ZERO <= filled <= amount or accepted != filled
                    or (status == "filled" and filled != amount)):
                raise LighterReadError("terminal fills not reflected in account ledger")
            self._terminal_proofs[identifier] = (status, filled, amount)

    async def snapshot(self):
        try:
            started = self.clock.monotonic()
            first = list(await self.adapter.get_account_trades(self.symbol, limit=100))
            fees = await self.adapter.get_account_fee_and_funding(self.symbol, limit=100)
            orders = self._orders(await self.adapter.get_open_orders())
            balances = list(await self.adapter.get_balances())
            second = list(await self.adapter.get_account_trades(self.symbol, limit=100))
            confirmed_fees = await self.adapter.get_account_fee_and_funding(self.symbol, limit=100)
            confirmed_orders = self._orders(await self.adapter.get_open_orders())
            now = self.clock.monotonic()
            trades, funding = _trade_map(second), _funding_map(fees["fundings"])
            if (not 0 <= now - started <= 10 or _trade_map(first) != trades
                    or fees != confirmed_fees or orders != confirmed_orders):
                raise LighterReadError("account changed during consistent read")
            account = self._account(balances, orders, fees, now)
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
                        raise LighterReadError("account fills and position disagree")
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
                    expected_equity = (self._baseline.equity + report.realized_net_pnl
                                       + report.external_transfers + account.unrealized_pnl)
                    if expected_equity != account.equity:
                        raise LighterReadError("unattributed account cashflow or equity mismatch")
                self._trades.update(trades)
                self._fundings.update(funding)
                self._sequence = max((row[13] for row in trades.values()), default=self._sequence)
                self._funding_time = max((row[0] for row in funding.values()), default=self._funding_time)
            await self._check_terminal_fills()
            if not 0 <= self.clock.monotonic() - started <= 10:
                raise LighterReadError("account audit exceeded freshness bound")
            self.latest_orders = orders
            return account
        except Exception:
            raise LighterReadError("authenticated account audit unavailable") from None


class LighterMarketData:
    """Bounded REST depth, not unproven WS sequence continuity or cached freshness."""

    def __init__(self, adapter, symbol, clock, *, working_orders=lambda: ()):
        self.adapter, self.symbol, self.clock = adapter, symbol, clock
        self._working = working_orders
        self._state = None
        self._valid = False
        self.min_quote_amount = ZERO

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
            book = await self.adapter.get_orderbook(self.symbol, limit=20)
            now = self.clock.monotonic()
            if book.symbol != self.symbol or not 0 <= now - started <= 3 or own != self._working():
                raise LighterReadError("incoherent or slow book read")
            def levels(rows):
                # REST returns individual orders, so equal prices are aggregated.
                result = {}
                for row in rows:
                    price, size = _number(row.price), _number(row.size)
                    if price <= ZERO or size <= ZERO:
                        raise LighterReadError("invalid book level")
                    result[price] = result.get(price, ZERO) + size
                return tuple(result.items())
            result = self._state.update(bids=levels(book.bids), asks=levels(book.asks),
                own_bids=tuple((row.price, row.remaining_size) for row in own if row.side == Side.BUY),
                own_asks=tuple((row.price, row.remaining_size) for row in own if row.side == Side.SELL),
                observed_monotonic=now, trusted=True)
            self._valid = True
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

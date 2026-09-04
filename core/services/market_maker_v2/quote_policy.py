"""Pure one-layer quote proposals. No execution, entry breakeven or session gates."""

from decimal import Decimal, DecimalException, localcontext

from .domain import (
    AccountSnapshot, InventoryDecision, MarketStateSnapshot, QuoteIntent,
    QuotePlan, Side, StrategyState, ZERO, _decimal, _time,
)
from .market_state import MAX_VOLATILITY_BUFFER_BPS


BPS = Decimal("10000")


class QuoteUnavailable(ValueError):
    """Inputs cannot safely support a quote proposal; no execution was attempted."""


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value // step) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    quotient, remainder = divmod(value, step)
    return (quotient + bool(remainder)) * step


class VolumeQuotePolicy:
    """Consume explicit governor permissions; never infer capacity from order count.

    Phase 4 only renders quotes from synthetic decisions. The Phase 5 governor
    owns soft-band classification and dynamic pre-trade loss reserve; execution
    owns old-order cancellation/proof before replacing a desired quote plan.
    """

    def __init__(self, *, order_size: Decimal, target_net_edge_bps: Decimal,
                 volatility_multiplier: Decimal, hard_inventory_limit: Decimal,
                 skew_bps_at_hard: Decimal):
        _decimal(order_size, positive=True)
        _decimal(hard_inventory_limit, positive=True)
        for value in (target_net_edge_bps, volatility_multiplier, skew_bps_at_hard):
            _decimal(value)
            if value < ZERO:
                raise ValueError("quote parameters must be nonnegative")
        if skew_bps_at_hard >= BPS:
            raise ValueError("inventory shift must keep reservation positive")
        self.order_size = order_size
        self.target_net_edge_bps = target_net_edge_bps
        self.volatility_multiplier = volatility_multiplier
        self.hard_inventory_limit = hard_inventory_limit
        self.skew_bps_at_hard = skew_bps_at_hard

    def propose(self, market: MarketStateSnapshot, account: AccountSnapshot,
                risk: InventoryDecision, *, now: float) -> QuotePlan:
        """Return at most one POST_ONLY quote per side; no mutations or fill claims."""
        self._validate_inputs(market, account, risk, now)
        values = (self.order_size, self.target_net_edge_bps, self.volatility_multiplier,
                  self.hard_inventory_limit, self.skew_bps_at_hard, market.external_bid,
                  market.external_ask, market.microprice or ZERO, market.tick_size,
                  market.size_step, market.ewma_move_bps, account.position,
                  account.maker_fee_rate, risk.buy_capacity, risk.sell_capacity)
        # Preserve tiny residuals/capacity fractions through products and aligned
        # subtraction before lot rounding. Bound pathological input precision.
        precision = (sum(len(v.as_tuple().digits) for v in values)
                     + max(v.adjusted() for v in values)
                     - min(v.as_tuple().exponent for v in values) + 16)
        if precision > 4096:
            raise QuoteUnavailable("quote input precision exceeds supported range")
        try:
            with localcontext() as context:
                context.prec = max(context.prec, precision)
                return self._propose(market, account, risk)
        except DecimalException:
            raise QuoteUnavailable("unusable quote arithmetic") from None

    def _propose(self, market, account, risk):
        empty = QuotePlan(market.symbol)
        if risk.state in {StrategyState.FLATTENING, StrategyState.COOLDOWN,
                          StrategyState.SESSION_COMPLETE}:
            return empty
        if abs(account.position) > self.hard_inventory_limit:
            # The governor must flatten, not try to repair a breach with new quotes.
            return empty
        if risk.state == StrategyState.REDUCE_ONLY:
            return self._reduce_plan(market, account, risk)
        if abs(account.position) == self.hard_inventory_limit:
            raise QuoteUnavailable("hard inventory requires reducing-only decision")

        bid, ask = self._prices(market, account)
        quotes = []
        for side, price, capacity, direction in (
                (Side.BUY, bid, risk.buy_capacity, Decimal("1")),
                (Side.SELL, ask, risk.sell_capacity, Decimal("-1"))):
            limit = min(self.order_size, capacity,
                        self.hard_inventory_limit - direction * account.position)
            if risk.state == StrategyState.SKEWED and direction * account.position > ZERO:
                # Fixed soft-band size reduction; no extra user-facing tuning knob.
                limit = min(limit, self.order_size / 2)
            size = _floor(limit, market.size_step)
            if size >= market.min_order_size and size > ZERO:
                quotes.append(QuoteIntent(side, price, size))
        return QuotePlan(market.symbol, tuple(quotes))

    @staticmethod
    def _validate_inputs(market, account, risk, now):
        if (type(market) is not MarketStateSnapshot or type(account) is not AccountSnapshot
                or type(risk) is not InventoryDecision):
            raise QuoteUnavailable("typed market, account and risk inputs required")
        try:
            _time(now)
        except ValueError:
            raise QuoteUnavailable("valid monotonic clock required") from None
        if (market.symbol != account.symbol or not market.trusted or not account.authenticated
                or not 0 <= now - market.observed_monotonic <= 3
                or not 0 <= now - account.observed_monotonic <= 10):
            raise QuoteUnavailable("fresh trusted same-symbol market and account required")

    def _prices(self, market, account):
        reference = market.microprice
        if reference is None or not market.external_bid <= reference <= market.external_ask:
            reference = (market.external_bid + market.external_ask) / 2
        ratio = max(Decimal("-1"), min(Decimal("1"),
                                      account.position / self.hard_inventory_limit))
        reservation = reference * (1 - ratio * self.skew_bps_at_hard / BPS)
        volatility = min(MAX_VOLATILITY_BUFFER_BPS,
                         self.volatility_multiplier * market.ewma_move_bps)
        half_bps = account.maker_fee_rate * BPS + self.target_net_edge_bps / 2 + volatility
        if half_bps >= BPS:
            raise QuoteUnavailable("spread cannot produce positive bid")
        bid = min(_floor(reservation * (1 - half_bps / BPS), market.tick_size),
                  market.external_ask - market.tick_size)
        ask = max(_ceil(reservation * (1 + half_bps / BPS), market.tick_size),
                  market.external_bid + market.tick_size)
        if bid >= ask:
            # Zero fee/edge/vol can round both sides to one tick; widen outward.
            ask = bid + market.tick_size
        if bid <= ZERO:
            raise QuoteUnavailable("no positive passive bid at this tick size")
        return bid, ask

    def _reduce_plan(self, market, account, risk):
        """§7.2 passive-touch exception to entry fee floor, never an entry-price lock."""
        if account.position == ZERO:
            return QuotePlan(market.symbol)
        if account.position > ZERO:
            side, price, capacity = Side.SELL, market.external_ask, risk.sell_capacity
        else:
            side, price, capacity = Side.BUY, market.external_bid, risk.buy_capacity
        size = _floor(min(self.order_size, capacity, abs(account.position)), market.size_step)
        if size < market.min_order_size or size <= ZERO:
            return QuotePlan(market.symbol)
        return QuotePlan(market.symbol, (QuoteIntent(side, price, size, reduce_only=True),))

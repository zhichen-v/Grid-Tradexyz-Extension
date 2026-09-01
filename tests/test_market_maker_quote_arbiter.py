from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from core.adapters.exchanges.models import OrderSide
from core.services.market_maker.controllers import (
    QuoteControllerDecision,
    SideQuoteAdjustment,
)
from core.services.market_maker.models import (
    DesiredOrder,
    DesiredQuotes,
    MarketMetadata,
    PositionSnapshot,
    RuntimeState,
)
from core.services.market_maker.quote_arbiter import (
    QuoteArbiterContext,
    apply_entry_controller,
)
from core.services.market_maker.risk_manager import RiskDecision


class QuoteArbiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = MarketMetadata(
            symbol="BTC",
            price_decimals=1,
            size_decimals=1,
            price_tick=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_base_amount=Decimal("0.1"),
            min_quote_amount=Decimal("0"),
        )
        self.position = PositionSnapshot(
            symbol="BTC",
            signed_size=Decimal("0"),
            entry_price=None,
            unrealized_pnl=Decimal("0"),
            received_monotonic=100.0,
        )
        self.risk = RiskDecision(
            buy_amount=Decimal("0.2"),
            sell_amount=Decimal("0.2"),
            buy_reduce_only=False,
            sell_reduce_only=False,
            buy_capacity=Decimal("1"),
            sell_capacity=Decimal("1"),
            worst_long=Decimal("0.2"),
            worst_short=Decimal("-0.2"),
            inventory_ratio=Decimal("0"),
            runtime_state=RuntimeState.ACTIVE,
            reason="normal",
            safe=True,
        )

    @staticmethod
    def base(*, bid=True, ask=True, reduce_only=False) -> DesiredQuotes:
        return DesiredQuotes(
            bid=(
                DesiredOrder(
                    OrderSide.BUY,
                    Decimal("99.9"),
                    Decimal("0.2"),
                    reduce_only,
                    "normal",
                )
                if bid
                else None
            ),
            ask=(
                DesiredOrder(
                    OrderSide.SELL,
                    Decimal("100.1"),
                    Decimal("0.2"),
                    reduce_only,
                    "normal",
                )
                if ask
                else None
            ),
            reference_price=Decimal("100"),
            reservation_price=Decimal("100"),
            half_spread=Decimal("0.1"),
            inventory_ratio=Decimal("0"),
            runtime_state=RuntimeState.ACTIVE,
            reason="normal",
        )

    @staticmethod
    def decision(mode="active", *, bid_extra=2, ask_extra=3, ready=True):
        return QuoteControllerDecision(
            mode=mode,
            controller="toxicity_v1",
            ready=ready,
            reason="toxicity_ready",
            decision_id=7,
            bid=SideQuoteAdjustment(extra_spread_ticks=bid_extra),
            ask=SideQuoteAdjustment(extra_spread_ticks=ask_extra),
            features=object(),
        )

    def apply(self, base, decision, **kwargs):
        return apply_entry_controller(
            base,
            decision,
            kwargs.pop("position", self.position),
            kwargs.pop("risk", self.risk),
            kwargs.pop("metadata", self.metadata),
            context=kwargs.pop("context", None),
        )

    def test_fixed_and_shadow_are_exact_no_ops(self) -> None:
        base = self.base()

        fixed = self.apply(base, self.decision("fixed"))
        shadow = self.apply(base, self.decision("shadow", ready=False))

        self.assertIs(fixed, base)
        self.assertIs(shadow, base)

    def test_active_widens_outward_without_changing_size_or_quote_metadata(self) -> None:
        base = self.base()

        applied = self.apply(base, self.decision())

        self.assertEqual(applied.bid.price, Decimal("99.7"))
        self.assertEqual(applied.ask.price, Decimal("100.4"))
        self.assertEqual(applied.bid.amount, base.bid.amount)
        self.assertEqual(applied.ask.amount, base.ask.amount)
        self.assertEqual(applied.bid.reduce_only, base.bid.reduce_only)
        self.assertEqual(applied.ask.reduce_only, base.ask.reduce_only)
        self.assertEqual(applied.reference_price, base.reference_price)
        self.assertEqual(applied.reservation_price, base.reservation_price)
        self.assertEqual(applied.half_spread, base.half_spread)
        self.assertIn("controller=toxicity_v1", applied.reason)

    def test_active_blocks_a_side_and_never_adds_a_missing_side(self) -> None:
        base = self.base(ask=False)
        decision = replace(
            self.decision(),
            bid=SideQuoteAdjustment(blocked=True),
        )

        applied = self.apply(base, decision)

        self.assertIsNone(applied.bid)
        self.assertIsNone(applied.ask)

    def test_nonflat_position_bypasses_even_invalid_active_decision(self) -> None:
        base = self.base(reduce_only=True)
        position = replace(
            self.position,
            signed_size=Decimal("0.2"),
            entry_price=Decimal("100"),
        )

        applied = self.apply(
            base,
            self.decision(ready=False),
            position=position,
        )

        self.assertIs(applied, base)

    def test_reduce_only_base_bypasses_controller(self) -> None:
        base = self.base(reduce_only=True)

        self.assertIs(self.apply(base, self.decision()), base)

    def test_protective_contexts_take_precedence(self) -> None:
        cases = (
            QuoteArbiterContext(economic_stop_pending=True),
            QuoteArbiterContext(entry_admission_allowed=False),
            QuoteArbiterContext(risk_reduction_active=True),
            QuoteArbiterContext(inventory_unwind_active=True),
            QuoteArbiterContext(active_unwind_pending=True),
            QuoteArbiterContext(active_unwind_ready=True),
            QuoteArbiterContext(ordinary_flat_entry=False),
        )
        for context in cases:
            with self.subTest(context=context):
                base = self.base()
                self.assertIs(
                    self.apply(base, self.decision(), context=context),
                    base,
                )

    def test_non_active_risk_state_takes_precedence(self) -> None:
        base = self.base()
        risk = replace(self.risk, runtime_state=RuntimeState.RISK_REDUCTION)

        self.assertIs(self.apply(base, self.decision(), risk=risk), base)

    def test_invalid_active_decision_fails_closed(self) -> None:
        base = self.base()

        applied = self.apply(base, self.decision(ready=False))

        self.assertIsNone(applied.bid)
        self.assertIsNone(applied.ask)
        self.assertIn("controller_error=invalid_active_decision", applied.reason)

    def test_negative_active_adjustment_fails_closed(self) -> None:
        base = self.base()
        decision = replace(
            self.decision(),
            bid=SideQuoteAdjustment(extra_spread_ticks=-1),
        )

        applied = self.apply(base, decision)

        self.assertIsNone(applied.bid)
        self.assertIsNone(applied.ask)

    def test_misaligned_or_crossed_base_fails_closed(self) -> None:
        misaligned = self.base()
        misaligned = replace(
            misaligned,
            bid=replace(misaligned.bid, price=Decimal("99.95")),
        )
        crossed = self.base()
        crossed = replace(
            crossed,
            bid=replace(crossed.bid, price=Decimal("100.1")),
        )

        for base in (misaligned, crossed):
            with self.subTest(base=base):
                applied = self.apply(base, self.decision())
                self.assertIsNone(applied.bid)
                self.assertIsNone(applied.ask)


if __name__ == "__main__":
    unittest.main()

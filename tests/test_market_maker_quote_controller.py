from __future__ import annotations

import unittest
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from core.services.market_maker.controllers import (
    FixedEntryQuoteController,
    QuoteControllerContext,
    ToxicityAwareEntryQuoteController,
)


@dataclass(frozen=True)
class _Features:
    health: object = "ready"
    reason: str = "ready"
    sample_count: int = 30
    mid: Decimal | None = Decimal("100")
    return_5s_ticks: Decimal | None = Decimal("0")
    return_15s_ticks: Decimal | None = Decimal("0")
    rms_1s_move_15s_ticks: Decimal | None = Decimal("0")
    microprice_shift_ticks: Decimal | None = Decimal("0")


class QuoteControllerTests(unittest.TestCase):
    @staticmethod
    def controller(**overrides) -> ToxicityAwareEntryQuoteController:
        values = {
            "mode": "shadow",
            "min_signal_ticks": Decimal("1"),
            "widen_start_ticks": Decimal("1"),
            "max_extra_spread_ticks": 10,
            "block_threshold_ticks": Decimal("5"),
            "resume_threshold_ticks": Decimal("0.5"),
            "block_confirmations": 2,
            "resume_confirmations": 2,
            "min_block_seconds": 5,
        }
        values.update(overrides)
        return ToxicityAwareEntryQuoteController(**values)

    @staticmethod
    def evaluate(controller, features=None, now=100.0, feedback=None):
        return controller.evaluate(
            QuoteControllerContext(
                now_monotonic=now,
                features=features if features is not None else _Features(),
                metadata=SimpleNamespace(price_tick=Decimal("0.1")),
                entry_markout_feedback=feedback,
            )
        )

    def test_fixed_controller_has_zero_adjustment_and_monotonic_ids(self) -> None:
        controller = FixedEntryQuoteController()

        first = self.evaluate(controller)
        second = self.evaluate(controller)

        self.assertTrue(first.ready)
        self.assertEqual(first.mode, "fixed")
        self.assertEqual(first.bid.extra_spread_ticks, 0)
        self.assertEqual(first.ask.extra_spread_ticks, 0)
        self.assertEqual((first.decision_id, second.decision_id), (1, 2))

    def test_upward_pressure_only_adds_directional_ask_toxicity(self) -> None:
        decision = self.evaluate(
            self.controller(),
            _Features(
                return_5s_ticks=Decimal("3"),
                return_15s_ticks=Decimal("9"),
                microprice_shift_ticks=Decimal("2"),
            ),
        )

        self.assertEqual(decision.bid.toxicity_score_ticks, Decimal("0"))
        self.assertEqual(decision.bid.extra_spread_ticks, 0)
        self.assertEqual(decision.ask.toxicity_score_ticks, Decimal("3"))
        self.assertEqual(decision.ask.extra_spread_ticks, 2)
        self.assertEqual(decision.ask.directional_confirmations, 3)

    def test_downward_pressure_only_adds_directional_bid_toxicity(self) -> None:
        decision = self.evaluate(
            self.controller(),
            _Features(
                return_5s_ticks=Decimal("-3"),
                return_15s_ticks=Decimal("-9"),
                microprice_shift_ticks=Decimal("-2"),
            ),
        )

        self.assertEqual(decision.bid.toxicity_score_ticks, Decimal("3"))
        self.assertEqual(decision.bid.extra_spread_ticks, 2)
        self.assertEqual(decision.ask.toxicity_score_ticks, Decimal("0"))
        self.assertEqual(decision.ask.extra_spread_ticks, 0)

    def test_rms_sqrt_five_buffer_widens_both_sides_and_clamps(self) -> None:
        decision = self.evaluate(
            self.controller(max_extra_spread_ticks=3),
            _Features(rms_1s_move_15s_ticks=Decimal("2")),
        )

        self.assertGreater(decision.bid.toxicity_score_ticks, Decimal("4.47"))
        self.assertEqual(decision.bid.toxicity_score_ticks, decision.ask.toxicity_score_ticks)
        self.assertEqual(decision.bid.extra_spread_ticks, 3)
        self.assertEqual(decision.ask.extra_spread_ticks, 3)

    def test_warming_and_invalid_features_are_not_ready(self) -> None:
        controller = self.controller()

        warming = self.evaluate(controller, _Features(health="warming"))
        invalid = self.evaluate(
            controller,
            _Features(return_5s_ticks=Decimal("NaN")),
        )

        self.assertFalse(warming.ready)
        self.assertEqual(warming.reason, "features_warming")
        self.assertFalse(invalid.ready)
        self.assertEqual(invalid.reason, "features_invalid")

    def test_side_block_requires_confirmations_and_resume_hysteresis(self) -> None:
        controller = self.controller()
        adverse = _Features(
            return_5s_ticks=Decimal("6"),
            return_15s_ticks=Decimal("18"),
            microprice_shift_ticks=Decimal("6"),
        )
        calm = _Features()

        first = self.evaluate(controller, adverse, now=100.0)
        blocked = self.evaluate(controller, adverse, now=101.0)
        held = self.evaluate(controller, calm, now=103.0)
        first_resume = self.evaluate(controller, calm, now=106.0)
        resumed = self.evaluate(controller, calm, now=107.0)

        self.assertFalse(first.ask.blocked)
        self.assertTrue(blocked.ask.blocked)
        self.assertTrue(held.ask.blocked)
        self.assertTrue(first_resume.ask.blocked)
        self.assertFalse(resumed.ask.blocked)
        self.assertEqual(resumed.ask.reason, "toxicity_resumed")
        self.assertFalse(blocked.bid.blocked)

    def test_zero_block_threshold_disables_blocking(self) -> None:
        controller = self.controller(
            block_threshold_ticks=Decimal("0"),
            resume_threshold_ticks=Decimal("0"),
        )
        adverse = _Features(
            return_5s_ticks=Decimal("20"),
            return_15s_ticks=Decimal("60"),
            microprice_shift_ticks=Decimal("20"),
        )

        self.evaluate(controller, adverse)
        decision = self.evaluate(controller, adverse, now=101.0)

        self.assertFalse(decision.ask.blocked)

    def test_invalid_block_threshold_order_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "resume < widen_start < block"):
            self.controller(resume_threshold_ticks=Decimal("1"))

        with self.assertRaisesRegex(ValueError, "three directional signals"):
            self.controller(block_confirmations=4)

        with self.assertRaisesRegex(ValueError, "cannot use markout feedback"):
            self.controller(mode="active", use_markout_feedback=True)

    def test_markout_feedback_is_opt_in_and_requires_enough_samples(self) -> None:
        feedback = {
            "buy": {"5s": {"count": 8, "ewma_bps": Decimal("-20")}},
            "sell": {"5s": {"count": 8, "ewma_bps": Decimal("2")}},
        }
        disabled = self.evaluate(self.controller(), feedback=feedback)
        insufficient = self.evaluate(
            self.controller(
                use_markout_feedback=True,
                markout_min_samples=9,
            ),
            feedback=feedback,
        )
        enabled = self.evaluate(
            self.controller(
                use_markout_feedback=True,
                markout_min_samples=8,
            ),
            feedback=feedback,
        )

        self.assertEqual(disabled.bid.toxicity_score_ticks, Decimal("0"))
        self.assertEqual(insufficient.bid.toxicity_score_ticks, Decimal("0"))
        self.assertEqual(enabled.bid.toxicity_score_ticks, Decimal("2.0"))
        self.assertEqual(enabled.bid.extra_spread_ticks, 1)
        self.assertEqual(enabled.ask.toxicity_score_ticks, Decimal("0"))


if __name__ == "__main__":
    unittest.main()

import unittest
from decimal import Decimal

from core.services.market_maker.controllers import (
    QuoteControllerDecision,
    SideQuoteAdjustment,
)
from core.services.market_maker.metrics import MarketMakerMetrics
from core.services.market_maker.models import RuntimeState


class MarketMakerControllerTelemetryTests(unittest.TestCase):
    @staticmethod
    def decision(
        decision_id: int,
        *,
        ready: bool = True,
        bid_extra: int = 0,
        bid_blocked: bool = False,
        ask_blocked: bool = False,
    ) -> QuoteControllerDecision:
        return QuoteControllerDecision(
            mode="shadow",
            controller="toxicity_v1",
            ready=ready,
            reason="ready" if ready else "features_warming",
            decision_id=decision_id,
            bid=SideQuoteAdjustment(
                extra_spread_ticks=bid_extra,
                blocked=bid_blocked,
            ),
            ask=SideQuoteAdjustment(blocked=ask_blocked),
            features=None,
        )

    @staticmethod
    def record(
        metrics: MarketMakerMetrics,
        decision: QuoteControllerDecision,
        now: float,
        *,
        entry_applicable: bool = True,
        shadow_bid: Decimal = Decimal("99.8"),
    ) -> None:
        if metrics.runtime_state is not RuntimeState.ACTIVE:
            metrics.transition(RuntimeState.ACTIVE)
        metrics.record_controller_decision(
            decision,
            now=now,
            base_bid=Decimal("99.9"),
            base_ask=Decimal("100.1"),
            shadow_bid=shadow_bid,
            shadow_ask=Decimal("100.1"),
            applied_bid=Decimal("99.9"),
            applied_ask=Decimal("100.1"),
            feature_snapshot={"health": "ready"},
            entry_applicable=entry_applicable,
        )

    def test_decision_history_is_transition_only_and_bounded(self) -> None:
        metrics = MarketMakerMetrics(0.0)
        self.record(metrics, self.decision(1), 0.0)
        self.record(metrics, self.decision(2), 1.0)
        self.assertEqual(len(metrics.controller_decision_history), 1)

        for index in range(250):
            self.record(
                metrics,
                self.decision(index + 3, bid_extra=index % 2),
                float(index + 2),
            )

        self.assertEqual(len(metrics.controller_decision_history), 200)
        self.assertEqual(
            metrics.controller_decision_history[-1]["decision_id"], 252
        )
        self.assertEqual(metrics.controller_decision_history_total, 250)

    def test_history_records_shadow_reprice_and_applicability_change(self) -> None:
        metrics = MarketMakerMetrics(0.0)
        self.record(metrics, self.decision(1), 0.0)
        self.record(
            metrics,
            self.decision(2),
            1.0,
            shadow_bid=Decimal("99.7"),
        )
        self.record(
            metrics,
            self.decision(3),
            2.0,
            shadow_bid=Decimal("99.7"),
            entry_applicable=False,
        )

        history = metrics.controller_decision_history
        self.assertEqual(len(history), 3)
        self.assertEqual(
            metrics.snapshot(2.0)["controller_decision_history_total"], 3
        )
        self.assertEqual(history[1]["bid"]["shadow_price"], Decimal("99.7"))
        self.assertFalse(history[2]["entry_applicable"])

    def test_ready_warming_and_blocked_seconds_accrue(self) -> None:
        metrics = MarketMakerMetrics(0.0)
        self.record(metrics, self.decision(1), 0.0)
        self.record(
            metrics,
            self.decision(2, bid_blocked=True),
            10.0,
        )
        self.record(
            metrics,
            self.decision(3, ready=False),
            20.0,
        )
        snapshot = metrics.snapshot(25.0)

        self.assertEqual(snapshot["controller_ready_seconds"], 20.0)
        self.assertEqual(snapshot["controller_warming_seconds"], 5.0)
        self.assertEqual(snapshot["controller_bid_blocked_seconds"], 10.0)
        self.assertEqual(snapshot["controller_ask_blocked_seconds"], 0.0)

    def test_quote_context_registry_is_bounded_and_annotates_markout(self) -> None:
        metrics = MarketMakerMetrics(0.0)
        for index in range(501):
            metrics.record_quote_context(
                f"order-{index}",
                {
                    "order_id": f"order-{index}",
                    "base_price": Decimal("100"),
                    "shadow_price": Decimal("99.9"),
                },
            )

        contexts = metrics.snapshot(0.0)["quote_contexts"]
        self.assertEqual(len(contexts), 500)
        self.assertEqual(contexts[0]["order_id"], "order-1")

        recorded = metrics.record_maker_fill_markout(
            order_id="order-500",
            side="buy",
            cumulative_filled=Decimal("0.2"),
            cumulative_cost=Decimal("20"),
            average_price=Decimal("100"),
            now=1.0,
            mid=Decimal("100"),
            source="websocket_order_update",
            terminal=True,
        )
        self.assertTrue(recorded)
        self.assertEqual(
            metrics.fill_markouts[-1]["quote_context"]["order_id"],
            "order-500",
        )

    def test_fill_history_uses_placement_context_not_latest_decision(self) -> None:
        metrics = MarketMakerMetrics(0.0)
        metrics.record_quote_context(
            "placed-order",
            {
                "order_id": "placed-order",
                "decision_id": 7,
                "controller_mode": "shadow",
                "base_price": Decimal("100"),
                "shadow_price": Decimal("99.9"),
                "feature_snapshot": {"return_5s_ticks": Decimal("-2")},
            },
        )
        self.record(metrics, self.decision(99, bid_extra=3), 10.0)

        metrics.record_controller_fill_snapshot("placed-order", 20.0)
        fill = metrics.controller_decision_history[-1]
        self.assertEqual(fill["event"], "maker_fill")
        self.assertEqual(fill["decision_id"], 99)
        self.assertEqual(
            fill["placement_quote_context"]["decision_id"], 7
        )
        self.assertEqual(
            fill["placement_quote_context"]["shadow_price"], Decimal("99.9")
        )
        metrics.record_controller_fill_snapshot("missing-context", 20.0)
        self.assertIsNone(
            metrics.controller_decision_history[-1]["placement_quote_context"]
        )
        self.assertEqual(len(metrics.controller_decision_history), 3)
        self.assertEqual(metrics.controller_decision_history_total, 3)

    def test_event_sequence_orders_decision_placement_fill_and_snapshot(
        self,
    ) -> None:
        metrics = MarketMakerMetrics(0.0)
        self.record(metrics, self.decision(1), 10.0)
        decision_sequence = metrics.quote_controller["event_sequence"]
        run_id = metrics.quote_controller["event_sequence_run_id"]
        metrics.record_quote_context(
            "same-time-order",
            {
                "order_id": "same-time-order",
                "base_price": Decimal("100"),
                "shadow_price": Decimal("99.9"),
                "created_monotonic": 10.0,
            },
        )
        placement_sequence = metrics.snapshot(10.0)["quote_contexts"][0][
            "placement_event_sequence"
        ]
        placement_context = metrics.snapshot(10.0)["quote_contexts"][0]
        self.assertTrue(
            metrics.record_maker_fill_markout(
                order_id="same-time-order",
                side="buy",
                cumulative_filled=Decimal("0.2"),
                cumulative_cost=Decimal("20"),
                average_price=Decimal("100"),
                now=10.0,
                mid=Decimal("100"),
                source="websocket_order_update",
                terminal=True,
            )
        )
        fill_sequence = metrics.fill_markouts[-1][
            "fill_observation_event_sequence"
        ]
        fill_event = metrics.fill_markouts[-1]
        metrics.record_controller_fill_snapshot("same-time-order", 10.0)
        fill_snapshot = metrics.controller_decision_history[-1]
        self.record(
            metrics,
            self.decision(2),
            10.0,
            entry_applicable=False,
        )
        post_fill_decision_sequence = metrics.quote_controller[
            "event_sequence"
        ]

        self.assertLess(decision_sequence, placement_sequence)
        self.assertLess(placement_sequence, fill_sequence)
        self.assertLess(fill_sequence, fill_snapshot["event_sequence"])
        self.assertLess(
            fill_snapshot["event_sequence"], post_fill_decision_sequence
        )
        self.assertEqual(placement_context["event_sequence_run_id"], run_id)
        self.assertEqual(fill_event["event_sequence_run_id"], run_id)
        self.assertEqual(fill_snapshot["event_sequence_run_id"], run_id)
        self.assertEqual(metrics.snapshot(10.0)["event_sequence_run_id"], run_id)
        self.assertNotEqual(
            MarketMakerMetrics(0.0).snapshot(0.0)["event_sequence_run_id"],
            run_id,
        )
        self.assertEqual(
            fill_snapshot["last_controller_decision_event_sequence"],
            decision_sequence,
        )
        self.assertEqual(
            fill_snapshot["fill_observation_event_sequence"], fill_sequence
        )

    def test_seconds_exclude_inapplicable_and_stopped_intervals(self) -> None:
        metrics = MarketMakerMetrics(0.0)
        self.record(metrics, self.decision(1, bid_blocked=True), 0.0)
        self.record(
            metrics,
            self.decision(2, bid_blocked=True),
            5.0,
            entry_applicable=False,
        )
        self.record(
            metrics,
            self.decision(3, bid_blocked=True),
            10.0,
            entry_applicable=True,
        )
        metrics.transition(RuntimeState.STOPPED)
        snapshot = metrics.snapshot(20.0)

        self.assertEqual(snapshot["controller_ready_seconds"], 5.0)
        self.assertEqual(snapshot["controller_bid_blocked_seconds"], 5.0)

    def test_authenticated_feedback_exposes_decimal_recency_weighted_mean(
        self,
    ) -> None:
        metrics = MarketMakerMetrics(0.0)
        template = {
            "side": "buy",
            "attribution_state": "authenticated",
            "fill_role": "entry",
            "active_unwind": False,
            "observation_source": "websocket_order_update",
            "external_mid_markout_15s_bps": Decimal("0"),
        }
        metrics.fill_markouts = [
            {
                **template,
                "started_monotonic": 0.0,
                "external_mid_markout_5s_bps": Decimal("-10"),
            },
            {
                **template,
                "started_monotonic": 90.0,
                "external_mid_markout_5s_bps": Decimal("-2"),
            },
        ]

        feedback = metrics.authenticated_entry_markout_feedback(
            now_monotonic=100.0,
            half_life_seconds=10,
        )

        stats = feedback["buy"]["5s"]
        self.assertEqual(stats["count"], 2)
        self.assertIsInstance(stats["ewma_bps"], Decimal)
        self.assertGreater(stats["ewma_bps"], stats["mean_bps"])
        self.assertLess(stats["ewma_bps"], Decimal("-2"))


if __name__ == "__main__":
    unittest.main()

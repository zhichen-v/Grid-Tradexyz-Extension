from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace

from core.adapters.exchanges.models import OrderSide
from core.services.market_maker.account_monitor import (
    AccountAuditError,
    SessionEconomics,
)
from core.services.market_maker.config import MarketMakerConfig
from core.services.market_maker.metrics import MarketMakerMetrics


def trade(
    trade_id: str,
    order_id: str,
    side: OrderSide,
    amount: str,
    *,
    role: str = "maker",
    fee_rate: str = "0.000120",
    timestamp: int = 1,
):
    turnover = Decimal(amount) * Decimal("100")
    rate = Decimal(fee_rate)
    return SimpleNamespace(
        id=trade_id,
        order_id=order_id,
        side=side,
        amount=Decimal(amount),
        cost=turnover,
        fee={"role": role, "rate": rate, "cost": turnover * rate},
        raw_data={
            "realized_pnl": Decimal("0"),
            "timestamp": timestamp,
            "trade_sequence": int(trade_id),
            "trade_type": "trade",
            "integrator_fee_tick": 0,
        },
    )


def attribution(
    trade_id: str,
    order_id: str,
    side: str,
    role: str,
    *,
    episode_sequence: int = 1,
    active_unwind: bool = False,
    position_flip: bool = False,
    timestamp: int = 1,
) -> dict:
    return {
        "trade_id": trade_id,
        "order_id": order_id,
        "side": side,
        "role": role,
        "episode_sequence": episode_sequence,
        "prior_position": Decimal("0"),
        "next_position": Decimal("0.1"),
        "exchange_timestamp": timestamp,
        "active_unwind": active_unwind,
        "position_flip": position_flip,
    }


class AuthenticatedFillRoleTests(unittest.TestCase):
    def economics(self) -> SessionEconomics:
        return SessionEconomics(
            MarketMakerConfig(
                maker_fee_rate="0.000120",
                taker_fee_rate="0.0004",
                economic_min_fills=10_000,
            ),
            baseline_equity=Decimal("100"),
        )

    def apply(
        self,
        economics: SessionEconomics,
        fills: list,
        position: str,
        *,
        active_order_ids: set[str] | None = None,
    ) -> None:
        active_order_ids = active_order_ids or set()
        economics.apply(
            fills,
            current_position=Decimal(position),
            current_equity=Decimal("100"),
            managed_order_ids={fill.order_id for fill in fills}
            - active_order_ids,
            active_unwind_order_ids=active_order_ids,
        )

    def test_roles_use_authenticated_positions_and_partial_order_role_is_stable(
        self,
    ) -> None:
        economics = self.economics()
        entry_partials = [
            trade("1", "entry", OrderSide.BUY, "0.1", timestamp=1),
            trade("2", "entry", OrderSide.BUY, "0.1", timestamp=2),
        ]
        self.apply(economics, entry_partials, "0.2")
        self.apply(economics, entry_partials, "0.2")
        self.apply(
            economics,
            [trade("3", "increase", OrderSide.BUY, "0.1", timestamp=3)],
            "0.3",
        )
        self.apply(
            economics,
            [trade("4", "passive", OrderSide.SELL, "0.1", timestamp=4)],
            "0.2",
        )
        self.apply(
            economics,
            [
                trade(
                    "5",
                    "active",
                    OrderSide.SELL,
                    "0.2",
                    role="taker",
                    fee_rate="0.0004",
                    timestamp=5,
                )
            ],
            "0",
            active_order_ids={"active"},
        )

        ledger = economics.snapshot()["authenticated_fill_attributions"]
        self.assertEqual(
            [item["role"] for item in ledger],
            [
                "entry",
                "entry",
                "risk_increasing",
                "passive_exit",
                "active_exit",
            ],
        )
        self.assertEqual({item["episode_sequence"] for item in ledger}, {1})
        self.assertEqual(len(ledger), 5)
        self.assertTrue(ledger[-1]["active_unwind"])

    def test_maker_position_flip_is_explicitly_marked(self) -> None:
        economics = self.economics()
        self.apply(
            economics,
            [trade("1", "entry", OrderSide.BUY, "0.1")],
            "0.1",
        )
        unsafe_batch = [
            trade("2", "increase", OrderSide.BUY, "0.1", timestamp=2),
            trade("3", "overshoot", OrderSide.SELL, "0.3", timestamp=3),
        ]

        self.apply(economics, unsafe_batch, "-0.1")

        self.assertEqual(economics.ledger_position, Decimal("-0.1"))
        self.assertEqual(economics.seen_trade_ids, {"1", "2", "3"})
        ledger = economics.authenticated_fill_attributions
        self.assertFalse(ledger[-2]["position_flip"])
        self.assertTrue(ledger[-1]["position_flip"])
        self.assertEqual(ledger[-1]["role"], "risk_increasing")

    def test_short_entry_increase_and_passive_exit_roles(self) -> None:
        economics = self.economics()
        self.apply(
            economics,
            [trade("1", "short-entry", OrderSide.SELL, "0.1", timestamp=1)],
            "-0.1",
        )
        self.apply(
            economics,
            [trade("2", "short-add", OrderSide.SELL, "0.1", timestamp=2)],
            "-0.2",
        )
        self.apply(
            economics,
            [trade("3", "short-exit", OrderSide.BUY, "0.1", timestamp=3)],
            "-0.1",
        )

        self.assertEqual(
            [
                item["role"]
                for item in economics.authenticated_fill_attributions
            ],
            ["entry", "risk_increasing", "passive_exit"],
        )

    def test_authenticated_fill_ledger_is_bounded_and_replay_safe(self) -> None:
        economics = self.economics()
        position = Decimal("0")
        for index in range(501):
            fill = trade(
                str(index + 1),
                f"order-{index + 1}",
                OrderSide.BUY,
                "0.01",
                timestamp=index + 1,
            )
            position += Decimal("0.01")
            self.apply(economics, [fill], str(position))

        self.assertEqual(len(economics.authenticated_fill_attributions), 500)
        self.assertEqual(
            economics.authenticated_fill_attributions[0]["trade_id"], "2"
        )
        self.apply(economics, [fill], str(position))
        self.assertEqual(len(economics.authenticated_fill_attributions), 500)

    def test_trade_identity_registry_evicts_beyond_current_page(self) -> None:
        economics = self.economics()
        economics.seen_trade_ids = {str(index) for index in range(8191)}
        economics._last_applied_fill_sort_key = (8190, 8190)
        final_slot = trade(
            "9000", "entry", OrderSide.BUY, "0.1", timestamp=9000
        )
        self.apply(economics, [final_slot], "0.1")
        self.apply(economics, [final_slot], "0.1")
        next_fill = trade(
            "9001", "increase", OrderSide.BUY, "0.1", timestamp=9001
        )
        self.apply(economics, [next_fill], "0.2")

        evicted_ids = (
            {str(index) for index in range(8191)} | {"9000", "9001"}
        ) - economics.seen_trade_ids
        self.assertEqual(len(evicted_ids), 1)
        self.assertEqual(len(economics.seen_trade_ids), 8192)
        self.assertIn("9000", economics.seen_trade_ids)
        self.assertIn("9001", economics.seen_trade_ids)
        self.assertEqual(economics.seen_trade_id_evictions, 1)
        self.assertEqual(economics.ledger_position, Decimal("0.2"))
        self.apply(economics, [next_fill], "0.2")
        self.assertEqual(economics.unique_maker_fills, 2)

        evicted_id = evicted_ids.pop()
        before = (
            set(economics.seen_trade_ids),
            dict(economics._seen_trade_id_order),
            dict(economics._order_role_bindings),
            economics.ledger_position,
            economics.unique_maker_fills,
            economics.seen_trade_id_evictions,
            economics.order_role_binding_evictions,
        )
        with self.assertRaisesRegex(AccountAuditError, "ledger order"):
            self.apply(
                economics,
                [
                    trade(
                        evicted_id,
                        f"old-{evicted_id}",
                        OrderSide.BUY,
                        "0.1",
                        timestamp=int(evicted_id),
                    )
                ],
                "0.3",
            )
        self.assertEqual(
            (
                set(economics.seen_trade_ids),
                dict(economics._seen_trade_id_order),
                dict(economics._order_role_bindings),
                economics.ledger_position,
                economics.unique_maker_fills,
                economics.seen_trade_id_evictions,
                economics.order_role_binding_evictions,
            ),
            before,
        )

    def test_role_registry_evicts_terminal_but_pins_open_episode(self) -> None:
        economics = self.economics()
        economics._order_role_bindings = {
            f"old-{index}": ("buy", "entry", index + 1, False)
            for index in range(8191)
        }
        economics._order_role_bindings["open-episode"] = (
            "buy",
            "entry",
            8192,
            False,
        )
        economics._episode_order_ids = {"open-episode"}
        economics._episode_sequence = 8192
        economics.ledger_position = Decimal("0.1")
        terminal_ids = {
            f"old-{index}" for index in range(8191) if index != 2
        }

        economics.apply(
            [trade("9000", "increase", OrderSide.BUY, "0.1", timestamp=9000)],
            current_position=Decimal("0.2"),
            current_equity=Decimal("100"),
            managed_order_ids={"increase"},
            open_order_ids={"old-0"},
            terminal_order_ids=terminal_ids,
        )

        self.assertEqual(len(economics._order_role_bindings), 8192)
        self.assertIn("open-episode", economics._order_role_bindings)
        self.assertIn("old-0", economics._order_role_bindings)
        self.assertIn("old-2", economics._order_role_bindings)
        self.assertIn("increase", economics._order_role_bindings)
        self.assertNotIn("old-1", economics._order_role_bindings)
        self.assertEqual(economics.order_role_binding_evictions, 1)

        economics._episode_order_ids = set(economics._order_role_bindings)
        economics.seen_trade_ids.update(str(index) for index in range(8191))
        before = (
            set(economics.seen_trade_ids),
            dict(economics._seen_trade_id_order),
            dict(economics._order_role_bindings),
            economics.ledger_position,
            economics.seen_trade_id_evictions,
            economics.order_role_binding_evictions,
        )
        with self.assertRaisesRegex(AccountAuditError, "registry exhausted"):
            economics.apply(
                [
                    trade(
                        "9001",
                        "all-pinned",
                        OrderSide.BUY,
                        "0.1",
                        timestamp=9001,
                    )
                ],
                current_position=Decimal("0.3"),
                current_equity=Decimal("100"),
                managed_order_ids={"all-pinned"},
                terminal_order_ids=set(economics._order_role_bindings),
            )
        self.assertEqual(
            (
                set(economics.seen_trade_ids),
                dict(economics._seen_trade_id_order),
                dict(economics._order_role_bindings),
                economics.ledger_position,
                economics.seen_trade_id_evictions,
                economics.order_role_binding_evictions,
            ),
            before,
        )

    def test_baseline_seed_validation_is_atomic(self) -> None:
        economics = self.economics()
        economics.seen_trade_ids = {str(index) for index in range(8191)}
        economics._last_applied_fill_sort_key = (100, 100)
        before = (
            set(economics.seen_trade_ids),
            economics._last_applied_fill_sort_key,
        )

        with self.assertRaisesRegex(AccountAuditError, "registry exhausted"):
            economics.seed(
                [
                    trade("9000", "first", OrderSide.BUY, "0.1", timestamp=9000),
                    trade("9001", "second", OrderSide.BUY, "0.1", timestamp=9001),
                ]
            )
        self.assertEqual(
            (set(economics.seen_trade_ids), economics._last_applied_fill_sort_key),
            before,
        )

        duplicate = trade(
            "9000", "duplicate", OrderSide.BUY, "0.1", timestamp=9000
        )
        with self.assertRaisesRegex(AccountAuditError, "duplicate ids"):
            economics.seed([duplicate, duplicate])
        self.assertEqual(
            (set(economics.seen_trade_ids), economics._last_applied_fill_sort_key),
            before,
        )

    def test_partial_role_binding_survives_fill_ledger_eviction(self) -> None:
        economics = self.economics()
        position = Decimal("0.1")
        self.apply(
            economics,
            [trade("1", "entry", OrderSide.BUY, "0.1", timestamp=1)],
            str(position),
        )
        for index in range(2, 502):
            position += Decimal("0.01")
            self.apply(
                economics,
                [
                    trade(
                        str(index),
                        f"order-{index}",
                        OrderSide.BUY,
                        "0.01",
                        timestamp=index,
                    )
                ],
                str(position),
            )
        self.assertNotIn(
            "1",
            {
                item["trade_id"]
                for item in economics.authenticated_fill_attributions
            },
        )

        position += Decimal("0.1")
        self.apply(
            economics,
            [trade("502", "entry", OrderSide.BUY, "0.1", timestamp=502)],
            str(position),
        )

        self.assertEqual(
            economics.authenticated_fill_attributions[-1]["role"], "entry"
        )

    def test_cross_audit_fill_order_watermark_fails_closed_atomically(self) -> None:
        for first_timestamp, late_timestamp in ((2, 1), (5, 5)):
            with self.subTest(
                first_timestamp=first_timestamp,
                late_timestamp=late_timestamp,
            ):
                economics = self.economics()
                self.apply(
                    economics,
                    [
                        trade(
                            "2",
                            "first",
                            OrderSide.BUY,
                            "0.1",
                            timestamp=first_timestamp,
                        )
                    ],
                    "0.1",
                )
                before = (
                    economics.ledger_position,
                    set(economics.seen_trade_ids),
                    list(economics.authenticated_fill_attributions),
                    economics._last_applied_fill_sort_key,
                )

                with self.assertRaisesRegex(AccountAuditError, "ledger order"):
                    self.apply(
                        economics,
                        [
                            trade(
                                "1",
                                "late",
                                OrderSide.BUY,
                                "0.1",
                                timestamp=late_timestamp,
                            )
                        ],
                        "0.2",
                    )

                self.assertEqual(
                    (
                        economics.ledger_position,
                        set(economics.seen_trade_ids),
                        list(economics.authenticated_fill_attributions),
                        economics._last_applied_fill_sort_key,
                    ),
                    before,
                )


class MarkoutAttributionTests(unittest.TestCase):
    def metrics(self) -> MarketMakerMetrics:
        return MarketMakerMetrics(started_monotonic=0.0)

    def record(
        self,
        metrics: MarketMakerMetrics,
        order_id: str,
        side: str,
        *,
        source: str = "websocket_order_update",
        now: float = 0.0,
    ) -> None:
        self.assertTrue(
            metrics.record_maker_fill_markout(
                order_id=order_id,
                side=side,
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=now,
                mid=Decimal("100"),
                external_mid=Decimal("100"),
                source=source,
                terminal=True,
            )
        )

    def test_late_attribution_backfills_partials_without_replay_duplication(self):
        metrics = self.metrics()
        self.assertTrue(
            metrics.record_maker_fill_markout(
                order_id="entry",
                side="buy",
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=0.0,
                mid=Decimal("100"),
                source="websocket_order_update",
                terminal=False,
            )
        )
        self.assertTrue(
            metrics.record_maker_fill_markout(
                order_id="entry",
                side="buy",
                cumulative_filled=Decimal("0.2"),
                cumulative_cost=Decimal("20.2"),
                average_price=None,
                now=1.0,
                mid=Decimal("100"),
                source="reconciliation",
                terminal=True,
            )
        )
        self.assertFalse(
            metrics.record_maker_fill_markout(
                order_id="entry",
                side="buy",
                cumulative_filled=Decimal("0.2"),
                cumulative_cost=Decimal("20.2"),
                average_price=None,
                now=2.0,
                mid=Decimal("100"),
                source="reconciliation",
                terminal=True,
            )
        )
        self.assertTrue(
            all(
                event["attribution_state"] == "pending"
                for event in metrics.fill_markouts
            )
        )

        attributions = [
            attribution("1", "entry", "buy", "entry", timestamp=1),
            attribution("2", "entry", "buy", "entry", timestamp=2),
        ]
        metrics.apply_authenticated_fill_attributions(attributions)
        metrics.apply_authenticated_fill_attributions(attributions)
        self.record(metrics, "unknown", "buy", now=2.0)
        metrics.update_fill_markouts(
            now=20.0,
            mid=Decimal("99"),
            external_mid=Decimal("99"),
        )

        self.assertEqual(len(metrics.fill_markouts), 3)
        self.assertEqual(len(metrics._authenticated_fill_attributions), 2)
        self.assertTrue(
            all(
                event["attribution_state"] == "authenticated"
                for event in metrics.fill_markouts[:2]
            )
        )
        summary = metrics.snapshot(20.0)["authenticated_markout_summary"]
        self.assertEqual(summary["buy"]["entry"]["5s"]["count"], 2)
        self.assertEqual(summary["buy"]["pending_count"], 1)
        self.assertIsNotNone(summary["buy"]["entry"]["5s"]["median_bps"])

    def test_nonunique_order_attribution_remains_pending(self) -> None:
        metrics = self.metrics()
        self.record(metrics, "ambiguous", "buy")
        metrics.apply_authenticated_fill_attributions(
            [attribution("1", "ambiguous", "buy", "entry")]
        )
        self.assertEqual(
            metrics.fill_markouts[0]["attribution_state"], "authenticated"
        )

        metrics.apply_authenticated_fill_attributions(
            [attribution("2", "ambiguous", "buy", "risk_increasing")]
        )

        self.assertEqual(metrics.fill_markouts[0]["attribution_state"], "pending")
        self.assertIsNone(metrics.fill_markouts[0]["fill_role"])

    def test_attribution_conflict_stays_pending_after_registry_eviction(self) -> None:
        metrics = self.metrics()
        self.record(metrics, "ambiguous", "buy")
        metrics.apply_authenticated_fill_attributions(
            [
                attribution("1", "ambiguous", "buy", "entry"),
                attribution("2", "ambiguous", "buy", "risk_increasing"),
            ]
        )
        metrics.apply_authenticated_fill_attributions(
            [
                attribution(
                    str(index),
                    f"order-{index}",
                    "buy",
                    "entry",
                    episode_sequence=index,
                    timestamp=index,
                )
                for index in range(3, 503)
            ]
        )

        event = metrics.fill_markouts[0]
        self.assertEqual(event["attribution_state"], "pending")
        self.assertTrue(event["attribution_conflict"])

    def test_authenticated_event_keeps_copied_proof_after_registry_eviction(self):
        metrics = self.metrics()
        self.record(metrics, "entry", "buy")
        metrics.apply_authenticated_fill_attributions(
            [attribution("1", "entry", "buy", "entry")]
        )
        metrics.apply_authenticated_fill_attributions(
            [
                attribution(
                    str(index),
                    f"order-{index}",
                    "buy",
                    "entry",
                    episode_sequence=index,
                    timestamp=index,
                )
                for index in range(2, 502)
            ]
        )

        event = metrics.fill_markouts[0]
        self.assertEqual(event["attribution_state"], "authenticated")
        self.assertEqual(event["fill_role"], "entry")
        self.assertEqual(event["attribution_signature"]["role"], "entry")

    def test_conflicting_duplicate_trade_id_is_sticky_pending(self) -> None:
        metrics = self.metrics()
        self.record(metrics, "entry", "buy")
        metrics.apply_authenticated_fill_attributions(
            [attribution("1", "entry", "buy", "entry")]
        )
        metrics.apply_authenticated_fill_attributions(
            [attribution("1", "entry", "buy", "risk_increasing")]
        )

        self.assertEqual(
            metrics.fill_markouts[0]["attribution_state"], "pending"
        )
        self.assertTrue(metrics.fill_markouts[0]["attribution_conflict"])

    def test_position_flip_attribution_never_authenticates_markout(self) -> None:
        metrics = self.metrics()
        self.record(metrics, "flip", "sell")
        metrics.apply_authenticated_fill_attributions(
            [
                attribution(
                    "1",
                    "flip",
                    "sell",
                    "risk_increasing",
                    position_flip=True,
                )
            ]
        )

        self.assertEqual(
            metrics.fill_markouts[0]["attribution_state"], "pending"
        )
        self.assertTrue(metrics.fill_markouts[0]["attribution_conflict"])

    def test_fill_markout_events_are_bounded(self) -> None:
        metrics = self.metrics()
        for index in range(101):
            self.record(metrics, f"order-{index}", "buy", now=float(index))

        self.assertEqual(len(metrics.fill_markouts), 100)
        self.assertEqual(metrics.fill_markouts[0]["order_id"], "order-1")

    def test_open_fill_progress_refuses_new_identity_at_capacity(self) -> None:
        metrics = self.metrics()
        for index in range(500):
            self.assertTrue(
                metrics.record_maker_fill_markout(
                    order_id=f"open-{index}",
                    side="buy",
                    cumulative_filled=Decimal("0.1"),
                    cumulative_cost=Decimal("10"),
                    average_price=None,
                    now=float(index),
                    mid=Decimal("100"),
                    source="websocket_order_update",
                    terminal=False,
                )
            )

        self.assertFalse(
            metrics.record_maker_fill_markout(
                order_id="overflow",
                side="buy",
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=501.0,
                mid=Decimal("100"),
                source="websocket_order_update",
                terminal=False,
            )
        )
        self.assertEqual(len(metrics._maker_fill_progress), 500)
        self.assertEqual(len(metrics._maker_fill_open_ids), 500)
        self.assertNotIn("overflow", metrics._maker_fill_progress)

    def test_entry_feedback_excludes_rest_and_active_exit(self) -> None:
        metrics = self.metrics()
        cases = (
            ("ws-entry", "buy", "websocket_order_update", "entry", False),
            ("rest-entry", "buy", "rest_open_order_sync", "entry", False),
            ("active-exit", "sell", "reconciliation", "active_exit", True),
            ("recon-entry", "sell", "reconciliation", "entry", False),
        )
        attributions = []
        for index, (order_id, side, source, role, active) in enumerate(cases, 1):
            self.record(metrics, order_id, side, source=source)
            attributions.append(
                attribution(
                    str(index),
                    order_id,
                    side,
                    role,
                    active_unwind=active,
                    timestamp=index,
                )
            )
        metrics.apply_authenticated_fill_attributions(attributions)
        metrics.update_fill_markouts(
            now=15.0,
            mid=Decimal("99"),
            external_mid=Decimal("99"),
        )

        snapshot = metrics.snapshot(15.0)
        feedback = snapshot["authenticated_entry_markout_feedback"]
        self.assertEqual(feedback["buy"]["5s"]["count"], 1)
        self.assertEqual(feedback["sell"]["5s"]["count"], 1)
        self.assertEqual(
            snapshot["authenticated_markout_summary"]["sell"]["active_exit"]
            ["5s"]["count"],
            1,
        )

    def test_feedback_and_summaries_use_external_mid_markout_only(self) -> None:
        metrics = self.metrics()
        self.assertTrue(
            metrics.record_maker_fill_markout(
                order_id="external-entry",
                side="buy",
                cumulative_filled=Decimal("0.1"),
                cumulative_cost=Decimal("10"),
                average_price=None,
                now=0.0,
                mid=Decimal("100"),
                external_mid=Decimal("100.1"),
                source="websocket_order_update",
                terminal=True,
            )
        )
        metrics.apply_authenticated_fill_attributions(
            [attribution("1", "external-entry", "buy", "entry")]
        )
        metrics.update_fill_markouts(
            now=5.0,
            mid=Decimal("99"),
            external_mid=Decimal("101"),
        )

        event = metrics.fill_markouts[0]
        self.assertEqual(event["raw_mid_at_start"], Decimal("100"))
        self.assertEqual(event["external_mid_at_start"], Decimal("100.1"))
        self.assertEqual(event["raw_mid_5s"], Decimal("99"))
        self.assertEqual(event["external_mid_5s"], Decimal("101"))
        self.assertEqual(event["markout_5s_bps"], Decimal("-100"))
        self.assertEqual(
            event["raw_mid_markout_5s_bps"], Decimal("-100")
        )
        self.assertEqual(
            event["external_mid_markout_5s_bps"], Decimal("100")
        )
        snapshot = metrics.snapshot(5.0)
        self.assertEqual(
            snapshot["side_markout_summary"]["buy"]["5s"]["mean_bps"],
            Decimal("100"),
        )
        self.assertEqual(
            snapshot["authenticated_markout_summary"]["buy"]["entry"]
            ["5s"]["mean_bps"],
            Decimal("100"),
        )
        self.assertEqual(
            snapshot["authenticated_entry_markout_feedback"]["buy"]["5s"]
            ["mean_bps"],
            Decimal("100"),
        )

    def test_untrusted_external_observation_is_not_backfilled(self) -> None:
        metrics = self.metrics()
        self.record(metrics, "missing-external", "buy")
        metrics.apply_authenticated_fill_attributions(
            [attribution("1", "missing-external", "buy", "entry")]
        )
        metrics.update_fill_markouts(now=5.0, mid=Decimal("99"))
        metrics.update_fill_markouts(
            now=15.0,
            mid=Decimal("98"),
            external_mid=Decimal("101"),
        )

        event = metrics.fill_markouts[0]
        self.assertEqual(event["markout_5s_bps"], Decimal("-100"))
        self.assertIsNone(event["external_mid_5s"])
        self.assertIsNone(event["external_mid_markout_5s_bps"])
        self.assertEqual(event["external_mid_15s"], Decimal("101"))
        self.assertEqual(
            metrics.authenticated_entry_markout_feedback()["buy"]["5s"]
            ["count"],
            0,
        )

    def test_metrics_attribution_registry_is_bounded(self) -> None:
        metrics = self.metrics()
        metrics.apply_authenticated_fill_attributions(
            [
                attribution(
                    str(index),
                    f"order-{index}",
                    "buy",
                    "entry",
                    episode_sequence=index,
                    timestamp=index,
                )
                for index in range(1, 502)
            ]
        )

        self.assertEqual(len(metrics._authenticated_fill_attributions), 500)
        self.assertNotIn("1", metrics._authenticated_fill_attributions)


if __name__ == "__main__":
    unittest.main()

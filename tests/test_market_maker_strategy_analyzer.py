import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts.analyze_market_maker_strategy import (
    analyze,
    load_local_records,
    render_json,
    render_markdown,
)


class MarketMakerStrategyAnalyzerTests(unittest.TestCase):
    def test_json_analysis_separates_roles_and_marks_proxy(self) -> None:
        snapshot = {
            "account_audit": {
                "unique_maker_fills": 2,
                "unique_taker_fills": 0,
                "completed_round_trips": 1,
                "completed_turnover": "20",
                "completed_gross": "0.02",
                "completed_exact_fee": "0.01",
                "completed_net_ex_funding": "0.01",
                "completed_episode_ledger": [
                    {
                        "gross": "0.02",
                        "net_ex_funding": "0.01",
                        "maker_fee": "0.007",
                        "taker_fee": "0.003",
                        "close_type": "maker_flat",
                        "active_unwind_used": False,
                    }
                ],
            },
            "fill_markouts": [
                {
                    "order_id": "entry-1",
                    "side": "buy",
                    "price": "100",
                    "fill_role": "entry",
                    "attribution_state": "authenticated",
                    "active_unwind": False,
                    "source": "websocket_order_update",
                    "markout_5s_bps": "8",
                    "raw_mid_markout_5s_bps": "8",
                    "external_mid_markout_5s_bps": "-1.5",
                    "quote_context": {
                        "base_price": "100",
                        "shadow_price": "99",
                    },
                },
                {
                    "order_id": "exit-1",
                    "side": "sell",
                    "price": "101",
                    "fill_role": "passive_exit",
                    "attribution_state": "authenticated",
                    "active_unwind": False,
                    "source": "reconciliation",
                    "markout_5s_bps": "9",
                    "raw_mid_markout_5s_bps": "9",
                    "external_mid_markout_5s_bps": "0.5",
                },
            ],
            "fill_markout_coverage": {"retained_events": 2},
            "controller_ready_seconds": 100,
            "controller_bid_blocked_seconds": 10,
            "controller_ask_blocked_seconds": 20,
            "controller_both_blocked_seconds": 25,
            "controller_decision_history": [
                {
                    "feature_health": "ready",
                    "bid": {"toxicity_score_ticks": "1.5"},
                    "ask": {"toxicity_score_ticks": "2.5"},
                }
            ],
        }

        report = analyze([snapshot])

        self.assertTrue(report["analysis_contract"]["local_read_only"])
        self.assertFalse(report["analysis_contract"]["true_queue_backtest"])
        entry = report["markouts"]["authenticated_by_role_side_horizon"]
        self.assertEqual(entry["entry"]["buy"]["5"]["mean"], Decimal("-1.5"))
        self.assertEqual(entry["passive_exit"]["sell"]["5"]["count"], 1)
        self.assertEqual(
            report["markouts"]["entry_by_side_horizon"]["buy"]["5"]["count"],
            1,
        )
        self.assertEqual(report["coverage"]["external_reference_missing"], 0)
        self.assertEqual(
            report["markouts"]["exit_by_role_side_horizon"]
            ["passive_exit"]["sell"]["5"]["count"],
            1,
        )
        self.assertEqual(
            report["shadow_counterfactual"]["classification_counts"][
                "likely_filtered"
            ],
            1,
        )
        entry_row = next(
            row
            for row in report["shadow_counterfactual"]["fills"]
            if row["order_id"] == "entry-1"
        )
        self.assertEqual(entry_row["actual_fill_price"], Decimal("100"))
        self.assertEqual(entry_row["base_price"], Decimal("100"))
        self.assertEqual(entry_row["shadow_price"], Decimal("99"))
        self.assertTrue(entry_row["shadow_farther"])
        exit_row = next(
            row
            for row in report["shadow_counterfactual"]["fills"]
            if row["order_id"] == "exit-1"
        )
        self.assertEqual(exit_row["classification"], "indeterminate")
        self.assertIsNone(exit_row["shadow_farther"])
        self.assertEqual(report["session"]["completed_maker_fee"], Decimal("0.007"))
        self.assertEqual(report["session"]["completed_taker_fee"], Decimal("0.003"))
        self.assertEqual(
            report["volume_retention_proxy"]["shadow_active_quote_seconds"],
            Decimal("75"),
        )
        markdown = render_markdown(report)
        self.assertIn("not a true queue-fill backtest", markdown)
        for heading in (
            "## Episodes",
            "## Authenticated entry markouts",
            "## Exit markouts",
            "## Controller",
            "## Shadow counterfactual proxy",
            "## Volume retention proxy",
            "## Coverage and pending attribution",
        ):
            self.assertIn(heading, markdown)
        self.assertIn("fill=100, base=100, shadow=99", markdown)
        json.loads(render_json(report))

    def test_unknown_attribution_is_pending_not_in_mean(self) -> None:
        report = analyze(
            [
                {
                    "account_audit": {},
                    "fill_markouts": [
                        {
                            "order_id": "unknown",
                            "side": "buy",
                            "external_mid_markout_5s_bps": "9",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(report["coverage"]["pending_attribution"], 1)
        self.assertEqual(
            report["markouts"]["authenticated_by_role_side_horizon"], {}
        )

    def test_raw_only_markout_is_diagnostic_not_strategy_evidence(self) -> None:
        report = analyze(
            [
                {
                    "account_audit": {},
                    "fill_markouts": [
                        {
                            "order_id": "raw-only",
                            "side": "buy",
                            "fill_role": "entry",
                            "attribution_state": "authenticated",
                            "active_unwind": False,
                            "markout_5s_bps": "12",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(
            report["markouts"]["authenticated_by_role_side_horizon"], {}
        )
        self.assertEqual(report["coverage"]["external_reference_missing"], 1)
        row = report["shadow_counterfactual"]["fills"][0]
        self.assertEqual(row["markouts"], {})
        self.assertEqual(row["raw_markouts"], {"5": "12"})

    def test_legacy_e2ay_summary_stays_all_pending_and_indeterminate(self) -> None:
        text = """
Completed maker fills / round trips / authenticated-flat episodes: 16 / 8 / 8.
Completed turnover: 252.524840 USDG.
Completed gross: -0.002040 USDG.
Exact fee: 0.03030298080 USDG.
Completed net ex funding: -0.03234298080 USDG.
Completed net turnover bps: -1.280784131968957984489763462.
Fee-cover ratio: -0.06732010997413165374146955206.
Retained maker fill events: 16.
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "legacy.metrics.txt")
            path.write_text(text, encoding="utf-8")
            report = analyze(load_local_records([path]))

        self.assertEqual(report["session"]["unique_maker_fills"], 16)
        self.assertEqual(report["coverage"]["pending_attribution"], 16)
        self.assertEqual(
            report["shadow_counterfactual"]["classification_counts"],
            {"indeterminate": 16},
        )
        self.assertFalse(report["controller"]["available"])

    def test_cumulative_records_are_order_independent_and_deduplicated(self) -> None:
        retained = {
            "order_id": "retained",
            "side": "buy",
            "fill_amount": "0.1",
            "fill_price": "100",
            "started_monotonic": 2.0,
            "observation_source": "websocket_order_update",
            "attribution_state": "pending",
            "markout_5s_bps": None,
        }
        earlier_only = {
            "order_id": "earlier-only",
            "side": "sell",
            "fill_amount": "0.1",
            "fill_price": "101",
            "started_monotonic": 1.0,
            "observation_source": "reconciliation",
            "attribution_state": "authenticated",
            "fill_role": "entry",
            "active_unwind": False,
            "external_mid_markout_5s_bps": "1",
            "quote_context": {"base_price": "101", "shadow_price": "102"},
        }
        downgraded = {
            "order_id": "downgraded",
            "side": "buy",
            "fill_amount": "0.1",
            "fill_price": "100",
            "started_monotonic": 3.0,
            "observation_source": "reconciliation",
            "attribution_state": "authenticated",
            "fill_role": "entry",
            "active_unwind": False,
            "external_mid_markout_5s_bps": "-3",
        }
        early = {
            "uptime_seconds": 10,
            "account_audit": {"completed_fills": 1, "unique_maker_fills": 1},
            "fill_markouts": [earlier_only, retained, downgraded],
            "controller_decision_history": [
                {"decision_id": 1, "recorded_monotonic": 1.0}
            ],
        }
        later = {
            "uptime_seconds": 20,
            "account_audit": {"completed_fills": 2, "unique_maker_fills": 2},
            "fill_markouts": [
                {
                    **retained,
                    "attribution_state": "authenticated",
                    "fill_role": "entry",
                    "active_unwind": False,
                    "external_mid_markout_5s_bps": "-2",
                    "quote_context": {
                        "base_price": "100",
                        "shadow_price": "99",
                    },
                },
                {
                    **downgraded,
                    "attribution_state": "pending",
                    "fill_role": None,
                    "active_unwind": None,
                },
            ],
            "controller_decision_history": [
                {"decision_id": 1, "recorded_monotonic": 1.0},
                {"decision_id": 2, "recorded_monotonic": 2.0},
            ],
        }

        forward = analyze([early, later])
        reverse = analyze([later, early])

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["session"]["completed_fills"], 2)
        self.assertEqual(forward["coverage"]["merged_unique_events"], 3)
        self.assertEqual(forward["coverage"]["pending_attribution"], 1)
        self.assertEqual(forward["controller"]["decision_count"], 2)
        self.assertEqual(
            forward["markouts"]["entry_by_side_horizon"]["buy"]["5"]
            ["count"],
            1,
        )

    def test_controller_accepts_history_and_placement_context_schemas(self) -> None:
        report = analyze(
            [
                {
                    "account_audit": {},
                    "controller_decision_history": [
                        {
                            "decision_id": 1,
                            "recorded_monotonic": 1.0,
                            "feature_health": "ready",
                            "bid": {"toxicity_score_ticks": "1.5"},
                            "ask": {"toxicity_score_ticks": "2.5"},
                            "feature_snapshot": {
                                "health": "ready",
                                "return_5s_ticks": "1",
                                "rms_1s_move_15s_ticks": "2",
                            },
                        }
                    ],
                    "quote_controller": {
                        "decision_id": 3,
                        "bid_score_ticks": "5",
                        "ask_score_ticks": "6",
                    },
                    "quote_contexts": [
                        {
                            "order_id": "placement",
                            "decision_id": 2,
                            "side": "buy",
                            "created_monotonic": 2.0,
                            "toxicity_score_ticks": "3",
                            "feature_snapshot": {
                                "health": "ready",
                                "return_5s_ticks": "2",
                                "rms_1s_move_15s_ticks": "4",
                            },
                        }
                    ],
                }
            ]
        )

        controller = report["controller"]
        self.assertTrue(controller["available"])
        self.assertEqual(controller["placement_count"], 1)
        self.assertEqual(controller["bid_score_ticks"]["count"], 3)
        self.assertEqual(controller["ask_score_ticks"]["count"], 2)
        self.assertEqual(
            controller["feature_distributions"]["return_5s_ticks"]["mean"],
            Decimal("1.5"),
        )
        self.assertEqual(controller["feature_health_distribution"], {"ready": 2})

    def test_counterfactual_only_classifies_authenticated_non_active_entry(
        self,
    ) -> None:
        def event(
            order_id: str,
            *,
            role: str = "entry",
            state: str = "authenticated",
            active: bool = False,
            shadow: str | None = "99",
        ) -> dict:
            return {
                "order_id": order_id,
                "side": "buy",
                "price": "100",
                "fill_role": role,
                "attribution_state": state,
                "active_unwind": active,
                "external_mid_markout_5s_bps": "-1",
                "quote_context": {
                    "base_price": "100",
                    "shadow_price": shadow,
                },
            }

        report = analyze(
            [
                {
                    "account_audit": {},
                    "fill_markouts": [
                        event("entry"),
                        event("blocked", shadow=None),
                        event("exit", role="passive_exit"),
                        event("pending", state="pending"),
                        event("active", active=True),
                    ],
                }
            ]
        )

        rows = {
            row["order_id"]: row
            for row in report["shadow_counterfactual"]["fills"]
        }
        self.assertEqual(rows["entry"]["classification"], "likely_filtered")
        self.assertEqual(rows["blocked"]["classification"], "likely_filtered")
        for order_id in ("exit", "pending", "active"):
            self.assertEqual(rows[order_id]["classification"], "indeterminate")
            self.assertIsNone(rows[order_id]["shadow_farther"])

    def test_counterfactual_is_indeterminate_after_later_block_or_reprice(
        self,
    ) -> None:
        def event(order_id: str, side: str, created: str) -> dict:
            return {
                "order_id": order_id,
                "side": side,
                "price": "100",
                "started_monotonic": "20",
                "fill_role": "entry",
                "attribution_state": "authenticated",
                "active_unwind": False,
                "external_mid_markout_5s_bps": "-1",
                "quote_context": {
                    "base_price": "100",
                    "shadow_price": "99" if side == "buy" else "101",
                    "extra_spread_ticks": "1",
                    "created_monotonic": created,
                },
            }

        report = analyze(
            [
                {
                    "uptime_seconds": 20,
                    "account_audit": {},
                    "fill_markouts": [
                        event("later-block", "buy", "1"),
                        event("later-reprice", "sell", "2"),
                        event("later-unavailable", "buy", "12"),
                        event("later-price-reprice", "buy", "10.5"),
                        event("later-inapplicable", "sell", "16"),
                    ],
                    "controller_decision_history": [
                        {
                            "decision_id": 2,
                            "recorded_monotonic": "10",
                            "bid": {"blocked": True, "extra_spread_ticks": "1"},
                            "ask": {
                                "blocked": False,
                                "extra_spread_ticks": "2",
                            },
                        },
                        {
                            "decision_id": 3,
                            "recorded_monotonic": "11",
                            "ready": True,
                            "entry_applicable": True,
                            "bid": {
                                "blocked": False,
                                "extra_spread_ticks": "1",
                                "shadow_price": "98",
                            },
                        },
                        {
                            "decision_id": 4,
                            "recorded_monotonic": "15",
                            "ready": False,
                            "error": "feature_pipeline_failed",
                        },
                        {
                            "decision_id": 5,
                            "recorded_monotonic": "17",
                            "ready": True,
                            "entry_applicable": False,
                        }
                    ],
                }
            ]
        )

        rows = {
            row["order_id"]: row
            for row in report["shadow_counterfactual"]["fills"]
        }
        self.assertEqual(rows["later-block"]["classification"], "indeterminate")
        self.assertEqual(
            rows["later-block"]["classification_reason"],
            "later_shadow_block",
        )
        self.assertEqual(
            rows["later-reprice"]["classification"], "indeterminate"
        )
        self.assertEqual(
            rows["later-reprice"]["classification_reason"],
            "later_shadow_reprice",
        )
        self.assertEqual(
            rows["later-unavailable"]["classification"], "indeterminate"
        )
        self.assertEqual(
            rows["later-unavailable"]["classification_reason"],
            "later_shadow_unavailable",
        )
        self.assertEqual(
            rows["later-price-reprice"]["classification_reason"],
            "later_shadow_reprice",
        )
        self.assertEqual(
            rows["later-inapplicable"]["classification_reason"],
            "later_entry_inapplicable",
        )

    def test_episode_ledgers_merge_across_checkpoints_by_stable_identity(
        self,
    ) -> None:
        def episode(sequence: int) -> dict:
            return {
                "session_id": "session-a",
                "episode_sequence": sequence,
                "opened_at": f"2026-09-01T00:0{sequence}:00Z",
                "closed_at": f"2026-09-01T00:0{sequence}:30Z",
                "entry_side": "buy",
                "gross": str(sequence),
                "net_ex_funding": str(sequence),
                "close_type": "maker_flat",
            }

        early = {
            "uptime_seconds": 10,
            "account_audit": {
                "completed_fills": 2,
                "completed_episode_ledger": [episode(1), episode(2)],
            },
        }
        later = {
            "uptime_seconds": 20,
            "account_audit": {
                "completed_fills": 3,
                "completed_episode_ledger": [episode(2), episode(3)],
            },
        }

        forward = analyze([early, later])
        reverse = analyze([later, early])

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["episodes"]["count"], 3)
        self.assertEqual(forward["episodes"]["gross"]["mean"], Decimal("2"))
        self.assertEqual(forward["coverage"]["merged_unique_episodes"], 3)
        self.assertEqual(forward["coverage"]["identified_episodes"], 3)
        self.assertEqual(
            forward["coverage"]["legacy_final_snapshot_episodes"], 0
        )

    def test_incomplete_controller_history_fails_counterfactual_closed(
        self,
    ) -> None:
        report = analyze(
            [
                {
                    "account_audit": {},
                    "controller_decision_history_total": 2,
                    "controller_decision_history": [
                        {
                            "decision_id": 2,
                            "recorded_monotonic": "2",
                            "ready": True,
                            "entry_applicable": True,
                        }
                    ],
                    "fill_markouts": [
                        {
                            "order_id": "history-gap",
                            "side": "buy",
                            "price": "99",
                            "started_monotonic": "3",
                            "fill_role": "entry",
                            "attribution_state": "authenticated",
                            "active_unwind": False,
                            "external_mid_markout_5s_bps": "-1",
                            "quote_context": {
                                "base_price": "100",
                                "shadow_price": "99",
                                "created_monotonic": "1",
                            },
                        }
                    ],
                }
            ]
        )

        row = report["shadow_counterfactual"]["fills"][0]
        self.assertEqual(row["classification"], "indeterminate")
        self.assertEqual(
            row["classification_reason"], "controller_history_incomplete"
        )
        self.assertFalse(report["coverage"]["controller_history_complete"])


if __name__ == "__main__":
    unittest.main()

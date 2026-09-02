import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from core.services.market_maker.metrics import MarketMakerMetrics
from scripts.mm_calibration_campaign import (
    CampaignValidationError,
    analyze_campaign,
    load_manifest,
    render_json,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mm_calibration_campaign.py"
HARD_COUNTERS = {
    "active_unwind_ambiguous": 0,
    "ambiguous_cancellations": 0,
    "ambiguous_submissions": 0,
    "http_429": 0,
    "markout_telemetry_errors": 0,
    "mutation_limiter_blocks": 0,
    "reconciliation_failure": 0,
    "unknown_orders": 0,
    "unresolved_cancellations": 0,
}


def manifest(inputs: list[str], **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "campaign_id": "toxicity-cal-001",
        "candidate_id": "candidate-v1",
        "expected_commit_sha": "a" * 40,
        "expected_config_sha256": "b" * 64,
        "symbol": "BTC",
        "controller_profile_id": "profile-v1",
        "maker_fee_rate": "0.000120",
        "taker_fee_rate": "0.000350",
        "max_cumulative_flat_loss_usdg": "1.00",
        "inputs": inputs,
    }
    result.update(overrides)
    return result


def episode(session_id: str, sequence: int = 1, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "session_id": session_id,
        "episode_sequence": sequence,
        "entry_side": "buy",
        "entry_vwap": "100",
        "exit_vwap": "101",
        "quantity": "1",
        "gross": "1",
        "maker_fee": "0.02",
        "taker_fee": "0",
        "net_ex_funding": "0.98",
        "inventory_duration_seconds": "12",
        "final_exit_stage": "strict_profit",
        "final_binding_constraint": "normal_passive",
        "passive_loss_used": "0",
        "surplus_spent": "0",
        "max_unlocked_episode_loss": "0",
        "entered_inventory_hold": False,
        "active_attempts": 0,
        "close_policy_coverage": True,
    }
    result.update(overrides)
    return result


def record(
    run_id: str | None,
    *,
    buy_entries: int = 0,
    sell_entries: int = 0,
    passive_exits: int = 0,
    active_exits: int = 0,
    pending: int = 0,
    commit_sha: str = "a" * 40,
    config_sha256: str = "b" * 64,
    profile_id: str = "profile-v1",
    symbol: str = "BTC",
    maker_fee: str = "0.000120",
    taker_fee: str = "0.000350",
    position: str = "0",
    open_orders: int = 0,
    flat_change: str = "0.25",
    completed_net: str = "0.20",
    drawdown: str = "0.05",
    hard_counter: tuple[str, int] | None = None,
    history_total: int = 1,
    history: list[dict[str, object]] | None = None,
    episodes: list[dict[str, object]] | None = None,
    scores: list[str] | None = None,
) -> dict[str, object]:
    events: list[dict[str, object]] = []
    attributions: list[dict[str, object]] = []
    generated_episodes: list[dict[str, object]] = []
    score_values = scores or ["0", "0.5", "1", "2", "3"]

    def add(
        kind: str,
        side: str,
        index: int,
        authenticated: bool = True,
        *,
        sequence: int = 1,
        prior_position: str | None = None,
        next_position: str | None = None,
    ) -> None:
        order_id = f"{run_id or 'old'}-{kind}-{side}-{index}"
        trade_id = f"trade-{order_id}"
        role = {
            "base_entry": "entry",
            "controller_entry": "entry",
            "passive_exit": "passive_exit",
            "active_exit": "active_exit",
        }[kind]
        event = {
            "event_sequence_run_id": run_id,
            "fill_observation_event_sequence": len(events) + 100,
            "trade_id": trade_id,
            "order_id": order_id,
            "side": side,
            "fill_amount": "1",
            "fill_price": "100",
            "started_monotonic": str(len(events) + 1),
            "observation_source": "rest_open_order_sync",
            "attribution_state": "authenticated" if authenticated else "pending",
            "fill_role": role if authenticated else None,
            "episode_sequence": sequence if authenticated else None,
            "active_unwind": kind == "active_exit" if authenticated else None,
            "attribution_signature": (
                {
                    "side": side,
                    "role": role,
                    "episode_sequence": sequence,
                    "active_unwind": kind == "active_exit",
                }
                if authenticated
                else None
            ),
            "external_mid_markout_5s_bps": str(index - 2),
            "external_mid_markout_15s_bps": str(index - 1),
            "quote_context": {
                "order_id": order_id,
                "side": side,
                "decision_id": index + 1 if kind == "controller_entry" else None,
                "controller_mode": (
                    "active" if kind == "controller_entry" else "fixed"
                ),
                "event_sequence_run_id": run_id,
                "placement_event_sequence": len(events) + 2,
                "toxicity_score_ticks": score_values[index % len(score_values)],
                "base_price": "100",
                "shadow_price": "100",
                "applied_price": "100",
                "extra_spread_ticks": "0",
                "reduce_only": False,
            },
        }
        if kind != "active_exit":
            events.append(event)
        if authenticated:
            attributions.append(
                {
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "side": side,
                    "role": role,
                    "episode_sequence": sequence,
                    "active_unwind": kind == "active_exit",
                    "position_flip": False,
                    "intent_kind": kind,
                    "prior_position": (
                        prior_position
                        if prior_position is not None
                        else
                        "1"
                        if kind in {"passive_exit", "active_exit"} and side == "sell"
                        else "-1"
                        if kind in {"passive_exit", "active_exit"}
                        else "0"
                    ),
                    "next_position": (
                        next_position
                        if next_position is not None
                        else
                        "0"
                        if kind in {"passive_exit", "active_exit"}
                        else "1"
                        if side == "buy"
                        else "-1"
                    ),
                    "policy_decision_id": (
                        index + 1
                        if kind in {"passive_exit", "active_exit"}
                        else None
                    ),
                    "exit_stage": (
                        "active_ioc"
                        if kind == "active_exit"
                        else "strict_profit"
                        if kind == "passive_exit"
                        else None
                    ),
                    "binding_constraint": (
                        "active_slippage"
                        if kind == "active_exit"
                        else "normal_passive"
                        if kind == "passive_exit"
                        else None
                    ),
                    "controller_decision_id": (
                        index + 1 if kind == "controller_entry" else None
                    ),
                }
            )

    auto_close = bool(buy_entries or sell_entries) and not passive_exits and not active_exits
    sequence = 1
    for index in range(buy_entries):
        add("base_entry", "buy", index, sequence=sequence)
        if auto_close:
            add(
                "passive_exit",
                "sell",
                10_000 + index,
                sequence=sequence,
            )
            generated_episodes.append(
                episode(f"session-{run_id}-{sequence}", sequence=sequence)
            )
        sequence += 1
    for index in range(sell_entries):
        add("controller_entry", "sell", index, sequence=sequence)
        if auto_close:
            add(
                "passive_exit",
                "buy",
                20_000 + index,
                sequence=sequence,
            )
            generated_episodes.append(
                episode(
                    f"session-{run_id}-{sequence}",
                    sequence=sequence,
                    entry_side="sell",
                    entry_vwap="101",
                    exit_vwap="100",
                )
            )
        sequence += 1
    for index in range(passive_exits):
        add("passive_exit", "sell", index)
    for index in range(active_exits):
        add("active_exit", "buy", index)
    for index in range(pending):
        add("base_entry", "buy", index + 1000, authenticated=False)

    if episodes is not None:
        final_episodes = [dict(item) for item in episodes]
    elif generated_episodes:
        completed_total = Decimal(completed_net)
        final_episodes = []
        for index, item in enumerate(generated_episodes):
            net = (
                completed_total
                if index == len(generated_episodes) - 1
                else Decimal("0")
            )
            maker_episode_fee = Decimal("0.02")
            gross = net + maker_episode_fee
            entry_price = Decimal(str(item["entry_vwap"]))
            item = dict(item)
            item["exit_vwap"] = str(
                entry_price + gross
                if item["entry_side"] == "buy"
                else entry_price - gross
            )
            item["gross"] = str(gross)
            item["maker_fee"] = str(maker_episode_fee)
            item["taker_fee"] = "0"
            item["net_ex_funding"] = str(net)
            final_episodes.append(item)
    else:
        final_episodes = []
    completed_gross_total = sum(
        (Decimal(str(item["gross"])) for item in final_episodes), Decimal("0")
    )
    completed_fee_total = sum(
        (
            Decimal(str(item["maker_fee"]))
            + Decimal(str(item["taker_fee"]))
            for item in final_episodes
        ),
        Decimal("0"),
    )
    completed_net_total = sum(
        (Decimal(str(item["net_ex_funding"])) for item in final_episodes),
        Decimal("0"),
    )
    default_history = [
        {
            "event_sequence_run_id": run_id,
            "event_sequence": event["quote_context"]["placement_event_sequence"] - 1,
            "decision_id": event["quote_context"]["decision_id"],
            "mode": "active",
            "ready": True,
            "entry_applicable": True,
            "error": None,
            "bid": {
                "toxicity_score_ticks": "1",
                "base_price": "100",
                "shadow_price": "100",
                "applied_price": "100",
                "extra_spread_ticks": "0",
                "blocked": False,
            },
            "ask": {
                "toxicity_score_ticks": event["quote_context"]["toxicity_score_ticks"],
                "base_price": "100",
                "shadow_price": "100",
                "applied_price": "100",
                "extra_spread_ticks": "0",
                "blocked": False,
            },
        }
        for event in events
        if event["quote_context"].get("decision_id") is not None
    ]
    if not default_history and run_id is not None:
        default_history = [
            {
                "event_sequence_run_id": run_id,
                "event_sequence": 1,
                "decision_id": 1,
                "ready": True,
                "entry_applicable": True,
                "error": None,
                "bid": {"toxicity_score_ticks": "1"},
                "ask": {"toxicity_score_ticks": "1"},
            }
        ]
    reported_history_total = (
        len(default_history) if history is None and history_total == 1 else history_total
    )

    counters = dict(HARD_COUNTERS)
    if hard_counter is not None:
        counters[hard_counter[0]] = hard_counter[1]
    period_hour = sum(ord(character) for character in (run_id or "old")) % 20
    flat_delta = Decimal(flat_change)
    current_drawdown = max(Decimal("0"), -flat_delta)
    max_drawdown = max(Decimal(drawdown), current_drawdown)
    final: dict[str, object] = {
        "event_sequence_run_id": run_id,
        "campaign_id": "toxicity-cal-001",
        "candidate_id": "candidate-v1",
        "commit_sha": commit_sha,
        "semantic_config_sha256": config_sha256,
        "controller_profile_id": profile_id,
        "symbol": symbol,
        "maker_fee_rate": maker_fee,
        "taker_fee_rate": taker_fee,
        "market_period_id": f"period-{run_id}",
        "started_at_utc": f"2026-09-03T{period_hour:02d}:00:00Z",
        "ended_at_utc": f"2026-09-03T{period_hour + 1:02d}:00:00Z",
        "uptime_seconds": "3600",
        "cycles": 100,
        "consecutive_errors": 0,
        "failed_cycles": 0,
        "controller_error_count": 0,
        "signed_position": position,
        "authenticated_open_orders": open_orders,
        "preflight": {
            "authenticated": True,
            "position": "0",
            "open_orders": 0,
        },
        "postflight": {
            "authenticated": True,
            "position": position,
            "open_orders": open_orders,
        },
        "counters": counters,
        "eligible_quote_seconds": "3600",
        "fill_markout_coverage": {
            "unit": "observed_order_fill_delta",
            "retained_events": len(events),
            "observed_event_total": len(events),
        },
        "controller_decision_history_total": reported_history_total,
        "controller_decision_history": history
        if history is not None
        else default_history,
        "fill_markouts": events,
        "account_audit": {
            "state": "healthy",
            "reason": None,
            "economic_state": "collecting",
            "economic_reason": "need more completed maker fills",
            "total_read_failures": 0,
            "last_audit_authenticated": True,
            "audited_position": position,
            "ledger_position": position,
            "baseline_equity": "100",
            "current_equity": str(Decimal("100") + flat_delta),
            "current_drawdown": str(current_drawdown),
            "unique_maker_fills": sum(
                attribution["active_unwind"] is False
                for attribution in attributions
            ),
            "unique_taker_fills": sum(
                attribution["active_unwind"] is True
                for attribution in attributions
            ),
            "maker_turnover": str(
                sum(
                    attribution["active_unwind"] is False
                    for attribution in attributions
                )
            ),
            "completed_net_ex_funding": str(completed_net_total),
            "completed_gross": str(completed_gross_total),
            "completed_exact_fee": str(completed_fee_total),
            "completed_round_trips": len(final_episodes),
            "episode_flat_success": len(final_episodes),
            "flat_equity_change": flat_change,
            "max_observed_drawdown": str(max_drawdown),
            "policy_context_missing_count": 0,
            "authenticated_fill_attributions": attributions,
            "completed_episode_ledger": final_episodes,
        },
    }
    return final


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, default=str) + "\n", encoding="utf-8")


class MarketMakerCalibrationCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _input(self, name: str, value: object) -> str:
        path = self.root / name
        if isinstance(value, list):
            path.write_text(
                "\n".join(json.dumps(item, default=str) for item in value)
                + "\n",
                encoding="utf-8",
            )
        else:
            write_json(path, value)
        return name

    def test_same_fingerprint_multi_run_aggregates_and_can_complete(self) -> None:
        first = self._input("run1.json", record("run-1", buy_entries=30))
        second = self._input("run2.json", record("run-2", sell_entries=30))

        report = analyze_campaign(manifest([first, second]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 2)
        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 30)
        self.assertEqual(report["denominator"]["authenticated_sell_entries"], 30)
        self.assertEqual(report["campaign"]["status"], "calibration_complete")
        self.assertTrue(report["campaign"]["recommendation_allowed"])
        self.assertFalse(report["analysis_contract"]["live_profile_generated_or_applied"])

    def test_mixed_fingerprint_profile_symbol_and_fee_are_diagnostic_only(self) -> None:
        good = self._input("good.json", record("good", buy_entries=1))
        bad_commit = self._input("commit.json", record("c", commit_sha="c" * 40))
        bad_config = self._input("config.json", record("g", config_sha256="d" * 64))
        bad_profile = self._input("profile.json", record("p", profile_id="profile-v2"))
        bad_symbol = self._input("symbol.json", record("s", symbol="ETH"))
        bad_fee = self._input("fee.json", record("f", maker_fee="0.0002"))
        relabelled = record("relabelled")
        relabelled["campaign_id"] = "different-campaign"
        bad_campaign = self._input("campaign.json", relabelled)

        report = analyze_campaign(
            manifest(
                [
                    good,
                    bad_commit,
                    bad_config,
                    bad_profile,
                    bad_symbol,
                    bad_fee,
                    bad_campaign,
                ]
            ),
            base_dir=self.root,
        )

        self.assertEqual(report["runs"]["included_run_ids"], ["good"])
        reasons = {reason for item in report["runs"]["diagnostic_only"] for reason in item["reasons"]}
        self.assertTrue(any("mismatched_commit_sha" in reason for reason in reasons))
        self.assertTrue(any("mismatched_config_sha256" in reason for reason in reasons))
        self.assertTrue(any("mismatched_controller_profile_id" in reason for reason in reasons))
        self.assertTrue(any("mismatched_symbol" in reason for reason in reasons))
        self.assertTrue(any("mismatched_maker_fee_rate" in reason for reason in reasons))
        self.assertTrue(any("mismatched_campaign_id" in reason for reason in reasons))

    def test_hex_fingerprints_are_case_normalized(self) -> None:
        path = self._input(
            "uppercase.json",
            record("uppercase", commit_sha="A" * 40, config_sha256="B" * 64),
        )

        report = analyze_campaign(
            manifest(
                [path],
                expected_commit_sha="A" * 40,
                expected_config_sha256="B" * 64,
            ),
            base_dir=self.root,
        )

        self.assertEqual(report["runs"]["included_run_ids"], ["uppercase"])
        self.assertEqual(report["campaign"]["expected_commit_sha"], "a" * 40)

    def test_duplicate_run_id_is_never_in_denominator(self) -> None:
        one = self._input("one.json", record("duplicate", buy_entries=2))
        two = self._input("two.json", record("duplicate", sell_entries=2))

        report = analyze_campaign(manifest([one, two]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 0)
        self.assertTrue(
            all("duplicate_runtime_run_id" in item["reasons"] for item in report["runs"]["diagnostic_only"])
        )

    def test_old_schema_and_missing_run_metadata_are_diagnostic_only(self) -> None:
        old = self._input("old.json", record(None, buy_entries=5))
        undecorated = record("new-but-undecorated", buy_entries=5)
        undecorated.pop("commit_sha")
        missing = self._input("missing.json", undecorated)

        report = analyze_campaign(manifest([old, missing]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        reasons = {reason for item in report["runs"]["diagnostic_only"] for reason in item["reasons"]}
        self.assertIn("old_or_missing_runtime_run_id", reasons)
        self.assertIn("snapshot_0_missing_commit_sha", reasons)

    def test_passive_active_and_pending_are_separate_from_entry_denominator(self) -> None:
        passive = self._input(
            "passive-role.json",
            record("passive-role", buy_entries=1, passive_exits=1),
        )
        active = self._input(
            "active-role.json",
            record("active-role", sell_entries=1, active_exits=1),
        )
        pending = self._input(
            "pending-role.json",
            record("pending-role", pending=6),
        )

        denominator = analyze_campaign(
            manifest([passive, active, pending]),
            base_dir=self.root,
        )["denominator"]

        self.assertEqual(denominator["authenticated_buy_entries"], 1)
        self.assertEqual(denominator["authenticated_sell_entries"], 1)
        self.assertEqual(denominator["excluded_passive_exits"], 1)
        self.assertEqual(denominator["excluded_active_exits"], 1)
        self.assertEqual(denominator["pending_or_indeterminate"], 6)

    def test_nonflat_open_orders_history_and_hard_counter_runs_are_excluded(self) -> None:
        nonflat = self._input("nonflat.json", record("nonflat", position="1"))
        orders = self._input("orders.json", record("orders", open_orders=1))
        history = self._input("history.json", record("history", history_total=2))
        hard = self._input(
            "hard.json", record("hard", hard_counter=("reconciliation_failure", 1))
        )

        report = analyze_campaign(
            manifest([nonflat, orders, history, hard]), base_dir=self.root
        )

        self.assertEqual(report["runs"]["included_count"], 0)
        reasons = {reason for item in report["runs"]["diagnostic_only"] for reason in item["reasons"]}
        self.assertIn("final_audited_position_non_flat", reasons)
        self.assertIn("final_authenticated_open_orders_nonzero", reasons)
        self.assertIn("controller_history_incomplete", reasons)
        self.assertIn("hard_counter_nonzero_or_invalid:reconciliation_failure", reasons)

    def test_campaign_loss_cap_stops_recommendation_and_reports_risk(self) -> None:
        one = self._input(
            "loss1.json", record("loss-1", buy_entries=30, flat_change="-0.07", completed_net="-0.06")
        )
        two = self._input(
            "loss2.json", record("loss-2", sell_entries=30, flat_change="-0.05", completed_net="-0.04")
        )

        report = analyze_campaign(
            manifest([one, two], max_cumulative_flat_loss_usdg="0.10"),
            base_dir=self.root,
        )

        self.assertEqual(report["risk"]["sum_session_realized_losses_usdg"], Decimal("0.12"))
        self.assertEqual(report["risk"]["remaining_operator_budget_usdg"], Decimal("-0.02"))
        self.assertFalse(report["risk"]["within_campaign_loss_cap"])
        self.assertIn("campaign_loss_cap_exceeded", report["campaign"]["incomplete_reasons"])
        self.assertFalse(report["campaign"]["recommendation_allowed"])

    def test_diagnostic_run_loss_is_excluded_from_denominator_but_debits_risk(self) -> None:
        bad = self._input(
            "diagnostic-loss.json",
            record(
                "diagnostic-loss",
                buy_entries=30,
                flat_change="-0.07",
                hard_counter=("reconciliation_failure", 1),
            ),
        )
        good = self._input(
            "good-profit.json",
            record("good-profit", sell_entries=30, flat_change="0.02"),
        )

        report = analyze_campaign(manifest([bad, good]), base_dir=self.root)

        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 0)
        self.assertEqual(report["denominator"]["authenticated_sell_entries"], 30)
        self.assertEqual(
            report["risk"]["sum_session_realized_losses_usdg"],
            Decimal("0.07"),
        )
        self.assertEqual(
            report["risk"]["accounted_run_ids"],
            ["diagnostic-loss", "good-profit"],
        )
        self.assertFalse(report["risk"]["risk_evidence_complete"])

    def test_mixed_fingerprint_loss_still_debits_manifest_roster_risk(self) -> None:
        mixed = self._input(
            "mixed-loss.json",
            record(
                "mixed-loss",
                config_sha256="c" * 64,
                flat_change="-0.08",
            ),
        )
        good = self._input(
            "matching-profit.json",
            record("matching-profit", buy_entries=1, flat_change="0.50"),
        )

        report = analyze_campaign(manifest([mixed, good]), base_dir=self.root)

        self.assertEqual(
            report["risk"]["sum_session_realized_losses_usdg"],
            Decimal("0.08"),
        )
        self.assertEqual(
            report["risk"]["accounted_run_ids"],
            ["matching-profit", "mixed-loss"],
        )
        self.assertFalse(report["risk"]["risk_evidence_complete"])

    def test_duplicate_run_risk_uses_the_more_conservative_loss_once(self) -> None:
        smaller = self._input(
            "duplicate-smaller.json",
            record("duplicate-risk", flat_change="-0.05"),
        )
        larger = self._input(
            "duplicate-larger.json",
            record("duplicate-risk", flat_change="-0.08"),
        )

        report = analyze_campaign(manifest([smaller, larger]), base_dir=self.root)

        self.assertEqual(
            report["risk"]["sum_session_realized_losses_usdg"],
            Decimal("0.08"),
        )
        self.assertEqual(report["risk"]["accounted_run_ids"], ["duplicate-risk"])

    def test_missing_run_id_flat_loss_is_reported_as_unidentified_risk(self) -> None:
        old = self._input(
            "old-loss.json",
            record(None, flat_change="-0.04"),
        )

        report = analyze_campaign(manifest([old]), base_dir=self.root)

        self.assertEqual(
            report["risk"]["sum_session_realized_losses_usdg"],
            Decimal("0.04"),
        )
        self.assertEqual(
            report["risk"]["unidentified_evidence_sources"],
            ["old-loss.json"],
        )
        self.assertFalse(report["risk"]["risk_evidence_complete"])

    def test_budget_equality_is_exhausted_not_replenished_by_profit(self) -> None:
        loss = self._input(
            "exact-loss.json",
            record("exact-loss", buy_entries=30, flat_change="-0.10"),
        )
        profit = self._input(
            "profit.json",
            record("profit", sell_entries=30, flat_change="0.50"),
        )

        report = analyze_campaign(
            manifest([loss, profit], max_cumulative_flat_loss_usdg="0.10"),
            base_dir=self.root,
        )

        self.assertEqual(report["risk"]["cumulative_flat_equity_change_usdg"], Decimal("0.40"))
        self.assertEqual(report["risk"]["sum_session_realized_losses_usdg"], Decimal("0.10"))
        self.assertEqual(report["risk"]["remaining_operator_budget_usdg"], Decimal("0.00"))
        self.assertFalse(report["risk"]["within_campaign_loss_cap"])

    def test_score_bins_quartiles_and_negative_probability_are_deterministic(self) -> None:
        path = self._input(
            "bins.json",
            record("bins", buy_entries=4, scores=["0", "0.5", "1", "3"]),
        )

        first = analyze_campaign(manifest([path]), base_dir=self.root)
        second = analyze_campaign(manifest([path]), base_dir=self.root)
        bins = first["score_bins"]["buy"]

        self.assertEqual(render_json(first), render_json(second))
        self.assertEqual(list(bins), ["0", "(0,1)", "[1,2)", "[2,3)", "[3,+inf)"])
        self.assertEqual(bins["0"]["external_markout_5s_bps"]["p25"], Decimal("-2"))
        self.assertEqual(
            bins["0"]["external_markout_5s_bps"]["negative_probability"], Decimal("1")
        )
        self.assertEqual(bins["[3,+inf)"]["entry_count"], 1)

    def test_checkpoint_episodes_deduplicate_by_session_and_sequence(self) -> None:
        checkpoint_episode = episode("session-checkpoint", gross="1", net_ex_funding="0.98")
        early = record("checkpoint", buy_entries=1, episodes=[checkpoint_episode])
        early["uptime_seconds"] = "10"
        early["eligible_quote_seconds"] = "10"
        final = record("checkpoint", buy_entries=1, episodes=[checkpoint_episode])
        path = self._input("checkpoints.jsonl", [early, final])

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 1)
        self.assertEqual(report["episode_coverage"]["deduplicated_completed_episode_count"], 1)

    def test_conflicting_checkpoint_episode_is_diagnostic_only(self) -> None:
        early = record(
            "conflict",
            episodes=[episode("session-conflict", gross="1", net_ex_funding="0.98")],
        )
        early["uptime_seconds"] = "10"
        early["eligible_quote_seconds"] = "10"
        final = record(
            "conflict",
            episodes=[episode("session-conflict", gross="2", net_ex_funding="1.98")],
        )
        path = self._input("conflict.jsonl", [early, final])

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertTrue(
            any(
                reason.startswith("conflicting_duplicate_episode:")
                for reason in report["runs"]["diagnostic_only"][0]["reasons"]
            )
        )

    def test_final_ledger_cannot_drop_completed_checkpoint_episode(self) -> None:
        early = record("dropped-episode", buy_entries=1)
        early["uptime_seconds"] = "10"
        early["eligible_quote_seconds"] = "10"
        final = record("dropped-episode", buy_entries=1, episodes=[])
        path = self._input("dropped-episode.jsonl", [early, final])

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertIn(
            "completed_episode_aggregate_incomplete_or_inconsistent",
            report["runs"]["diagnostic_only"][0]["reasons"],
        )

    def test_zero_history_missing_counter_and_missing_economics_fail_closed(self) -> None:
        zero_history = record("zero-history", history_total=0, history=[])
        missing_counter = record("missing-counter")
        del missing_counter["counters"]["unknown_orders"]  # type: ignore[index]
        missing_economics = record("missing-economics")
        del missing_economics["account_audit"]["flat_equity_change"]  # type: ignore[index]
        missing_position = record("missing-position")
        del missing_position["account_audit"]["audited_position"]  # type: ignore[index]
        missing_scalar = record("missing-scalar")
        del missing_scalar["failed_cycles"]
        invalid_fills = record("invalid-fills")
        invalid_fills["account_audit"]["unique_maker_fills"] = "not-an-int"  # type: ignore[index]
        missing_markout_unit = record("missing-markout-unit")
        del missing_markout_unit["fill_markout_coverage"]
        truncated_markouts = record("truncated-markouts", buy_entries=1)
        truncated_markouts["fill_markout_coverage"]["observed_event_total"] = 3  # type: ignore[index]
        inputs = [
            self._input("zero-history.json", zero_history),
            self._input("missing-counter.json", missing_counter),
            self._input("missing-economics.json", missing_economics),
            self._input("missing-position.json", missing_position),
            self._input("missing-scalar.json", missing_scalar),
            self._input("invalid-fills.json", invalid_fills),
            self._input("missing-markout-unit.json", missing_markout_unit),
            self._input("truncated-markouts.json", truncated_markouts),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        reasons = {reason for item in report["runs"]["diagnostic_only"] for reason in item["reasons"]}
        self.assertIn("controller_history_incomplete", reasons)
        self.assertIn("missing_hard_counter:unknown_orders", reasons)
        self.assertIn("missing_or_invalid_flat_equity_change", reasons)
        self.assertIn("missing_final_audited_position", reasons)
        self.assertIn("missing_hard_counter:failed_cycles", reasons)
        self.assertIn("missing_or_invalid_unique_maker_fills", reasons)
        self.assertIn("missing_or_invalid_fill_markout_coverage_unit", reasons)
        self.assertIn("fill_markout_history_incomplete", reasons)

    def test_entry_denominator_uses_observed_fill_delta_not_trade_count(self) -> None:
        value = record("fill-delta", buy_entries=1)
        attributions = value["account_audit"][  # type: ignore[index]
            "authenticated_fill_attributions"
        ]
        entry_attribution = attributions[0]
        exit_attribution = attributions[1]
        entry_attribution["next_position"] = "0.5"
        second_entry = dict(entry_attribution)
        second_entry["trade_id"] = "second-trade-for-same-delta"
        second_entry["prior_position"] = "0.5"
        second_entry["next_position"] = "1"
        value["account_audit"]["authenticated_fill_attributions"] = [  # type: ignore[index]
            entry_attribution,
            second_entry,
            exit_attribution,
        ]
        value["account_audit"]["unique_maker_fills"] = 3  # type: ignore[index]
        path = self._input("fill-delta.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 1)
        self.assertEqual(
            report["analysis_contract"]["entry_sample_unit"],
            "observed_order_fill_delta",
        )

    def test_missing_or_conflicting_typed_intent_never_enters_denominator(self) -> None:
        missing = record("missing-intent", buy_entries=1)
        missing["account_audit"]["authenticated_fill_attributions"][0][  # type: ignore[index]
            "intent_kind"
        ] = None
        conflicting = record("conflicting-intent", sell_entries=1)
        second = dict(
            conflicting["account_audit"]["authenticated_fill_attributions"][0]  # type: ignore[index]
        )
        second["trade_id"] = "conflicting-second-trade"
        second["intent_kind"] = "base_entry"
        conflicting["account_audit"]["authenticated_fill_attributions"].append(  # type: ignore[index]
            second
        )
        inputs = [
            self._input("missing-intent.json", missing),
            self._input("conflicting-intent.json", conflicting),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 0)
        self.assertEqual(report["denominator"]["authenticated_sell_entries"], 0)
        self.assertEqual(len(report["runs"]["diagnostic_only"]), 2)
        self.assertIn(
            "typed_attribution_evidence_incomplete",
            report["campaign"]["incomplete_reasons"],
        )

    def test_exit_attribution_requires_complete_canonical_policy_context(self) -> None:
        passive = record("missing-exit-policy", buy_entries=1, passive_exits=1)
        passive["account_audit"]["authenticated_fill_attributions"][1][  # type: ignore[index]
            "policy_decision_id"
        ] = None
        active = record("invalid-active-stage", sell_entries=1, active_exits=1)
        active["account_audit"]["authenticated_fill_attributions"][1][  # type: ignore[index]
            "exit_stage"
        ] = "strict_profit"
        inputs = [
            self._input("missing-exit-policy.json", passive),
            self._input("invalid-active-stage.json", active),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )
        self.assertIn(
            "typed_attribution_evidence_incomplete",
            report["campaign"]["incomplete_reasons"],
        )

    def test_nested_run_and_placement_provenance_are_required(self) -> None:
        wrong_event = record("nested-event", buy_entries=1)
        wrong_event["fill_markouts"][0]["event_sequence_run_id"] = "other-run"  # type: ignore[index]
        wrong_context = record("nested-context", sell_entries=1)
        wrong_context["fill_markouts"][0]["quote_context"][  # type: ignore[index]
            "event_sequence_run_id"
        ] = "other-run"
        inputs = [
            self._input("nested-event.json", wrong_event),
            self._input("nested-context.json", wrong_context),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 0)
        self.assertEqual(report["denominator"]["authenticated_sell_entries"], 0)
        self.assertIn(
            "pending_or_indeterminate_evidence_present",
            report["campaign"]["incomplete_reasons"],
        )

    def test_complete_entry_counts_without_episode_economics_stay_incomplete(self) -> None:
        buy = self._input(
            "no-episode-buy.json",
            record("no-episode-buy", buy_entries=30, episodes=[]),
        )
        sell = self._input(
            "no-episode-sell.json",
            record("no-episode-sell", sell_entries=30, episodes=[]),
        )

        report = analyze_campaign(manifest([buy, sell]), base_dir=self.root)

        self.assertTrue(report["calibration_coverage"]["side_threshold_met"])
        self.assertFalse(
            report["calibration_coverage"][
                "completed_episode_economics_complete"
            ]
        )
        self.assertIn(
            "completed_episode_economics_incomplete",
            report["campaign"]["incomplete_reasons"],
        )

    def test_completed_episode_requires_authenticated_matching_final_exit(self) -> None:
        value = record("missing-final-exit", buy_entries=1)
        value["fill_markouts"] = value["fill_markouts"][:1]
        value["fill_markout_coverage"]["retained_events"] = 1  # type: ignore[index]
        value["fill_markout_coverage"]["observed_event_total"] = 1  # type: ignore[index]
        value["account_audit"]["authenticated_fill_attributions"] = value[  # type: ignore[index]
            "account_audit"
        ]["authenticated_fill_attributions"][:1]  # type: ignore[index]
        value["account_audit"]["unique_maker_fills"] = 1  # type: ignore[index]
        path = self._input("missing-final-exit.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )
        self.assertFalse(
            report["calibration_coverage"][
                "completed_episode_economics_complete"
            ]
        )
        self.assertEqual(
            report["episode_coverage"]["deduplicated_completed_episode_count"],
            0,
        )

    def test_episode_financial_identity_must_be_exact(self) -> None:
        inconsistent = episode("session-inconsistent", net_ex_funding="0.99")
        path = self._input(
            "inconsistent-episode.json",
            record("inconsistent-episode", buy_entries=1, episodes=[inconsistent]),
        )

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"][
                "completed_episode_economics_complete"
            ]
        )

    def test_fill_observation_event_sequence_must_be_unique(self) -> None:
        value = record("duplicate-event-sequence", buy_entries=2)
        value["fill_markouts"][2]["fill_observation_event_sequence"] = value[  # type: ignore[index]
            "fill_markouts"
        ][0]["fill_observation_event_sequence"]  # type: ignore[index]
        path = self._input("duplicate-event-sequence.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )
        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 0)
        self.assertTrue(
            any(
                reason.startswith("conflicting_raw_fill_event:")
                for reason in report["runs"]["diagnostic_only"][0]["reasons"]
            )
        )

    def test_attribution_position_chain_must_close_contiguously(self) -> None:
        value = record("broken-position-chain", buy_entries=2)
        value["account_audit"]["authenticated_fill_attributions"][1][  # type: ignore[index]
            "prior_position"
        ] = "2"
        path = self._input("broken-position-chain.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )
        self.assertIn(
            "typed_attribution_evidence_incomplete",
            report["campaign"]["incomplete_reasons"],
        )

    def test_fill_delta_amount_must_conserve_authenticated_position_delta(self) -> None:
        value = record("inflated-fill-deltas", buy_entries=1)
        original = value["fill_markouts"][0]  # type: ignore[index]
        for sequence in range(1_000, 1_029):
            duplicate = dict(original)
            duplicate["fill_observation_event_sequence"] = sequence
            value["fill_markouts"].append(duplicate)  # type: ignore[union-attr]
        value["fill_markout_coverage"]["retained_events"] = 31  # type: ignore[index]
        value["fill_markout_coverage"]["observed_event_total"] = 31  # type: ignore[index]
        path = self._input("inflated-fill-deltas.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 30)
        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )
        self.assertFalse(report["campaign"]["recommendation_allowed"])

    def test_only_current_scalar_external_markouts_prove_coverage(self) -> None:
        value = record("nested-markout", buy_entries=1)
        entry = value["fill_markouts"][0]  # type: ignore[index]
        del entry["external_mid_markout_5s_bps"]
        entry["external_mid_markouts"] = {"5": "1", "15": "1"}
        path = self._input("nested-markout.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"][
                "external_5s_15s_markout_coverage_complete"
            ]
        )
        self.assertIn(
            "external_markout_coverage_incomplete",
            report["campaign"]["incomplete_reasons"],
        )

    def test_account_financial_identities_must_be_exact(self) -> None:
        value = record("inconsistent-finance", buy_entries=1, flat_change="-0.10")
        value["account_audit"]["current_equity"] = "100.10"  # type: ignore[index]
        value["account_audit"]["completed_gross"] = "999"  # type: ignore[index]
        path = self._input("inconsistent-finance.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertIn(
            "account_financial_identity_incomplete_or_inconsistent",
            report["runs"]["diagnostic_only"][0]["reasons"],
        )
        self.assertEqual(
            report["risk"]["sum_session_realized_losses_usdg"],
            Decimal("0"),
        )

    def test_hard_stop_is_diagnostic_but_authenticated_economic_no_go_is_allowed(self) -> None:
        unsafe = record("unsafe-state", buy_entries=1)
        unsafe["account_audit"]["state"] = "hard_stop"  # type: ignore[index]
        unsafe["account_audit"]["reason"] = "unknown order"  # type: ignore[index]
        stale_economic = record("stale-economic-state", buy_entries=1)
        stale_economic["account_audit"]["state"] = "hard_stop"  # type: ignore[index]
        stale_economic["account_audit"]["reason"] = "account read failed"  # type: ignore[index]
        stale_economic["account_audit"]["economic_state"] = "no_go"  # type: ignore[index]
        stale_economic["account_audit"]["economic_reason"] = "economic gate failed"  # type: ignore[index]
        stale_economic["account_audit"]["total_read_failures"] = 1  # type: ignore[index]
        economic = record("economic-stop", sell_entries=1, flat_change="-0.01")
        economic["account_audit"]["state"] = "hard_stop"  # type: ignore[index]
        economic["account_audit"]["reason"] = "economic gate failed"  # type: ignore[index]
        economic["account_audit"]["economic_state"] = "no_go"  # type: ignore[index]
        economic["account_audit"]["economic_reason"] = "economic gate failed"  # type: ignore[index]
        economic["account_audit"]["total_read_failures"] = 1  # type: ignore[index]
        regressed_early = record("economic-regression", buy_entries=1)
        regressed_early["uptime_seconds"] = "1800"
        regressed_early["eligible_quote_seconds"] = "1800"
        regressed_early["account_audit"].update(  # type: ignore[union-attr]
            state="hard_stop",
            reason="economic gate failed",
            economic_state="no_go",
            economic_reason="economic gate failed",
        )
        regressed_final = record("economic-regression", buy_entries=1)
        inputs = [
            self._input("unsafe-state.json", unsafe),
            self._input("stale-economic-state.json", stale_economic),
            self._input("economic-stop.json", economic),
            self._input(
                "economic-regression.jsonl",
                [regressed_early, regressed_final],
            ),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertEqual(report["runs"]["included_run_ids"], ["economic-stop"])
        self.assertEqual(len(report["runs"]["diagnostic_only"]), 3)
        self.assertEqual(
            sum(
                "final_account_state_not_safe_or_economic_stop" in item["reasons"]
                for item in report["runs"]["diagnostic_only"]
            ),
            2,
        )
        self.assertTrue(
            any(
                any("account_stop_state_regressed" in reason for reason in item["reasons"])
                for item in report["runs"]["diagnostic_only"]
            )
        )

    def test_active_taker_count_must_match_authenticated_attribution(self) -> None:
        value = record("active-count", sell_entries=1, active_exits=1)
        value["account_audit"]["unique_taker_fills"] = 0  # type: ignore[index]
        path = self._input("active-count.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["denominator"]["excluded_active_exits"], 1)
        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )
        self.assertIn(
            "typed_attribution_evidence_incomplete",
            report["campaign"]["incomplete_reasons"],
        )

    def test_placement_context_score_cannot_be_overridden_at_event_top_level(self) -> None:
        value = record("score-provenance", buy_entries=1, scores=["0"])
        value["fill_markouts"][0]["toxicity_score_ticks"] = "99"  # type: ignore[index]
        path = self._input("score-provenance.json", value)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["score_bins"]["buy"]["0"]["entry_count"], 1)
        self.assertEqual(
            report["score_bins"]["buy"]["[3,+inf)"]["entry_count"],
            0,
        )

    def test_side_threshold_without_two_populated_bins_stays_incomplete(self) -> None:
        buy = self._input(
            "one-bin-buy.json",
            record("one-bin-buy", buy_entries=30, scores=["1"]),
        )
        sell = self._input(
            "one-bin-sell.json",
            record("one-bin-sell", sell_entries=30, scores=["1"]),
        )

        report = analyze_campaign(manifest([buy, sell]), base_dir=self.root)

        self.assertTrue(report["calibration_coverage"]["side_threshold_met"])
        self.assertFalse(report["calibration_coverage"]["meaningful_score_bins"])
        self.assertIn(
            "meaningful_score_bins_not_proven",
            report["campaign"]["incomplete_reasons"],
        )

    def test_four_entries_do_not_make_a_score_bin_meaningful(self) -> None:
        scores = ["0"] * 4 + ["1"] * 26
        buy_record = record("four-bin-buy", buy_entries=30, scores=scores)
        sell_record = record("four-bin-sell", sell_entries=30, scores=scores)
        buy_record["started_at_utc"] = "2026-09-03T02:00:00Z"
        buy_record["ended_at_utc"] = "2026-09-03T02:30:00Z"
        buy_record["uptime_seconds"] = "1800"
        buy_record["eligible_quote_seconds"] = "1800"
        sell_record["started_at_utc"] = "2026-09-03T03:00:00Z"
        sell_record["ended_at_utc"] = "2026-09-03T03:30:00Z"
        sell_record["uptime_seconds"] = "1800"
        sell_record["eligible_quote_seconds"] = "1800"
        buy = self._input("four-bin-buy.json", buy_record)
        sell = self._input("four-bin-sell.json", sell_record)

        report = analyze_campaign(manifest([buy, sell]), base_dir=self.root)

        self.assertEqual(report["score_bins"]["buy"]["0"]["entry_count"], 4)
        self.assertFalse(report["calibration_coverage"]["meaningful_score_bins"])

    def test_overlapping_utc_periods_do_not_prove_fresh_runs(self) -> None:
        buy_record = record("overlap-buy", buy_entries=30)
        sell_record = record("overlap-sell", sell_entries=30)
        buy_record["started_at_utc"] = "2026-09-03T01:00:00Z"
        buy_record["ended_at_utc"] = "2026-09-03T01:30:00Z"
        buy_record["uptime_seconds"] = "1800"
        buy_record["eligible_quote_seconds"] = "1800"
        sell_record["started_at_utc"] = "2026-09-03T01:15:00Z"
        sell_record["ended_at_utc"] = "2026-09-03T01:45:00Z"
        sell_record["uptime_seconds"] = "1800"
        sell_record["eligible_quote_seconds"] = "1800"
        buy = self._input("overlap-buy.json", buy_record)
        sell = self._input("overlap-sell.json", sell_record)

        report = analyze_campaign(manifest([buy, sell]), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"]["utc_periods_non_overlapping"]
        )
        self.assertIn(
            "multiple_fresh_runs_not_proven",
            report["campaign"]["incomplete_reasons"],
        )

    def test_any_diagnostic_input_keeps_an_otherwise_complete_campaign_incomplete(self) -> None:
        buy = self._input("complete-buy.json", record("complete-buy", buy_entries=30))
        sell = self._input("complete-sell.json", record("complete-sell", sell_entries=30))
        old = self._input("diagnostic-old.json", record(None, buy_entries=1))

        report = analyze_campaign(manifest([buy, sell, old]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 2)
        self.assertIn("diagnostic_inputs_present", report["campaign"]["incomplete_reasons"])
        self.assertEqual(report["campaign"]["status"], "calibration_incomplete")
        self.assertFalse(report["risk"]["risk_evidence_complete"])
        self.assertIsNone(report["risk"]["remaining_operator_budget_usdg"])

    def test_manifest_rejects_placeholder_nonpositive_budget_and_secret_keys(self) -> None:
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(manifest(["unused"], max_cumulative_flat_loss_usdg="OPERATOR_SUPPLIED"))
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(manifest(["unused"], max_cumulative_flat_loss_usdg="0"))
        unsafe = manifest(["unused"])
        unsafe["api_key"] = "must-not-appear"
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(unsafe)
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(manifest(["unused"], expected_commit_sha="not-a-sha"))
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(
                manifest(["unused"], expected_config_sha256="c" * 63)
            )
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(manifest([r"\\server\share\metrics.json"]))
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(manifest([r"  \\server\share\metrics.json"]))
        with self.assertRaises(CampaignValidationError):
            load_manifest(r"\\server\share\manifest.json")
        with self.assertRaises(CampaignValidationError):
            analyze_campaign(
                manifest(["metrics.json"]),
                base_dir=r"\\server\share",
            )

    def test_malformed_jsonl_and_non_object_array_items_are_diagnostic(self) -> None:
        valid = json.dumps(record("truncated", buy_entries=1), default=str)
        malformed = self.root / "truncated.jsonl"
        malformed.write_text(valid + "\n{\"event_sequence_run_id\":", encoding="utf-8")
        mixed = self.root / "mixed-array.json"
        mixed.write_text(
            json.dumps([record("mixed-array"), "not-a-snapshot"], default=str),
            encoding="utf-8",
        )

        report = analyze_campaign(
            manifest([malformed.name, mixed.name]),
            base_dir=self.root,
        )

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertEqual(len(report["runs"]["diagnostic_only"]), 2)
        self.assertTrue(
            all(
                any(reason.startswith("unreadable_or_invalid_input:") for reason in item["reasons"])
                for item in report["runs"]["diagnostic_only"]
            )
        )

    def test_duplicate_json_keys_are_rejected_in_manifest_and_evidence(self) -> None:
        manifest_path = self.root / "duplicate-manifest.json"
        manifest_text = json.dumps(manifest(["unused.json"]))
        manifest_path.write_text(
            manifest_text.replace(
                '"max_cumulative_flat_loss_usdg": "1.00"',
                '"max_cumulative_flat_loss_usdg": "0.10", '
                '"max_cumulative_flat_loss_usdg": "100"',
            ),
            encoding="utf-8",
        )
        with self.assertRaises(CampaignValidationError):
            load_manifest(manifest_path)

        evidence_path = self.root / "duplicate-evidence.json"
        evidence_text = json.dumps(record("duplicate-key", buy_entries=1))
        evidence_path.write_text(
            evidence_text.replace(
                '"reconciliation_failure": 0',
                '"reconciliation_failure": 1, "reconciliation_failure": 0',
            ),
            encoding="utf-8",
        )
        report = analyze_campaign(
            manifest([evidence_path.name]), base_dir=self.root
        )
        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertTrue(
            report["runs"]["diagnostic_only"][0]["reasons"][0].startswith(
                "unreadable_or_invalid_input:"
            )
        )

    def test_unhashable_event_sequence_and_extreme_decimal_fail_closed(self) -> None:
        unhashable = record("unhashable-sequence", buy_entries=1)
        unhashable["fill_markouts"][0]["fill_observation_event_sequence"] = []  # type: ignore[index]
        extreme = record("extreme-decimal", buy_entries=1)
        extreme["eligible_quote_seconds"] = "1e999999"
        inputs = [
            self._input("unhashable-sequence.json", unhashable),
            self._input("extreme-decimal.json", extreme),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertFalse(report["campaign"]["recommendation_allowed"])
        self.assertIn(
            "typed_attribution_evidence_incomplete",
            report["campaign"]["incomplete_reasons"],
        )
        self.assertTrue(
            any(
                "missing_or_invalid_eligible_quote_seconds" in reason
                for item in report["runs"]["diagnostic_only"]
                for reason in item["reasons"]
            )
        )

    def test_nonfinite_json_and_exact_decimal_arithmetic_fail_closed(self) -> None:
        deeply_nested = '{"x":' + "[" * 2000 + "0" + "]" * 2000 + "}"
        deep_path = self.root / "deep.json"
        deep_path.write_text(deeply_nested, encoding="utf-8")
        with self.assertRaises(CampaignValidationError):
            load_manifest(deep_path)
        infinity_path = self.root / "infinity.json"
        infinity_path.write_text(
            json.dumps(record("infinity", buy_entries=1)).replace(
                '"fill_price": "100"', '"fill_price": Infinity', 1
            ),
            encoding="utf-8",
        )
        rounded = record("rounded-arithmetic", buy_entries=1)
        rounded_episode = rounded["account_audit"]["completed_episode_ledger"][0]  # type: ignore[index]
        rounded_episode.update(
            entry_vwap="1",
            exit_vwap="10000000000000000000000000002",
            gross="10000000000000000000000000000",
            maker_fee="0",
            net_ex_funding="10000000000000000000000000000",
        )
        rounded["account_audit"].update(  # type: ignore[union-attr]
            completed_gross="10000000000000000000000000000",
            completed_exact_fee="0",
            completed_net_ex_funding="10000000000000000000000000000",
        )
        rounded_path = self._input("rounded-arithmetic.json", rounded)
        overflow = record("overflow", buy_entries=2)
        for item in overflow["account_audit"]["completed_episode_ledger"]:  # type: ignore[index]
            item["gross"] = "9e999999"
            item["net_ex_funding"] = "9e999999"
        overflow_path = self._input("overflow.json", overflow)

        report = analyze_campaign(
            manifest(
                [deep_path.name, infinity_path.name, rounded_path, overflow_path]
            ),
            base_dir=self.root,
        )

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertEqual(len(report["runs"]["diagnostic_only"]), 4)
        self.assertFalse(report["campaign"]["recommendation_allowed"])

    def test_historical_hard_counter_and_impossible_time_are_diagnostic(self) -> None:
        early = record("counter-reset", buy_entries=1)
        early["uptime_seconds"] = "10"
        early["eligible_quote_seconds"] = "10"
        early["counters"]["reconciliation_failure"] = 1  # type: ignore[index]
        final = record("counter-reset", buy_entries=1)
        reset_path = self._input("counter-reset.jsonl", [early, final])
        impossible = record("impossible-time", buy_entries=1)
        impossible["ended_at_utc"] = impossible["started_at_utc"]
        impossible["ended_at_utc"] = (
            str(impossible["ended_at_utc"])[:14] + "01:00Z"
        )
        impossible["uptime_seconds"] = "1800"
        impossible["eligible_quote_seconds"] = "3600"
        time_path = self._input("impossible-time.json", impossible)

        report = analyze_campaign(
            manifest([reset_path, time_path]), base_dir=self.root
        )
        reasons = {
            reason
            for item in report["runs"]["diagnostic_only"]
            for reason in item["reasons"]
        }

        self.assertTrue(
            any("snapshot_0_hard_counter_nonzero_or_invalid" in reason for reason in reasons)
        )
        self.assertTrue(
            any("eligible_quote_seconds_exceed_uptime" in reason for reason in reasons)
        )
        self.assertTrue(any("uptime_exceeds_utc_period" in reason for reason in reasons))

    def test_invalid_history_or_attribution_is_diagnostic_and_loss_is_debited(self) -> None:
        invalid_history = record("invalid-history", buy_entries=1)
        invalid_history["controller_decision_history_total"] = "1"
        invalid_history["controller_decision_history"] = [
            {
                "event_sequence_run_id": "invalid-history",
                "event_sequence": 1,
            }
        ]
        analyzer_error_loss = record(
            "analyzer-error-loss",
            buy_entries=1,
            flat_change="-0.50",
        )
        analyzer_error_loss["controller_decision_history_total"] = []
        invalid_attribution = record("invalid-attribution", buy_entries=1)
        invalid_attribution["account_audit"]["authenticated_fill_attributions"][1][  # type: ignore[index]
            "binding_constraint"
        ] = []
        fake_maker_fill = record("fake-maker-fill", buy_entries=1)
        fake_maker_fill["controller_decision_history"].append(  # type: ignore[union-attr]
            {
                "event_sequence_run_id": "fake-maker-fill",
                "event_sequence": 999999,
                "event": "maker_fill",
            }
        )
        fake_maker_fill["controller_decision_history_total"] += 1  # type: ignore[operator]
        hidden_controller_error = record("hidden-controller-error", buy_entries=1)
        hidden_controller_error["controller_decision_history"].append(  # type: ignore[union-attr]
            {
                "event_sequence_run_id": "hidden-controller-error",
                "event_sequence": 2,
                "decision_id": 2,
                "ready": False,
                "entry_applicable": True,
                "error": "controller exploded",
                "bid": {"toxicity_score_ticks": None},
                "ask": {"toxicity_score_ticks": None},
            }
        )
        hidden_controller_error["controller_decision_history_total"] += 1  # type: ignore[operator]
        inputs = [
            self._input("invalid-history.json", invalid_history),
            self._input("analyzer-error-loss.json", analyzer_error_loss),
            self._input("invalid-attribution.json", invalid_attribution),
            self._input("fake-maker-fill.json", fake_maker_fill),
            self._input("hidden-controller-error.json", hidden_controller_error),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertEqual(len(report["runs"]["diagnostic_only"]), 5)
        self.assertEqual(
            report["risk"]["sum_session_realized_losses_usdg"],
            Decimal("0.50"),
        )
        self.assertIn("analyzer-error-loss", report["risk"]["accounted_run_ids"])
        self.assertFalse(report["campaign"]["recommendation_allowed"])

    def test_controller_entry_must_bind_to_placement_decision(self) -> None:
        value = record("controller-binding", sell_entries=1)
        value["fill_markouts"][0]["quote_context"]["decision_id"] = 999  # type: ignore[index]
        value["account_audit"]["authenticated_fill_attributions"][0][  # type: ignore[index]
            "controller_decision_id"
        ] = 999
        not_applicable = record("controller-not-applicable", sell_entries=1)
        not_applicable["controller_decision_history"][0].update(  # type: ignore[index]
            ready=False,
            entry_applicable=False,
        )
        wrong_mode = record("controller-wrong-mode", sell_entries=1)
        wrong_mode["controller_decision_history"][0]["mode"] = "fixed"  # type: ignore[index]
        wrong_mode["controller_decision_history"][0]["ask"]["blocked"] = True  # type: ignore[index]
        wrong_mode["fill_markouts"][0]["quote_context"][  # type: ignore[index]
            "controller_mode"
        ] = "fixed"
        inputs = [
            self._input("controller-binding.json", value),
            self._input("controller-not-applicable.json", not_applicable),
            self._input("controller-wrong-mode.json", wrong_mode),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )
        self.assertFalse(report["campaign"]["recommendation_allowed"])

    def test_episode_quantity_and_account_aggregate_must_conserve(self) -> None:
        quantity = record("quantity-mismatch", buy_entries=1)
        quantity_episode = quantity["account_audit"]["completed_episode_ledger"][0]  # type: ignore[index]
        quantity_episode.update(
            quantity="2",
            gross="0.44",
            net_ex_funding="0.42",
        )
        quantity["account_audit"].update(  # type: ignore[union-attr]
            completed_gross="0.44",
            completed_exact_fee="0.02",
            completed_net_ex_funding="0.42",
        )
        aggregate = record("aggregate-mismatch", buy_entries=1)
        aggregate["account_audit"].update(  # type: ignore[union-attr]
            completed_gross="0.30",
            completed_exact_fee="0.10",
            completed_net_ex_funding="0.20",
        )
        inputs = [
            self._input("quantity-mismatch.json", quantity),
            self._input("aggregate-mismatch.json", aggregate),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertEqual(report["runs"]["included_count"], 0)
        self.assertTrue(
            all(
                "completed_episode_aggregate_incomplete_or_inconsistent"
                in item["reasons"]
                for item in report["runs"]["diagnostic_only"]
            )
        )

    def test_episode_chain_cannot_switch_entry_order_or_reopen_after_flat(self) -> None:
        switched = record("switched-entry", buy_entries=1)
        entry = switched["account_audit"]["authenticated_fill_attributions"][0]  # type: ignore[index]
        entry["next_position"] = "0.5"
        switched["fill_markouts"][0]["fill_amount"] = "0.5"  # type: ignore[index]
        second = dict(entry)
        second.update(
            trade_id="switched-entry-second-trade",
            order_id="switched-entry-second-order",
            prior_position="0.5",
            next_position="1",
        )
        switched["account_audit"]["authenticated_fill_attributions"].insert(1, second)  # type: ignore[index]
        second_event = dict(switched["fill_markouts"][0])  # type: ignore[index]
        second_event.update(
            order_id=second["order_id"],
            fill_amount="0.5",
            fill_observation_event_sequence=999,
        )
        second_event["quote_context"] = dict(second_event["quote_context"])
        second_event["quote_context"]["order_id"] = second["order_id"]
        switched["fill_markouts"].append(second_event)  # type: ignore[union-attr]
        switched["fill_markout_coverage"].update(  # type: ignore[union-attr]
            retained_events=3, observed_event_total=3
        )
        switched["account_audit"]["unique_maker_fills"] = 3  # type: ignore[index]

        reopened = record("reopened-episode", buy_entries=2)
        for attribution in reopened["account_audit"]["authenticated_fill_attributions"][2:]:  # type: ignore[index]
            attribution["episode_sequence"] = 1
        for event in reopened["fill_markouts"][2:]:  # type: ignore[index]
            event["episode_sequence"] = 1
            event["attribution_signature"]["episode_sequence"] = 1
        reopened["account_audit"]["completed_episode_ledger"][1]["episode_sequence"] = 1  # type: ignore[index]
        inputs = [
            self._input("switched-entry.json", switched),
            self._input("reopened-episode.json", reopened),
        ]

        report = analyze_campaign(manifest(inputs), base_dir=self.root)

        self.assertFalse(report["campaign"]["recommendation_allowed"])
        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )

    def test_raw_markout_and_per_order_quote_context_cannot_drift(self) -> None:
        early = record("raw-drift", buy_entries=1)
        early["uptime_seconds"] = "10"
        early["eligible_quote_seconds"] = "10"
        final = record("raw-drift", buy_entries=1)
        early["fill_markouts"][0]["external_mid_markout_5s_bps"] = "1"  # type: ignore[index]
        final["fill_markouts"][0]["external_mid_markout_5s_bps"] = "2"  # type: ignore[index]
        raw_path = self._input("raw-drift.jsonl", [early, final])

        context = record("context-drift", buy_entries=1)
        entry = context["account_audit"]["authenticated_fill_attributions"][0]  # type: ignore[index]
        entry["next_position"] = "0.5"
        second = dict(entry)
        second.update(
            trade_id="context-second-trade",
            prior_position="0.5",
            next_position="1",
        )
        context["account_audit"]["authenticated_fill_attributions"].insert(1, second)  # type: ignore[index]
        context["fill_markouts"][0]["fill_amount"] = "0.5"  # type: ignore[index]
        second_event = dict(context["fill_markouts"][0])  # type: ignore[index]
        second_event["fill_observation_event_sequence"] = 999
        second_event["quote_context"] = dict(second_event["quote_context"])
        second_event["quote_context"]["toxicity_score_ticks"] = "3"
        context["fill_markouts"].append(second_event)  # type: ignore[union-attr]
        context["fill_markout_coverage"].update(  # type: ignore[union-attr]
            retained_events=3, observed_event_total=3
        )
        context["account_audit"]["unique_maker_fills"] = 3  # type: ignore[index]
        context_path = self._input("context-drift.json", context)

        report = analyze_campaign(
            manifest([raw_path, context_path]), base_dir=self.root
        )

        self.assertFalse(report["campaign"]["recommendation_allowed"])
        self.assertTrue(
            any(
                reason.startswith("conflicting_raw_fill_event:")
                for item in report["runs"]["diagnostic_only"]
                for reason in item["reasons"]
            )
        )
        self.assertFalse(
            report["calibration_coverage"]["typed_attribution_evidence_complete"]
        )

    def test_direct_cli_is_local_read_only_and_imports_from_repo_root(self) -> None:
        run_name = self._input("cli-run.json", record("cli", buy_entries=1))
        manifest_path = self.root / "manifest.json"
        write_json(manifest_path, manifest([run_name]))
        before = sorted(path.name for path in self.root.iterdir())
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

        completed = subprocess.run(
            [sys.executable, str(SCRIPT), str(manifest_path)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["analysis_contract"]["local_read_only"])
        self.assertFalse(report["analysis_contract"]["network_access"])
        self.assertFalse(report["analysis_contract"]["exchange_mutation"])
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))

    def test_each_input_is_read_once_for_hash_and_parse(self) -> None:
        path = self._input("read-once.json", record("read-once", buy_entries=1))
        original = Path.read_bytes

        with patch.object(
            Path,
            "read_bytes",
            autospec=True,
            side_effect=lambda target: original(target),
        ) as read_bytes:
            analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(read_bytes.call_count, 1)

    def test_current_metrics_snapshot_with_evidence_envelope_is_accepted(self) -> None:
        metrics = MarketMakerMetrics(0.0)
        run_id = metrics.snapshot(0.0)["event_sequence_run_id"]
        metrics.signed_position = Decimal("0")
        metrics.eligible_quote_seconds = 3600.0
        metrics.record_quote_context(
            "real-order",
            {
                "order_id": "real-order",
                "side": "buy",
                "toxicity_score_ticks": Decimal("1.5"),
            },
        )
        self.assertTrue(
            metrics.record_maker_fill_markout(
                order_id="real-order",
                side="buy",
                cumulative_filled=Decimal("0.0002"),
                cumulative_cost=Decimal("20"),
                average_price=Decimal("100000"),
                now=1.0,
                mid=Decimal("100000"),
                external_mid=Decimal("100000"),
                source="websocket_order_update",
                terminal=True,
            )
        )
        self.assertTrue(
            metrics.record_maker_fill_markout(
                order_id="real-exit-order",
                side="sell",
                cumulative_filled=Decimal("0.0002"),
                cumulative_cost=Decimal("20.002"),
                average_price=Decimal("100010"),
                now=2.0,
                mid=Decimal("100010"),
                external_mid=Decimal("100010"),
                source="reconciliation",
                terminal=True,
            )
        )
        metrics.update_fill_markouts(
            now=17.0,
            mid=Decimal("100010"),
            external_mid=Decimal("100010"),
        )
        attribution = {
            "trade_id": "real-trade",
            "order_id": "real-order",
            "side": "buy",
            "role": "entry",
            "episode_sequence": 1,
            "prior_position": Decimal("0"),
            "next_position": Decimal("0.0002"),
            "exchange_timestamp": 1,
            "active_unwind": False,
            "position_flip": False,
            "intent_kind": "base_entry",
        }
        exit_attribution = {
            "trade_id": "real-exit-trade",
            "order_id": "real-exit-order",
            "side": "sell",
            "role": "passive_exit",
            "episode_sequence": 1,
            "prior_position": Decimal("0.0002"),
            "next_position": Decimal("0"),
            "exchange_timestamp": 2,
            "active_unwind": False,
            "position_flip": False,
            "intent_kind": "passive_exit",
            "policy_decision_id": 1,
            "exit_stage": "strict_profit",
            "binding_constraint": "normal_passive",
        }
        metrics.apply_authenticated_fill_attributions(
            [attribution, exit_attribution]
        )
        metrics.controller_decision_history = [
            {
                "event_sequence_run_id": run_id,
                "event_sequence": 3,
                "decision_id": 1,
                "bid": {"toxicity_score_ticks": Decimal("1.5")},
                "ask": {"toxicity_score_ticks": Decimal("1.5")},
            }
        ]
        metrics.controller_decision_history_total = 1
        metrics.record_account_audit(
            {
                "state": "healthy",
                "reason": None,
                "economic_state": "collecting",
                "total_read_failures": 0,
                "last_audit_authenticated": True,
                "audited_position": Decimal("0"),
                "ledger_position": Decimal("0"),
                "baseline_equity": Decimal("100"),
                "current_equity": Decimal("100.01"),
                "current_drawdown": Decimal("0"),
                "unique_maker_fills": 2,
                "unique_taker_fills": 0,
                "maker_turnover": Decimal("40.002"),
                "completed_net_ex_funding": Decimal("0.001"),
                "completed_gross": Decimal("0.002"),
                "completed_exact_fee": Decimal("0.001"),
                "completed_round_trips": 1,
                "episode_flat_success": 1,
                "flat_equity_change": Decimal("0.01"),
                "max_observed_drawdown": Decimal("0.01"),
                "policy_context_missing_count": 0,
                "authenticated_fill_attributions": [
                    attribution,
                    exit_attribution,
                ],
                "completed_episode_ledger": [
                    episode(
                        "real-session",
                        entry_vwap="100000",
                        exit_vwap="100010",
                        quantity="0.0002",
                        gross="0.002",
                        maker_fee="0.001",
                        net_ex_funding="0.001",
                    )
                ],
            }
        )
        snapshot = metrics.snapshot(3600.0)
        snapshot.update(
            {
                "campaign_id": "toxicity-cal-001",
                "candidate_id": "candidate-v1",
                "commit_sha": "a" * 40,
                "semantic_config_sha256": "b" * 64,
                "controller_profile_id": "profile-v1",
                "symbol": "BTC",
                "maker_fee_rate": "0.000120",
                "taker_fee_rate": "0.000350",
                "started_at_utc": "2026-09-03T00:00:00Z",
                "ended_at_utc": "2026-09-03T01:00:00Z",
                "authenticated_open_orders": 0,
                "preflight": {
                    "authenticated": True,
                    "position": "0",
                    "open_orders": 0,
                },
                "postflight": {
                    "authenticated": True,
                    "position": "0",
                    "open_orders": 0,
                },
            }
        )
        path = self._input("current-schema.json", snapshot)

        report = analyze_campaign(manifest([path]), base_dir=self.root)

        self.assertEqual(report["runs"]["included_run_ids"], [run_id])
        self.assertEqual(report["denominator"]["authenticated_buy_entries"], 1)
        self.assertEqual(report["denominator"]["excluded_passive_exits"], 1)
        self.assertEqual(report["runs"]["diagnostic_only"], [])


if __name__ == "__main__":
    unittest.main()

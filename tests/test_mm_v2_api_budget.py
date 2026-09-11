"""Offline request-boundary tests; these are not live wire-traffic measurements."""

import unittest
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.frames import Frame, Opcode
from websockets.protocol import Protocol, Side, State

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_read_stream import LighterReadStream
from core.adapters.exchanges.adapters.lighter_rest import LighterRest
from core.services.market_maker_v2.api_budget import ApiBudget, ApiBudgetUnavailable
from test_lighter_read_stream import Socket


ZERO = {"rest": 0, "ws": 0, "tx": 0}


class ApiBudgetTests(unittest.TestCase):
    def test_flat_read_preserves_exact_final_proof_rest_and_ws_boundaries(self):
        for boundary, bucket, projected, limit in (
            ({"rest": 19600, "ws": 0, "tx": 0}, "rest", 24001, 24000),
            ({"rest": 0, "ws": 185, "tx": 0}, "ws", 201, 200),
        ):
            with self.subTest(bucket=bucket):
                budget = ApiBudget(lambda: 100.0)
                budget.require_flat_read(boundary, operation="funding_proof_read")
                with self.assertRaises(ApiBudgetUnavailable) as caught:
                    budget.require_flat_read(boundary | {bucket: boundary[bucket] + 1},
                                             operation="funding_proof_read")
                error = caught.exception
                self.assertEqual(error.budget_diagnostic, {
                    "operation": "funding_proof_read", "blocking_bucket": bucket,
                    "blocking_offset_seconds": "0", "projected_usage": projected, "limit": limit})
                self.assertEqual(error.diagnostic_values["api_next_" + bucket], Decimal(boundary[bucket] + 1))
                self.assertEqual(budget.snapshot()["recent_admission_denials"], [
                    {"observed_monotonic": 100.0, **error.budget_diagnostic}])
                self.assertEqual(budget.snapshot()["deferrals"], 0)

    def test_flat_read_counts_observed_requests_and_cannot_spend_final_audit(self):
        budget = ApiBudget(lambda: 100.0)
        for _ in range(98):
            budget.observe("rest", "accountInactiveOrders")
        # The prior exit has already proven flat. Ordinary admission would
        # reserve another full IOC exit and refuse this read at a future step.
        with self.assertRaises(ApiBudgetUnavailable):
            budget.require_normal({"rest": 300, "ws": 5, "tx": 0})
        budget.require_flat_read({"rest": 300, "ws": 5, "tx": 0})
        while budget.snapshot()["used"]["rest"] < 19400:
            budget.require_flat_read({"rest": 300, "ws": 0, "tx": 0})
            budget.observe("rest", "account")
        self.assertEqual(budget.snapshot()["used"]["rest"], 19400)
        with self.assertRaises(ApiBudgetUnavailable):
            budget.require_flat_read({"rest": 300, "ws": 0, "tx": 0})
        self.assertEqual(budget.snapshot()["used"]["rest"] + 4400, 23800)
        for _ in range(185):
            budget.observe("ws", 1)
        with self.assertRaises(ApiBudgetUnavailable) as caught:
            budget.require_flat_read({"rest": 0, "ws": 1, "tx": 0})
        self.assertEqual(caught.exception.blocking_bucket, "ws")

    def test_flat_read_rejects_mutations_incomplete_costs_and_unbounded_labels(self):
        budget = ApiBudget(lambda: 1.0)
        for cost in (ZERO | {"tx": 1}, {"rest": 0}, ZERO | {"other": 0},
                     ZERO | {"rest": -1}, ZERO | {"ws": False}, ZERO | {"tx": 0.0}):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                budget.require_flat_read(cost)
        for operation in ("", "flat?auth=sensitive-test-value", "A" * 65, None):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                budget.require_flat_read(ZERO, operation=operation)
        self.assertNotIn("sensitive-test-value", repr(budget.snapshot()))
        self.assertEqual(budget.snapshot()["attempts"], {})

    def test_account_read_exit_attribution_separates_proof_failure_classes(self):
        from core.services.market_maker_v2.lighter_runtime import AccountReadRace, _AccountCashRace, UnattributedCashflow

        budget = ApiBudget(lambda: 1.0)
        cases = (
            (AccountReadRace, "exact terminal order proof unavailable", "terminal_order_history"),
            (AccountReadRace, "terminal fills not reflected in account ledger", "terminal_fill_history"),
            (AccountReadRace, "account changed during stream/REST bracket", "stream_rest_bracket"),
            (AccountReadRace, "account trade count and history disagree", "trade_counter_history"),
            (AccountReadRace, "account history exceeds activity counter", "history_ahead_of_counter"),
            (AccountReadRace, "active order fills not reflected in account history", "active_fill_history"),
            (AccountReadRace, "account fills and position disagree", "fill_position"),
            (_AccountCashRace, "new fill exceeds observed fee terms", "fill_fee_terms"),
            (_AccountCashRace, "unattributed account cashflow or equity mismatch", "cash_equity"),
            (UnattributedCashflow, "unattributed account cashflow or equity mismatch", "cash_equity"),
        )
        for number, (kind, message, subreason) in enumerate(cases, 1):
            budget.record_account_read_exit(phase="authorizing_quotes", exit_id=f"exit-{number}",
                error=kind(message, values={"provider_payload": "sensitive-test-value"}))
            row = budget.snapshot()["recent_account_read_exits"][-1]
            self.assertEqual(row, {
                "number": number, "observed_monotonic": 1.0,
                "reason": "account_cash_conflict" if issubclass(kind, _AccountCashRace) else "account_read_race",
                "subreason": subreason, "phase": "authorizing_quotes", "exit_id": f"exit-{number}",
            })
        self.assertEqual(budget.snapshot()["deferrals"], 0)
        self.assertEqual(budget.snapshot()["recent_backpressure_exits"], [])
        self.assertNotIn("sensitive-test-value", repr(budget.snapshot()))

    def test_account_read_exit_attribution_is_bounded_and_never_logs_unknown_payloads(self):
        import json
        from core.services.market_maker_v2.lighter_runtime import AccountReadRace

        now = [1.0]
        budget = ApiBudget(lambda: now[0])
        for number in range(70):
            now[0] += 1
            budget.record_account_read_exit(phase="reconciling_quotes", exit_id=f"exit-{number + 1}",
                error=AccountReadRace("provider auth=sensitive-test-value"))
        result = budget.snapshot()
        self.assertEqual(result["account_read_deferrals"], 70)
        self.assertEqual(len(result["recent_account_read_exits"]), 64)
        self.assertEqual(result["recent_account_read_exits"][0]["number"], 7)
        self.assertTrue(all(row["subreason"] == "unclassified_account_read_race"
                            for row in result["recent_account_read_exits"]))
        self.assertNotIn("sensitive-test-value", json.dumps(result, allow_nan=False))
        for overrides in ({"phase": "provider auth=sensitive-test-value"},
                          {"exit_id": "exit-sensitive-test-value"},
                          {"exit_id": "exit-"}, {"exit_id": None},
                          {"error": ValueError("provider auth=sensitive-test-value")}):
            arguments = {"phase": "syncing_orders", "exit_id": "exit-71",
                         "error": AccountReadRace("account fills and position disagree")} | overrides
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                budget.record_account_read_exit(**arguments)
        self.assertEqual(budget.snapshot()["account_read_deferrals"], 70)

    def test_phase_costs_count_actual_rest_attempts_and_preserve_local_limits(self):
        phase = ["startup"]
        budget = ApiBudget(lambda: 1.0, work_phase=lambda: phase[0])
        budget.observe("rest", "account")
        phase[0] = "normal"
        budget.observe("rest", "trades")
        budget.observe("rest", "sendTx")
        budget.observe("ws", 1)
        phase[0] = "exit"
        budget.observe("rest", "accountInactiveOrders")
        result = budget.snapshot()
        self.assertEqual(result["rest_weight_by_phase"], {"startup": 300, "normal": 600, "exit": 100})
        self.assertEqual(sum(result["rest_weight_by_phase"].values()), result["used"]["rest"])
        self.assertEqual(result["limits"], {"rest": 24000, "ws": 200, "tx": 40})
        self.assertIsNone(result["account_profile"])

    def test_scheduled_exit_distinguishes_uniform_and_burst_expirations(self):
        def usage(times):
            now = [0.0]
            budget = ApiBudget(lambda: now[0])
            for index, timestamp in enumerate(times):
                now[0] = timestamp
                if index == 45:
                    continue  # Leave 300 weight for the exact funding round read.
                if index == 46:
                    for _ in range(2):
                        budget.observe("rest", "https://example.invalid/api/v1/accountInactiveOrders")
                else:
                    budget.observe("rest", "https://example.invalid/api/v1/account")
            now[0] = 60.0
            self.assertEqual(budget.snapshot()["used"]["rest"], 13700)
            return budget
        uniform = usage([1 + 58 * index / 46 for index in range(47)])
        expiring = usage([1.0] * 47)
        recent = usage([59.0] * 47)
        self.assertTrue(uniform.scheduled_live_available(ZERO))
        self.assertTrue(expiring.scheduled_live_available(ZERO))
        self.assertFalse(recent.scheduled_live_available(ZERO))
        # The peak at16s includes both the forced funding and coherent retry.
        self.assertTrue(uniform.scheduled_live_available(ZERO | {"rest": 94}))
        self.assertFalse(uniform.scheduled_live_available(ZERO | {"rest": 95}))
        with self.assertRaisesRegex(ApiBudgetUnavailable, "^API capacity reserved for bounded exit$"):
            uniform.require_normal(ZERO | {"rest": 300})
        self.assertIsNone(expiring.require_normal(ZERO | {"rest": 300}))

    def test_scheduled_exit_never_spends_future_expiry_credit_at_or_before_a_step(self):
        def burst(timestamp, accounts, histories=0):
            now = [timestamp]
            budget = ApiBudget(lambda: now[0])
            for endpoint, count in (("account", accounts), ("accountInactiveOrders", histories)):
                for _ in range(count):
                    budget.observe("rest", "https://example.invalid/api/v1/" + endpoint)
            now[0] = 60.0
            return budget
        # A request expiring soon remains charged at t=0.
        self.assertFalse(burst(0.5, 52).scheduled_live_available(ZERO))
        # 15100+8806 fits before t=.5. The new100-weight prefix at t=.5
        # fits only if that burst has expired by that exact boundary.
        self.assertTrue(burst(0.5, 50, 1).scheduled_live_available(ZERO))
        self.assertFalse(burst(0.500000001, 50, 1).scheduled_live_available(ZERO))

    def test_scheduled_exit_reserves_ws_tx_and_validates_all_cost_domains(self):
        budget = ApiBudget(lambda: 1.0)
        self.assertTrue(budget.scheduled_live_available({"rest": 8294, "ws": 133, "tx": 35}))
        for cost in ({"rest": 8295, "ws": 133, "tx": 35},
                     {"rest": 8294, "ws": 134, "tx": 35},
                     {"rest": 8294, "ws": 133, "tx": 36}):
            self.assertFalse(budget.scheduled_live_available(cost))
        for cost in ({"rest": 0}, ZERO | {"other": 0}, ZERO | {"rest": -1},
                     ZERO | {"ws": False}, ZERO | {"tx": 1.0}):
            with self.assertRaises(ValueError):
                budget.scheduled_live_available(cost)
        with self.assertRaisesRegex(RuntimeError, "unclassified API accounting endpoint"):
            budget.observe("rest", "https://example.invalid/api/v1/tokens/create?auth=test-secret")
        self.assertEqual(budget.snapshot()["attempts"], {})

    def test_refusal_diagnostic_keeps_only_numeric_usage_and_next_cost(self):
        from decimal import Decimal
        from core.services.market_maker_v2.telemetry import failure_diagnostic
        budget = ApiBudget(lambda: 1.0)
        for _ in range(130):
            budget.observe("ws", 1)
        with self.assertRaises(ApiBudgetUnavailable) as caught:
            budget.require_normal({"rest": 1900, "ws": 5, "tx": 0})
        diagnostic = failure_diagnostic("BTC", "authorizing_quotes", caught.exception)
        values = {row.name: row.value for row in diagnostic.values}
        self.assertEqual(values, {"api_used_rest": Decimal(0), "api_used_ws": Decimal(130),
            "api_used_tx": Decimal(0), "api_next_rest": Decimal(1900),
            "api_next_ws": Decimal(5), "api_next_tx": Decimal(0)})

    def test_refusal_identifies_first_bucket_or_future_reserve_checkpoint(self):
        for normal, bucket, offset, projected, limit in (
            (ZERO | {"rest": 15195, "ws": 134}, "rest", "0", 24001, 24000),
            (ZERO | {"ws": 134, "tx": 36}, "ws", "0", 201, 200),
            (ZERO | {"tx": 36}, "tx", "0", 41, 40),
            (ZERO | {"rest": 8295}, "rest", "32", 24001, 24000),
        ):
            with self.subTest(normal=normal):
                budget = ApiBudget(lambda: 1.0)
                self.assertFalse(budget.scheduled_live_available(normal))
                with self.assertRaises(ApiBudgetUnavailable) as caught:
                    budget.require_normal(normal, operation="create")
                error = caught.exception
                self.assertEqual((error.operation, error.blocking_bucket,
                                  error.blocking_offset_seconds, error.projected_usage, error.limit),
                                 ("create", bucket, Decimal(offset), projected, limit))
                self.assertEqual(budget.snapshot()["recent_admission_denials"], [
                    {"observed_monotonic": 1.0, **error.budget_diagnostic}])
                budget.record_backpressure_exit(phase="authorizing_quotes", exit_id="exit-1", error=error)
                row = budget.snapshot()["recent_backpressure_exits"][-1]
                for key, value in error.budget_diagnostic.items():
                    self.assertEqual(row[key], value)

    def test_future_refusal_reports_exact_boundary_without_crediting_later_expiry(self):
        now = [0.500000001]
        budget = ApiBudget(lambda: now[0])
        for _ in range(50):
            budget.observe("rest", "account")
        budget.observe("rest", "accountInactiveOrders")
        now[0] = 60.0
        with self.assertRaises(ApiBudgetUnavailable) as caught:
            budget.require_normal(ZERO, operation="optional_reprice")
        error = caught.exception
        self.assertEqual(error.blocking_offset_seconds, Decimal("0.5"))
        self.assertEqual(error.projected_usage, 24006)
        # Formatting diagnostics cannot round a binary-clock boundary through float.
        offset = Fraction.from_float(0.500000001)
        exact = ApiBudgetUnavailable("diagnostic", blocker=("rest", offset, 24006, 24000))
        self.assertEqual(Fraction(exact.blocking_offset_seconds), offset)

    def test_admission_denials_are_bounded_serializable_and_separate_from_exits(self):
        import json
        budget = ApiBudget(lambda: 1.0)
        for _ in range(70):
            with self.assertRaises(ApiBudgetUnavailable):
                budget.require_normal(ZERO | {"tx": 36}, operation="create")
        self.assertIsNone(budget.require_normal(ZERO))
        result = budget.snapshot()
        self.assertEqual(len(result["recent_admission_denials"]), 64)
        self.assertEqual(result["deferrals"], 0)
        self.assertEqual(result["recent_backpressure_exits"], [])
        json.dumps(result, allow_nan=False)
        for invalid in ("", "create?auth=secret", "A" * 65, None):
            with self.subTest(operation=invalid), self.assertRaises(ValueError):
                budget.require_normal(ZERO, operation=invalid)
        self.assertNotIn("secret", repr(budget.snapshot()))

    def test_weights_attempts_expiration_and_query_redaction(self):
        now = [1.0]
        budget = ApiBudget(lambda: now[0])
        for endpoint in ("account", "trades", "recentTrades", "accountInactiveOrders",
                         "nextNonce", "apikeys", "sendTx", "sendTxBatch"):
            budget.observe("rest", "https://example.invalid/api/v1/" + endpoint + "?auth=test-secret")
        for opcode in (0, 1, 2, 8, 9, 10):
            budget.observe("ws", opcode)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["used"], {"rest": 1756, "tx": 2, "ws": 6})
        self.assertEqual(snapshot["scope"], "owned_python_transports")
        self.assertNotIn("test-secret", repr(snapshot))
        now[0] = 60.999
        self.assertEqual(budget.snapshot()["used"], snapshot["used"])
        now[0] = 61.0
        expired = budget.snapshot()
        self.assertTrue(all(value == 0 for value in expired["used"].values()))
        self.assertEqual(expired["peaks"], snapshot["peaks"])
        self.assertEqual(expired["attempts"], snapshot["attempts"])

    def test_admission_reserves_every_bucket_and_rejects_invalid_cost_bounds(self):
        budget = ApiBudget(lambda: 1.0)
        self.assertTrue(budget.available(normal=ZERO, reserve=budget.LIMITS))
        budget.observe("rest", "https://example.invalid/api/v1/nextNonce")
        self.assertFalse(budget.available(normal=ZERO, reserve=budget.LIMITS))
        self.assertTrue(budget.available(normal=ZERO | {"rest": 23994}, reserve=ZERO))
        self.assertFalse(budget.available(normal=ZERO | {"rest": 23995}, reserve=ZERO))
        for costs in ({"rest": 0, "ws": 0}, ZERO | {"unknown": 0}, ZERO | {"tx": -1},
                      ZERO | {"tx": 0.0}, ZERO | {"tx": False}):
            with self.subTest(costs=costs):
                with self.assertRaises(ValueError):
                    budget.available(normal=costs, reserve=ZERO)
                with self.assertRaises(ValueError):
                    budget.available(normal=ZERO, reserve=costs)
        for _ in range(201):
            budget.observe("ws", 1)
        self.assertFalse(budget.available(normal=ZERO, reserve=ZERO))
        self.assertEqual(budget.snapshot()["peaks"]["ws"], 201)

    def test_invalid_transport_and_clock_discontinuity_cannot_produce_headroom(self):
        now = [42.0]
        budget = ApiBudget(lambda: now[0])
        budget.observe("ws", 1)
        for transport, target in (("http", "/account"), ("ws", True), ("ws", "1"), ("ws", 3)):
            with self.subTest(transport=transport, target=target):
                with self.assertRaises(RuntimeError):
                    budget.observe(transport, target)
        for value in (41.0, float("inf"), float("nan")):
            now[0] = value
            with self.assertRaises(RuntimeError):
                budget.available(normal=ZERO, reserve=ZERO)
        now[0] = 42.0
        self.assertEqual(budget.snapshot()["attempts"], {"ws:1": 1})


class OwnedRequestObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_mm_cancel_bound_covers_four_terminal_reads_and_nonce_refresh(self):
        from lighter.signer_client import SignerClient
        from lighter.nonce_manager import OptimisticNonceManager
        from lighter.exceptions import BadRequestException
        for invalid_nonce in (False, True):
            with self.subTest(invalid_nonce=invalid_nonce):
                budget = ApiBudget(lambda: 1.0)
                transport = SimpleNamespace(request=AsyncMock())
                client = SimpleNamespace(rest_client=transport)
                signer = object.__new__(SignerClient)
                signer.api_client = client
                signer.nonce_manager = OptimisticNonceManager(7, client, [0])
                signer.sign_cancel_order = Mock(return_value=(15, "{}", "test-tx", None))
                async def next_nonce(**kwargs):
                    await transport.request("GET", "https://example.invalid/api/v1/nextNonce")
                    return SimpleNamespace(nonce=123)
                async def send_tx(**kwargs):
                    await transport.request("POST", "https://example.invalid/api/v1/sendTx")
                    if invalid_nonce:
                        raise BadRequestException(status=400, reason="invalid nonce")
                    return SimpleNamespace(code=200, tx_hash="test-tx")
                signer.tx_api = SimpleNamespace(send_tx=AsyncMock(side_effect=send_tx))
                rest = object.__new__(LighterRest)
                rest.signer_client = signer
                rest.get_market_index = Mock(return_value=1)
                rest.enable_terminal_cancellation_outcomes()
                rest.get_open_orders = AsyncMock()
                async def history(*args, **kwargs):
                    await transport.request("GET", "https://example.invalid/api/v1/accountInactiveOrders")
                    return []
                rest.get_order_history = AsyncMock(side_effect=history)
                adapter = object.__new__(LighterAdapter)
                adapter._rest, adapter._connected = rest, False
                adapter.set_market_maker_request_observer(budget.observe, enforce_admission=True)
                with patch("lighter.nonce_manager.TransactionApi", return_value=SimpleNamespace(next_nonce=next_nonce)), \
                     patch("lighter.signer_client.CancelOrder.from_json", return_value=object()), \
                     patch("core.adapters.exchanges.adapters.lighter_rest.asyncio.sleep", new=AsyncMock()):
                    self.assertFalse(await rest.cancel_order("BTC", "987"))
                counts = budget.snapshot()["attempts"]
                self.assertEqual(counts["rest:sendTx"], 1)
                self.assertEqual(counts["rest:nextNonce"], 2 if invalid_nonce else 1)
                self.assertEqual(counts.get("rest:accountInactiveOrders", 0), 0 if invalid_nonce else 4)
                self.assertLessEqual(budget.snapshot()["used"]["rest"], 412)
                self.assertEqual(budget.snapshot()["used"]["tx"], 1)
                self.assertNotIn("ws", budget.snapshot()["used"])
                rest.get_open_orders.assert_not_awaited()

    def test_normal_cancel_admission_counts_selected_sides_and_skips_exit(self):
        from core.services.market_maker_v2.orchestrator import VolumeSession
        session = object.__new__(VolumeSession)
        session._funding_proof_context = None
        session._budget_active, session._budget_exiting = True, False
        session.api_budget = SimpleNamespace(require_normal=Mock())
        session.account = SimpleNamespace(normal_terms_refresh_cost=Mock(return_value=0))
        session._admit_cancel(0)
        session.api_budget.require_normal.assert_not_called()
        session._admit_cancel(1)
        session.api_budget.require_normal.assert_called_once_with({"rest": 412, "ws": 0, "tx": 1}, operation="cancel")
        session._admit_cancel(2)
        session.api_budget.require_normal.assert_called_with({"rest": 824, "ws": 0, "tx": 2}, operation="cancel")
        session._admit_mutation()
        session.api_budget.require_normal.assert_called_with({"rest": 2600, "ws": 13, "tx": 2}, operation="create")
        session.api_budget.require_normal.reset_mock()
        session._budget_exiting = True
        session._admit_cancel(2)
        session.api_budget.require_normal.assert_not_called()
        for count in (-1, 3, True, 1.0):
            with self.subTest(count=count), self.assertRaises(ValueError):
                session._admit_cancel(count)

    def test_optional_revision_preflight_counts_only_selected_sides_and_due_terms(self):
        from core.services.market_maker_v2.orchestrator import VolumeSession
        session = object.__new__(VolumeSession)
        session._budget_active, session._budget_exiting = True, False
        session.api_budget = SimpleNamespace(require_normal=Mock())
        session.account = SimpleNamespace(normal_terms_refresh_cost=Mock(return_value=900))
        for count in (1, 2):
            with self.subTest(count=count):
                session._admit_optional_revision(count)
                session.api_budget.require_normal.assert_called_with(
                    {"rest": 412 * count + 1200 + 1400 + 1200 + 900, "ws": 18, "tx": count + 2},
                    operation="optional_reprice")
                session.account.normal_terms_refresh_cost.assert_called_with(15)
        session.account.normal_terms_refresh_cost.return_value = 0
        session._admit_optional_revision(1)
        session.api_budget.require_normal.assert_called_with(
            {"rest": 4212, "ws": 18, "tx": 3}, operation="optional_reprice")
        session._budget_exiting = True
        session.api_budget.require_normal.reset_mock()
        session.account.normal_terms_refresh_cost.reset_mock()
        session._admit_optional_revision(2)
        session.api_budget.require_normal.assert_not_called()
        session.account.normal_terms_refresh_cost.assert_not_called()
        for count in (-1, 0, 3, True, 1.0):
            with self.subTest(count=count), self.assertRaises(ValueError):
                session._admit_optional_revision(count)

    def test_create_cannot_spend_the_following_account_monitor_headroom(self):
        from core.services.market_maker_v2.orchestrator import VolumeSession
        session = object.__new__(VolumeSession)
        session._funding_proof_context = None
        session._budget_active, session._budget_exiting = True, False
        session.account = SimpleNamespace(normal_terms_refresh_cost=Mock(return_value=0))
        session.api_budget = ApiBudget(lambda: 100.0)
        for _ in range(10):
            session.api_budget.observe("rest", "trades")
        for _ in range(5):
            session.api_budget.observe("rest", "accountInactiveOrders")
        # The old create-only gate passes, but its upper-bound cost leaves
        # insufficient room for the next complete account monitor plus exit.
        session.api_budget.require_normal({"rest": 1400, "ws": 8, "tx": 2})
        with self.assertRaises(ApiBudgetUnavailable):
            session._admit_mutation()
        # Declining an optional create still leaves monitoring and mandatory
        # cancellation available; no risk/exit reserve is reduced to pass.
        session.api_budget.require_normal({"rest": 1200, "ws": 5, "tx": 0})
        session._admit_cancel(2)
        session.account.normal_terms_refresh_cost.assert_called_with(15)

    def test_optional_wait_requires_monitor_capacity_for_every_nonempty_account(self):
        from core.services.market_maker_v2.orchestrator import VolumeSession
        session = object.__new__(VolumeSession)
        session._budget_active, session._budget_exiting = True, False
        session._stop = SimpleNamespace(is_set=Mock(return_value=False))
        session.clock = SimpleNamespace(monotonic=lambda: 10.0)
        session.governor = SimpleNamespace(session_deadline_monotonic=100.0)
        session.api_budget = SimpleNamespace(require_normal=Mock())
        session.account = SimpleNamespace(stream=SimpleNamespace(transport_healthy=True),
            normal_terms_refresh_cost=Mock(return_value=900), has_complete_empty_order_proof=False)
        for position, orders in ((Decimal(".0002"), ()), (Decimal(0), ("known",))):
            authorization = SimpleNamespace(account=SimpleNamespace(position=position, open_order_ids=orders))
            with self.subTest(position=position, orders=orders):
                session._on_optional_refusal(authorization)
                session.api_budget.require_normal.assert_called_with(
                    {"rest": 2100, "ws": 5, "tx": 0}, operation="monitor")
                session.account.normal_terms_refresh_cost.assert_called_with(5)
        session.account.normal_terms_refresh_cost.return_value = 0
        session._on_optional_refusal(authorization)
        session.api_budget.require_normal.assert_called_with(
            {"rest": 1200, "ws": 5, "tx": 0}, operation="monitor")
        refusal = ApiBudgetUnavailable("required monitor unavailable")
        session.api_budget.require_normal.side_effect = refusal
        with self.assertRaises(ApiBudgetUnavailable) as caught:
            session._on_optional_refusal(authorization)
        self.assertIs(caught.exception, refusal)
        session.api_budget.require_normal.reset_mock()
        authorization.account.open_order_ids = ()
        session._on_optional_refusal(authorization)
        self.assertTrue(session._optional_flat_wait)
        session.api_budget.require_normal.assert_not_called()

    def test_proven_empty_order_monitor_retains_cash_and_terms_without_speculative_fills(self):
        from core.services.market_maker_v2.domain import ExecutionHealth
        from core.services.market_maker_v2.orchestrator import VolumeSession
        session = object.__new__(VolumeSession)
        session._funding_proof_context = None
        session._budget_active, session._budget_exiting = True, False
        session._stop = SimpleNamespace(is_set=lambda: False)
        session.clock = SimpleNamespace(monotonic=lambda: 100.0)
        session.governor = SimpleNamespace(session_deadline_monotonic=1000.0)
        session.account = SimpleNamespace(stream=SimpleNamespace(transport_healthy=True),
            normal_terms_refresh_cost=Mock(return_value=1200), has_complete_empty_order_proof=True)
        session.manager = SimpleNamespace(can_reconcile_known_orders=True, snapshot=Mock(return_value=()))
        snapshot = SimpleNamespace(health=ExecutionHealth.HEALTHY, orders=(), managed_order_count=0)
        session.execution = SimpleNamespace(snapshot=Mock(return_value=snapshot))
        account = SimpleNamespace(position=Decimal(".0006"), open_order_ids=(),
                                  authenticated=True, fresh=Mock(return_value=True))
        authorization = SimpleNamespace(account=account)
        session.api_budget = ApiBudget(lambda: 100.0)
        for _ in range(61):
            session.api_budget.observe("rest", "accountInactiveOrders")
        # Same 32s reserve blocker as the recorded monitor class. No limits or
        # reserve change: empty proven orders remove only impossible own fills.
        with self.assertRaises(ApiBudgetUnavailable):
            session.api_budget.require_normal({"rest": 2400, "ws": 5, "tx": 0}, operation="monitor")
        session._on_optional_refusal(authorization)
        self.assertEqual(session.api_budget.snapshot()["used"], {"rest": 6100})
        self.assertEqual(session.api_budget.deferrals, 0)
        session.account.normal_terms_refresh_cost.assert_called_with(5)
        # Future creates still need the full following-monitor budget.
        with self.assertRaises(ApiBudgetUnavailable):
            session._admit_mutation()
        session.account.normal_terms_refresh_cost.assert_called_with(15)
        for condition in ("proof", "stale", "unauthenticated", "account_orders", "manager_orders",
                          "uncertain", "execution_health", "execution_orders", "execution_count"):
            with self.subTest(condition=condition):
                session.account.has_complete_empty_order_proof = condition != "proof"
                account.fresh.return_value = condition != "stale"
                account.authenticated = condition != "unauthenticated"
                account.open_order_ids = ("known",) if condition == "account_orders" else ()
                session.manager.snapshot.return_value = (object(),) if condition == "manager_orders" else ()
                session.manager.can_reconcile_known_orders = condition != "uncertain"
                snapshot.health = ExecutionHealth.PAUSED_ORDER_STATE if condition == "execution_health" else ExecutionHealth.HEALTHY
                snapshot.orders = (object(),) if condition == "execution_orders" else ()
                snapshot.managed_order_count = 1 if condition == "execution_count" else 0
                with self.assertRaises(ApiBudgetUnavailable):
                    session._on_optional_refusal(authorization)

    def test_optional_wait_cannot_bypass_stop_deadline_or_unhealthy_monitor(self):
        from core.services.market_maker_v2.orchestrator import VolumeSession
        from core.services.market_maker_v2.execution_port import ExecutionUnavailable
        for condition in ("stop", "deadline", "exiting", "inactive", "unhealthy", "no_stream"):
            with self.subTest(condition=condition):
                session = object.__new__(VolumeSession)
                session._budget_active = condition != "inactive"
                session._budget_exiting = condition == "exiting"
                session._stop = SimpleNamespace(is_set=lambda: condition == "stop")
                session.clock = SimpleNamespace(monotonic=lambda: 10.0)
                session.governor = SimpleNamespace(session_deadline_monotonic=(
                    10.0 if condition == "deadline" else 100.0))
                session.api_budget = SimpleNamespace(require_normal=Mock())
                session.account = SimpleNamespace(stream=(None if condition == "no_stream"
                    else SimpleNamespace(transport_healthy=condition != "unhealthy")))
                authorization = SimpleNamespace(account=SimpleNamespace(position=Decimal(".0002"), open_order_ids=()))
                with self.assertRaises(ExecutionUnavailable):
                    session._on_optional_refusal(authorization)
                session.api_budget.require_normal.assert_not_called()

    async def test_empty_quote_plan_only_admits_existing_cancellations(self):
        from core.services.market_maker_v2.domain import QuotePlan
        from test_mm_v2_quote_execution import QuoteExecutionTests
        fixture = QuoteExecutionTests()
        fixture.setUp()
        fixture.port.before_cancel = Mock()
        fixture.port.before_mutation = Mock()
        await fixture.port.reconcile_quotes(QuotePlan("BTC"))
        fixture.port.before_cancel.assert_not_called()
        fixture.port.before_mutation.assert_not_called()
        await fixture.quote_both()
        fixture.port.before_mutation.reset_mock()
        await fixture.port.reconcile_quotes(QuotePlan("BTC"))
        fixture.port.before_cancel.assert_called_once_with(2)
        fixture.port.before_mutation.assert_not_called()

    async def test_selected_quote_revision_separately_admits_one_cancel_and_one_create(self):
        from dataclasses import replace
        from decimal import Decimal
        from core.services.market_maker_v2.domain import QuotePlan, Side
        from test_mm_v2_quote_execution import QuoteExecutionTests
        fixture = QuoteExecutionTests()
        fixture.setUp()
        await fixture.quote_both()
        before = {order.side: order for order in fixture.port.snapshot().orders}
        async def one_side(execution):
            value = await fixture.refresh_quote(execution)
            return replace(value, plan=QuotePlan("BTC", tuple(
                replace(quote, price=before[Side.BUY].price - Decimal("1"))
                if quote.side is Side.BUY else quote for quote in value.plan.quotes)))
        fixture.refresh.side_effect = one_side
        fixture.port.before_cancel, fixture.port.before_mutation = Mock(), Mock()
        result = await fixture.port.reconcile_quotes(fixture.proposal)
        self.assertEqual((result.cancelled_count, result.submitted_count), (1, 1))
        fixture.port.before_cancel.assert_called_once_with(1)
        fixture.port.before_mutation.assert_called_once_with()

    async def test_cached_execution_order_handoff_still_admits_following_terminal_reads(self):
        from core.services.market_maker_v2.lighter_runtime import LighterAccountPort
        from test_mm_v2_lighter_runtime import Adapter, Clock, ReadStream, ADDRESS

        adapter, clock = Adapter(), Clock()
        account = LighterAccountPort(adapter, "BTC", clock, account_index=7,
            expected_l1_address=ADDRESS, mutation_generation=lambda: 0)
        account.stream = ReadStream(adapter, clock)
        await account.snapshot()
        account.before_read = Mock(side_effect=ApiBudgetUnavailable("no headroom"))
        with self.assertRaises(ApiBudgetUnavailable):
            await account.read_execution_orders("BTC")
        account.before_read.assert_called_once()

    async def test_empty_normal_quote_plan_admits_cancellation_before_sending(self):
        from core.services.market_maker_v2.domain import QuotePlan
        from test_mm_v2_quote_execution import QuoteExecutionTests

        fixture = QuoteExecutionTests()
        fixture.setUp()
        await fixture.quote_both()
        fixture.adapter.cancel_order.reset_mock()
        fixture.port.before_mutation = Mock(side_effect=ApiBudgetUnavailable("no headroom"))
        try:
            await fixture.port.reconcile_quotes(QuotePlan("BTC"))
        except ApiBudgetUnavailable:
            pass
        fixture.port.before_mutation.assert_called_once()
        fixture.adapter.cancel_order.assert_not_awaited()
        self.assertEqual(len(fixture.open), 2)

    async def test_quota_refusal_after_terminal_cancel_keeps_fresh_cleanup_possible(self):
        from core.services.market_maker_v2.domain import ExecutionHealth, ExecutionStatus
        from test_mm_v2_quote_execution import QuoteExecutionTests

        fixture = QuoteExecutionTests()
        fixture.setUp()
        await fixture.quote_both()
        read_account = fixture.account.snapshot
        fixture.account.snapshot = AsyncMock(side_effect=ApiBudgetUnavailable("no headroom"))
        with self.assertRaises(ApiBudgetUnavailable):
            await fixture.port.cancel_all_managed()
        self.assertIs(fixture.port.snapshot().health, ExecutionHealth.HEALTHY)
        self.assertEqual(fixture.open, {})
        self.assertEqual(fixture.adapter.cancel_order.await_count, 2)
        fixture.account.snapshot = read_account
        proved = await fixture.port.cancel_all_managed()
        self.assertIs(proved.status, ExecutionStatus.CONFIRMED)
        self.assertEqual(proved.account_snapshot.open_order_ids, ())
        self.assertEqual(fixture.adapter.cancel_order.await_count, 2)

    async def test_account_budget_refusal_keeps_type_and_quote_refusal_does_not_mark_failure(self):
        from core.services.market_maker_v2.lighter_runtime import LighterAccountPort
        from test_mm_v2_lighter_runtime import Adapter, Clock, ReadStream, ADDRESS
        from test_mm_v2_quote_execution import QuoteExecutionTests

        adapter, clock = Adapter(), Clock()
        account = LighterAccountPort(adapter, "BTC", clock, account_index=7,
            expected_l1_address=ADDRESS, mutation_generation=lambda: 0)
        account.stream = ReadStream(adapter, clock)
        await account.snapshot()
        account.before_read = Mock(side_effect=ApiBudgetUnavailable("no headroom"))
        with self.assertRaises(ApiBudgetUnavailable):
            await account.snapshot()
        account.before_read = None
        self.assertTrue((await account.snapshot()).authenticated)

        fixture = QuoteExecutionTests()
        fixture.setUp()
        fixture.refresh.side_effect = ApiBudgetUnavailable("no headroom")
        fixture.port._on_failure = Mock()
        with self.assertRaises(ApiBudgetUnavailable):
            await fixture.port.reconcile_quotes(fixture.proposal)
        fixture.adapter.create_order.assert_not_awaited()
        fixture.port._on_failure.assert_not_called()

    @staticmethod
    def adapter():
        signer_transport = SimpleNamespace(request=AsyncMock(return_value="signer-response"))
        rest = object.__new__(LighterRest)
        rest.signer_client = SimpleNamespace(api_client=SimpleNamespace(rest_client=signer_transport),
                                             check_client=Mock(return_value=None))
        rest.base_url = "https://example.invalid"
        rest._load_markets = AsyncMock()
        adapter = object.__new__(LighterAdapter)
        adapter._rest, adapter._connected = rest, False
        return adapter, rest, signer_transport

    async def test_signer_and_initialized_rest_clients_observed_without_global_changes(self):
        adapter, rest, signer_transport = self.adapter()
        observer = Mock()
        unrelated = SimpleNamespace(request=AsyncMock(return_value="unrelated"))
        created_transport = SimpleNamespace(request=AsyncMock(return_value="account-response"))
        created_client = SimpleNamespace(rest_client=created_transport)
        adapter.set_market_maker_request_observer(observer)
        with patch("core.adapters.exchanges.adapters.lighter_rest.ApiClient", return_value=created_client):
            await rest.initialize()
        self.assertEqual(await signer_transport.request("GET", "https://example.invalid/api/v1/nextNonce"),
                         "signer-response")
        self.assertEqual(await created_transport.request("GET", "https://example.invalid/api/v1/account"),
                         "account-response")
        await unrelated.request("GET", "https://example.invalid/api/v1/account")
        self.assertEqual(observer.call_count, 2)
        self.assertEqual([call.args for call in observer.call_args_list], [
            ("rest", "https://example.invalid/api/v1/nextNonce"),
            ("rest", "https://example.invalid/api/v1/account")])

    async def test_repeated_or_connected_observer_registration_is_rejected(self):
        adapter, _, transport = self.adapter()
        first, second = Mock(), Mock()
        adapter.set_market_maker_request_observer(first)
        with self.assertRaises(ValueError):
            adapter.set_market_maker_request_observer(second)
        await transport.request("GET", "https://example.invalid/api/v1/account")
        first.assert_called_once()
        second.assert_not_called()
        adapter._connected = True
        with self.assertRaises(ValueError):
            adapter.set_market_maker_request_observer(Mock())

    async def test_read_429_retry_and_failed_attempt_are_both_counted(self):
        adapter, rest, transport = self.adapter()
        transport.request.side_effect = [RuntimeError("HTTP 429"), "confirmed"]
        budget = ApiBudget(lambda: 1.0)
        adapter.set_market_maker_request_observer(budget.observe)
        rest._safety_request_depth = 1  # Do not spend a real second on the existing cooldown.
        result = await rest._call_api("account query",
            lambda: transport.request("GET", "https://example.invalid/api/v1/account"))
        self.assertEqual(result, "confirmed")
        self.assertEqual(budget.snapshot()["used"], {"rest": 600})
        self.assertEqual(budget.snapshot()["attempts"], {"rest:account": 2})

    async def test_live_mm_admission_counts_and_stops_after_one_429_attempt(self):
        adapter, rest, transport = self.adapter()
        transport.request.side_effect = [RuntimeError("HTTP 429"), "must not retry"]
        budget = ApiBudget(lambda: 1.0)
        adapter.set_market_maker_request_observer(budget.observe, enforce_admission=True)
        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            await rest._call_api("account query",
                lambda: transport.request("GET", "https://example.invalid/api/v1/account"))
        self.assertEqual(budget.snapshot()["used"], {"rest": 300})
        self.assertEqual(budget.snapshot()["attempts"], {"rest:account": 1})

    async def test_protocol_text_ping_automatic_pong_and_close_are_counted_per_connection(self):
        class FramedSocket(Socket):
            def __init__(self):
                super().__init__()
                self.protocol = Protocol(Side.CLIENT)

            async def send(self, raw):
                self.protocol.send_text(raw.encode())
                await super().send(raw)

            async def close(self):
                if self.protocol.state is State.OPEN:
                    self.protocol.send_close(1000)
                await super().close()

        socket = FramedSocket()
        untouched = Protocol(Side.CLIENT)
        budget = ApiBudget(lambda: 42.5)
        stream = LighterReadStream("wss://example.invalid/stream", 7, 0, lambda: "test-auth",
            connect_factory=AsyncMock(return_value=socket), clock=lambda: 42.5,
            wall_clock=lambda: 1, timeout=0.1, request_observer=budget.observe)
        self.addAsyncCleanup(stream.close)
        await stream.start()
        await stream.request_snapshot("account_orders")
        await stream.request_snapshot("account_orders")
        socket.protocol.send_ping(b"client-keepalive")
        socket.protocol.receive_data(Frame(Opcode.PING, b"server-keepalive").serialize(mask=False))
        untouched.send_ping(b"unrelated")
        await stream.close()
        self.assertEqual(budget.snapshot()["attempts"], {"ws:1": 4, "ws:9": 1, "ws:10": 1, "ws:8": 1})
        self.assertEqual(budget.snapshot()["used"], {"ws": 7})


if __name__ == "__main__":
    unittest.main()

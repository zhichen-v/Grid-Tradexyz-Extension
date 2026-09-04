"""Offline inventory, reserve and terminal-proof public contracts."""

from dataclasses import replace
from decimal import Decimal as D
import unittest

from core.services.market_maker_v2.domain import (
    AccountSnapshot, ExecutionHealth, ExecutionResult, ExecutionSnapshot,
    ExecutionStatus, MarketStateSnapshot, SessionReport, Side, StrategyState,
    WorkingOrder,
)
from core.services.market_maker_v2.inventory_governor import (
    GovernorUnavailable, InventoryGovernor,
)
from core.services.market_maker_v2.quote_policy import VolumeQuotePolicy


class InventoryGovernorTests(unittest.TestCase):
    def governor(self, **changes):
        values = dict(order_size=D("1"), soft_limit=D("2"), hard_limit=D("3"),
            stop_loss_usdg=D("10"), max_hold_seconds=60, cooldown_seconds=5,
            max_session_loss_usdg=D("1000"), session_started_monotonic=0,
            session_deadline_monotonic=1000, ioc_slippage_ticks=1)
        return InventoryGovernor(**(values | changes))

    def market(self, now=100, **changes):
        values = dict(symbol="BTC", observed_monotonic=now, external_bid=D("99"),
            external_ask=D("101"), tick_size=D("1"), size_step=D("0.1"),
            min_order_size=D("0.1"), trusted=True)
        return MarketStateSnapshot(**(values | changes))

    def account(self, position="0", now=100, **changes):
        values = dict(symbol="BTC", observed_monotonic=now, position=D(position),
            equity=D("1000"), maker_fee_rate=D("0"), taker_fee_rate=D("0"),
            open_order_count=0, authenticated=True,
            entry_price=D("100") if D(position) else None)
        return AccountSnapshot(**(values | changes))

    def report(self, position="0", now=100, **changes):
        values = dict(symbol="BTC", complete=False, final_position=D(position),
            final_open_order_count=None, ledger_position=D(position),
            duration_seconds=D(str(now)), inventory_age=D("5") if D(position) else D("0"))
        return SessionReport(**(values | changes))

    def execution(self, now=100, orders=(), **changes):
        values = dict(health=ExecutionHealth.HEALTHY, managed_order_count=len(orders),
            simulated=False, symbol="BTC", observed_monotonic=now, orders=orders)
        return ExecutionSnapshot(**(values | changes))

    def evaluate(self, governor=None, *, position="0", now=100, market=None,
                 account=None, report=None, execution=None, **changes):
        return (governor or self.governor()).evaluate(
            market or self.market(now), account or self.account(position, now),
            report or self.report(position, now), execution or self.execution(now),
            now=now, **changes)

    def confirmed(self, now=101, **changes):
        values = dict(status=ExecutionStatus.CONFIRMED, snapshot=self.execution(now),
                      account_snapshot=self.account(now=now))
        return ExecutionResult(**(values | changes))

    def test_low_soft_and_hard_bands_are_symmetric(self):
        for position in ("0", "1", "-1", "2", "-2", "3", "-3"):
            with self.subTest(position=position):
                decision = self.evaluate(position=position)
                quantity = abs(D(position))
                expected = (StrategyState.REDUCE_ONLY if quantity == 3 else
                            StrategyState.SKEWED if quantity >= 2 else StrategyState.QUOTING)
                self.assertEqual(decision.state, expected)
                if quantity == 3:
                    self.assertEqual(decision.buy_capacity if D(position) > 0 else decision.sell_capacity, 0)
                    self.assertEqual(decision.sell_capacity if D(position) > 0 else decision.buy_capacity, 1)
                elif quantity == 2:
                    self.assertEqual(decision.buy_capacity if D(position) > 0 else decision.sell_capacity, D("0.5"))

    def test_adverse_long_short_move_reaches_touch_then_bounded_loss_exit(self):
        policy = VolumeQuotePolicy(order_size=D("1"), target_net_edge_bps=D("0"),
            volatility_multiplier=D("0"), hard_inventory_limit=D("3"), skew_bps_at_hard=D("100"))
        for sign in (1, -1):
            with self.subTest(sign=sign):
                governor = self.governor()
                account = self.account(str(sign * 2))
                skew = self.evaluate(governor, position=str(sign * 2))
                self.assertEqual(skew.state, StrategyState.SKEWED)
                self.assertEqual(len(policy.propose(self.market(), account, skew, now=100).quotes), 2)
                hard = self.evaluate(governor, position=str(sign * 3))
                quotes = policy.propose(self.market(), self.account(str(sign * 3)), hard, now=100).quotes
                self.assertEqual(len(quotes), 1)
                self.assertTrue(quotes[0].reduce_only)
                self.assertEqual(quotes[0].price, D("101") if sign > 0 else D("99"))
                adverse = self.market(101, external_bid=D("93") if sign > 0 else D("105"),
                                      external_ask=D("95") if sign > 0 else D("107"))
                exit_risk = self.evaluate(governor, position=str(sign * 2), now=101, market=adverse)
                self.assertEqual(exit_risk.state, StrategyState.FLATTENING)
                self.assertEqual(exit_risk.flatten.side, Side.SELL if sign > 0 else Side.BUY)
                self.assertEqual(exit_risk.flatten.limit_price, D("92") if sign > 0 else D("108"))
                self.assertEqual(exit_risk.flatten.size, D("2"))
                self.assertTrue(exit_risk.flatten.reduce_only)
                self.assertEqual(exit_risk.flatten.time_in_force, "IOC")
                self.assertEqual(exit_risk.flatten.deadline_monotonic, 131)

    def test_profit_does_not_hide_inventory_loss_and_account_loss_is_conservative(self):
        for account, report in (
                (self.account("1", unrealized_pnl=D("-10")), self.report("1", realized_net_pnl=D("100"))),
                (self.account("1", entry_price=D("109"), unrealized_pnl=D("100")), self.report("1"))):
            decision = self.evaluate(account=account, report=report)
            self.assertEqual(decision.state, StrategyState.FLATTENING)

    def test_age_threshold_and_hard_breach_flatten_without_breakeven_anchor(self):
        for position, age in (("1", "60"), ("-1", "60"), ("3.1", "5"), ("-3.1", "5")):
            with self.subTest(position=position, age=age):
                decision = self.evaluate(position=position,
                    report=self.report(position, inventory_age=D(age)))
                self.assertEqual(decision.state, StrategyState.FLATTENING)

    def test_operator_stop_and_deadline_while_flat_still_require_terminal_cancel(self):
        order = WorkingOrder("old", Side.BUY, D("1"), D("99"))
        for changes in (dict(stop_requested=True), dict(now=1000)):
            with self.subTest(changes=changes):
                now = changes.get("now", 100)
                governor = self.governor()
                risk = self.evaluate(governor, account=self.account(now=now, open_order_count=1),
                    execution=self.execution(now, orders=(order,)), **changes)
                self.assertEqual(risk.state, StrategyState.FLATTENING)
                self.assertIsNone(risk.flatten)
                self.assertEqual((risk.buy_capacity, risk.sell_capacity), (0, 0))
                # A later flat snapshot alone is not the execution bridge's terminal result.
                self.assertEqual(self.evaluate(governor, now=now + 1).state, StrategyState.FLATTENING)
                self.assertEqual(governor.confirm_exit(self.confirmed(now + 1), now=now + 1).state,
                                 StrategyState.SESSION_COMPLETE)

    def test_stop_and_original_deadline_survive_data_or_order_pause(self):
        governor = self.governor()
        with self.assertRaises(GovernorUnavailable):
            self.evaluate(governor, market=self.market(trusted=False), stop_requested=True)
        self.assertEqual(governor.exit_deadline, 130)
        with self.assertRaises(GovernorUnavailable) as error:
            self.evaluate(governor, now=110,
                          execution=self.execution(110, health=ExecutionHealth.PAUSED_ORDER_STATE))
        self.assertEqual(error.exception.health, ExecutionHealth.PAUSED_ORDER_STATE)
        self.assertEqual(governor.exit_deadline, 130)
        self.assertEqual(self.evaluate(governor, now=111).state, StrategyState.FLATTENING)
        self.assertEqual(governor.confirm_exit(self.confirmed(112), now=112).state,
                         StrategyState.SESSION_COMPLETE)

    def test_session_net_loss_and_drawdown_latch_complete_after_cleanup(self):
        for report in (self.report(realized_net_pnl=D("-1000")),
                       self.report(max_drawdown=D("1000"))):
            with self.subTest(report=report):
                governor = self.governor()
                self.assertEqual(self.evaluate(governor, report=report).state, StrategyState.FLATTENING)
                self.assertEqual(governor.confirm_exit(self.confirmed(), now=101).state,
                                 StrategyState.SESSION_COMPLETE)
                self.assertEqual(self.evaluate(governor, now=1001).state, StrategyState.SESSION_COMPLETE)

    def test_exact_terminal_flat_proof_starts_cooldown_then_quote(self):
        governor = self.governor()
        self.evaluate(governor, position="1", report=self.report("1", inventory_age=D("60")))
        self.assertEqual(governor.confirm_exit(self.confirmed(), now=101).state, StrategyState.COOLDOWN)
        self.assertEqual(self.evaluate(governor, now=105.999).state, StrategyState.COOLDOWN)
        self.assertEqual(self.evaluate(governor, now=106).state, StrategyState.QUOTING)
        self.assertIsNone(governor.exit_deadline)

    def test_exit_deadline_stays_halted_even_after_late_authenticated_flat(self):
        governor = self.governor()
        self.evaluate(governor, position="1", report=self.report("1", inventory_age=D("60")))
        for now in (130, 131):
            with self.subTest(now=now):
                with self.assertRaises(GovernorUnavailable) as error:
                    self.evaluate(governor, now=now)
                self.assertEqual(error.exception.health, ExecutionHealth.HALTED)
                with self.assertRaises(GovernorUnavailable) as error:
                    governor.confirm_exit(self.confirmed(now), now=now)
                self.assertEqual(error.exception.health, ExecutionHealth.HALTED)
                self.assertEqual(governor.exit_deadline, 130)

    def test_confirm_rejects_old_or_missing_or_nonterminal_or_nonflat_proof(self):
        variants = [self.confirmed(100), self.confirmed(account_snapshot=None),
            self.confirmed(status=ExecutionStatus.BLOCKED),
            self.confirmed(account_snapshot=self.account("0.1", 101)),
            self.confirmed(account_snapshot=self.account(now=101, authenticated=False)),
            self.confirmed(account_snapshot=self.account(now=101, symbol="ETH")),
            self.confirmed(snapshot=self.execution(101, health=ExecutionHealth.PAUSED_ORDER_STATE)),
            ExecutionResult(ExecutionStatus.SIMULATED, self.execution(101, simulated=True),
                            account_snapshot=self.account(now=101))]
        for result in variants:
            with self.subTest(result=result):
                governor = self.governor()
                self.evaluate(governor, stop_requested=True)
                with self.assertRaises(GovernorUnavailable):
                    governor.confirm_exit(result, now=101)
                self.assertEqual(self.evaluate(governor, now=102).state, StrategyState.FLATTENING)

    def test_residual_reappearing_during_cooldown_reenters_exit(self):
        governor = self.governor()
        self.evaluate(governor, position="1", report=self.report("1", inventory_age=D("60")))
        governor.confirm_exit(self.confirmed(), now=101)
        risk = self.evaluate(governor, position="0.1", now=102)
        self.assertEqual(risk.state, StrategyState.FLATTENING)
        self.assertEqual(risk.flatten.size, D("0.1"))
        self.assertEqual(governor.exit_deadline, 132)

    def test_strict_reserve_boundary_and_partial_quantity_stop_floor(self):
        for cap, expected in (("10", "0"), ("10.000001", "1")):
            with self.subTest(cap=cap):
                risk = self.evaluate(self.governor(max_session_loss_usdg=D(cap), ioc_slippage_ticks=0))
                self.assertEqual((risk.buy_capacity, risk.sell_capacity), (D(expected), D(expected)))
        risk = self.evaluate(self.governor(order_size=D("0.5"), max_session_loss_usdg=D("9.999")))
        self.assertEqual((risk.buy_capacity, risk.sell_capacity), (0, 0))

    def test_taker_slippage_and_both_side_maker_fees_are_jointly_reserved(self):
        account = self.account(maker_fee_rate=D("0.01"), taker_fee_rate=D("0.01"))
        # At quantity 1: stop 10 + taker/slip 2.02 + two maker sides 2.02 = 14.04.
        risk = self.evaluate(self.governor(max_session_loss_usdg=D("14.04")), account=account)
        self.assertEqual((risk.buy_capacity, risk.sell_capacity), (D("0.9"), D("0.9")))
        larger = self.evaluate(self.governor(max_session_loss_usdg=D("14.040000001")), account=account)
        self.assertEqual((larger.buy_capacity, larger.sell_capacity), (1, 1))

    def test_working_quantities_are_counted_with_new_quotes_before_terminal_cancel(self):
        order = WorkingOrder("old-buy", Side.BUY, D("0.8"), D("99"))
        risk = self.evaluate(position="1.5", account=self.account("1.5", open_order_count=1),
                             execution=self.execution(orders=(order,)))
        self.assertEqual(risk.buy_capacity, D("0.7"))
        self.assertLessEqual(D("1.5") + order.remaining_size + risk.buy_capacity, D("3"))
        cleared = self.evaluate(position="1.5")
        self.assertEqual(cleared.buy_capacity, D("1"))

    def test_working_gap_loss_and_risky_reduce_only_orders_do_not_grant_new_risk(self):
        for order in (WorkingOrder("old", Side.BUY, D("1"), D("200")),
                      WorkingOrder("old", Side.SELL, D("4"), D("101"), reduce_only=True)):
            with self.subTest(order=order):
                risk = self.evaluate(self.governor(max_session_loss_usdg=D("100")),
                    account=self.account(open_order_count=1), execution=self.execution(orders=(order,)))
                self.assertEqual((risk.buy_capacity, risk.sell_capacity), (0, 0))

    def test_exhausted_reserve_still_allows_only_actual_inventory_reduction(self):
        risk = self.evaluate(self.governor(max_session_loss_usdg=D("10")), position="0.5")
        self.assertEqual(risk.state, StrategyState.REDUCE_ONLY)
        self.assertEqual((risk.buy_capacity, risk.sell_capacity), (0, D("0.5")))

    def test_high_precision_capacity_never_rounds_over_hard_limit(self):
        position = "1.999999999999999999999999999999999999999"
        risk = self.evaluate(position=position)
        self.assertEqual(risk.buy_capacity, D("1"))
        risk = self.evaluate(position="2.000000000000000000000000000000000000001",
            market=self.market(size_step=D("0.5"), min_order_size=D("0.5")))
        self.assertEqual(risk.buy_capacity, D("0.5"))
        order = WorkingOrder("old", Side.BUY, D("0.000000000000000000000000000000000000001"), D("99"))
        risk = self.evaluate(position="1", account=self.account("1", open_order_count=1),
            execution=self.execution(orders=(order,)),
            governor=self.governor(order_size=D("2")))
        self.assertEqual(risk.buy_capacity, D("1.9"))

    def test_stale_untrusted_mismatched_and_failed_truth_fail_closed(self):
        invalid = [dict(market=self.market(trusted=False)),
            dict(market=self.market(96.99)), dict(account=self.account(now=89.99)),
            dict(account=self.account(authenticated=False)),
            dict(execution=self.execution(89.99)),
            dict(execution=ExecutionSnapshot(ExecutionHealth.HEALTHY, 0, False)),
            dict(account=self.account(open_order_count=1)),
            dict(report=self.report(now=99)), dict(report=self.report(failed=True)),
            dict(report=self.report(ledger_position=D("1"))),
            dict(report=self.report(inventory_age=D("1"))),
            dict(report=self.report(max_drawdown=D("-1"))),
            dict(market=self.market(symbol="ETH")), dict(execution=self.execution(symbol="ETH")),
            dict(account=self.account(maker_fee_rate=D("1E-5000")))]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(GovernorUnavailable):
                    self.evaluate(**changes)

    def test_pause_never_resets_inventory_age_or_allows_clock_rewind(self):
        governor = self.governor()
        with self.assertRaises(GovernorUnavailable):
            self.evaluate(governor, position="1", market=self.market(trusted=False))
        risk = self.evaluate(governor, position="1", now=110,
                             report=self.report("1", 110, inventory_age=D("65")))
        self.assertEqual(risk.state, StrategyState.FLATTENING)
        with self.assertRaises(GovernorUnavailable):
            self.evaluate(governor, now=109)
        self.assertEqual(governor.exit_deadline, 135)

    def test_trusted_ledger_risk_latches_even_while_market_or_order_truth_is_paused(self):
        for changes in (dict(market=self.market(trusted=False)),
                        dict(execution=self.execution(health=ExecutionHealth.PAUSED_ORDER_STATE))):
            with self.subTest(changes=changes):
                governor = self.governor()
                with self.assertRaises(GovernorUnavailable):
                    self.evaluate(governor, position="1",
                        report=self.report("1", inventory_age=D("70")), **changes)
                self.assertEqual(governor.exit_deadline, 120)
                with self.assertRaises(GovernorUnavailable) as error:
                    self.evaluate(governor, position="1", now=120,
                        report=self.report("1", 120, inventory_age=D("90")))
                self.assertEqual(error.exception.health, ExecutionHealth.HALTED)
                self.assertEqual(governor.exit_deadline, 120)
        governor = self.governor()
        with self.assertRaises(GovernorUnavailable):
            self.evaluate(governor, position="1", account=self.account("1", unrealized_pnl=D("-10")),
                          market=self.market(trusted=False))
        self.assertEqual(governor.exit_deadline, 130)
        delayed = self.governor()
        with self.assertRaises(GovernorUnavailable) as error:
            self.evaluate(delayed, now=1100)
        self.assertEqual(error.exception.health, ExecutionHealth.HALTED)
        self.assertEqual(delayed.exit_deadline, 1030)

    def test_invalid_parameters_and_untyped_inputs_are_rejected(self):
        for changes in (dict(soft_limit=D("3")), dict(order_size=D("4")),
                dict(stop_loss_usdg=D("0")), dict(max_hold_seconds=0),
                dict(cooldown_seconds=-1), dict(ioc_slippage_ticks=True),
                dict(max_session_loss_usdg=1.0), dict(session_deadline_monotonic=0)):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.governor(**changes)
        for inputs in (({}, self.account(), self.report(), self.execution()),
                       (self.market(), {}, self.report(), self.execution())):
            with self.assertRaises(GovernorUnavailable):
                self.governor().evaluate(*inputs, now=100)
        with self.assertRaises(GovernorUnavailable):
            self.evaluate(stop_requested=1)


if __name__ == "__main__":
    unittest.main()

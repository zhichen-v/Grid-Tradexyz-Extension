"""V2 ports and a deliberately dry-only legacy execution compatibility layer."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from .domain import (
    AccountSnapshot,
    ExecutionHealth,
    ExecutionResult,
    ExecutionSnapshot,
    ExecutionStatus,
    FlattenIntent,
    MarketStateSnapshot,
    QuotePlan,
)

if TYPE_CHECKING:
    from ..market_maker.order_manager import MarketMakerOrderManager, ReconcileResult


class MarketDataPort(Protocol):
    def snapshot(self) -> MarketStateSnapshot: ...


class AccountPort(Protocol):
    async def snapshot(self) -> AccountSnapshot: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...


class TelemetrySink(Protocol):
    def emit(self, event: QuotePlan | ExecutionResult) -> None: ...


class ExecutionPort(Protocol):
    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult: ...

    async def cancel_all_managed(self) -> ExecutionResult: ...

    async def flatten_ioc(self, intent: FlattenIntent) -> ExecutionResult: ...

    def snapshot(self) -> ExecutionSnapshot: ...


class ExecutionUnavailable(RuntimeError):
    """Execution is unavailable; backend details are intentionally not exposed."""


class LegacyDryExecutionPort:
    """Exercise public V1 safety APIs without enabling trading or V1 policy.

    Phase 2 supports empty quote plans and simulated cancellation only. Nonempty
    quotes require the V2 risk contract; bounded flatten belongs to Phase 5.
    A simulated result is not authenticated account or terminal-flat evidence.
    """

    def __init__(self, manager: MarketMakerOrderManager):
        self.manager = manager
        self._require_dry()

    def _require_dry(self) -> str:
        try:
            dry = self.manager.config.dry_run is True
            symbol = self.manager.config.symbol
        except Exception:
            raise ExecutionUnavailable("dry execution unavailable") from None
        if not dry:
            raise ExecutionUnavailable("dry execution unavailable")
        return symbol

    def snapshot(self) -> ExecutionSnapshot:
        self._require_dry()
        try:
            from ..market_maker.models import RuntimeState

            managed = self.manager.snapshot()
            simulated = all(order.simulated is True for order in managed)
            if not simulated or self.manager.has_uncertain_state or self.manager.has_unknown_order_state:
                health = ExecutionHealth.PAUSED_ORDER_STATE
            elif self.manager.runtime_state in {
                RuntimeState.SYNCING, RuntimeState.ACTIVE, RuntimeState.RISK_REDUCTION,
            }:
                health = ExecutionHealth.HEALTHY
            elif self.manager.runtime_state in {
                RuntimeState.PAUSED_DATA, RuntimeState.PAUSED_MARKET,
                RuntimeState.PAUSED_POSITION, RuntimeState.PAUSED_EXCHANGE,
            }:
                health = ExecutionHealth.PAUSED_DATA
            elif self.manager.runtime_state is RuntimeState.PAUSED_ORDER_STATE:
                health = ExecutionHealth.PAUSED_ORDER_STATE
            else:
                health = ExecutionHealth.HALTED
            return ExecutionSnapshot(health, len(managed), simulated=simulated)
        except Exception:
            raise ExecutionUnavailable("dry execution snapshot unavailable") from None

    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult:
        symbol = self._require_dry()
        if not isinstance(plan, QuotePlan) or plan.symbol != symbol:
            raise ExecutionUnavailable("quote symbol does not match execution")
        if plan.quotes:
            raise ExecutionUnavailable("Phase 2 supports empty dry plans only")
        snapshot = self.snapshot()
        if snapshot.health is not ExecutionHealth.HEALTHY:
            return ExecutionResult(ExecutionStatus.BLOCKED, snapshot)
        from ..market_maker.models import DesiredQuotes, RuntimeState
        from ..market_maker.risk_manager import RiskDecision

        zero = Decimal("0")
        desired = DesiredQuotes(
            bid=None, ask=None, reference_price=zero, reservation_price=zero,
            half_spread=zero, inventory_ratio=zero, runtime_state=RuntimeState.ACTIVE,
            reason="v2 empty quote plan",
        )
        risk = RiskDecision(
            buy_amount=None, sell_amount=None, buy_reduce_only=False,
            sell_reduce_only=False, buy_capacity=zero, sell_capacity=zero,
            worst_long=zero, worst_short=zero, inventory_ratio=zero,
            runtime_state=RuntimeState.ACTIVE, reason="v2 no new orders", safe=True,
        )
        try:
            result = await self.manager.reconcile(desired, risk)
            return self._result(result)
        except Exception:
            raise ExecutionUnavailable("dry quote reconciliation unavailable") from None

    async def cancel_all_managed(self) -> ExecutionResult:
        self._require_dry()
        snapshot = self.snapshot()
        if snapshot.health is not ExecutionHealth.HEALTHY or not snapshot.simulated:
            return ExecutionResult(ExecutionStatus.BLOCKED, snapshot)
        try:
            result = await self.manager.cancel_managed_orders("v2 dry cancellation")
            return self._result(result)
        except Exception:
            raise ExecutionUnavailable("dry cancellation unavailable") from None

    async def flatten_ioc(self, intent: FlattenIntent) -> ExecutionResult:
        symbol = self._require_dry()
        if not isinstance(intent, FlattenIntent) or intent.symbol != symbol:
            raise ExecutionUnavailable("flatten symbol does not match execution")
        raise ExecutionUnavailable("Bounded flatten is not implemented")

    def _result(self, result: ReconcileResult) -> ExecutionResult:
        snapshot = self.snapshot()
        status = (
            ExecutionStatus.BLOCKED
            if result.errors or snapshot.health is not ExecutionHealth.HEALTHY
            else ExecutionStatus.SIMULATED
        )
        return ExecutionResult(
            status, snapshot, submitted_count=0,
            cancelled_count=sum(action.operation == "would_cancel" for action in result.actions),
        )

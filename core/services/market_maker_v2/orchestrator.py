"""Phase 2 synthetic plumbing only; no strategy, scheduler, connection or live runner."""

from math import isfinite
import warnings

from .domain import (
    AccountSnapshot, ExecutionHealth, ExecutionResult, ExecutionSnapshot,
    ExecutionStatus, MarketStateSnapshot, QuotePlan,
)
from .execution_port import AccountPort, Clock, ExecutionPort, MarketDataPort, TelemetrySink


class DryCycleUnavailable(RuntimeError):
    """A synthetic cycle could not establish its input/execution contract."""


async def dry_synthetic_cycle(
    market_data: MarketDataPort,
    account_port: AccountPort,
    execution: ExecutionPort,
    clock: Clock,
    telemetry: TelemetrySink,
) -> QuotePlan:
    """Emit an empty plan using synthetic inputs and explicitly simulated execution.

    The 3s book / 10s account ages are plan-owned safety defaults, not strategy
    configuration. They move into the code-owned runtime profile in Phase 6.
    Authenticated=True on a synthetic fixture is not fresh exchange evidence.
    """
    before = execution.snapshot()
    if (not isinstance(before, ExecutionSnapshot) or not before.simulated
            or before.health != ExecutionHealth.HEALTHY):
        raise DryCycleUnavailable("healthy simulated execution required")
    try:
        market = market_data.snapshot()
        account = await account_port.snapshot()
        now = clock.monotonic()
    except Exception:
        raise DryCycleUnavailable("synthetic input read failed") from None
    if not isinstance(market, MarketStateSnapshot) or not isinstance(account, AccountSnapshot):
        raise DryCycleUnavailable("typed market and account snapshots required")
    if type(now) not in (int, float) or not isfinite(now) or now < 0:
        raise DryCycleUnavailable("valid monotonic clock required")
    if (market.symbol != account.symbol or not market.trusted or not account.authenticated
            or not 0 <= now - market.observed_monotonic <= 3
            or not 0 <= now - account.observed_monotonic <= 10):
        raise DryCycleUnavailable("fresh trusted same-symbol snapshots required")
    if account.position != 0 or account.open_order_count != 0:
        raise DryCycleUnavailable("synthetic flat start with zero exchange orders required")
    plan = QuotePlan(symbol=market.symbol)
    result = await execution.reconcile_quotes(plan)
    if (not isinstance(result, ExecutionResult) or result.status != ExecutionStatus.SIMULATED
            or not result.snapshot.simulated
            or result.snapshot.health != ExecutionHealth.HEALTHY):
        raise DryCycleUnavailable("empty dry plan was not reconciled")
    try:
        telemetry.emit(plan)
        telemetry.emit(result)
    except Exception:
        # Telemetry is not account/risk truth; do not convert a safe cycle to a risk halt.
        warnings.warn("V2 dry-cycle telemetry unavailable", RuntimeWarning, stacklevel=2)
    return plan

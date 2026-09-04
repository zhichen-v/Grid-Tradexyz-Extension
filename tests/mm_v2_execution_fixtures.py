"""Small fake clock and exchange-order DTO shared by V2 execution tests."""

from datetime import datetime
from decimal import Decimal

from core.adapters.exchanges.models import OrderData, OrderSide, OrderStatus, OrderType


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def exchange_order(
    order_id: str,
    side: OrderSide,
    *,
    status: OrderStatus = OrderStatus.OPEN,
    price: str = "100",
    amount: str = "0.2",
    remaining: str | None = None,
    client_id: str | None = None,
    params: dict | None = None,
    raw_data: dict | None = None,
) -> OrderData:
    amount_value = Decimal(amount)
    remaining_value = (
        amount_value if remaining is None else Decimal(remaining)
    )
    return OrderData(
        id=order_id,
        client_id=client_id or f"client-{order_id}",
        symbol="BTC",
        side=side,
        type=OrderType.LIMIT,
        amount=amount_value,
        price=Decimal(price),
        filled=amount_value - remaining_value,
        remaining=remaining_value,
        cost=Decimal("0"),
        average=None,
        status=status,
        timestamp=datetime.now(),
        updated=None,
        fee=None,
        trades=[],
        params=params or {},
        raw_data=raw_data or {},
    )

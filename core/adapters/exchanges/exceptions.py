"""Typed exchange-adapter failures with known mutation outcomes."""


class OrderSubmissionNotSentError(RuntimeError):
    """An order was not sent and its local nonce state was restored."""


class OrderSubmissionRejectedError(RuntimeError):
    """The exchange definitively rejected an order submission."""


class OrderCancellationNotSentError(RuntimeError):
    """An exact cancellation was not sent and its nonce reservation was undone."""

    def __init__(self, *, symbol: str, order_id: str):
        if type(symbol) is not str or not symbol or type(order_id) is not str or not order_id:
            raise ValueError("exact cancellation identity required")
        self.symbol = symbol
        self.order_id = order_id
        super().__init__("order cancellation was not sent and nonce state was restored")

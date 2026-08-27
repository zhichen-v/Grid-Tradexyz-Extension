"""Typed exchange-adapter failures with known mutation outcomes."""


class OrderSubmissionNotSentError(RuntimeError):
    """An order was not sent and its local nonce state was restored."""

from __future__ import annotations

from .base import (
    QuoteControllerContext,
    QuoteControllerDecision,
    SideQuoteAdjustment,
)


class FixedEntryQuoteController:
    def __init__(self) -> None:
        self._decision_id = 0

    def evaluate(self, context: QuoteControllerContext) -> QuoteControllerDecision:
        self._decision_id += 1
        return QuoteControllerDecision(
            mode="fixed",
            controller="fixed",
            ready=True,
            reason="fixed",
            decision_id=self._decision_id,
            bid=SideQuoteAdjustment(),
            ask=SideQuoteAdjustment(),
            features=context.features,
        )

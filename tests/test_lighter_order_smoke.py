import unittest
from decimal import Decimal
from types import SimpleNamespace

from lighter_order_smoke import _matching_orders, build_passive_buy_plan

from core.adapters.exchanges.models import OrderSide


class LighterOrderSmokeTests(unittest.TestCase):
    def test_builds_minimum_order_under_cap(self):
        price, amount, notional = build_passive_buy_plan(
            [Decimal("62976.0"), Decimal("62975.5")],
            {
                "price_decimals": 1,
                "size_decimals": 5,
                "min_base_amount": Decimal("0.00020"),
                "min_quote_amount": Decimal("10"),
            },
            Decimal("15"),
            Decimal("0.50"),
        )
        self.assertEqual(price, Decimal("31487.7"))
        self.assertEqual(amount, Decimal("0.00032"))
        self.assertGreaterEqual(notional, Decimal("10"))
        self.assertLessEqual(notional, Decimal("15"))

    def test_rejects_minimum_order_above_cap(self):
        with self.assertRaisesRegex(ValueError, "exceeds cap"):
            build_passive_buy_plan(
                [Decimal("100000")],
                {
                    "price_decimals": 1,
                    "size_decimals": 5,
                    "min_base_amount": Decimal("0.001"),
                    "min_quote_amount": Decimal("10"),
                },
                Decimal("15"),
                Decimal("0.50"),
            )

    def test_matches_unique_client_id_before_price_fallback(self):
        matching = SimpleNamespace(
            id="42",
            client_id="1234",
            side=OrderSide.BUY,
            price=Decimal("30000"),
            amount=Decimal("0.00034"),
        )
        unrelated = SimpleNamespace(
            id="41",
            client_id="9999",
            side=OrderSide.BUY,
            price=Decimal("30000"),
            amount=Decimal("0.00034"),
        )
        self.assertEqual(
            _matching_orders([unrelated, matching], 1234),
            [matching],
        )

    def test_never_falls_back_to_same_price_and_amount(self):
        unrelated = SimpleNamespace(
            id="42",
            client_id="9999",
            side=OrderSide.BUY,
            price=Decimal("30000"),
            amount=Decimal("0.00034"),
        )
        self.assertEqual(_matching_orders([unrelated], 1234), [])


if __name__ == "__main__":
    unittest.main()

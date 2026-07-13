from django.test import SimpleTestCase, override_settings

from apps.stripe.management.commands.provision_overage_billing import _tier_problems
from apps.stripe.overage import cost_cents_for_units, units_for_budget

# A small, easy-to-reason-about graduated schedule: $0.10/unit for the first
# 100 units, then $0.05/unit beyond.
TEST_TIERS = [(100, "0.10"), (None, "0.05")]


@override_settings(GLITCHTIP_OVERAGE_TIERS=TEST_TIERS)
class OverageMathTestCase(SimpleTestCase):
    def test_cost_zero_and_negative(self):
        self.assertEqual(cost_cents_for_units(0), 0)
        self.assertEqual(cost_cents_for_units(-5), 0)

    def test_cost_within_first_tier(self):
        # 50 * $0.10 = $5.00
        self.assertEqual(cost_cents_for_units(50), 500)

    def test_cost_at_first_tier_boundary(self):
        self.assertEqual(cost_cents_for_units(100), 1000)

    def test_cost_spans_tiers_graduated(self):
        # 100 * $0.10 + 50 * $0.05 = $12.50 (graduated, not volume)
        self.assertEqual(cost_cents_for_units(150), 1250)

    def test_units_for_budget_zero(self):
        self.assertEqual(units_for_budget(0), 0)

    def test_units_for_budget_within_first_tier(self):
        # $5.00 buys 50 units at $0.10
        self.assertEqual(units_for_budget(500), 50)

    def test_units_for_budget_exact_tier_fill(self):
        # $10.00 exactly fills the 100-unit first tier
        self.assertEqual(units_for_budget(1000), 100)

    def test_units_for_budget_spans_tiers(self):
        # $12.50 -> 100 (tier 1) + 50 (tier 2 at $0.05) = 150
        self.assertEqual(units_for_budget(1250), 150)

    def test_round_trip_cost_units_consistency(self):
        for cap_cents in (250, 500, 999, 1000, 1500, 5000):
            units = units_for_budget(cap_cents)
            # The cost of the affordable units must never exceed the budget,
            # but one more unit must (it's the maximal affordable count).
            self.assertLessEqual(cost_cents_for_units(units), cap_cents)
            self.assertGreater(cost_cents_for_units(units + 1), cap_cents)


@override_settings(
    GLITCHTIP_OVERAGE_TIERS=[
        (400_000, "0.00015"),
        (2_000_000, "0.00010"),
        (None, "0.00008"),
    ]
)
class OverageDefaultScheduleTestCase(SimpleTestCase):
    """Sanity-check the production-shaped schedule used in the plan."""

    def test_small_overage_cost(self):
        # 10,000 events over quota at $0.15/1k = $1.50
        self.assertEqual(cost_cents_for_units(10_000), 150)

    def test_budget_converts_to_units(self):
        # $20 budget, first tier $0.00015/event -> floor(20 / 0.00015) = 133,333
        self.assertEqual(units_for_budget(2000), 133_333)


@override_settings(GLITCHTIP_OVERAGE_TIERS=TEST_TIERS)
class TierVerifyTestCase(SimpleTestCase):
    """--verify's schedule comparison between a live Stripe price and settings."""

    def _raw_price(self, **overrides):
        # Stripe's unit_amount_decimal is in cents: $0.10/unit -> "10".
        raw = {
            "tiers_mode": "graduated",
            "currency": "usd",
            "tiers": [
                {"up_to": 100, "unit_amount_decimal": "10"},
                {"up_to": None, "unit_amount_decimal": "5"},
            ],
        }
        raw.update(overrides)
        return raw

    def test_matching_schedule_has_no_problems(self):
        self.assertEqual(_tier_problems(self._raw_price()), [])

    def test_matching_is_numeric_not_textual(self):
        raw = self._raw_price(
            tiers=[
                {"up_to": 100, "unit_amount_decimal": "10.00"},
                {"up_to": None, "unit_amount_decimal": "5.0"},
            ]
        )
        self.assertEqual(_tier_problems(raw), [])

    def test_rate_drift_is_reported(self):
        raw = self._raw_price(
            tiers=[
                {"up_to": 100, "unit_amount_decimal": "15"},
                {"up_to": None, "unit_amount_decimal": "5"},
            ]
        )
        self.assertEqual(len(_tier_problems(raw)), 1)
        self.assertIn("tier 0", _tier_problems(raw)[0])

    def test_boundary_drift_is_reported(self):
        raw = self._raw_price(
            tiers=[
                {"up_to": 200, "unit_amount_decimal": "10"},
                {"up_to": None, "unit_amount_decimal": "5"},
            ]
        )
        self.assertIn("tier 0", _tier_problems(raw)[0])

    def test_missing_tier_is_reported(self):
        raw = self._raw_price(tiers=[{"up_to": None, "unit_amount_decimal": "10"}])
        self.assertTrue(any("1 tiers" in p for p in _tier_problems(raw)))

    def test_volume_mode_is_reported(self):
        problems = _tier_problems(self._raw_price(tiers_mode="volume"))
        self.assertTrue(any("tiers_mode" in p for p in problems))

    def test_flat_amount_is_reported(self):
        raw = self._raw_price(
            tiers=[
                {"up_to": 100, "unit_amount_decimal": "10", "flat_amount": 500},
                {"up_to": None, "unit_amount_decimal": "5"},
            ]
        )
        self.assertTrue(any("flat_amount" in p for p in _tier_problems(raw)))

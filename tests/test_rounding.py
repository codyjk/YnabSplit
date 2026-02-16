"""Exhaustive rounding error tests for YNAB Tools split reconciler."""

from datetime import datetime
from decimal import Decimal

import pytest

from ynab_tools.models import SplitwiseExpense, SplitwiseUserShare
from ynab_tools.split.reconciler import (
    RoundingError,
    compute_splits_with_adjustment,
    to_milliunits,
    verify_no_precision_loss,
)


# Helper function for tests
def make_expense(id: int, net: Decimal) -> SplitwiseExpense:
    """Create a mock SplitwiseExpense for testing."""
    return SplitwiseExpense(
        id=id,
        group_id=999,
        description=f"Test expense {id}",
        details=None,
        date=datetime(2025, 1, 15),
        cost=abs(net),
        currency_code="USD",
        payment=False,
        users=[
            SplitwiseUserShare(
                user_id=123,
                paid_share=max(net, Decimal("0")),
                owed_share=abs(min(net, Decimal("0"))),
                net_balance=net,
            )
        ],
    )


class TestRoundingPerfectMatch:
    """Test cases where no rounding adjustment is needed."""

    def test_whole_dollars_only(self):
        """All expenses are whole dollar amounts."""
        expenses = [
            make_expense(id=1, net=Decimal("10.00")),
            make_expense(id=2, net=Decimal("20.00")),
            make_expense(id=3, net=Decimal("-15.00")),
        ]
        expected_total = 15000  # $15.00 in milliunits

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert lines[0].amount_milliunits == 10000
        assert lines[1].amount_milliunits == 20000
        assert lines[2].amount_milliunits == -15000

    def test_cents_exact(self):
        """Amounts with cents that convert exactly to milliunits."""
        expenses = [
            make_expense(id=1, net=Decimal("12.34")),
            make_expense(id=2, net=Decimal("56.78")),
        ]
        expected_total = 69120  # $69.12 in milliunits

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total


class TestRoundingSmallResiduals:
    """Test cases with small rounding residuals (within threshold)."""

    def test_single_milliunit_positive(self):
        """Residual of +1 milliunit (under threshold)."""
        expenses = [
            make_expense(id=1, net=Decimal("10.001")),  # Rounds to 10001
            make_expense(id=2, net=Decimal("20.002")),  # Rounds to 20002
        ]
        expected_total = 30004  # +1 milliunit residual

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        # Largest split (20002) should absorb the +1 adjustment
        assert lines[1].amount_milliunits == 20003

    def test_single_milliunit_negative(self):
        """Residual of -1 milliunit (under threshold)."""
        expenses = [
            make_expense(id=1, net=Decimal("10.001")),
            make_expense(id=2, net=Decimal("20.002")),
        ]
        expected_total = 30002  # -1 milliunit residual

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert lines[1].amount_milliunits == 20001

    def test_multi_cent_residual(self):
        """Residual of 50 milliunits ($0.05) - under threshold."""
        expenses = [
            make_expense(id=1, net=Decimal("33.333")),  # Rounds to 33333
            make_expense(id=2, net=Decimal("33.333")),  # Rounds to 33333
            make_expense(id=3, net=Decimal("33.333")),  # Rounds to 33333
        ]
        # Total rounds to 99999, but using 100049 as expected creates +50 residual

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=100049
        )

        assert sum(line.amount_milliunits for line in lines) == 100049

    def test_threshold_boundary_safe(self):
        """Residual of exactly 99 milliunits (just under threshold)."""
        expenses = [make_expense(id=1, net=Decimal("100.00"))]
        expected_total = 100099  # +99 milliunit residual

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert lines[0].amount_milliunits == 100099


class TestRoundingThresholdViolation:
    """Test cases that exceed safety threshold and should raise errors."""

    def test_threshold_boundary_fail(self):
        """Residual of exactly 101 milliunits ($0.101) - should FAIL."""
        expenses = [make_expense(id=1, net=Decimal("100.00"))]
        expected_total = 100101  # +101 milliunit residual

        with pytest.raises(RoundingError, match="exceeds safety threshold"):
            compute_splits_with_adjustment(
                expenses, user_id=123, expected_total_milliunits=expected_total
            )

    def test_threshold_boundary_exactly_100(self):
        """Residual of exactly 100 milliunits ($0.10) - should PASS (boundary)."""
        expenses = [make_expense(id=1, net=Decimal("100.00"))]
        expected_total = 100100  # +100 milliunit residual (at boundary)

        # Should pass - threshold is > 100, not >= 100
        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_large_mismatch_positive(self):
        """Residual of $1.00+ - clear data integrity issue."""
        expenses = [make_expense(id=1, net=Decimal("100.00"))]
        expected_total = 101000  # +$1.00 residual

        with pytest.raises(RoundingError, match="exceeds safety threshold"):
            compute_splits_with_adjustment(
                expenses, user_id=123, expected_total_milliunits=expected_total
            )

    def test_large_mismatch_negative(self):
        """Residual of -$5.00 - clear data integrity issue."""
        expenses = [make_expense(id=1, net=Decimal("100.00"))]
        expected_total = 95000  # -$5.00 residual

        with pytest.raises(RoundingError, match="exceeds safety threshold"):
            compute_splits_with_adjustment(
                expenses, user_id=123, expected_total_milliunits=expected_total
            )


class TestRoundingSignHandling:
    """Test rounding with negative amounts (outflows)."""

    def test_all_negative_splits(self):
        """All expenses are outflows (user owes money)."""
        expenses = [
            make_expense(id=1, net=Decimal("-10.00")),
            make_expense(id=2, net=Decimal("-20.00")),
        ]
        expected_total = -30000

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_all_positive_splits(self):
        """All expenses are inflows (user is owed money)."""
        expenses = [
            make_expense(id=1, net=Decimal("15.00")),
            make_expense(id=2, net=Decimal("25.00")),
        ]
        expected_total = 40000

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_mixed_signs_net_positive(self):
        """Mix of inflows and outflows, net positive."""
        expenses = [
            make_expense(id=1, net=Decimal("50.00")),  # inflow
            make_expense(id=2, net=Decimal("-20.00")),  # outflow
            make_expense(id=3, net=Decimal("-10.00")),  # outflow
        ]
        expected_total = 20000  # net $20 owed to user

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_mixed_signs_with_residual(self):
        """Mixed signs with rounding residual adjustment."""
        expenses = [
            make_expense(id=1, net=Decimal("50.005")),  # Rounds to 50005
            make_expense(id=2, net=Decimal("-20.003")),  # Rounds to -20003
        ]
        expected_total = 30000  # Residual = -2 milliunits

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        # Largest absolute value is 50005, should absorb -2
        assert lines[0].amount_milliunits == 50003


class TestRoundingAmountRanges:
    """Test rounding with various amount magnitudes."""

    def test_very_small_amounts(self):
        """Sub-dollar amounts (e.g., $0.37 split coffee)."""
        expenses = [
            make_expense(id=1, net=Decimal("0.37")),
            make_expense(id=2, net=Decimal("0.42")),
            make_expense(id=3, net=Decimal("0.21")),
        ]
        expected_total = 1000  # $1.00

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_large_amounts(self):
        """Large amounts like rent ($1500+)."""
        expenses = [
            make_expense(id=1, net=Decimal("1500.00")),
            make_expense(id=2, net=Decimal("250.50")),
        ]
        expected_total = 1750500

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_many_small_splits(self):
        """50+ expenses in one settlement - accumulation test."""
        expenses = [make_expense(id=i, net=Decimal("1.99")) for i in range(50)]
        expected_total = 99500  # 50 * $1.99 = $99.50

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert len(lines) == 50


class TestRoundingDecimalPrecision:
    """Test handling of various decimal precisions."""

    def test_three_decimal_places_exact(self):
        """Three decimals that convert exactly to milliunits."""
        expenses = [make_expense(id=1, net=Decimal("12.345"))]
        expected_total = 12345

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert lines[0].amount_milliunits == 12345

    def test_four_decimal_places(self):
        """Four decimals require rounding."""
        expenses = [make_expense(id=1, net=Decimal("12.3456"))]  # Rounds to 12346
        expected_total = 12346

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_bankers_rounding_half_up(self):
        """Test ROUND_HALF_UP behavior."""
        # $12.5005 with ROUND_HALF_UP should round to 12501
        assert to_milliunits(Decimal("12.5005")) == 12501
        assert to_milliunits(Decimal("12.5004")) == 12500
        assert to_milliunits(Decimal("12.4995")) == 12500

    def test_precision_warning_detection(self):
        """Verify detection of unusual precision in Splitwise data."""
        # Typical data (2 decimals) - should not warn
        normal_data = {
            "expenses": [
                {"id": 1, "cost": "12.34"},
                {"id": 2, "cost": "56.78"},
            ]
        }
        assert verify_no_precision_loss(normal_data) is True

        # Unusual precision (4 decimals) - should warn but not fail
        unusual_data = {
            "expenses": [
                {"id": 1, "cost": "12.3456"},
            ]
        }
        # Should log warning but return True
        assert verify_no_precision_loss(unusual_data) is True


class TestRoundingRealWorldScenarios:
    """Integration-like tests with realistic settlement scenarios."""

    def test_typical_two_person_settlement(self):
        """Realistic scenario: dinner, groceries, utilities."""
        expenses = [
            make_expense(id=1, net=Decimal("45.67")),  # Dinner (you paid)
            make_expense(id=2, net=Decimal("-32.18")),  # Groceries (partner paid)
            make_expense(id=3, net=Decimal("18.50")),  # Utilities (you paid)
        ]
        expected_total = 31990  # Net: $31.99 owed to you

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_rent_split_even(self):
        """Large even split: $2000 rent."""
        expenses = [
            make_expense(id=1, net=Decimal("1000.00")),  # Your half
        ]
        expected_total = 1000000

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_uneven_split_percentage(self):
        """70/30 split on $100 expense."""
        expenses = [
            make_expense(
                id=1, net=Decimal("40.00")
            ),  # You paid $70, owe $30 → net +$40
        ]
        expected_total = 40000

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total


class TestEdgeCasesAndUnusualSituations:
    """Test edge cases and unusual situations."""

    def test_zero_amount_expense(self):
        """Expense with zero net amount."""
        expenses = [
            make_expense(id=1, net=Decimal("0.00")),
            make_expense(id=2, net=Decimal("10.00")),
        ]
        expected_total = 10000

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert lines[0].amount_milliunits == 0
        assert lines[1].amount_milliunits == 10000

    def test_single_expense_only(self):
        """Settlement with only one expense."""
        expenses = [make_expense(id=1, net=Decimal("42.42"))]
        expected_total = 42420

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert len(lines) == 1
        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_alternating_signs_pattern(self):
        """Alternating positive and negative amounts."""
        expenses = [
            make_expense(id=1, net=Decimal("10.00")),
            make_expense(id=2, net=Decimal("-5.00")),
            make_expense(id=3, net=Decimal("15.00")),
            make_expense(id=4, net=Decimal("-8.00")),
            make_expense(id=5, net=Decimal("3.00")),
        ]
        expected_total = 15000  # 10 - 5 + 15 - 8 + 3 = 15

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_nearly_zero_net_result(self):
        """Settlement where positive and negative amounts nearly cancel out."""
        expenses = [
            make_expense(id=1, net=Decimal("100.00")),
            make_expense(id=2, net=Decimal("-99.99")),
        ]
        expected_total = 10  # $0.01

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_all_same_amount(self):
        """All expenses have the same amount."""
        expenses = [make_expense(id=i, net=Decimal("5.55")) for i in range(10)]
        expected_total = 55500  # 10 * $5.55

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert len(lines) == 10

    def test_precision_at_milliunit_boundary(self):
        """Amounts that are exactly at milliunit precision."""
        expenses = [
            make_expense(id=1, net=Decimal("0.001")),  # 1 milliunit
            make_expense(id=2, net=Decimal("0.002")),  # 2 milliunits
            make_expense(id=3, net=Decimal("0.997")),  # 997 milliunits
        ]
        expected_total = 1000  # $1.00

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_extreme_decimal_precision(self):
        """Test with many decimal places."""
        expenses = [
            make_expense(id=1, net=Decimal("10.123456789")),
            make_expense(id=2, net=Decimal("20.987654321")),
        ]
        # These round to 10123 and 20988 = 31111
        expected_total = 31111

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_negative_total_settlement(self):
        """Total settlement is negative (user owes overall)."""
        expenses = [
            make_expense(id=1, net=Decimal("-50.00")),
            make_expense(id=2, net=Decimal("-30.00")),
            make_expense(id=3, net=Decimal("10.00")),
        ]
        expected_total = -70000  # -50 - 30 + 10 = -70

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_rounding_with_one_cent(self):
        """Test rounding with exactly one cent amounts."""
        expenses = [
            make_expense(id=1, net=Decimal("0.01")),
            make_expense(id=2, net=Decimal("0.01")),
            make_expense(id=3, net=Decimal("0.01")),
        ]
        expected_total = 30  # 3 cents

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total

    def test_hundred_expenses(self):
        """Test with 100 expenses (stress test)."""
        expenses = [make_expense(id=i, net=Decimal("1.23")) for i in range(100)]
        expected_total = 123000  # 100 * $1.23

        lines = compute_splits_with_adjustment(
            expenses, user_id=123, expected_total_milliunits=expected_total
        )

        assert sum(line.amount_milliunits for line in lines) == expected_total
        assert len(lines) == 100

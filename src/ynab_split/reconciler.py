"""Core reconciliation logic for computing YNAB split transactions from Splitwise expenses."""

import hashlib
import logging
from decimal import ROUND_HALF_UP, Decimal

from .models import ProposedSplitLine, SplitwiseExpense, SplitwisePayment


class RoundingError(Exception):
    """Raised when rounding residual exceeds safety threshold."""

    pass


def to_milliunits(amount: Decimal) -> int:
    """
    Convert Decimal dollars to integer milliunits.
    Uses ROUND_HALF_UP for consistency.

    Args:
        amount: Dollar amount as Decimal

    Returns:
        Amount in milliunits (integer)
    """
    milliunits = amount * 1000
    return int(milliunits.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def verify_no_precision_loss(splitwise_data: dict) -> bool:
    """
    Verify that Splitwise data doesn't have unexpected precision.
    All amounts should have <= 2 decimal places typically.

    Args:
        splitwise_data: Dictionary with 'expenses' key

    Returns:
        True (always, but logs warnings for unusual precision)
    """
    for expense in splitwise_data.get("expenses", []):
        cost = Decimal(str(expense.get("cost", "0")))
        exponent = cost.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:  # More than 2 decimal places
            logging.warning(f"Expense {expense['id']} has unusual precision: {cost}")

    return True


def determine_expected_total(
    expenses: list[SplitwiseExpense],
    settlement: SplitwisePayment | None,
    user_id: int,
) -> int:
    """
    Determine the expected total in milliunits.

    Priority:
    1. Use settlement amount if available (explicit settle-up)
    2. Otherwise compute from expense nets

    Args:
        expenses: List of Splitwise expenses
        settlement: Optional settlement/payment object
        user_id: User ID to calculate total for

    Returns:
        Expected total in milliunits

    Raises:
        ValueError: If settlement amount doesn't match computed total
                   (beyond rounding threshold)
    """
    # Compute from expenses
    computed_total = Decimal("0")
    for expense in expenses:
        computed_total += expense.get_user_net(user_id)

    computed_milliunits = to_milliunits(computed_total)

    # If explicit settlement exists, use it as source of truth
    if settlement:
        settlement_milliunits = to_milliunits(settlement.amount)

        # Verify computed matches settlement (within threshold)
        residual = abs(settlement_milliunits - computed_milliunits)
        if residual > 100:  # $0.10 threshold
            raise ValueError(
                f"Settlement amount mismatch:\n"
                f"  Splitwise settlement: ${settlement.amount}\n"
                f"  Computed from expenses: ${computed_total}\n"
                f"  Residual: ${residual / 1000:.2f}\n"
                f"This indicates expenses don't match the settlement."
            )

        return settlement_milliunits

    # No explicit settlement, use computed total
    return computed_milliunits


def compute_splits_with_adjustment(
    expenses: list[SplitwiseExpense],
    user_id: int,
    expected_total_milliunits: int,
) -> list[ProposedSplitLine]:
    """
    Compute split lines with automatic rounding adjustment.

    Steps:
    1. Calculate each split's milliunits independently
    2. Sum all splits
    3. Compute residual = expected_total - actual_sum
    4. If residual is within threshold, adjust largest absolute value split
    5. If residual exceeds threshold, raise error

    Args:
        expenses: List of Splitwise expenses
        user_id: User ID to calculate splits for
        expected_total_milliunits: Expected total (from settlement or computed)

    Returns:
        List of proposed split lines

    Raises:
        RoundingError: If residual exceeds safety threshold
    """
    lines = []

    # Step 1: Calculate each split independently using Decimal
    for expense in expenses:
        user_net = expense.get_user_net(user_id)  # Decimal
        amount_milliunits = to_milliunits(user_net)

        lines.append(
            ProposedSplitLine(
                splitwise_expense_id=expense.id,
                amount_milliunits=amount_milliunits,
                memo=f"Splitwise: {expense.description} (exp_{expense.id})",
            )
        )

    # Step 2: Sum all splits
    actual_total = sum(line.amount_milliunits for line in lines)

    # Step 3: Compute residual
    residual = expected_total_milliunits - actual_total

    # Step 4: Validate and adjust
    SAFETY_THRESHOLD = 100  # $0.10 - anything larger indicates data problem

    if abs(residual) > SAFETY_THRESHOLD:
        raise RoundingError(
            f"Total mismatch exceeds safety threshold:\n"
            f"  Expected: {expected_total_milliunits} milliunits "
            f"(${expected_total_milliunits / 1000:.2f})\n"
            f"  Actual:   {actual_total} milliunits "
            f"(${actual_total / 1000:.2f})\n"
            f"  Residual: {residual} milliunits "
            f"(${abs(residual) / 1000:.2f})\n"
            f"  Threshold: {SAFETY_THRESHOLD} milliunits "
            f"(${SAFETY_THRESHOLD / 1000:.2f})\n"
            f"This likely indicates a data integrity issue."
        )

    if residual != 0:
        # Adjust the split with largest absolute value
        # This minimizes relative error impact
        largest_split = max(lines, key=lambda x: abs(x.amount_milliunits))
        largest_split.amount_milliunits += residual

        # Log the adjustment for audit trail
        logging.info(
            f"Applied rounding adjustment: {residual} milliunits "
            f"to expense {largest_split.splitwise_expense_id}"
        )

    # Final verification
    final_total = sum(line.amount_milliunits for line in lines)
    assert final_total == expected_total_milliunits, "Adjustment failed"

    return lines


def compute_draft_hash(expenses: list[SplitwiseExpense], user_id: int) -> str:
    """
    Compute a hash of expense IDs and amounts for idempotency checking.

    Args:
        expenses: List of Splitwise expenses
        user_id: User ID

    Returns:
        SHA256 hash as hex string
    """
    # Create a stable string representation of the expenses
    expense_data = []
    for expense in expenses:
        net = expense.get_user_net(user_id)
        expense_data.append(f"{expense.id}:{net}")

    combined = "|".join(sorted(expense_data))
    return hashlib.sha256(combined.encode()).hexdigest()

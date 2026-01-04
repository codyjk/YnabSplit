"""Splitwise API client."""

import logging
from datetime import date, datetime
from decimal import Decimal

import httpx

from ..models import SplitwiseExpense, SplitwiseUserShare

logger = logging.getLogger(__name__)


class SplitwiseClient:
    """Client for the Splitwise API v3."""

    BASE_URL = "https://secure.splitwise.com/api/v3.0"

    def __init__(self, api_key: str):
        """Initialize the Splitwise client."""
        self.api_key = api_key
        self.client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def get_current_user(self) -> int:
        """Get the authenticated user's ID."""
        response = self.client.get("/get_current_user")
        response.raise_for_status()
        data = response.json()
        user_id: int = data["user"]["id"]
        return user_id

    def get_expenses(
        self,
        group_id: int,
        dated_after: date | None = None,
        dated_before: date | None = None,
        limit: int = 100,
    ) -> list[SplitwiseExpense]:
        """
        Get expenses for a group.

        Args:
            group_id: The Splitwise group ID
            dated_after: Only include expenses after this date
            dated_before: Only include expenses before this date
            limit: Maximum number of expenses to return

        Returns:
            List of Splitwise expenses
        """
        params: dict[str, str | int] = {
            "group_id": group_id,
            "limit": limit,
        }

        if dated_after:
            params["dated_after"] = dated_after.isoformat()
        if dated_before:
            params["dated_before"] = dated_before.isoformat()

        response = self.client.get("/get_expenses", params=params)
        response.raise_for_status()
        data = response.json()

        expenses = []
        for exp_data in data.get("expenses", []):
            # Skip deleted expenses
            if exp_data.get("deleted_at"):
                continue

            # Parse user shares
            users = []
            for user_data in exp_data["users"]:
                users.append(
                    SplitwiseUserShare(
                        user_id=user_data["user_id"],
                        paid_share=Decimal(user_data["paid_share"]),
                        owed_share=Decimal(user_data["owed_share"]),
                        net_balance=Decimal(user_data["net_balance"]),
                    )
                )

            expense = SplitwiseExpense(
                id=exp_data["id"],
                group_id=exp_data["group_id"],
                description=exp_data["description"],
                details=exp_data.get("details"),
                date=datetime.fromisoformat(exp_data["date"].replace("Z", "+00:00")),
                cost=Decimal(exp_data["cost"]),
                currency_code=exp_data["currency_code"],
                payment=exp_data.get("payment", False),
                users=users,
            )
            expenses.append(expense)

        return expenses

    def get_last_settlement_date(self, group_id: int, user_id: int) -> date | None:
        """
        Find the most recent settlement (payment) in the group.

        Args:
            group_id: The Splitwise group ID
            user_id: The user ID to filter payments for

        Returns:
            The date of the most recent settlement, or None if no settlements found
        """
        # Fetch recent expenses (including payments)
        expenses = self.get_expenses(group_id=group_id, limit=100)

        # Filter for payments (settlements)
        settlements = [exp for exp in expenses if exp.payment]

        if not settlements:
            return None

        # Find the most recent settlement
        most_recent = max(settlements, key=lambda s: s.date)
        return most_recent.date.date()

    def get_settlement_history(
        self, group_id: int, count: int = 2
    ) -> list[SplitwiseExpense]:
        """
        Get the N most recent settlements in a group.

        Args:
            group_id: The Splitwise group ID
            count: Number of settlements to retrieve

        Returns:
            List of settlement payments, sorted newest first
        """
        expenses = self.get_expenses(group_id=group_id, limit=1000)
        settlements = [exp for exp in expenses if exp.payment]
        settlements.sort(key=lambda s: s.date, reverse=True)
        return settlements[:count]

    def calculate_current_balance(
        self, group_id: int, user_id: int, since_date: date | None = None
    ) -> Decimal:
        """
        Calculate current balance for a user in a group.

        Args:
            group_id: The Splitwise group ID
            user_id: The user ID
            since_date: Optional date to calculate balance from

        Returns:
            Net balance (negative = user owes, positive = user is owed)
        """
        if since_date:
            expenses = self.get_expenses(
                group_id=group_id, dated_after=since_date, limit=1000
            )
        else:
            # Get recent expenses to calculate current state
            last_settlement = self.get_last_settlement_date(group_id, user_id)
            expenses = self.get_expenses(
                group_id=group_id,
                dated_after=last_settlement,
                limit=1000,
            )

        # Filter out settlements, calculate net
        regular_expenses = [exp for exp in expenses if not exp.payment]
        balance: Decimal = sum(
            (exp.get_user_net(user_id) for exp in regular_expenses), Decimal("0")
        )
        return balance

    def get_expenses_since_last_settlement(
        self, group_id: int, user_id: int
    ) -> tuple[list[SplitwiseExpense], str]:
        """
        Get all expenses since the last settlement, with auto-detection.

        Auto-detects whether to use:
        - Pre-settlement mode: Expenses since last settlement (balance exists)
        - Post-settlement mode: Expenses between last 2 settlements (just settled)

        Args:
            group_id: The Splitwise group ID
            user_id: The user ID

        Returns:
            Tuple of (expenses, mode description)
        """
        from datetime import datetime

        # Get settlement history
        settlements = self.get_settlement_history(group_id, count=2)
        logger.info(f"Found {len(settlements)} settlements in history")

        if not settlements:
            # No settlements ever - get all expenses
            logger.info("No settlements found, fetching all expenses")
            expenses = self.get_expenses(group_id=group_id, limit=1000)
            regular_expenses = [exp for exp in expenses if not exp.payment]
            return regular_expenses, "all time (no previous settlements)"

        last_settlement = settlements[0]
        last_settlement_date = last_settlement.date.date()
        logger.info(
            f"Last settlement ID: {last_settlement.id}, date: {last_settlement_date}"
        )

        # Check if settlement is recent (within 48 hours)
        days_since_settlement = (datetime.now().date() - last_settlement_date).days
        is_recent = days_since_settlement <= 2

        logger.info(
            f"Last settlement: {last_settlement_date} "
            f"({days_since_settlement} days ago, recent={is_recent})"
        )

        if is_recent:
            # Calculate current balance since last settlement
            balance = self.calculate_current_balance(
                group_id, user_id, since_date=last_settlement_date
            )

            logger.info(f"Current balance since settlement: ${balance:.2f}")

            # If balance is near zero (within $1), we're in post-settlement mode
            if abs(balance) < Decimal("1.00"):
                logger.info(
                    "Post-settlement mode detected: "
                    f"recent settlement on {last_settlement_date}, "
                    f"balance ≈ $0 (${balance:.2f})"
                )

                # Fetch expenses between last 2 settlements
                if len(settlements) >= 2:
                    previous_settlement_date = settlements[1].date.date()
                    expenses = self.get_expenses(
                        group_id=group_id,
                        dated_after=previous_settlement_date,
                        dated_before=last_settlement_date,
                        limit=1000,
                    )
                else:
                    # Only one settlement exists, get everything before it
                    expenses = self.get_expenses(
                        group_id=group_id,
                        dated_before=last_settlement_date,
                        limit=1000,
                    )

                regular_expenses = [exp for exp in expenses if not exp.payment]
                return (
                    regular_expenses,
                    f"between settlements (settled on {last_settlement_date})",
                )

        # Pre-settlement mode: get expenses since last settlement
        logger.info(
            f"Pre-settlement mode: fetching expenses since {last_settlement_date}"
        )
        expenses = self.get_expenses(
            group_id=group_id,
            dated_after=last_settlement_date,
            limit=1000,
        )

        regular_expenses = [exp for exp in expenses if not exp.payment]
        return regular_expenses, f"since {last_settlement_date} (pending settlement)"

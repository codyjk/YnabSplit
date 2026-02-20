"""Splitwise API client."""

import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

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
        dated_after: date | datetime | None = None,
        dated_before: date | datetime | None = None,
        limit: int = 100,
    ) -> list[SplitwiseExpense]:
        """
        Get expenses for a group.

        Args:
            group_id: The Splitwise group ID
            dated_after: Only include expenses after this date/datetime
            dated_before: Only include expenses before this date/datetime
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

    def get_group_members(self, group_id: int) -> list[dict]:
        """Get members of a Splitwise group.

        Returns:
            List of dicts with 'id' (int) and 'name' (str) for each member.
        """
        response = self.client.get(f"/get_group/{group_id}")
        response.raise_for_status()
        data = response.json()
        members = []
        for m in data.get("group", {}).get("members", []):
            name = m.get("first_name", "")
            last = m.get("last_name") or ""
            if last:
                name = f"{name} {last}"
            members.append({"id": m["id"], "name": name.strip()})
        return members

    def create_expense(
        self,
        description: str,
        cost: Decimal,
        group_id: int,
        paid_by_user_id: int,
        split_with_user_id: int,
        expense_date: date | None = None,
        currency_code: str = "USD",
    ) -> int:
        """Create a new expense split equally between two users.

        Args:
            description: Expense description.
            cost: Total cost (e.g., Decimal("46.80")).
            group_id: Splitwise group ID.
            paid_by_user_id: ID of the user who paid.
            split_with_user_id: ID of the other user in the split.
            expense_date: Date of the expense (defaults to today).
            currency_code: ISO 4217 currency code.

        Returns:
            The new Splitwise expense ID.
        """
        half = (cost / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        other_half = cost - half  # absorbs any rounding penny

        params: dict[str, str] = {
            "cost": str(cost),
            "description": description,
            "group_id": str(group_id),
            "currency_code": currency_code,
            "users__0__user_id": str(paid_by_user_id),
            "users__0__paid_share": str(cost),
            "users__0__owed_share": str(half),
            "users__1__user_id": str(split_with_user_id),
            "users__1__paid_share": "0.00",
            "users__1__owed_share": str(other_half),
        }
        if expense_date:
            params["date"] = expense_date.isoformat()

        # Splitwise's users__N__* syntax requires form-encoded data.
        # We bypass the client's default Content-Type: application/json by
        # encoding the body manually and setting the header explicitly.
        response = self.client.post(
            "/create_expense",
            content=urlencode(params).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = response.json()

        errors = data.get("errors", {})
        if errors and any(errors.values()):
            raise ValueError(f"Splitwise API error: {errors}")

        return int(data["expenses"][0]["id"])

    def get_expenses_since_last_settlement(
        self, group_id: int, user_id: int
    ) -> tuple[list[SplitwiseExpense], str]:
        """
        Get all expenses since the last settlement.

        Simple approach: Always fetch expenses since the last settlement date.
        Duplicate prevention is handled by YNAB's deterministic import_id system.

        Args:
            group_id: The Splitwise group ID
            user_id: The user ID (unused but kept for API compatibility)

        Returns:
            Tuple of (expenses, mode description)
        """
        logger.info("Fetching expenses since last settlement")

        # Get the last settlement date
        last_settlement_date = self.get_last_settlement_date(group_id, user_id)

        if not last_settlement_date:
            # No settlements ever - get all expenses
            logger.info("No settlements found, fetching all expenses")
            expenses = self.get_expenses(group_id=group_id, limit=1000)
            regular_expenses = [exp for exp in expenses if not exp.payment]
            return regular_expenses, "all time (no previous settlements)"

        # Simple: always fetch expenses since last settlement
        logger.info(f"Fetching expenses since last settlement: {last_settlement_date}")
        expenses = self.get_expenses(
            group_id=group_id,
            dated_after=last_settlement_date,
            limit=1000,
        )

        regular_expenses = [exp for exp in expenses if not exp.payment]
        logger.info(
            f"Found {len(regular_expenses)} expenses since {last_settlement_date}"
        )

        return regular_expenses, f"since {last_settlement_date}"

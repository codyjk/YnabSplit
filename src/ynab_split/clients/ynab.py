"""YNAB API client."""

import hashlib

import httpx

from ..models import ClearingTransactionDraft, YnabAccount, YnabCategory


class YnabClient:
    """Client for the YNAB API v1."""

    BASE_URL = "https://api.ynab.com/v1"

    def __init__(self, access_token: str):
        """Initialize the YNAB client."""
        self.access_token = access_token
        self.client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
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

    def get_categories(
        self, budget_id: str, active_only: bool = True
    ) -> list[YnabCategory]:
        """
        Get categories for a budget.

        Args:
            budget_id: The YNAB budget ID
            active_only: If True, filter out hidden/deleted categories

        Returns:
            List of YNAB categories
        """
        response = self.client.get(f"/budgets/{budget_id}/categories")
        response.raise_for_status()
        data = response.json()

        categories = []
        for group_data in data["data"]["category_groups"]:
            group_name = group_data["name"]

            for cat_data in group_data["categories"]:
                # Skip internal categories (like "Inflow: Ready to Assign")
                if cat_data.get("category_group_id") is None:
                    continue

                category = YnabCategory(
                    id=cat_data["id"],
                    name=cat_data["name"],
                    category_group_name=group_name,
                    hidden=cat_data.get("hidden", False),
                    deleted=cat_data.get("deleted", False),
                )

                # Filter by active status if requested
                if active_only and (category.hidden or category.deleted):
                    continue

                categories.append(category)

        return categories

    def get_accounts(self, budget_id: str) -> list[YnabAccount]:
        """
        Get accounts for a budget.

        Args:
            budget_id: The YNAB budget ID

        Returns:
            List of YNAB accounts
        """
        response = self.client.get(f"/budgets/{budget_id}/accounts")
        response.raise_for_status()
        data = response.json()

        accounts = []
        for acc_data in data["data"]["accounts"]:
            account = YnabAccount(
                id=acc_data["id"],
                name=acc_data["name"],
                type=acc_data["type"],
                on_budget=acc_data.get("on_budget", True),
                closed=acc_data.get("closed", False),
                balance=acc_data.get("balance", 0),
            )
            accounts.append(account)

        return accounts

    def create_transaction(
        self, budget_id: str, draft: ClearingTransactionDraft
    ) -> str:
        """
        Create a split transaction in YNAB from a draft.

        Args:
            budget_id: The YNAB budget ID
            draft: The draft transaction to create

        Returns:
            The created YNAB transaction ID
        """
        # Generate import_id for idempotency (hash of draft_id)
        import_id = self._generate_import_id(draft)

        # Build subtransactions (split lines)
        subtransactions = []
        for line in draft.split_lines:
            subtransactions.append(
                {
                    "amount": line.amount_milliunits,
                    "category_id": line.category_id,
                    "memo": line.memo,
                }
            )

        # Build main transaction
        transaction = {
            "account_id": draft.account_id,
            "date": draft.settlement_date.isoformat(),
            "amount": draft.total_amount_milliunits,
            "payee_name": draft.payee_name,
            "memo": f"Splitwise settlement (draft: {draft.draft_id})",
            "cleared": "cleared",
            "approved": True,
            "import_id": import_id,
            "subtransactions": subtransactions,
        }

        # POST to YNAB
        response = self.client.post(
            f"/budgets/{budget_id}/transactions", json={"transaction": transaction}
        )
        response.raise_for_status()

        data = response.json()
        transaction_id: str = data["data"]["transaction"]["id"]

        return transaction_id

    def _generate_import_id(self, draft: ClearingTransactionDraft) -> str:
        """
        Generate a unique import_id for idempotency.

        YNAB uses import_id to prevent duplicate imports.
        Format: YNABSPLIT:{hash}:{date}

        Args:
            draft: The draft transaction

        Returns:
            Import ID string
        """
        # Create hash from draft_id
        hash_obj = hashlib.sha256(draft.draft_id.encode())
        hash_str = hash_obj.hexdigest()[:16]

        # Format: YNABSPLIT:hash:date (max 36 chars per YNAB spec)
        import_id = f"YNABSPLIT:{hash_str}:{draft.settlement_date.isoformat()}"

        return import_id

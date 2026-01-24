"""YNAB API client."""

import hashlib
import logging
from datetime import date, timedelta
from typing import cast

import httpx

from ..models import ClearingTransactionDraft, YnabAccount, YnabCategory

logger = logging.getLogger(__name__)


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
        # Validate that all split lines have categories
        uncategorized_lines = [
            line for line in draft.split_lines if line.category_id is None
        ]
        if uncategorized_lines:
            error_msg = (
                f"Cannot create transaction: {len(uncategorized_lines)} split line(s) "
                f"are missing category assignments:\n"
            )
            for line in uncategorized_lines:
                error_msg += f"  - {line.memo}\n"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # Generate import_id for idempotency (hash of draft_id)
        import_id = self._generate_import_id(draft)

        # Build subtransactions (split lines)
        subtransactions = []
        for line in draft.split_lines:
            subtransaction = {
                "amount": line.amount_milliunits,
                "category_id": line.category_id,
                "memo": line.memo,
            }
            subtransactions.append(subtransaction)

        # Build main transaction
        transaction = {
            "account_id": draft.account_id,
            "date": draft.settlement_date.isoformat(),
            "amount": draft.total_amount_milliunits,
            "payee_name": draft.payee_name,
            "memo": f"Splitwise settlement (draft: {draft.draft_id})",
            "cleared": "uncleared",  # Leave uncleared so YNAB can auto-match
            "approved": True,
            "import_id": import_id,
            "subtransactions": subtransactions,
        }

        logger.debug(
            f"Creating YNAB transaction with {len(subtransactions)} split lines"
        )
        logger.debug(f"Transaction payload: {transaction}")

        # POST to YNAB
        try:
            response = self.client.post(
                f"/budgets/{budget_id}/transactions", json={"transaction": transaction}
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(f"YNAB API error: {e}")
            logger.error(f"Response body: {e.response.text}")
            raise

        data = response.json()
        transaction_id: str = data["data"]["transaction"]["id"]

        return transaction_id

    def _generate_import_id(self, draft: ClearingTransactionDraft) -> str:
        """
        Generate a unique import_id for idempotency.

        YNAB uses import_id to prevent duplicate imports (max 36 chars).
        Format: YS-{hash}-{date}

        Args:
            draft: The draft transaction

        Returns:
            Import ID string (max 36 characters)
        """
        # Create hash from draft_id
        hash_obj = hashlib.sha256(draft.draft_id.encode())
        hash_str = hash_obj.hexdigest()[:12]  # 12 chars for hash

        # Format: YS-{hash}-{date} = 3 + 12 + 1 + 10 = 26 chars (well under 36)
        import_id = f"YS-{hash_str}-{draft.settlement_date.isoformat()}"

        return import_id

    def get_transaction_by_import_id(
        self, budget_id: str, import_id: str, since_date: str
    ) -> object | None:
        """
        Get a transaction by its import_id.

        Args:
            budget_id: The YNAB budget ID
            import_id: The import_id to search for
            since_date: ISO date string to limit search

        Returns:
            Transaction object if found, None otherwise
        """
        try:
            response = self.client.get(
                f"/budgets/{budget_id}/transactions",
                params={"since_date": since_date},
            )
            response.raise_for_status()
            data = response.json()

            transactions: list[object] = cast(
                list[object], data.get("data", {}).get("transactions", [])
            )

            for transaction in transactions:
                tx_dict = cast(dict[str, object], transaction)
                if tx_dict.get("import_id") == import_id:
                    return transaction

            return None

        except httpx.HTTPError as e:
            logger.warning(f"Error fetching transaction by import_id: {e}")
            return None

    def update_transaction_import_id(
        self, budget_id: str, transaction_id: str, new_import_id: str
    ) -> bool:
        """
        Update a transaction's import_id.

        Args:
            budget_id: The YNAB budget ID
            transaction_id: The transaction ID to update
            new_import_id: The new import_id value

        Returns:
            True if successful, False otherwise
        """
        try:
            # YNAB API requires updating the full transaction
            # First, get the current transaction
            response = self.client.get(
                f"/budgets/{budget_id}/transactions/{transaction_id}"
            )
            response.raise_for_status()
            current_transaction = response.json()["data"]["transaction"]

            # Update only the import_id field
            current_transaction["import_id"] = new_import_id

            # PUT the updated transaction
            response = self.client.put(
                f"/budgets/{budget_id}/transactions/{transaction_id}",
                json={"transaction": current_transaction},
            )
            response.raise_for_status()

            logger.info(
                f"Successfully updated transaction {transaction_id} "
                f"with import_id: {new_import_id}"
            )
            return True

        except httpx.HTTPStatusError as e:
            logger.error(f"YNAB API error updating import_id: {e}")
            logger.error(f"Response body: {e.response.text}")
            return False
        except httpx.HTTPError as e:
            logger.error(f"HTTP error updating import_id: {e}")
            return False

    def get_transactions_since(
        self, budget_id: str, since_date: str
    ) -> list[dict[str, object]]:
        """
        Get all transactions since a given date.

        Args:
            budget_id: The YNAB budget ID
            since_date: ISO date string (YYYY-MM-DD)

        Returns:
            List of transaction dictionaries
        """
        try:
            response = self.client.get(
                f"/budgets/{budget_id}/transactions",
                params={"since_date": since_date},
            )
            response.raise_for_status()
            data = response.json()
            transactions: list[dict[str, object]] = data.get("data", {}).get(
                "transactions", []
            )
            return transactions

        except httpx.HTTPError as e:
            logger.warning(f"Error fetching transactions since {since_date}: {e}")
            return []

    def has_ys_transaction_on_date(self, budget_id: str, transaction_date: str) -> bool:
        """
        Check if a YS- transaction exists on a specific date.

        Args:
            budget_id: The YNAB budget ID
            transaction_date: ISO date string (YYYY-MM-DD)

        Returns:
            True if a YS- transaction exists on this date
        """
        # Search ±7 days around the target date
        target_date = date.fromisoformat(transaction_date)
        since_date = (target_date - timedelta(days=7)).isoformat()

        transactions = self.get_transactions_since(budget_id, since_date)

        for transaction in transactions:
            tx_import_id = transaction.get("import_id")
            tx_date = transaction.get("date")
            if (
                tx_import_id
                and isinstance(tx_import_id, str)
                and tx_import_id.startswith("YS-")
                and tx_date == transaction_date
            ):
                return True

        return False

    def get_most_recent_ys_transaction(
        self, budget_id: str, since_date: str
    ) -> tuple[str, dict[str, object]] | None:
        """
        Get the most recent YS- transaction.

        Args:
            budget_id: The YNAB budget ID
            since_date: ISO date string to start search from

        Returns:
            Tuple of (transaction_date, transaction_dict) or None if not found
        """
        transactions = self.get_transactions_since(budget_id, since_date)

        # Find all YS- transactions
        ys_transactions = []
        for transaction in transactions:
            tx_import_id = transaction.get("import_id")
            tx_date = transaction.get("date")
            if (
                tx_import_id
                and isinstance(tx_import_id, str)
                and tx_import_id.startswith("YS-")
            ):
                if isinstance(tx_date, str):
                    ys_transactions.append((tx_date, transaction))

        if not ys_transactions:
            return None

        # Sort by date, most recent first
        ys_transactions.sort(key=lambda x: x[0], reverse=True)
        return ys_transactions[0]

    def transaction_exists(
        self, budget_id: str, draft: ClearingTransactionDraft
    ) -> bool:
        """
        Check if a transaction for this draft already exists in YNAB.

        Args:
            budget_id: The YNAB budget ID
            draft: The draft transaction to check

        Returns:
            True if a transaction with this import_id exists in YNAB
        """
        # Generate the import_id for this draft
        import_id = self._generate_import_id(draft)

        # Fetch transactions from YNAB around the settlement date
        # Check ±7 days to account for timing differences
        since_date = (draft.settlement_date - timedelta(days=7)).isoformat()
        transactions = self.get_transactions_since(budget_id, since_date)

        logger.info(
            f"Checking YNAB for import_id: {import_id} "
            f"(found {len(transactions)} transactions since {since_date})"
        )

        # Check if any transaction has this import_id
        for transaction in transactions:
            tx_import_id = transaction.get("import_id")
            if tx_import_id == import_id:
                logger.info(
                    f"Found existing transaction in YNAB with import_id: {import_id}"
                )
                return True
            elif (
                tx_import_id
                and isinstance(tx_import_id, str)
                and tx_import_id.startswith("YS-")
            ):
                logger.debug(
                    f"Found YS transaction but different ID: {tx_import_id} "
                    f"(date: {transaction.get('date')})"
                )

        logger.info(f"No matching transaction found for import_id: {import_id}")
        return False

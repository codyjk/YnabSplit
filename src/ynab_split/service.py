"""Service layer that composes Splitwise and YNAB operations.

This module provides a higher-level API that composes the lower-level clients
and reconciliation logic in a functional, immutable way.
"""

import logging

from .categorizer import ExpenseCategorizer
from .clients.openai_client import CategoryClassifier
from .clients.splitwise import SplitwiseClient
from .clients.ynab import YnabClient
from .config import Settings
from .db import Database
from .mapper import CategoryMapper
from .models import (
    ClearingTransactionDraft,
    ProcessedSettlement,
    SplitwiseExpense,
    YnabCategory,
)
from .reconciler import (
    compute_splits_with_adjustment,
    determine_expected_total,
)

logger = logging.getLogger(__name__)


class SettlementService:
    """Service for processing Splitwise settlements into YNAB transactions."""

    def __init__(self, settings: Settings, database: Database):
        """Initialize the settlement service."""
        self.settings = settings
        self.db = database

    def fetch_expenses_since_last_settlement(
        self,
    ) -> tuple[list[SplitwiseExpense], str]:
        """
        Fetch expenses from Splitwise since the last settlement.

        Auto-detects whether to use pre-settlement or post-settlement mode.

        Returns:
            Tuple of (expenses, mode_description)
        """
        with SplitwiseClient(self.settings.splitwise_api_key) as client:
            user_id = client.get_current_user()

            # Get expenses with auto-detection
            expenses, mode = client.get_expenses_since_last_settlement(
                self.settings.splitwise_group_id, user_id
            )

            logger.info(f"Fetched {len(expenses)} expenses {mode}")

            return expenses, mode

    def create_draft_transaction(
        self, expenses: list[SplitwiseExpense]
    ) -> ClearingTransactionDraft:
        """
        Create a draft clearing transaction from Splitwise expenses.

        This is a pure function that transforms expenses into a draft transaction.

        Args:
            expenses: List of Splitwise expenses

        Returns:
            Draft clearing transaction ready for review
        """
        if not expenses:
            raise ValueError("No expenses to process")

        with SplitwiseClient(self.settings.splitwise_api_key) as client:
            user_id = client.get_current_user()

        # Compute expected total (from expense nets)
        expected_total = determine_expected_total(
            expenses=expenses, settlement=None, user_id=user_id
        )

        # Compute split lines with rounding adjustment
        split_lines = compute_splits_with_adjustment(
            expenses=expenses,
            user_id=user_id,
            expected_total_milliunits=expected_total,
        )

        # Determine settlement date (use most recent expense date)
        settlement_date = max(exp.date for exp in expenses).date()

        # Create draft
        draft = ClearingTransactionDraft(
            settlement_date=settlement_date,
            payee_name=self.settings.clearing_payee_name,
            account_id=self.settings.ynab_clearing_account_id,
            total_amount_milliunits=expected_total,
            split_lines=split_lines,
            metadata={
                "splitwise_group_id": self.settings.splitwise_group_id,
                "expense_ids": [exp.id for exp in expenses],
                "user_id": user_id,
            },
        )

        logger.info(
            f"Created draft with {len(split_lines)} split lines, "
            f"total: ${expected_total / 1000:.2f}"
        )

        return draft

    def check_if_already_processed(
        self, draft: ClearingTransactionDraft
    ) -> ProcessedSettlement | None:
        """
        Check if a draft has already been processed (idempotency check).

        Args:
            draft: The draft transaction to check

        Returns:
            Processed settlement record if already processed, None otherwise
        """
        # Compute hash from the draft split lines
        draft_hash = compute_draft_hash_from_draft(draft)

        existing = self.db.get_processed_settlement_by_hash(draft_hash)

        if existing:
            logger.info(f"Draft already processed: {existing.ynab_transaction_id}")

        return existing

    def get_ynab_categories(self) -> list[YnabCategory]:
        """
        Fetch YNAB categories for the configured budget.

        Filters out "Internal Master Category > Uncategorized" as it should
        never be used for expense categorization.

        Returns:
            List of active, usable YNAB categories
        """
        with YnabClient(self.settings.ynab_access_token) as client:
            categories: list[YnabCategory] = client.get_categories(
                budget_id=self.settings.ynab_budget_id, active_only=True
            )

        # Filter out uncategorized
        usable_categories = [
            cat
            for cat in categories
            if not (
                cat.category_group_name == "Internal Master Category"
                and cat.name == "Uncategorized"
            )
        ]

        logger.info(
            f"Fetched {len(usable_categories)} usable YNAB categories "
            f"(filtered {len(categories) - len(usable_categories)} internal)"
        )
        return usable_categories

    def categorize_draft(
        self, draft: ClearingTransactionDraft
    ) -> ClearingTransactionDraft:
        """
        Categorize all split lines in a draft using GPT + cache.

        This mutates the draft's split_lines by adding category information.

        Args:
            draft: The draft transaction to categorize

        Returns:
            The same draft with categorized split lines
        """
        # Get YNAB categories
        categories = self.get_ynab_categories()

        # Initialize categorization components
        mapper = CategoryMapper(self.db)
        classifier = CategoryClassifier(
            api_key=self.settings.openai_api_key, model="gpt-4o-mini"
        )
        categorizer = ExpenseCategorizer(
            mapper=mapper, classifier=classifier, categories=categories
        )

        # Categorize all split lines
        categorizer.categorize_all_split_lines(draft.split_lines)

        logger.info(
            f"Categorized {len(draft.split_lines)} split lines "
            f"({sum(1 for line in draft.split_lines if line.needs_review)} need review)"
        )

        return draft

    def apply_draft(self, draft: ClearingTransactionDraft) -> str:
        """
        Apply a draft transaction by creating it in YNAB.

        This also saves a ProcessedSettlement record to prevent duplicates.

        Args:
            draft: The draft transaction to apply

        Returns:
            YNAB transaction ID
        """
        # Check if already processed (idempotency)
        existing = self.check_if_already_processed(draft)
        if existing:
            logger.warning(f"Draft already processed on {existing.created_at.date()}")
            raise ValueError(
                f"This settlement was already processed on {existing.created_at.date()}. "
                f"YNAB transaction ID: {existing.ynab_transaction_id}"
            )

        # Create transaction in YNAB
        with YnabClient(self.settings.ynab_access_token) as client:
            transaction_id: str = client.create_transaction(
                budget_id=self.settings.ynab_budget_id, draft=draft
            )

        logger.info(f"Created YNAB transaction: {transaction_id}")

        # Save processed settlement record
        draft_hash = compute_draft_hash_from_draft(draft)
        settlement = ProcessedSettlement(
            settlement_date=draft.settlement_date,
            splitwise_group_id=draft.metadata["splitwise_group_id"],
            draft_hash=draft_hash,
            ynab_transaction_id=transaction_id,
        )
        self.db.save_processed_settlement(settlement)

        logger.info(f"Saved processed settlement record (hash: {draft_hash[:8]}...)")

        return transaction_id


def compute_draft_hash_from_draft(draft: ClearingTransactionDraft) -> str:
    """
    Compute hash from draft transaction.

    This is a pure function for idempotency checking.
    """
    # Create a stable string representation
    parts = []
    for line in sorted(draft.split_lines, key=lambda x: x.splitwise_expense_id):
        parts.append(f"{line.splitwise_expense_id}:{line.amount_milliunits}")

    import hashlib

    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()

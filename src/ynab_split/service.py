"""Service layer that composes Splitwise and YNAB operations.

This module provides a higher-level API that composes the lower-level clients
and reconciliation logic in a functional, immutable way.
"""

import hashlib
import logging
from datetime import date

from .categorizer import ExpenseCategorizer
from .clients.openai_client import CategoryClassifier
from .clients.splitwise import SplitwiseClient
from .clients.ynab import YnabClient
from .config import Settings
from .db import Database
from .exceptions import SettlementAlreadyProcessedError
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

    def get_recent_settlements(self, count: int = 3) -> list[SplitwiseExpense]:
        """
        Get recent settlements from Splitwise.

        Args:
            count: Number of recent settlements to fetch

        Returns:
            List of settlement expenses, sorted newest first
        """
        with SplitwiseClient(self.settings.splitwise_api_key) as client:
            settlements: list[SplitwiseExpense] = client.get_settlement_history(
                self.settings.splitwise_group_id, count=count
            )
            logger.info(f"Fetched {len(settlements)} recent settlements")
            return settlements

    def check_settlements_processed(
        self, settlements: list[SplitwiseExpense]
    ) -> list[bool]:
        """
        Check which settlements have already been processed (exist in local DB).

        Args:
            settlements: List of settlements to check

        Returns:
            List of booleans, True if settlement has been processed
        """
        return [self.db.has_settlement_on_date(s.date.date()) for s in settlements]

    def get_most_recent_processed_settlement(
        self, settlements: list[SplitwiseExpense]
    ) -> SplitwiseExpense | None:
        """
        Find the most recent settlement that has been processed (exists in local DB).

        Args:
            settlements: List of settlements to check (sorted newest first)

        Returns:
            The most recent processed settlement, or None if none found
        """
        most_recent_date = self.db.get_most_recent_settlement_date()

        if not most_recent_date:
            logger.info("No processed settlements found in local DB")
            return None

        # Find the settlement that matches this date
        for settlement in settlements:
            if settlement.date.date() == most_recent_date:
                logger.info(
                    f"Found most recent processed settlement: {most_recent_date}"
                )
                return settlement

        logger.warning(
            f"Found processed settlement on {most_recent_date} "
            f"but no matching settlement in Splitwise"
        )
        return None

    def fetch_expenses_after_settlement(
        self, settlement: SplitwiseExpense
    ) -> list[SplitwiseExpense]:
        """
        Fetch expenses after a settlement.

        The selected settlement is the LOWER BOUND (starting point).
        Gets ALL expenses that occurred AFTER the selected settlement timestamp, with no upper bound.

        Args:
            settlement: The settlement to use as the starting point (lower bound)

        Returns:
            List of all expenses after the selected settlement
        """
        with SplitwiseClient(self.settings.splitwise_api_key) as client:
            settlement_datetime = settlement.date

            # Fetch ALL expenses after the selected settlement (no upper bound)
            logger.info(f"Fetching all expenses after {settlement_datetime}")
            expenses = client.get_expenses(
                group_id=self.settings.splitwise_group_id,
                dated_after=settlement_datetime,
                limit=1000,
            )

            # Filter out payment transactions
            regular_expenses = [exp for exp in expenses if not exp.payment]
            logger.info(
                f"Found {len(regular_expenses)} expenses after {settlement_datetime}"
            )

            return regular_expenses

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

        # Compute deterministic draft_id
        expense_ids = [exp.id for exp in expenses]
        draft_id = compute_deterministic_draft_id(expense_ids, settlement_date)

        # Create draft
        draft = ClearingTransactionDraft(
            draft_id=draft_id,
            settlement_date=settlement_date,
            payee_name=self.settings.clearing_payee_name,
            account_id=self.settings.ynab_clearing_account_id,
            total_amount_milliunits=expected_total,
            split_lines=split_lines,
            metadata={
                "splitwise_group_id": self.settings.splitwise_group_id,
                "expense_ids": expense_ids,
                "user_id": user_id,
            },
        )

        logger.info(
            f"Created draft with {len(split_lines)} split lines, "
            f"total: ${expected_total / 1000:.2f}"
        )

        return draft

    def check_if_already_processed(self, draft: ClearingTransactionDraft) -> None:
        """
        Check if a draft has already been processed (idempotency check).

        Uses local database (draft_hash) since we don't set import_id on YNAB
        transactions (omitting import_id allows YNAB to auto-match with bank imports).

        Args:
            draft: The draft transaction to check

        Raises:
            SettlementAlreadyProcessedError: If the settlement has already been processed
        """
        draft_hash = compute_draft_hash_from_draft(draft)
        if self.db.is_settlement_processed(draft_hash):
            logger.info(
                f"Draft already exists in local DB for settlement on {draft.settlement_date}"
            )
            raise SettlementAlreadyProcessedError(str(draft.settlement_date))

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
            mapper=mapper,
            classifier=classifier,
            categories=categories,
            confidence_threshold=self.settings.gpt_confidence_threshold,
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
        self.check_if_already_processed(draft)

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


def compute_deterministic_draft_id(
    expense_ids: list[int], settlement_date: date
) -> str:
    """
    Compute deterministic draft ID from expense IDs and settlement date.

    This ensures the same settlement always generates the same import_id in YNAB,
    allowing proper idempotency checking.

    Args:
        expense_ids: List of Splitwise expense IDs
        settlement_date: The settlement date

    Returns:
        Deterministic draft ID (hex string)
    """
    # Sort expense IDs for consistency
    sorted_ids = sorted(expense_ids)

    # Create stable representation: date|id1|id2|id3
    parts = [settlement_date.isoformat()] + [str(exp_id) for exp_id in sorted_ids]
    combined = "|".join(parts)

    # Return full SHA256 hash as draft_id
    return hashlib.sha256(combined.encode()).hexdigest()


def compute_draft_hash_from_draft(draft: ClearingTransactionDraft) -> str:
    """
    Compute hash from draft transaction.

    This is a pure function for idempotency checking.
    """
    # Create a stable string representation
    parts = []
    for line in sorted(draft.split_lines, key=lambda x: x.splitwise_expense_id):
        parts.append(f"{line.splitwise_expense_id}:{line.amount_milliunits}")

    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()

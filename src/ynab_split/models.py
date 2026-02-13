"""Pydantic domain models for YnabSplit."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# Splitwise Models
# ============================================================================


class SplitwiseUserShare(BaseModel):
    """User's share in a Splitwise expense."""

    user_id: int
    paid_share: Decimal
    owed_share: Decimal
    net_balance: Decimal


class SplitwiseExpense(BaseModel):
    """A Splitwise expense."""

    id: int
    group_id: int
    description: str
    details: str | None = None
    date: datetime
    cost: Decimal
    currency_code: str
    payment: bool = False  # True = settlement, False = expense
    users: list[SplitwiseUserShare]

    def get_user_net(self, user_id: int) -> Decimal:
        """Get net amount for specific user (paid - owed)."""
        for user in self.users:
            if user.user_id == user_id:
                return user.net_balance
        raise ValueError(f"User {user_id} not in expense {self.id}")


class SplitwisePayment(BaseModel):
    """A Splitwise payment/settlement."""

    id: int
    date: datetime
    amount: Decimal
    from_user: int
    to_user: int


# ============================================================================
# YNAB Models
# ============================================================================


class YnabCategory(BaseModel):
    """A YNAB category."""

    id: str
    name: str
    category_group_name: str
    hidden: bool = False
    deleted: bool = False


class YnabAccount(BaseModel):
    """A YNAB account."""

    id: str
    name: str
    type: str
    on_budget: bool = True
    closed: bool = False
    balance: int = 0  # milliunits


# ============================================================================
# Internal Models
# ============================================================================


class ProposedSplitLine(BaseModel):
    """A proposed split line for a YNAB transaction."""

    splitwise_expense_id: int
    amount_milliunits: int  # signed: negative=outflow, positive=inflow
    category_id: str | None = None
    category_name: str | None = None
    memo: str
    confidence: float | None = None  # from GPT
    needs_review: bool = False


class ClearingTransactionDraft(BaseModel):
    """A draft clearing transaction for YNAB.

    ID/Hash Concepts:
    - draft_id: SHA256 hash of (settlement_date + expense_ids). Deterministic
                identifier for this settlement. Stored in YNAB memo for traceability.
    - draft_hash: (see ProcessedSettlement) - computed from split line details,
                  used for local database idempotency.

    Note: We intentionally do NOT set YNAB's import_id. Omitting it makes YNAB
    treat the transaction as manually-entered, enabling auto-matching when the
    real bank transaction arrives.
    """

    draft_id: str  # Deterministic settlement identifier
    settlement_date: date
    payee_name: str
    account_id: str
    total_amount_milliunits: int
    split_lines: list[ProposedSplitLine]
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Configuration Models
# ============================================================================


class CategoryMapping(BaseModel):
    """A cached category mapping."""

    id: int | None = None
    pattern: str  # normalized description
    ynab_category_id: str
    source: Literal["gpt", "manual", "rule"]
    confidence: float | None = None
    rationale: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class ProcessedSettlement(BaseModel):
    """A record of a processed settlement.

    The draft_hash is different from draft_id:
    - draft_hash: SHA256 of split lines (expense_id:amount pairs). Used for local
                  database idempotency checking. Includes actual amounts to detect
                  if the same expenses were settled with different amounts.
    - draft_id: (see ClearingTransactionDraft) - only includes expense IDs + date.
    """

    id: int | None = None
    settlement_date: date
    splitwise_group_id: int
    draft_hash: str  # Hash of split line details for exact duplicate detection
    ynab_transaction_id: str
    created_at: datetime = Field(default_factory=datetime.now)


# ============================================================================
# GPT Models
# ============================================================================


class GPTClassificationResult(BaseModel):
    """Result from GPT category classification."""

    category_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str

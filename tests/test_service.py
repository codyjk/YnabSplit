"""Tests for SettlementService layer."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from ynab_tools.config import Settings
from ynab_tools.db import Database
from ynab_tools.exceptions import SettlementAlreadyProcessedError
from ynab_tools.models import (
    ClearingTransactionDraft,
    ProcessedSettlement,
    ProposedSplitLine,
    SplitwiseExpense,
    SplitwiseUserShare,
)
from ynab_tools.split.service import (
    SettlementService,
    compute_draft_hash_from_draft,
)


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    return Settings(
        splitwise_api_key="test_key",
        splitwise_group_id=123,
        ynab_access_token="test_token",
        ynab_budget_id="test_budget",
        openai_api_key="test_openai_key",
    )


@pytest.fixture
def mock_db(tmp_path):
    """Create a temporary database."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    yield db
    db.close()


@pytest.fixture
def service(mock_settings, mock_db):
    """Create a SettlementService instance."""
    return SettlementService(mock_settings, mock_db)


@pytest.fixture
def sample_expenses():
    """Create sample Splitwise expenses."""
    return [
        SplitwiseExpense(
            id=1,
            group_id=123,
            description="Groceries",
            date=datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
            cost=Decimal("100.00"),
            currency_code="USD",
            payment=False,
            users=[
                SplitwiseUserShare(
                    user_id=1,
                    paid_share=Decimal("100.00"),
                    owed_share=Decimal("50.00"),
                    net_balance=Decimal("50.00"),
                ),
                SplitwiseUserShare(
                    user_id=2,
                    paid_share=Decimal("0.00"),
                    owed_share=Decimal("50.00"),
                    net_balance=Decimal("-50.00"),
                ),
            ],
        ),
        SplitwiseExpense(
            id=2,
            group_id=123,
            description="Dinner",
            date=datetime(2024, 1, 16, 19, 30, 0, tzinfo=UTC),
            cost=Decimal("80.00"),
            currency_code="USD",
            payment=False,
            users=[
                SplitwiseUserShare(
                    user_id=1,
                    paid_share=Decimal("0.00"),
                    owed_share=Decimal("40.00"),
                    net_balance=Decimal("-40.00"),
                ),
                SplitwiseUserShare(
                    user_id=2,
                    paid_share=Decimal("80.00"),
                    owed_share=Decimal("40.00"),
                    net_balance=Decimal("40.00"),
                ),
            ],
        ),
    ]


@pytest.fixture
def sample_settlement():
    """Create a sample settlement."""
    return SplitwiseExpense(
        id=100,
        group_id=123,
        description="Settlement",
        date=datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC),
        cost=Decimal("10.00"),
        currency_code="USD",
        payment=True,
        users=[
            SplitwiseUserShare(
                user_id=1,
                paid_share=Decimal("10.00"),
                owed_share=Decimal("0.00"),
                net_balance=Decimal("10.00"),
            ),
            SplitwiseUserShare(
                user_id=2,
                paid_share=Decimal("0.00"),
                owed_share=Decimal("10.00"),
                net_balance=Decimal("-10.00"),
            ),
        ],
    )


class TestGetRecentSettlements:
    """Tests for get_recent_settlements method."""

    @patch("ynab_tools.split.service.SplitwiseClient")
    def test_returns_settlements_sorted_newest_first(
        self, mock_client_class, service, sample_settlement
    ):
        """Should return settlements sorted by date, newest first."""
        older_settlement = SplitwiseExpense(
            **{
                **sample_settlement.model_dump(),
                "id": 99,
                "date": datetime(2024, 1, 10, 12, 0, 0, tzinfo=UTC),
            }
        )

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get_settlement_history.return_value = [
            sample_settlement,
            older_settlement,
        ]
        mock_client_class.return_value = mock_client

        result = service.get_recent_settlements(count=2)

        assert len(result) == 2
        assert result[0].date > result[1].date
        mock_client.get_settlement_history.assert_called_once_with(123, count=2)

    @patch("ynab_tools.split.service.SplitwiseClient")
    def test_returns_empty_list_when_no_settlements(self, mock_client_class, service):
        """Should return empty list when no settlements found."""
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get_settlement_history.return_value = []
        mock_client_class.return_value = mock_client

        result = service.get_recent_settlements(count=3)

        assert result == []


class TestCheckIfAlreadyProcessed:
    """Tests for check_if_already_processed method."""

    def test_raises_exception_when_already_processed(self, service, mock_db):
        """Should raise SettlementAlreadyProcessedError if already in local DB."""
        draft = ClearingTransactionDraft(
            draft_id="test-draft-id",
            settlement_date=date(2024, 1, 20),
            payee_name="Splitwise Settlement",
            account_id="test-account-id",
            total_amount_milliunits=10000,
            split_lines=[
                ProposedSplitLine(
                    splitwise_expense_id=1,
                    amount_milliunits=10000,
                    memo="Test",
                    category_id=None,
                    category_name=None,
                )
            ],
            metadata={"expense_ids": [1, 2]},
        )

        # Insert a matching settlement record into the real DB
        draft_hash = compute_draft_hash_from_draft(draft)
        mock_db.save_processed_settlement(
            ProcessedSettlement(
                settlement_date=draft.settlement_date,
                splitwise_group_id=123,
                draft_hash=draft_hash,
                ynab_transaction_id="existing-tx-id",
            )
        )

        with pytest.raises(SettlementAlreadyProcessedError) as exc_info:
            service.check_if_already_processed(draft)

        assert "2024-01-20" in str(exc_info.value)

    def test_does_not_raise_when_not_processed(self, service):
        """Should not raise exception if not in local DB."""
        draft = ClearingTransactionDraft(
            draft_id="test-draft-id",
            settlement_date=date(2024, 1, 20),
            payee_name="Splitwise Settlement",
            account_id="test-account-id",
            total_amount_milliunits=10000,
            split_lines=[
                ProposedSplitLine(
                    splitwise_expense_id=1,
                    amount_milliunits=10000,
                    memo="Test",
                    category_id=None,
                    category_name=None,
                )
            ],
            metadata={"expense_ids": [1, 2]},
        )

        # Empty DB - should not raise
        service.check_if_already_processed(draft)


class TestCreateDraftTransaction:
    """Tests for create_draft_transaction method."""

    @patch("ynab_tools.split.service.SplitwiseClient")
    def test_creates_draft_with_correct_totals(
        self, mock_client_class, service, sample_expenses
    ):
        """Should create draft with correct total amount."""
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get_current_user.return_value = 1
        mock_client_class.return_value = mock_client

        draft = service.create_draft_transaction(sample_expenses)

        # User 1: +50 from groceries, -40 from dinner = +10
        assert draft.total_amount_milliunits == 10_000
        assert len(draft.split_lines) == 2
        assert draft.settlement_date == date(2024, 1, 16)  # Latest expense date

    @patch("ynab_tools.split.service.SplitwiseClient")
    def test_draft_has_deterministic_id(
        self, mock_client_class, service, sample_expenses
    ):
        """Should generate deterministic draft_id."""
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get_current_user.return_value = 1
        mock_client_class.return_value = mock_client

        draft1 = service.create_draft_transaction(sample_expenses)
        draft2 = service.create_draft_transaction(sample_expenses)

        # draft_id should be deterministic (same inputs = same ID)
        assert draft1.draft_id == draft2.draft_id
        assert len(draft1.draft_id) == 64  # SHA256 hex length

    @patch("ynab_tools.split.service.SplitwiseClient")
    def test_split_lines_sum_equals_total(
        self, mock_client_class, service, sample_expenses
    ):
        """Should ensure split lines sum exactly equals total (rounding check)."""
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get_current_user.return_value = 1
        mock_client_class.return_value = mock_client

        draft = service.create_draft_transaction(sample_expenses)

        split_sum = sum(line.amount_milliunits for line in draft.split_lines)
        assert split_sum == draft.total_amount_milliunits


class TestFetchExpensesAfterSettlement:
    """Tests for fetch_expenses_after_settlement method."""

    @patch("ynab_tools.split.service.SplitwiseClient")
    def test_fetches_expenses_with_datetime_filter(
        self, mock_client_class, service, sample_settlement, sample_expenses
    ):
        """Should pass full datetime to client, not just date."""
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get_expenses.return_value = sample_expenses
        mock_client_class.return_value = mock_client

        service.fetch_expenses_after_settlement(sample_settlement)

        # Verify it passes the full datetime, not just date
        call_args = mock_client.get_expenses.call_args
        assert call_args[1]["dated_after"] == sample_settlement.date
        assert isinstance(call_args[1]["dated_after"], datetime)

    @patch("ynab_tools.split.service.SplitwiseClient")
    def test_filters_out_payment_transactions(
        self, mock_client_class, service, sample_settlement, sample_expenses
    ):
        """Should exclude payment transactions (settlements)."""
        # Add a payment transaction
        expenses_with_payment = sample_expenses + [sample_settlement]

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get_expenses.return_value = expenses_with_payment
        mock_client_class.return_value = mock_client

        result = service.fetch_expenses_after_settlement(sample_settlement)

        # Should only return non-payment expenses
        assert len(result) == 2
        assert all(not exp.payment for exp in result)


class TestCheckSettlementsProcessed:
    """Tests for check_settlements_processed method."""

    def test_returns_true_for_processed_settlement(self, service, mock_db):
        """Should return True for settlements with matching date in DB."""
        # Insert a processed settlement for 2024-01-20
        mock_db.save_processed_settlement(
            ProcessedSettlement(
                settlement_date=date(2024, 1, 20),
                splitwise_group_id=123,
                draft_hash="abc123",
                ynab_transaction_id="tx-1",
            )
        )

        settlements = [
            SplitwiseExpense(
                id=100,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("10.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
        ]

        result = service.check_settlements_processed(settlements)

        assert result == [True]

    def test_returns_false_for_unprocessed_settlement(self, service):
        """Should return False for settlements not in DB."""
        settlements = [
            SplitwiseExpense(
                id=100,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("10.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
        ]

        result = service.check_settlements_processed(settlements)

        assert result == [False]

    def test_returns_mixed_results(self, service, mock_db):
        """Should return correct booleans for mix of processed/unprocessed."""
        mock_db.save_processed_settlement(
            ProcessedSettlement(
                settlement_date=date(2024, 1, 20),
                splitwise_group_id=123,
                draft_hash="abc123",
                ynab_transaction_id="tx-1",
            )
        )

        settlements = [
            SplitwiseExpense(
                id=101,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 25, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("15.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
            SplitwiseExpense(
                id=100,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("10.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
        ]

        result = service.check_settlements_processed(settlements)

        assert result == [False, True]


class TestGetMostRecentProcessedSettlement:
    """Tests for get_most_recent_processed_settlement method."""

    def test_returns_matching_settlement(self, service, mock_db):
        """Should return the settlement matching the most recent DB date."""
        mock_db.save_processed_settlement(
            ProcessedSettlement(
                settlement_date=date(2024, 1, 20),
                splitwise_group_id=123,
                draft_hash="abc123",
                ynab_transaction_id="tx-1",
            )
        )

        settlements = [
            SplitwiseExpense(
                id=101,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 25, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("15.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
            SplitwiseExpense(
                id=100,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("10.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
        ]

        result = service.get_most_recent_processed_settlement(settlements)

        assert result is not None
        assert result.id == 100
        assert result.date.date() == date(2024, 1, 20)

    def test_returns_none_for_empty_db(self, service):
        """Should return None when no settlements in DB."""
        settlements = [
            SplitwiseExpense(
                id=100,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("10.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
        ]

        result = service.get_most_recent_processed_settlement(settlements)

        assert result is None

    def test_returns_none_when_no_matching_settlement(self, service, mock_db):
        """Should return None when DB date doesn't match any settlement."""
        mock_db.save_processed_settlement(
            ProcessedSettlement(
                settlement_date=date(2024, 1, 10),
                splitwise_group_id=123,
                draft_hash="abc123",
                ynab_transaction_id="tx-1",
            )
        )

        settlements = [
            SplitwiseExpense(
                id=100,
                group_id=123,
                description="Settlement",
                date=datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC),
                cost=Decimal("10.00"),
                currency_code="USD",
                payment=True,
                users=[],
            ),
        ]

        result = service.get_most_recent_processed_settlement(settlements)

        assert result is None

"""Tests for deterministic draft_id generation."""

from datetime import date

from ynab_tools.split.service import compute_deterministic_draft_id


def test_deterministic_draft_id_same_inputs():
    """Same inputs should always produce the same draft_id."""
    expense_ids = [12345, 67890, 11111]
    settlement_date = date(2026, 1, 4)

    draft_id_1 = compute_deterministic_draft_id(expense_ids, settlement_date)
    draft_id_2 = compute_deterministic_draft_id(expense_ids, settlement_date)

    assert draft_id_1 == draft_id_2


def test_deterministic_draft_id_order_independent():
    """Draft_id should be the same regardless of expense_ids order."""
    settlement_date = date(2026, 1, 4)

    draft_id_1 = compute_deterministic_draft_id([12345, 67890, 11111], settlement_date)
    draft_id_2 = compute_deterministic_draft_id([67890, 11111, 12345], settlement_date)
    draft_id_3 = compute_deterministic_draft_id([11111, 12345, 67890], settlement_date)

    assert draft_id_1 == draft_id_2 == draft_id_3


def test_deterministic_draft_id_different_expenses():
    """Different expense_ids should produce different draft_ids."""
    settlement_date = date(2026, 1, 4)

    draft_id_1 = compute_deterministic_draft_id([12345, 67890], settlement_date)
    draft_id_2 = compute_deterministic_draft_id([12345, 99999], settlement_date)

    assert draft_id_1 != draft_id_2


def test_deterministic_draft_id_different_dates():
    """Different settlement dates should produce different draft_ids."""
    expense_ids = [12345, 67890]

    draft_id_1 = compute_deterministic_draft_id(expense_ids, date(2026, 1, 4))
    draft_id_2 = compute_deterministic_draft_id(expense_ids, date(2026, 1, 5))

    assert draft_id_1 != draft_id_2


def test_deterministic_draft_id_format():
    """Draft_id should be a valid SHA256 hex string."""
    expense_ids = [12345, 67890]
    settlement_date = date(2026, 1, 4)

    draft_id = compute_deterministic_draft_id(expense_ids, settlement_date)

    # SHA256 hex string is 64 characters
    assert len(draft_id) == 64
    # Should only contain hex characters
    assert all(c in "0123456789abcdef" for c in draft_id)


def test_deterministic_draft_id_empty_expenses():
    """Should handle empty expense list (edge case)."""
    settlement_date = date(2026, 1, 4)

    draft_id = compute_deterministic_draft_id([], settlement_date)

    # Should still produce a valid hash
    assert len(draft_id) == 64
    assert all(c in "0123456789abcdef" for c in draft_id)


def test_deterministic_draft_id_single_expense():
    """Should handle single expense."""
    settlement_date = date(2026, 1, 4)

    draft_id = compute_deterministic_draft_id([12345], settlement_date)

    # Should produce a valid hash
    assert len(draft_id) == 64
    assert all(c in "0123456789abcdef" for c in draft_id)

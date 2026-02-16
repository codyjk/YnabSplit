"""Interactive UI components for expense categorization."""

import logging
from decimal import Decimal
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from ..models import YnabCategory

logger = logging.getLogger(__name__)


class CategoryCompleter(Completer):
    """Fuzzy search completer for YNAB categories."""

    def __init__(self, categories: list[YnabCategory]):
        """Initialize the completer with available categories."""
        self.categories = categories

        # Build searchable strings and name-to-id mapping
        self.searchable = []
        self.name_to_id = {}
        for cat in categories:
            full_name = f"{cat.category_group_name} > {cat.name}"
            self.searchable.append((cat.id, full_name, cat))
            self.name_to_id[full_name] = cat.id

    def get_completions(self, document: Document, complete_event: Any):
        """Get fuzzy-matched completions."""
        query = document.text.lower()

        if not query:
            # Show all categories when no query
            for _cat_id, full_name, _cat in self.searchable:
                yield Completion(
                    text=full_name,
                    start_position=0,
                    display=full_name,
                )
            return

        # Fuzzy match: all query characters must appear in order
        for _cat_id, full_name, _cat in self.searchable:
            if self._fuzzy_match(query, full_name.lower()):
                yield Completion(
                    text=full_name,
                    start_position=-len(document.text),
                    display=full_name,
                )

    def _fuzzy_match(self, query: str, text: str) -> bool:
        """
        Fuzzy match: all characters in query must appear in order in text.

        Example:
            query="gro" matches "Groceries"
            query="foo" matches "Food & Dining"
        """
        query_idx = 0
        for char in text:
            if query_idx < len(query) and char == query[query_idx]:
                query_idx += 1
        return query_idx == len(query)


def select_category_interactive(
    categories: list[YnabCategory],
    expense_description: str,
    suggested_category_id: str | None = None,
    confidence: float | None = None,
    auto_fill: bool = True,
) -> str | None:
    """
    Interactive category selection with fuzzy search.

    Args:
        categories: Available YNAB categories
        expense_description: Description of the expense being categorized
        suggested_category_id: Optional GPT-suggested category
        confidence: Optional confidence score
        auto_fill: Whether to pre-fill high-confidence suggestions (default: True)

    Returns:
        Selected category ID, or None to skip
    """
    # Filter out uncategorized
    usable_categories = [
        cat
        for cat in categories
        if not (
            cat.category_group_name == "Internal Master Category"
            and cat.name == "Uncategorized"
        )
    ]

    print(f"\n📝 Categorize: {expense_description}")

    # Show suggestion if available
    suggested_name = ""
    if suggested_category_id and confidence:
        for cat in usable_categories:
            if cat.id == suggested_category_id:
                suggested_name = f"{cat.category_group_name} > {cat.name}"
                print(
                    f"   💡 Suggested: {suggested_name} (confidence: {confidence:.2f})"
                )
                break

    print("   Type to search, press Enter to confirm, Ctrl+C to skip\n")

    # Create session with completer
    completer = CategoryCompleter(usable_categories)
    session: PromptSession[str] = PromptSession(completer=completer)

    try:
        # If suggestion exists and confidence is high, pre-fill with category name
        default_text = ""
        if auto_fill and suggested_name and confidence and confidence >= 0.8:
            default_text = suggested_name

        # Loop until valid category or skip
        while True:
            result = session.prompt(
                "Category: ",
                default=default_text,
                complete_while_typing=True,
            )

            if not result:
                # Empty input - skip
                return None

            # Map category name back to ID
            category_id = completer.name_to_id.get(result)
            if category_id:
                # Find the category to log the name
                for cat in usable_categories:
                    if cat.id == category_id:
                        logger.info(f"User selected category: {cat.name}")
                        return category_id

            # Invalid input - show error and retry
            print(
                "❌ Invalid category. Please select from the list or press Tab to complete."
            )
            default_text = ""  # Clear default for retry

    except KeyboardInterrupt:
        print("\n⏭️  Skipped")
        return None
    except EOFError:
        return None


def confirm_category(
    category_id: str, categories: list[YnabCategory], expense_description: str
) -> bool:
    """
    Simple yes/no confirmation for a category assignment.

    Args:
        category_id: The proposed category ID
        categories: Available categories
        expense_description: Description of the expense

    Returns:
        True if confirmed, False otherwise
    """
    # Find category name
    category_name = None
    for cat in categories:
        if cat.id == category_id:
            category_name = f"{cat.category_group_name} > {cat.name}"
            break

    if not category_name:
        return False

    print(f"\n📝 {expense_description}")
    print(f"   → {category_name}")

    response = input("   Confirm? [Y/n] ").strip().lower()

    return response in ("", "y", "yes")


def select_settlement_interactive(
    settlements: list,
    already_processed: list[bool] | None = None,
) -> int | None:
    """
    Interactive settlement selection.

    Args:
        settlements: List of SplitwiseExpense settlement objects, sorted newest first
        already_processed: Optional list of booleans indicating which settlements have been processed

    Returns:
        Index of selected settlement (0-based), or None to cancel
    """
    if not settlements:
        print("\n⚠️  No settlements found")
        return None

    print("\n📅 Recent Settlements:")
    print("Pick the last settlement you logged in YNAB (this is your starting point)\n")

    for idx, settlement in enumerate(settlements):
        # settlement is a SplitwiseExpense with payment=True
        date_str = settlement.date.strftime("%Y-%m-%d %H:%M")
        amount = settlement.cost

        # Find who paid who
        payer_id = None
        receiver_id = None
        for user in settlement.users:
            if user.paid_share > Decimal("0"):
                payer_id = user.user_id
            if user.owed_share > Decimal("0"):
                receiver_id = user.user_id

        # Simple display (could be enhanced with user names)
        direction = (
            f"User {payer_id} → User {receiver_id}"
            if payer_id and receiver_id
            else "Unknown"
        )

        # Show indicator if already processed
        processed_marker = ""
        if (
            already_processed
            and idx < len(already_processed)
            and already_processed[idx]
        ):
            processed_marker = " ✓ (processed)"

        print(f"  [{idx + 1}] {date_str}{processed_marker}")
        print(f"      Amount: ${amount}")
        print(f"      Direction: {direction}")
        print()

    try:
        max_selection = len(settlements)
        response = (
            input(f"Select settlement [1-{max_selection}, or q to quit]: ")
            .strip()
            .lower()
        )

        if response in ("q", "quit", ""):
            return None

        selection = int(response) - 1  # Convert to 0-based index

        if 0 <= selection < len(settlements):
            return selection
        else:
            print("❌ Invalid selection")
            return None

    except (ValueError, KeyboardInterrupt, EOFError):
        print("\n⏭️  Cancelled")
        return None

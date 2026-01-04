"""Interactive UI components for expense categorization."""

import logging
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from .models import YnabCategory

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

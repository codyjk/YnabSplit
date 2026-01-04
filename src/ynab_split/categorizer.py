"""Expense categorization with cache-first logic."""

import logging

from .clients.openai_client import CategoryClassifier
from .mapper import CategoryMapper
from .models import ProposedSplitLine, YnabCategory

logger = logging.getLogger(__name__)


class ExpenseCategorizer:
    """
    Categorizes expenses using a cache-first approach.

    Flow:
    1. Check cache for existing mapping
    2. If not cached, use GPT to classify
    3. Return classification result (cached or fresh)
    """

    def __init__(
        self,
        mapper: CategoryMapper,
        classifier: CategoryClassifier,
        categories: list[YnabCategory],
    ):
        """
        Initialize the categorizer.

        Args:
            mapper: Category mapper for cache operations
            classifier: GPT classifier for new classifications
            categories: Available YNAB categories
        """
        self.mapper = mapper
        self.classifier = classifier
        self.categories = categories

    def categorize_split_line(
        self, split_line: ProposedSplitLine
    ) -> tuple[str | None, float | None, bool]:
        """
        Categorize a single split line.

        Args:
            split_line: The proposed split line to categorize

        Returns:
            Tuple of (category_id, confidence, is_from_cache)
        """
        description = split_line.memo

        # Check cache first
        cached = self.mapper.get_cached_mapping(description)
        if cached:
            logger.info(
                f"Using cached category for '{description}': {cached.ynab_category_id}"
            )
            return cached.ynab_category_id, cached.confidence, True

        # No cache hit - use GPT
        logger.info(f"No cache for '{description}', using GPT classification")

        result = self.classifier.classify_expense(
            description=description,
            details=None,  # Split lines don't have separate details
            available_categories=self.categories,
        )

        # Save to cache
        self.mapper.save_mapping(
            description=description,
            category_id=result.category_id,
            source="gpt",
            confidence=result.confidence,
            rationale=result.rationale,
        )

        return result.category_id, result.confidence, False

    def categorize_all_split_lines(
        self, split_lines: list[ProposedSplitLine]
    ) -> list[ProposedSplitLine]:
        """
        Categorize all split lines in a draft.

        This mutates the split lines by adding category information.

        Args:
            split_lines: List of proposed split lines

        Returns:
            The same list with category_id and confidence populated
        """
        for split_line in split_lines:
            category_id, confidence, from_cache = self.categorize_split_line(split_line)

            # Find category name
            category_name = None
            for cat in self.categories:
                if cat.id == category_id:
                    category_name = f"{cat.category_group_name} > {cat.name}"
                    break

            # Update split line
            split_line.category_id = category_id
            split_line.category_name = category_name
            split_line.confidence = confidence

            # Flag for review if confidence is low
            if confidence is not None and confidence < 0.7:
                split_line.needs_review = True
                logger.warning(
                    f"Low confidence ({confidence:.2f}) for '{split_line.memo}', "
                    f"flagged for review"
                )

        return split_lines

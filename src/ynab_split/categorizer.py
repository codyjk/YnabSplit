"""Expense categorization with cache-first logic."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        Categorize all split lines with parallel GPT calls.

        This mutates the split lines by adding category information.
        Cache hits are processed synchronously, GPT calls are parallelized.

        Args:
            split_lines: List of proposed split lines

        Returns:
            The same list with category_id and confidence populated
        """
        # First pass: check cache for all items
        uncached_lines = []
        for split_line in split_lines:
            cached = self.mapper.get_cached_mapping(split_line.memo)
            if cached:
                # Cache hit - apply immediately
                self._apply_categorization(
                    split_line, cached.ynab_category_id, cached.confidence
                )
            else:
                # Cache miss - queue for GPT
                uncached_lines.append(split_line)

        # Second pass: parallelize GPT calls for uncached items
        if uncached_lines:
            logger.info(
                f"Categorizing {len(uncached_lines)} expenses with GPT (parallel)"
            )

            # Use ThreadPoolExecutor for parallel API calls
            with ThreadPoolExecutor(max_workers=10) as executor:
                # Submit all GPT classification tasks
                future_to_line = {
                    executor.submit(
                        self.classifier.classify_expense,
                        split_line.memo,
                        None,
                        self.categories,
                    ): split_line
                    for split_line in uncached_lines
                }

                # Process results as they complete
                for future in as_completed(future_to_line):
                    split_line = future_to_line[future]
                    try:
                        result = future.result()

                        # Apply categorization
                        self._apply_categorization(
                            split_line, result.category_id, result.confidence
                        )

                        # Save to cache
                        self.mapper.save_mapping(
                            description=split_line.memo,
                            category_id=result.category_id,
                            source="gpt",
                            confidence=result.confidence,
                            rationale=result.rationale,
                        )

                    except Exception as e:
                        logger.error(f"Error categorizing '{split_line.memo}': {e}")
                        # Leave uncategorized on error
                        split_line.needs_review = True

        return split_lines

    def _apply_categorization(
        self, split_line: ProposedSplitLine, category_id: str, confidence: float | None
    ) -> None:
        """Apply categorization results to a split line."""
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
        if confidence is not None and confidence < 0.9:
            split_line.needs_review = True

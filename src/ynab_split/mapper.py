"""Category mapping cache management."""

import logging
from datetime import datetime

from .db import Database
from .models import CategoryMapping

logger = logging.getLogger(__name__)


def normalize_description(description: str) -> str:
    """
    Normalize an expense description for consistent matching.

    Args:
        description: The raw expense description

    Returns:
        Normalized description (lowercase, stripped)
    """
    return description.lower().strip()


class CategoryMapper:
    """Manages the category mapping cache."""

    def __init__(self, database: Database):
        """Initialize the mapper."""
        self.db = database

    def get_cached_mapping(self, description: str) -> CategoryMapping | None:
        """
        Look up a cached category mapping.

        Args:
            description: The expense description

        Returns:
            Cached mapping if found, None otherwise
        """
        pattern = normalize_description(description)
        mapping = self.db.get_category_mapping(pattern)

        if mapping:
            logger.info(f"Cache hit for '{description}' -> {mapping.ynab_category_id}")
        else:
            logger.debug(f"Cache miss for '{description}'")

        return mapping

    def save_mapping(
        self,
        description: str,
        category_id: str,
        source: str,
        confidence: float | None = None,
        rationale: str | None = None,
    ) -> CategoryMapping:
        """
        Save a new category mapping to the cache.

        Args:
            description: The expense description
            category_id: The YNAB category ID
            source: Source of the mapping ('gpt', 'manual', 'rule')
            confidence: Optional confidence score
            rationale: Optional rationale

        Returns:
            The saved mapping
        """
        pattern = normalize_description(description)

        mapping = CategoryMapping(
            pattern=pattern,
            ynab_category_id=category_id,
            source=source,
            confidence=confidence,
            rationale=rationale,
            created_at=datetime.now(),
        )

        mapping_id = self.db.save_category_mapping(mapping)
        mapping.id = mapping_id

        logger.info(
            f"Saved mapping: '{description}' -> {category_id} (source: {source})"
        )

        return mapping

    def has_cached_mapping(self, description: str) -> bool:
        """
        Check if a mapping exists for this description.

        Args:
            description: The expense description

        Returns:
            True if a cached mapping exists
        """
        return self.get_cached_mapping(description) is not None

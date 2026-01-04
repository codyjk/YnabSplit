"""OpenAI GPT client for category classification."""

import logging

from openai import OpenAI

from ..models import GPTClassificationResult, YnabCategory

logger = logging.getLogger(__name__)


class CategoryClassifier:
    """GPT-based category classifier for expenses."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        """Initialize the classifier."""
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def classify_expense(
        self,
        description: str,
        details: str | None,
        available_categories: list[YnabCategory],
    ) -> GPTClassificationResult:
        """
        Classify an expense into a YNAB category using GPT.

        Args:
            description: The expense description
            details: Optional expense details
            available_categories: List of available YNAB categories

        Returns:
            Classification result with category_id, confidence, rationale
        """
        # Build category context
        category_list = []
        for cat in available_categories:
            category_list.append(f"- {cat.id}: {cat.category_group_name} > {cat.name}")
        categories_text = "\n".join(category_list)

        # Build expense context
        expense_text = f"Description: {description}"
        if details:
            expense_text += f"\nDetails: {details}"

        # System prompt
        system_prompt = """You are a financial category classifier. Given an expense description and a list of available YNAB budget categories, select the most appropriate category.

Your response must be a JSON object with:
- category_id: The exact category ID from the provided list
- confidence: A number between 0.0 and 1.0 indicating your confidence
- rationale: A brief explanation of why you chose this category

Be conservative with confidence scores. Only use 0.9+ for very clear matches."""

        # User prompt
        user_prompt = f"""Classify this expense:

{expense_text}

Available categories:
{categories_text}

Select the best category for this expense."""

        # Call GPT with structured output
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        # Parse response
        import json

        result_json = json.loads(response.choices[0].message.content or "{}")

        logger.info(
            f"GPT classified '{description}' -> {result_json.get('category_id')} "
            f"(confidence: {result_json.get('confidence')})"
        )

        return GPTClassificationResult(
            category_id=result_json["category_id"],
            confidence=float(result_json["confidence"]),
            rationale=result_json["rationale"],
        )

    def classify_batch(
        self,
        expenses: list[tuple[str, str | None]],
        available_categories: list[YnabCategory],
    ) -> list[GPTClassificationResult]:
        """
        Classify multiple expenses in sequence.

        Args:
            expenses: List of (description, details) tuples
            available_categories: List of available YNAB categories

        Returns:
            List of classification results
        """
        results = []
        for description, details in expenses:
            result = self.classify_expense(description, details, available_categories)
            results.append(result)
        return results

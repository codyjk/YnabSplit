"""Custom exceptions for YnabSplit."""


class YnabSplitError(Exception):
    """Base exception for all YnabSplit errors."""

    pass


class ConfigurationError(YnabSplitError):
    """Raised when configuration is invalid or missing."""

    pass


class SettlementNotFoundError(YnabSplitError):
    """Raised when no settlements are found in Splitwise."""

    pass


class SettlementAlreadyProcessedError(YnabSplitError):
    """Raised when attempting to process a settlement that already exists in YNAB."""

    def __init__(self, settlement_date: str, message: str | None = None):
        self.settlement_date = settlement_date
        super().__init__(
            message
            or f"Settlement on {settlement_date} has already been processed in YNAB"
        )


class APIError(YnabSplitError):
    """Base class for API-related errors."""

    pass


class SplitwiseAPIError(APIError):
    """Raised when Splitwise API request fails."""

    pass


class YnabAPIError(APIError):
    """Raised when YNAB API request fails."""

    pass


class OpenAIAPIError(APIError):
    """Raised when OpenAI API request fails."""

    pass


class CategorizationError(YnabSplitError):
    """Raised when expense categorization fails."""

    pass


class RoundingError(YnabSplitError):
    """Raised when split line totals don't match settlement amount after adjustment."""

    pass

"""Custom exceptions for YNAB Tools."""


class YnabToolsError(Exception):
    """Base exception for all YNAB Tools errors."""

    pass


class ConfigurationError(YnabToolsError):
    """Raised when configuration is invalid or missing."""

    pass


class SettlementNotFoundError(YnabToolsError):
    """Raised when no settlements are found in Splitwise."""

    pass


class SettlementAlreadyProcessedError(YnabToolsError):
    """Raised when attempting to process a settlement that already exists in YNAB."""

    def __init__(self, settlement_date: str, message: str | None = None):
        self.settlement_date = settlement_date
        super().__init__(
            message
            or f"Settlement on {settlement_date} has already been processed in YNAB"
        )


class APIError(YnabToolsError):
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


class CategorizationError(YnabToolsError):
    """Raised when expense categorization fails."""

    pass


class RoundingError(YnabToolsError):
    """Raised when split line totals don't match settlement amount after adjustment."""

    pass

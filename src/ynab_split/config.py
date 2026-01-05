"""Configuration management for YnabSplit."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Splitwise API
    splitwise_api_key: str
    splitwise_group_id: int

    # YNAB API
    ynab_access_token: str
    ynab_budget_id: str
    ynab_clearing_account_id: str

    # OpenAI API
    openai_api_key: str

    # Transaction settings
    clearing_payee_name: str = "Venmo"  # Payee name for clearing transactions

    # Categorization settings
    gpt_confidence_threshold: float = 0.9  # Flag for review if confidence < threshold

    # Database path
    database_path: Path = Path.home() / ".ynab_split" / "ynab_split.db"

    def __init__(self, **kwargs):
        """Initialize settings and create database directory if needed."""
        super().__init__(**kwargs)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    """Load application settings from environment variables."""
    try:
        return Settings()
    except Exception as e:
        raise ValueError(
            f"Failed to load settings. Make sure you have created a .env file "
            f"with all required variables. See .env.example for reference.\n"
            f"Error: {e}"
        ) from e

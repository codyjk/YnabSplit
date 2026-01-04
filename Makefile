.PHONY: help install dev-install lint format typecheck test test-rounding pre-commit clean run-draft run-apply default

.DEFAULT_GOAL := default

default: dev-install run-draft ## Install dependencies and run draft command (default target)

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	uv sync --no-dev

dev-install: ## Install all dependencies including dev tools
	uv sync
	uv run pre-commit install

lint: ## Run ruff linter
	uv run ruff check src/ tests/

format: ## Format code with ruff
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

typecheck: ## Run mypy type checker
	uv run mypy src/

test: ## Run all tests
	uv run pytest tests/ -v

test-rounding: ## Run exhaustive rounding error tests
	uv run pytest tests/test_rounding.py -v

test-coverage: ## Run tests with coverage report
	uv run pytest tests/ --cov=src/ynab_split --cov-report=html --cov-report=term

pre-commit: ## Run pre-commit hooks on all files
	uv run pre-commit run --all-files

clean: ## Clean up build artifacts and cache
	rm -rf .venv
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +

clear-cache: ## Clear category mapping cache
	@if [ -f ~/.ynab_split/ynab_split.db ]; then \
		sqlite3 ~/.ynab_split/ynab_split.db "DELETE FROM category_mappings; VACUUM;"; \
		echo "✓ Category mapping cache cleared"; \
	else \
		echo "No cache file found at ~/.ynab_split/ynab_split.db"; \
		echo "Run 'make' or 'ynab-split draft' first to create the database"; \
	fi

run-draft: ## Run draft command with categorization and review (dry-run mode)
	uv run ynab-split draft --since-last-settlement --categorize --review-all

run-apply: ## Run apply command with categorization (creates YNAB transaction)
	uv run ynab-split apply --since-last-settlement --categorize

# Development shortcuts
.PHONY: check fix
check: lint typecheck ## Run all checks (lint + typecheck)
fix: format ## Fix formatting and auto-fixable lint issues

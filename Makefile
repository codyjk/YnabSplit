.PHONY: help install dev-install lint format typecheck test pre-commit clean clear-cache check fix

.DEFAULT_GOAL := install

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: clean ## Install to PATH (uninstalls old version first)
	@echo "Uninstalling old version..."
	@uv tool uninstall ynab-split 2>/dev/null || true
	@echo "Installing fresh version..."
	uv tool install --force --reinstall .
	@echo "✓ Installation complete"

dev-install: ## Install development dependencies
	uv sync
	uv run pre-commit install

lint: ## Run ruff linter
	uv run ruff check src/ tests/

format: ## Format code with ruff
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

typecheck: ## Run mypy type checker
	uv run mypy src/

test: ## Run tests (use -k for specific tests, --cov for coverage)
	uv run pytest tests/ -v

pre-commit: ## Run pre-commit hooks on all files
	uv run pre-commit run --all-files

clean: ## Clean up build artifacts, cache, and installed tool
	@echo "Cleaning build artifacts..."
	@rm -rf build/ dist/ *.egg-info src/*.egg-info 2>/dev/null || true
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ Clean complete"

clear-cache: ## Clear category mapping cache
	@if [ -f ~/.ynab_split/ynab_split.db ]; then \
		sqlite3 ~/.ynab_split/ynab_split.db "DELETE FROM category_mappings; VACUUM;"; \
		echo "✓ Category mapping cache cleared"; \
	else \
		echo "No cache file found"; \
	fi

check: lint typecheck ## Run all checks (lint + typecheck)
fix: format ## Fix formatting and auto-fixable lint issues

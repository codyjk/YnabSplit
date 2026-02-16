"""MCP server for YNAB Tools — exposes settlement workflow as tools for Claude."""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from .clients.ynab import YnabClient
from .config import load_settings
from .db import Database
from .exceptions import SettlementAlreadyProcessedError, YnabToolsError
from .models import (
    ClearingTransactionDraft,
    SplitwiseExpense,
    YnabCategory,
)
from .split.mapper import CategoryMapper
from .split.service import SettlementService

logger = logging.getLogger(__name__)

mcp_app = FastMCP("ynab-tools")

# ---------------------------------------------------------------------------
# Session state — one MCP server process = one Claude conversation
# ---------------------------------------------------------------------------

WORKFLOW_INSTRUCTIONS = """\
You are managing YNAB Splitwise settlement clearing. Follow this workflow:

1. DISCOVER: Call list_settlements to see recent settlements.
   Pick the most recent UNPROCESSED settlement. If all are processed, tell the user.

2. FETCH: Call list_expenses with the chosen settlement index.
   Show the user a summary of the expenses found.

3. DRAFT: Call create_draft to compute the split transaction.
   Show the user the draft with amounts and totals.

4. CATEGORIZE: Call categorize_draft to apply cached category mappings.
   - Lines with cached categories are auto-assigned.
   - For any UNCATEGORIZED lines, review the descriptions yourself and decide
     the best YNAB category. Call list_categories if needed, then call
     update_category for each line you categorize.
   - If you're unsure about a category, ask the user.

5. APPLY: Once all categories are confirmed, show the final draft and ask:
   "Ready to create this transaction in YNAB?"
   If yes, call apply_draft. Report the transaction ID.

Always show amounts in accounting format. Negative = outflow (you owe), \
positive = inflow (owed to you).\
"""


@dataclass
class SessionState:
    """Holds state between MCP tool calls within a single conversation."""

    service: SettlementService | None = None
    db: Database | None = None
    ynab_client: YnabClient | None = None
    budget_id: str | None = None
    settlements: list[SplitwiseExpense] = field(default_factory=list)
    expenses: list[SplitwiseExpense] = field(default_factory=list)
    draft: ClearingTransactionDraft | None = None
    categories: list[YnabCategory] = field(default_factory=list)


_state = SessionState()


def _ensure_service() -> SettlementService:
    """Lazily initialize the SettlementService (loads .env config)."""
    if _state.service is None:
        settings = load_settings()
        _state.db = Database(settings.database_path)
        _state.service = SettlementService(settings, _state.db)
        _state.ynab_client = YnabClient(settings.ynab_access_token)
        _state.budget_id = settings.ynab_budget_id
    return _state.service


def _ensure_ynab() -> tuple[YnabClient, str]:
    """Return (YnabClient, budget_id), initializing if needed."""
    _ensure_service()
    assert _state.ynab_client is not None
    assert _state.budget_id is not None
    return _state.ynab_client, _state.budget_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_amount(milliunits: int) -> str:
    """Format milliunits as accounting-style dollar string."""
    amount = milliunits / 1000
    if amount < 0:
        return f"(${abs(amount):,.2f})"
    return f"${amount:,.2f}"


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp_app.tool()
def list_settlements() -> str:
    """List recent Splitwise settlements and whether each has been processed."""
    try:
        service = _ensure_service()
        settlements = service.get_recent_settlements(count=5)

        if not settlements:
            return "No settlements found in Splitwise."

        processed = service.check_settlements_processed(settlements)
        _state.settlements = settlements

        lines = ["Recent Settlements:"]
        for i, (s, is_processed) in enumerate(zip(settlements, processed, strict=True)):
            status = "PROCESSED" if is_processed else "NOT PROCESSED"
            direction = " | ".join(
                f"User {u.user_id}: net {u.net_balance}" for u in s.users
            )
            lines.append(f"[{i}] {s.date.date()} | ${s.cost} | {direction} | {status}")

        return "\n".join(lines)
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to list settlements: {e}"


@mcp_app.tool()
def list_expenses(settlement_index: int) -> str:
    """Fetch expenses that occurred after a given settlement.

    Args:
        settlement_index: Index into the settlements list from list_settlements.
    """
    try:
        service = _ensure_service()

        if not _state.settlements:
            return "Error: Call list_settlements first."

        if settlement_index < 0 or settlement_index >= len(_state.settlements):
            return (
                f"Error: Invalid index {settlement_index}. "
                f"Valid range: 0–{len(_state.settlements) - 1}"
            )

        settlement = _state.settlements[settlement_index]
        expenses = service.fetch_expenses_after_settlement(settlement)
        _state.expenses = expenses

        if not expenses:
            return f"No expenses found after settlement on {settlement.date.date()}."

        lines = [f"Expenses after {settlement.date.date()} ({len(expenses)} total):"]
        for exp in expenses:
            user_shares = ", ".join(
                f"User {u.user_id}: net {u.net_balance}" for u in exp.users
            )
            lines.append(
                f"  - {exp.description} | {exp.date.date()} | "
                f"${exp.cost} | {user_shares}"
            )

        return "\n".join(lines)
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to fetch expenses: {e}"


@mcp_app.tool()
def create_draft() -> str:
    """Create a draft clearing transaction from the fetched expenses.

    Uses expenses stored from the previous list_expenses call.
    """
    try:
        service = _ensure_service()

        if not _state.expenses:
            return "Error: No expenses loaded. Call list_expenses first."

        draft = service.create_draft_transaction(_state.expenses)

        # Idempotency check
        try:
            service.check_if_already_processed(draft)
        except SettlementAlreadyProcessedError as e:
            return f"This settlement has already been processed: {e}"

        _state.draft = draft

        # Build response
        lines = [
            "Draft Clearing Transaction:",
            f"  Date: {draft.settlement_date}",
            f"  Payee: {draft.payee_name}",
            f"  Total: {_format_amount(draft.total_amount_milliunits)}",
            "",
            "Split Lines:",
        ]
        for i, line in enumerate(draft.split_lines):
            desc = line.memo.replace("Splitwise: ", "").split(" (exp_")[0]
            lines.append(f"  [{i}] {desc} | {_format_amount(line.amount_milliunits)}")

        lines.append("")
        lines.append(f"Total split lines: {len(draft.split_lines)}")
        return "\n".join(lines)
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to create draft: {e}"


@mcp_app.tool()
def categorize_draft() -> str:
    """Apply cached category mappings to the current draft.

    Uses the draft stored from the previous create_draft call.
    Lines with cached mappings get auto-categorized; uncached lines are
    left uncategorized for Claude to handle via update_category.
    """
    try:
        _ensure_service()

        if _state.draft is None:
            return "Error: No draft loaded. Call create_draft first."
        if _state.db is None:
            return "Error: Database not initialized."

        mapper = CategoryMapper(_state.db)
        categories = _state.service.get_ynab_categories() if _state.service else []
        cat_lookup = {cat.id: cat for cat in categories}

        cached_count = 0
        for line in _state.draft.split_lines:
            mapping = mapper.get_cached_mapping(line.memo)
            if mapping:
                cat = cat_lookup.get(mapping.ynab_category_id)
                line.category_id = mapping.ynab_category_id
                line.category_name = (
                    f"{cat.category_group_name} > {cat.name}" if cat else None
                )
                line.confidence = mapping.confidence
                line.needs_review = False
                cached_count += 1

        lines = ["Categorized Draft (cache only):"]
        for i, line in enumerate(_state.draft.split_lines):
            desc = line.memo.replace("Splitwise: ", "").split(" (exp_")[0]
            if line.category_id:
                cat_display = line.category_name or line.category_id
                lines.append(
                    f"  [{i}] {desc} | {_format_amount(line.amount_milliunits)} | {cat_display}"
                )
            else:
                lines.append(
                    f"  [{i}] {desc} | {_format_amount(line.amount_milliunits)} | "
                    f"UNCATEGORIZED"
                )

        uncategorized = len(_state.draft.split_lines) - cached_count
        lines.append("")
        lines.append(f"{cached_count} cached, {uncategorized} uncategorized.")

        return "\n".join(lines)
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to categorize draft: {e}"


@mcp_app.tool()
def list_categories() -> str:
    """List all available YNAB budget categories."""
    try:
        service = _ensure_service()
        categories = service.get_ynab_categories()
        _state.categories = categories

        grouped: dict[str, list[YnabCategory]] = {}
        for cat in categories:
            grouped.setdefault(cat.category_group_name, []).append(cat)

        lines = [f"YNAB Categories ({len(categories)} total):"]
        for group, cats in sorted(grouped.items()):
            lines.append(f"\n  {group}:")
            for cat in sorted(cats, key=lambda c: c.name):
                lines.append(f"    - {cat.name} (id: {cat.id})")

        return "\n".join(lines)
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to list categories: {e}"


@mcp_app.tool()
def update_category(split_line_index: int, category_id: str) -> str:
    """Update the category for a specific split line in the current draft.

    Also saves the mapping to the cache so future expenses with the same
    description will be auto-categorized.

    Args:
        split_line_index: Index of the split line to update (from create_draft output).
        category_id: The YNAB category ID to assign.
    """
    try:
        _ensure_service()

        if _state.draft is None:
            return "Error: No draft loaded. Call create_draft first."

        if split_line_index < 0 or split_line_index >= len(_state.draft.split_lines):
            return (
                f"Error: Invalid index {split_line_index}. "
                f"Valid range: 0–{len(_state.draft.split_lines) - 1}"
            )

        line = _state.draft.split_lines[split_line_index]

        # Find category name
        cat_name = category_id
        if _state.categories:
            for cat in _state.categories:
                if cat.id == category_id:
                    cat_name = f"{cat.category_group_name} > {cat.name}"
                    break

        # Update the line
        line.category_id = category_id
        line.category_name = cat_name
        line.needs_review = False
        line.confidence = 1.0

        # Save manual mapping to cache
        if _state.db is not None:
            mapper = CategoryMapper(_state.db)
            mapper.save_mapping(
                description=line.memo,
                category_id=category_id,
                source="manual",
                confidence=1.0,
                rationale="Updated via MCP",
            )

        desc = line.memo.replace("Splitwise: ", "").split(" (exp_")[0]
        return f"Updated: [{split_line_index}] {desc} -> {cat_name}"
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to update category: {e}"


@mcp_app.tool()
def apply_draft() -> str:
    """Apply the current draft — creates the transaction in YNAB.

    Uses the draft stored from create_draft (and optionally modified by
    categorize_draft / update_category).
    """
    try:
        service = _ensure_service()

        if _state.draft is None:
            return "Error: No draft loaded. Call create_draft first."

        transaction_id = service.apply_draft(_state.draft)

        return (
            f"Transaction created successfully!\n"
            f"YNAB Transaction ID: {transaction_id}\n"
            f"Date: {_state.draft.settlement_date}\n"
            f"Amount: {_format_amount(_state.draft.total_amount_milliunits)}"
        )
    except SettlementAlreadyProcessedError as e:
        return f"This settlement has already been processed: {e}"
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to apply draft: {e}"


@mcp_app.tool()
def get_status() -> str:
    """Show status: last processed settlement and category mapping cache stats."""
    try:
        _ensure_service()
        assert _state.db is not None

        last_date = _state.db.get_most_recent_settlement_date()
        mappings = _state.db.get_all_category_mappings()

        lines = ["YNAB Tools Status:"]
        if last_date:
            lines.append(f"  Last processed settlement: {last_date}")
        else:
            lines.append("  No settlements processed yet.")

        lines.append(f"  Category mappings cached: {len(mappings)}")

        source_counts: dict[str, int] = {}
        for m in mappings:
            source_counts[m.source] = source_counts.get(m.source, 0) + 1
        if source_counts:
            lines.append("  Mapping sources:")
            for source, count in sorted(source_counts.items()):
                lines.append(f"    - {source}: {count}")

        return "\n".join(lines)
    except YnabToolsError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Failed to get status: {e}"


# ---------------------------------------------------------------------------
# Budget Analysis Tools
# ---------------------------------------------------------------------------


@mcp_app.tool()
def get_transactions(
    category_name: str | None = None,
    account_name: str | None = None,
    since_date: str | None = None,
    payee: str | None = None,
) -> str:
    """Fetch transactions from YNAB with optional filters.

    Args:
        category_name: Filter to a specific YNAB category (by name, not ID).
        account_name: Filter to a specific YNAB account (by name, not ID).
        since_date: Only return transactions on or after this date (YYYY-MM-DD).
                    Defaults to 3 months ago.
        payee: Filter to transactions matching this payee name (case-insensitive substring).
    """
    try:
        client, budget_id = _ensure_ynab()

        # Resolve category name to ID
        category_id: str | None = None
        if category_name:
            categories = client.get_categories(budget_id)
            match = next(
                (c for c in categories if c.name.lower() == category_name.lower()),
                None,
            )
            if match is None:
                return f"Error: No category found matching '{category_name}'."
            category_id = match.id

        # Resolve account name to ID
        account_id: str | None = None
        if account_name:
            accounts = client.get_accounts(budget_id)
            match_acc = next(
                (a for a in accounts if a.name.lower() == account_name.lower()),
                None,
            )
            if match_acc is None:
                return f"Error: No account found matching '{account_name}'."
            account_id = match_acc.id

        # Default since_date to 3 months ago
        if since_date is None:
            since_date = (date.today() - timedelta(days=90)).isoformat()

        transactions = client.get_transactions(
            budget_id,
            since_date=since_date,
            account_id=account_id,
            category_id=category_id,
        )

        # Client-side payee filter
        if payee:
            payee_lower = payee.lower()
            transactions = [
                t
                for t in transactions
                if t.payee_name and payee_lower in t.payee_name.lower()
            ]

        if not transactions:
            return "No transactions found matching the filters."

        lines = [f"Transactions ({len(transactions)} total):"]
        for t in transactions:
            lines.append(
                f"  {t.date} | {t.payee_name or '(no payee)'} | "
                f"{_format_amount(t.amount)} | {t.category_name or '(uncategorized)'} | "
                f"{t.account_name}"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch transactions: {e}"


@mcp_app.tool()
def get_monthly_budget(month: str | None = None) -> str:
    """Fetch budget details for a specific month, grouped by category group.

    Args:
        month: Month in YYYY-MM-DD format (e.g. "2026-02-01"). Defaults to
               the first day of the current month.
    """
    try:
        client, budget_id = _ensure_ynab()

        if month is None:
            today = date.today()
            month = today.replace(day=1).isoformat()

        categories = client.get_month_budget(budget_id, month)

        if not categories:
            return f"No budget data found for {month}."

        # Group by category group
        grouped: dict[str, list] = {}
        for cat in categories:
            grouped.setdefault(cat.category_group_name, []).append(cat)

        lines = [f"Budget for {month}:"]
        for group_name in sorted(grouped):
            lines.append(f"\n  {group_name}:")
            for cat in sorted(grouped[group_name], key=lambda c: c.name):
                goal_info = ""
                if cat.goal_type:
                    goal_info = f" | Goal: {cat.goal_type}"
                    if cat.goal_target:
                        goal_info += f" {_format_amount(cat.goal_target)}"
                    if cat.goal_percentage_complete is not None:
                        goal_info += f" ({cat.goal_percentage_complete}%)"
                lines.append(
                    f"    {cat.name}: "
                    f"budgeted {_format_amount(cat.budgeted)} | "
                    f"spent {_format_amount(cat.activity)} | "
                    f"remaining {_format_amount(cat.balance)}"
                    f"{goal_info}"
                )

        return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch monthly budget: {e}"


@mcp_app.tool()
def list_accounts() -> str:
    """List YNAB accounts (name, type, balance). Excludes closed accounts."""
    try:
        client, budget_id = _ensure_ynab()
        accounts = client.get_accounts(budget_id)

        open_accounts = [a for a in accounts if not a.closed]
        if not open_accounts:
            return "No open accounts found."

        lines = [f"Accounts ({len(open_accounts)} open):"]
        for a in sorted(open_accounts, key=lambda a: a.name):
            lines.append(f"  {a.name} | {a.type} | {_format_amount(a.balance)}")

        return "\n".join(lines)
    except Exception as e:
        return f"Failed to list accounts: {e}"


# ---------------------------------------------------------------------------
# MCP Prompts
# ---------------------------------------------------------------------------


@mcp_app.prompt()
def split_workflow() -> str:
    """Orchestration instructions for processing Splitwise settlements."""
    return WORKFLOW_INSTRUCTIONS


BUDGET_ANALYSIS_INSTRUCTIONS = """\
You have access to YNAB budget data via three tools: get_transactions, \
get_monthly_budget, and list_accounts. Use them to help with budget analysis.

## Use Cases

### 1. Category Breakdown (e.g. "Personal" spending)
- Call get_transactions(category_name="Personal") to see all transactions.
- Review descriptions and amounts.
- Suggest which transactions might belong in more specific categories.

### 2. Credit Card Optimization
- Call list_accounts() to see all cards and their types.
- Call get_transactions(account_name="Card Name") for each card to see \
spending by category.
- Compare spending patterns against typical card reward structures.
- Suggest shifting spending to cards with better rewards for those categories.

### 3. Goal / Budget Analysis
- Call get_monthly_budget() for several recent months (e.g. current and \
previous 2 months) to compare budgeted vs actual spending trends.
- Identify categories that are consistently over or under budget.
- Suggest goal adjustments based on actual spending patterns.

### 4. Frivolous / Redundant Spending
- Call get_monthly_budget() to identify discretionary categories.
- Call get_transactions() for those categories to review individual purchases.
- Ask the user about subscriptions or recurring charges that may be redundant.
- Flag unusually large or frequent purchases for review.

## Tips
- Amounts are in milliunits (divide by 1000 for dollars). The tools format \
these for you.
- Activity (spending) is negative in YNAB. Formatted amounts use accounting \
style: ($50.00) = outflow.
- Default transaction window is 3 months. Use since_date for longer periods.
- Always show specific numbers and examples when making recommendations.\
"""


@mcp_app.prompt()
def budget_analysis() -> str:
    """Guidance for using YNAB data tools for budget analysis."""
    return BUDGET_ANALYSIS_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_server():
    """Start the MCP server (stdio transport)."""
    mcp_app.run(transport="stdio")

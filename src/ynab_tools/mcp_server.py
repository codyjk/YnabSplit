"""MCP server for YNAB Tools — exposes settlement workflow as tools for Claude."""

import logging
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP

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
    return _state.service


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
# MCP Prompt
# ---------------------------------------------------------------------------


@mcp_app.prompt()
def split_workflow() -> str:
    """Orchestration instructions for processing Splitwise settlements."""
    return WORKFLOW_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_server():
    """Start the MCP server (stdio transport)."""
    mcp_app.run(transport="stdio")

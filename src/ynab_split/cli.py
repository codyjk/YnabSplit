"""CLI for YnabSplit using Typer."""

import logging
import sys

import typer
from rich.console import Console
from rich.table import Table

from .config import load_settings
from .db import Database
from .mapper import CategoryMapper
from .service import SettlementService
from .ui import confirm_category, select_category_interactive

app = typer.Typer(
    name="ynab-split",
    help="Automate YNAB clearing transactions from Splitwise settlements",
)

console = Console()


def setup_logging(verbose: bool = False):
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


@app.command()
def draft(
    since_last_settlement: bool = typer.Option(
        True, "--since-last-settlement", help="Fetch expenses since last settlement"
    ),
    categorize: bool = typer.Option(
        False, "--categorize", "-c", help="Categorize expenses using GPT"
    ),
    review: bool = typer.Option(
        False, "--review", "-r", help="Interactive review for low-confidence categories"
    ),
    review_all: bool = typer.Option(
        False, "--review-all", help="Interactive review for ALL categories"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """
    Create a draft transaction (dry-run mode).

    Fetches expenses from Splitwise, computes the split transaction,
    and displays what would be created in YNAB without actually creating it.

    Use --categorize to enable GPT-powered category classification.
    Use --review to interactively confirm low-confidence categorizations.
    """
    setup_logging(verbose)

    try:
        # Load configuration
        settings = load_settings()
        db = Database(settings.database_path)

        # Create service
        service = SettlementService(settings, db)

        # Fetch expenses
        console.print("\n[bold blue]Fetching expenses from Splitwise...[/bold blue]")
        expenses, mode = service.fetch_expenses_since_last_settlement()

        if not expenses:
            console.print("[yellow]No expenses found to process.[/yellow]")
            return

        console.print(f"[green]Found {len(expenses)} expenses {mode}[/green]\n")

        # Create draft
        console.print("[bold blue]Computing split transaction...[/bold blue]")
        draft = service.create_draft_transaction(expenses)

        # Check if already processed
        existing = service.check_if_already_processed(draft)
        if existing:
            console.print(
                f"\n[yellow]⚠️  This settlement was already processed on "
                f"{existing.created_at.date()}[/yellow]"
            )
            console.print(
                f"[yellow]YNAB transaction ID: {existing.ynab_transaction_id}[/yellow]\n"
            )
            return

        # Categorize if requested
        if categorize:
            console.print("[bold blue]Categorizing expenses with GPT...[/bold blue]")
            draft = service.categorize_draft(draft)

            # Interactive review for low-confidence categories
            if review or review_all:
                console.print("\n[bold blue]Reviewing categorizations...[/bold blue]\n")
                categories = service.get_ynab_categories()
                mapper = CategoryMapper(db)

                for line in draft.split_lines:
                    # Review all if --review-all, otherwise only review flagged items
                    should_review = review_all or (review and line.needs_review)
                    if should_review and line.category_id:
                        # Show current category and ask for confirmation
                        if not confirm_category(
                            line.category_id, categories, line.memo
                        ):
                            # User rejected - let them select interactively
                            new_category_id = select_category_interactive(
                                categories=categories,
                                expense_description=line.memo,
                                suggested_category_id=line.category_id,
                                confidence=line.confidence,
                            )

                            if new_category_id:
                                # Update the line
                                line.category_id = new_category_id

                                # Find category name
                                for cat in categories:
                                    if cat.id == new_category_id:
                                        line.category_name = (
                                            f"{cat.category_group_name} > {cat.name}"
                                        )
                                        break

                                # Save manual mapping
                                mapper.save_mapping(
                                    description=line.memo,
                                    category_id=new_category_id,
                                    source="manual",
                                    confidence=1.0,
                                    rationale="User override",
                                )

        # Display draft
        display_draft(draft, show_confidence=categorize)

        console.print("\n[bold green]✓ Draft created successfully![/bold green]")

        # Build apply command with appropriate flags
        apply_cmd = "ynab-split apply"
        if categorize:
            apply_cmd += " --categorize"
        if review_all:
            apply_cmd += " --review-all"
        elif review:
            apply_cmd += " --review"

        console.print(
            f"\n[bold]To create this transaction in YNAB, run:[/bold]\n"
            f"  [cyan]{apply_cmd}[/cyan]\n"
        )

    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        if verbose:
            raise
        sys.exit(1)
    finally:
        if "db" in locals():
            db.close()


def format_money(amount: float, use_color: bool = True) -> str:
    """
    Format money in accounting style with alignment.

    Negative amounts use parentheses: ($85.02)
    Positive amounts have spaces:      $85.02
    The spaces ensure decimal points align in tables.
    """
    abs_amount = abs(amount)
    if amount < 0:
        # Negative: ($85.02)
        if use_color:
            formatted = f"($[red]{abs_amount:,.2f}[/red])"
        else:
            formatted = f"(${abs_amount:,.2f})"
    else:
        # Positive:  $85.02  (leading and trailing space for alignment)
        if use_color:
            formatted = f" [green]${abs_amount:,.2f}[/green] "
        else:
            formatted = f" ${abs_amount:,.2f} "
    return formatted


def display_draft(draft, show_confidence: bool = False):
    """Display a draft transaction in a nice table format."""
    total_amount = draft.total_amount_milliunits / 1000

    console.print("\n[bold]Draft Clearing Transaction:[/bold]")
    console.print(f"  Date: {draft.settlement_date}")
    console.print(f"  Payee: {draft.payee_name}")
    console.print(
        f"  Total: {format_money(total_amount)} "
        f"({'inflow' if total_amount > 0 else 'outflow'})"
    )
    console.print()

    # Create table for split lines
    table = Table(title="Split Lines", show_header=True, header_style="bold magenta")
    table.add_column("ID", style="dim", width=10)
    table.add_column("Description", style="cyan", width=40)
    table.add_column("Amount", justify="right", width=12)
    table.add_column("Category", style="yellow", no_wrap=False)
    if show_confidence:
        table.add_column("Confidence", justify="center", style="dim", width=10)

    for line in draft.split_lines:
        amount = line.amount_milliunits / 1000
        amount_str = format_money(amount)

        # Extract expense description from memo
        desc = line.memo.replace("Splitwise: ", "").split(" (exp_")[0]

        # Prepare category display
        category_display = line.category_name or "[dim]Uncategorized[/dim]"
        if line.needs_review:
            category_display = f"⚠️  {category_display}"

        row = [
            str(line.splitwise_expense_id),
            desc[:40] + "..." if len(desc) > 40 else desc,
            amount_str,
            category_display,
        ]

        if show_confidence:
            conf_str = f"{line.confidence:.2f}" if line.confidence is not None else "—"
            row.append(conf_str)

        table.add_row(*row)

    console.print(table)

    # Summary
    console.print()
    console.print("[bold]Summary:[/bold]")
    console.print(f"  Total split lines: {len(draft.split_lines)}")
    console.print(f"  Net amount: {format_money(draft.total_amount_milliunits / 1000)}")

    # Verification
    computed_total = sum(line.amount_milliunits for line in draft.split_lines)
    if computed_total == draft.total_amount_milliunits:
        console.print("  [green]✓ Totals match (no rounding errors)[/green]")
    else:
        console.print(
            f"  [red]✗ Total mismatch: computed {computed_total}, "
            f"expected {draft.total_amount_milliunits}[/red]"
        )


@app.command()
def apply(
    since_last_settlement: bool = typer.Option(
        True, "--since-last-settlement", help="Fetch expenses since last settlement"
    ),
    categorize: bool = typer.Option(
        True, "--categorize", "-c", help="Categorize expenses using GPT"
    ),
    review: bool = typer.Option(
        False, "--review", "-r", help="Interactive review for low-confidence categories"
    ),
    review_all: bool = typer.Option(
        False, "--review-all", help="Interactive review for ALL categories"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """
    Apply a draft transaction (creates actual YNAB transaction).

    Fetches expenses from Splitwise, creates a draft, optionally categorizes
    and reviews it, then creates the transaction in YNAB.
    """
    setup_logging(verbose)

    try:
        # Load configuration
        settings = load_settings()
        db = Database(settings.database_path)

        # Create service
        service = SettlementService(settings, db)

        # Fetch expenses
        console.print("\n[bold blue]Fetching expenses from Splitwise...[/bold blue]")
        expenses, mode = service.fetch_expenses_since_last_settlement()

        if not expenses:
            console.print("[yellow]No expenses found to process.[/yellow]")
            return

        console.print(f"[green]Found {len(expenses)} expenses {mode}[/green]\n")

        # Create draft
        console.print("[bold blue]Computing split transaction...[/bold blue]")
        draft = service.create_draft_transaction(expenses)

        # Check if already processed
        existing = service.check_if_already_processed(draft)
        if existing:
            console.print(
                f"\n[yellow]⚠️  This settlement was already processed on "
                f"{existing.created_at.date()}[/yellow]"
            )
            console.print(
                f"[yellow]YNAB transaction ID: {existing.ynab_transaction_id}[/yellow]\n"
            )
            return

        # Categorize if requested
        if categorize:
            console.print("[bold blue]Categorizing expenses with GPT...[/bold blue]")
            draft = service.categorize_draft(draft)

            # Interactive review for low-confidence categories
            if review or review_all:
                console.print("\n[bold blue]Reviewing categorizations...[/bold blue]\n")
                categories = service.get_ynab_categories()
                mapper = CategoryMapper(db)

                for line in draft.split_lines:
                    # Review all if --review-all, otherwise only review flagged items
                    should_review = review_all or (review and line.needs_review)
                    if should_review and line.category_id:
                        # Show current category and ask for confirmation
                        if not confirm_category(
                            line.category_id, categories, line.memo
                        ):
                            # User rejected - let them select interactively
                            new_category_id = select_category_interactive(
                                categories=categories,
                                expense_description=line.memo,
                                suggested_category_id=line.category_id,
                                confidence=line.confidence,
                            )

                            if new_category_id:
                                # Update the line
                                line.category_id = new_category_id

                                # Find category name
                                for cat in categories:
                                    if cat.id == new_category_id:
                                        line.category_name = (
                                            f"{cat.category_group_name} > {cat.name}"
                                        )
                                        break

                                # Save manual mapping
                                mapper.save_mapping(
                                    description=line.memo,
                                    category_id=new_category_id,
                                    source="manual",
                                    confidence=1.0,
                                    rationale="User override",
                                )

        # Display draft
        display_draft(draft, show_confidence=categorize)

        # Confirmation prompt
        if not yes:
            console.print(
                "\n[bold yellow]⚠️  Ready to create this transaction in YNAB[/bold yellow]"
            )
            confirm = input("Continue? [y/N] ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[yellow]Cancelled.[/yellow]")
                return

        # Apply the draft
        console.print("\n[bold blue]Creating transaction in YNAB...[/bold blue]")
        transaction_id = service.apply_draft(draft)

        console.print("\n[bold green]✓ Transaction created successfully![/bold green]")
        console.print(f"[green]YNAB Transaction ID: {transaction_id}[/green]\n")

    except ValueError as e:
        # Already processed or validation error
        console.print(f"\n[bold yellow]⚠️  {e}[/bold yellow]\n")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        if verbose:
            raise
        sys.exit(1)
    finally:
        if "db" in locals():
            db.close()


@app.command()
def status():
    """
    Show status of last processed settlement.

    This command is not yet implemented (Phase 4).
    """
    console.print("[yellow]The 'status' command is not yet implemented.[/yellow]")
    console.print("[dim]This will be added in Phase 4 of the implementation.[/dim]")


if __name__ == "__main__":
    app()

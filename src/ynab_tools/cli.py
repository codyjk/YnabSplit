"""CLI for YNAB Tools."""

import typer

from .mcp_server import run_server
from .split.cli import app as split_app

app = typer.Typer(
    name="ynab-tools",
    help="A collection of YNAB productivity tools",
)

app.add_typer(split_app, name="split", help="Splitwise settlement clearing")


@app.command()
def mcp():
    """Start the MCP server for Claude integration."""
    run_server()


if __name__ == "__main__":
    app()

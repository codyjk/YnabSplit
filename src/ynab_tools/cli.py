"""CLI for YNAB Tools."""

import typer

from .split.cli import app as split_app

app = typer.Typer(
    name="ynab-tools",
    help="A collection of YNAB productivity tools",
)

app.add_typer(split_app, name="split", help="Splitwise settlement clearing")


if __name__ == "__main__":
    app()

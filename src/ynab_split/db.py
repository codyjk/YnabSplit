"""SQLite database operations for YnabSplit."""

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import CategoryMapping, ProcessedSettlement


class Database:
    """SQLite database manager."""

    def __init__(self, db_path: Path):
        """Initialize database connection."""
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema."""
        cursor = self.conn.cursor()

        # Processed settlements table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                settlement_date DATE NOT NULL,
                splitwise_group_id INTEGER NOT NULL,
                draft_hash TEXT NOT NULL UNIQUE,
                ynab_transaction_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Category mappings table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS category_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL UNIQUE,
                ynab_category_id TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL,
                rationale TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Config table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        self.conn.commit()

    def close(self):
        """Close database connection."""
        self.conn.close()

    # ========================================================================
    # Config operations
    # ========================================================================

    def get_config(self, key: str) -> str | None:
        """Get a config value by key."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        return str(row["value"]) if row else None

    def set_config(self, key: str, value: str):
        """Set a config value."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO config (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_last_processed_date(self) -> date | None:
        """Get the last processed settlement date."""
        value = self.get_config("last_processed_date")
        return date.fromisoformat(value) if value else None

    def set_last_processed_date(self, settlement_date: date):
        """Set the last processed settlement date."""
        self.set_config("last_processed_date", settlement_date.isoformat())

    # ========================================================================
    # Processed settlements operations
    # ========================================================================

    def save_processed_settlement(self, settlement: ProcessedSettlement) -> int:
        """Save a processed settlement record."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO processed_settlements (
                settlement_date, splitwise_group_id, draft_hash,
                ynab_transaction_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                settlement.settlement_date.isoformat(),
                settlement.splitwise_group_id,
                settlement.draft_hash,
                settlement.ynab_transaction_id,
                settlement.created_at.isoformat(),
            ),
        )
        self.conn.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("Failed to insert settlement record")
        return row_id

    def is_settlement_processed(self, draft_hash: str) -> bool:
        """Check if a settlement has already been processed."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id FROM processed_settlements WHERE draft_hash = ?",
            (draft_hash,),
        )
        return cursor.fetchone() is not None

    def get_processed_settlement_by_hash(
        self, draft_hash: str
    ) -> ProcessedSettlement | None:
        """Get a processed settlement by draft hash."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, settlement_date, splitwise_group_id, draft_hash,
                   ynab_transaction_id, created_at
            FROM processed_settlements
            WHERE draft_hash = ?
            """,
            (draft_hash,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        return ProcessedSettlement(
            id=row["id"],
            settlement_date=date.fromisoformat(row["settlement_date"]),
            splitwise_group_id=row["splitwise_group_id"],
            draft_hash=row["draft_hash"],
            ynab_transaction_id=row["ynab_transaction_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ========================================================================
    # Category mappings operations
    # ========================================================================

    def get_category_mapping(self, pattern: str) -> CategoryMapping | None:
        """Get a category mapping by pattern."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, pattern, ynab_category_id, source, confidence,
                   rationale, created_at
            FROM category_mappings
            WHERE pattern = ?
            """,
            (pattern,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        return CategoryMapping(
            id=row["id"],
            pattern=row["pattern"],
            ynab_category_id=row["ynab_category_id"],
            source=row["source"],
            confidence=row["confidence"],
            rationale=row["rationale"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def save_category_mapping(self, mapping: CategoryMapping) -> int:
        """Save a category mapping."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO category_mappings (
                pattern, ynab_category_id, source, confidence,
                rationale, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pattern) DO UPDATE SET
                ynab_category_id = excluded.ynab_category_id,
                source = excluded.source,
                confidence = excluded.confidence,
                rationale = excluded.rationale,
                created_at = excluded.created_at
            """,
            (
                mapping.pattern,
                mapping.ynab_category_id,
                mapping.source,
                mapping.confidence,
                mapping.rationale,
                mapping.created_at.isoformat(),
            ),
        )
        self.conn.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("Failed to insert category mapping")
        return row_id

    def get_all_category_mappings(self) -> list[CategoryMapping]:
        """Get all category mappings."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, pattern, ynab_category_id, source, confidence,
                   rationale, created_at
            FROM category_mappings
            ORDER BY created_at DESC
            """
        )
        return [
            CategoryMapping(
                id=row["id"],
                pattern=row["pattern"],
                ynab_category_id=row["ynab_category_id"],
                source=row["source"],
                confidence=row["confidence"],
                rationale=row["rationale"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in cursor.fetchall()
        ]

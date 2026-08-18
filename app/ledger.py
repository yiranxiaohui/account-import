from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = ROOT_DIR / "data" / "account-import.db"
LEGACY_DATABASE_PATH = ROOT_DIR / "data" / "team-import.db"


class AccountLedgerError(RuntimeError):
    """Raised when the local SQLite account ledger cannot be accessed."""


class AccountLedger:
    """SQLite audit ledger containing account identifiers, never credentials."""

    def __init__(self, path: Path | str | None = None) -> None:
        configured_path = (
            path
            or os.getenv("ACCOUNT_IMPORT_DATABASE_FILE")
            or os.getenv("TEAM_IMPORT_DATABASE_FILE")
            or (
                LEGACY_DATABASE_PATH
                if not DEFAULT_DATABASE_PATH.exists() and LEGACY_DATABASE_PATH.exists()
                else DEFAULT_DATABASE_PATH
            )
        )
        self.path = Path(configured_path).expanduser().resolve()
        self._lock = threading.RLock()
        self._initialized = False

    def record(
        self,
        *,
        operation: Literal["import", "recover_401"],
        status: Literal["success", "failed"],
        job_id: str,
        sub2api_base_url: str,
        email: str,
        card_code: str,
        sub2api_account_id: int | None = None,
        platform: str = "",
        message: str = "",
    ) -> None:
        created_at = datetime.now(UTC).isoformat()
        normalized_email = email.strip()
        normalized_code = card_code.strip()
        normalized_url = sub2api_base_url.rstrip("/")
        try:
            self._ensure_schema()
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO account_events (
                        operation, status, job_id, sub2api_base_url,
                        sub2api_account_id, email, card_code, platform,
                        message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation,
                        status,
                        job_id,
                        normalized_url,
                        sub2api_account_id,
                        normalized_email,
                        normalized_code,
                        platform,
                        message[:1000],
                        created_at,
                    ),
                )
                if status == "success" and sub2api_account_id is not None:
                    connection.execute(
                        """
                        INSERT INTO imported_accounts (
                            sub2api_base_url, sub2api_account_id, email,
                            card_code, platform, last_operation, last_status,
                            last_job_id, last_message, first_recorded_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(sub2api_base_url, sub2api_account_id) DO UPDATE SET
                            email = excluded.email,
                            card_code = excluded.card_code,
                            platform = excluded.platform,
                            last_operation = excluded.last_operation,
                            last_status = excluded.last_status,
                            last_job_id = excluded.last_job_id,
                            last_message = excluded.last_message,
                            updated_at = excluded.updated_at
                        """,
                        (
                            normalized_url,
                            sub2api_account_id,
                            normalized_email,
                            normalized_code,
                            platform,
                            operation,
                            status,
                            job_id,
                            message[:1000],
                            created_at,
                            created_at,
                        ),
                    )
        except sqlite3.Error as exc:
            raise AccountLedgerError(f"写入本地账号记录失败：{exc}") from exc

    def list_accounts(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        safe_limit = max(1, min(limit, 200))
        safe_offset = max(0, offset)
        try:
            self._ensure_schema()
            with self._connect() as connection:
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM imported_accounts"
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    """
                    SELECT id, sub2api_base_url, sub2api_account_id, email,
                           card_code, platform, last_operation, last_status,
                           last_job_id, last_message, first_recorded_at, updated_at
                    FROM imported_accounts
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (safe_limit, safe_offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AccountLedgerError(f"读取本地账号记录失败：{exc}") from exc

        return {"total": total, "items": [dict(row) for row in rows]}

    def list_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        try:
            self._ensure_schema()
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id, operation, status, job_id, sub2api_base_url,
                           sub2api_account_id, email, card_code, platform,
                           message, created_at
                    FROM account_events
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AccountLedgerError(f"读取本地操作记录失败：{exc}") from exc
        return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock:
            if self._initialized:
                return
            parent_exists = self.path.parent.exists()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not parent_exists:
                    os.chmod(self.path.parent, 0o700)
                with self._connect() as connection:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS imported_accounts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            sub2api_base_url TEXT NOT NULL,
                            sub2api_account_id INTEGER NOT NULL,
                            email TEXT NOT NULL,
                            card_code TEXT NOT NULL,
                            platform TEXT NOT NULL DEFAULT '',
                            last_operation TEXT NOT NULL,
                            last_status TEXT NOT NULL,
                            last_job_id TEXT NOT NULL,
                            last_message TEXT NOT NULL DEFAULT '',
                            first_recorded_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(sub2api_base_url, sub2api_account_id)
                        );

                        CREATE TABLE IF NOT EXISTS account_events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            operation TEXT NOT NULL,
                            status TEXT NOT NULL,
                            job_id TEXT NOT NULL,
                            sub2api_base_url TEXT NOT NULL,
                            sub2api_account_id INTEGER,
                            email TEXT NOT NULL,
                            card_code TEXT NOT NULL,
                            platform TEXT NOT NULL DEFAULT '',
                            message TEXT NOT NULL DEFAULT '',
                            created_at TEXT NOT NULL
                        );

                        CREATE INDEX IF NOT EXISTS idx_imported_accounts_email
                            ON imported_accounts(email);
                        CREATE INDEX IF NOT EXISTS idx_imported_accounts_card_code
                            ON imported_accounts(card_code);
                        CREATE INDEX IF NOT EXISTS idx_account_events_job_id
                            ON account_events(job_id);
                        CREATE INDEX IF NOT EXISTS idx_account_events_created_at
                            ON account_events(created_at);
                        """
                    )
                os.chmod(self.path, 0o600)
                self._initialized = True
            except (OSError, sqlite3.Error) as exc:
                raise AccountLedgerError(f"初始化本地账号数据库失败：{exc}") from exc

"""On-disk SQLite sliding-window quota; admission is one short serialized write."""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    tokens INTEGER NOT NULL CHECK (tokens >= 0),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_tenant_time ON reservations (tenant, created_at);
CREATE INDEX IF NOT EXISTS reservations_time ON reservations (created_at);
"""


class QuotaExceeded(Exception):
    """Raised instead of admitting a request; carries no tenant credential."""

    def __init__(self, *, used: int, requested: int, budget: int) -> None:
        self.used = used
        self.requested = requested
        self.budget = budget
        super().__init__("Token quota exceeded")


class QuotaStorageError(Exception):
    """Storage failed, so admission is refused rather than silently unmetered."""

    def __init__(self) -> None:
        super().__init__("Quota storage unavailable")


@dataclass(frozen=True, slots=True)
class Reservation:
    id: int
    tokens: int


class TokenWindowLimiter:
    """Token-aware sliding window over `(now - window, now]`, persisted on disk.

    Each call opens, uses and closes its own connection inside a worker thread, so
    a cancelled await never leaves a transaction open. Admission runs in one
    `BEGIN IMMEDIATE` transaction: the write lock is taken before the usage read,
    so concurrent connections cannot both observe the same headroom and insert.
    No transaction is ever held across provider I/O.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        token_budget: int = 50_000,
        window_seconds: float = 60.0,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if token_budget <= 0 or window_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("Quota settings must be positive")
        self.database_path = Path(database_path)
        self.token_budget = token_budget
        self.window_seconds = window_seconds
        self._timeout = timeout_seconds
        self._clock = clock if clock is not None else time.time

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=self._timeout, isolation_level=None
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def initialize_sync(self) -> None:
        parent = self.database_path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = self._connect()
            try:
                connection.executescript(SCHEMA)
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            logger.error("Quota database initialization failed")
            raise QuotaStorageError() from None

    def reserve_sync(self, tenant: str, tokens: int) -> Reservation:
        if tokens < 0:
            raise ValueError("Reservation must not be negative")
        now = self._clock()
        cutoff = now - self.window_seconds
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # Evict globally, including tenants that stopped sending traffic.
                    connection.execute("DELETE FROM reservations WHERE created_at <= ?", (cutoff,))
                    row = connection.execute(
                        "SELECT COALESCE(SUM(tokens), 0) FROM reservations"
                        " WHERE tenant = ? AND created_at > ?",
                        (tenant, cutoff),
                    ).fetchone()
                    used = int(row[0])
                    admitted = used + tokens <= self.token_budget
                    reservation_id = 0
                    if admitted:
                        cursor = connection.execute(
                            "INSERT INTO reservations (tenant, tokens, created_at)"
                            " VALUES (?, ?, ?)",
                            (tenant, tokens, now),
                        )
                        reservation_id = int(cursor.lastrowid or 0)
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                # Commit the eviction whether or not the request was admitted.
                connection.execute("COMMIT")
            finally:
                connection.close()
        except sqlite3.Error:
            logger.warning("Quota reservation failed against storage")
            raise QuotaStorageError() from None
        if not admitted:
            raise QuotaExceeded(used=used, requested=tokens, budget=self.token_budget)
        return Reservation(id=reservation_id, tokens=tokens)

    def reconcile_sync(self, reservation: Reservation, actual_tokens: int) -> int:
        """Lower a reservation to reported usage; it is never raised above the reserve.

        The reservation is the amount the gateway authorized, so a misreporting or
        compromised provider cannot inflate a tenant's recorded consumption.
        """
        charged = max(0, min(actual_tokens, reservation.tokens))
        try:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE reservations SET tokens = ? WHERE id = ?", (charged, reservation.id)
                )
            finally:
                connection.close()
        except sqlite3.Error:
            logger.warning("Quota reconciliation failed; the full reservation stands")
            raise QuotaStorageError() from None
        return charged

    def usage_sync(self, tenant: str) -> int:
        cutoff = self._clock() - self.window_seconds
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM reservations"
                    " WHERE tenant = ? AND created_at > ?",
                    (tenant, cutoff),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            raise QuotaStorageError() from None
        return int(row[0])

    def purge_sync(self) -> int:
        cutoff = self._clock() - self.window_seconds
        try:
            connection = self._connect()
            try:
                cursor = connection.execute(
                    "DELETE FROM reservations WHERE created_at <= ?", (cutoff,)
                )
            finally:
                connection.close()
        except sqlite3.Error:
            raise QuotaStorageError() from None
        return cursor.rowcount

    async def initialize(self) -> None:
        await asyncio.to_thread(self.initialize_sync)

    async def reserve(self, tenant: str, tokens: int) -> Reservation:
        return await asyncio.to_thread(self.reserve_sync, tenant, tokens)

    async def reconcile(self, reservation: Reservation, actual_tokens: int) -> int:
        return await asyncio.to_thread(self.reconcile_sync, reservation, actual_tokens)

    async def usage(self, tenant: str) -> int:
        return await asyncio.to_thread(self.usage_sync, tenant)

    async def purge(self) -> int:
        return await asyncio.to_thread(self.purge_sync)

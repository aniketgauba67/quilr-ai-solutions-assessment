"""Sliding-window admission on real on-disk SQLite, including concurrent writers."""

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quilr_assessment.task4_model_router.limiter import (
    QuotaExceeded,
    QuotaStorageError,
    Reservation,
    TokenWindowLimiter,
)
from quilr_assessment.task4_model_router.tenants import fingerprint

TENANT_A = fingerprint("tenant-a-key")
TENANT_B = fingerprint("tenant-b-key")


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(path: Path, *, clock: Clock | None = None, budget: int = 50_000) -> TokenWindowLimiter:
    limiter = TokenWindowLimiter(
        path / "quota.sqlite3",
        token_budget=budget,
        window_seconds=60.0,
        clock=clock if clock is not None else Clock(),
    )
    limiter.initialize_sync()
    return limiter


def test_database_is_created_on_disk_with_the_expected_schema(tmp_path: Path) -> None:
    limiter = build(tmp_path)
    assert limiter.database_path.is_file()
    connection = sqlite3.connect(limiter.database_path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reservations)")}
        (journal,) = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()
    assert "reservations" in tables
    assert {"reservations_tenant_time", "reservations_time"} <= tables
    assert columns == {"id", "tenant", "tokens", "created_at"}
    assert journal.lower() == "wal"


def test_initialization_is_idempotent_and_creates_missing_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    limiter = TokenWindowLimiter(nested / "quota.sqlite3", token_budget=10)
    limiter.initialize_sync()
    limiter.initialize_sync()
    assert limiter.database_path.is_file()


def test_requests_below_the_budget_are_admitted(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=1_000)
    first = limiter.reserve_sync(TENANT_A, 400)
    second = limiter.reserve_sync(TENANT_A, 400)
    assert first.id != second.id
    assert limiter.usage_sync(TENANT_A) == 800


def test_exactly_at_the_limit_is_admitted_and_one_more_token_is_not(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=1_000)
    limiter.reserve_sync(TENANT_A, 999)
    limiter.reserve_sync(TENANT_A, 1)
    assert limiter.usage_sync(TENANT_A) == 1_000
    with pytest.raises(QuotaExceeded) as failure:
        limiter.reserve_sync(TENANT_A, 1)
    assert failure.value.used == 1_000
    assert failure.value.requested == 1
    assert failure.value.budget == 1_000


def test_a_single_request_larger_than_the_budget_is_rejected(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=1_000)
    with pytest.raises(QuotaExceeded):
        limiter.reserve_sync(TENANT_A, 1_001)
    assert limiter.usage_sync(TENANT_A) == 0


def test_rejection_records_nothing_and_leaves_the_window_reusable(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=100)
    limiter.reserve_sync(TENANT_A, 100)
    for _ in range(3):
        with pytest.raises(QuotaExceeded):
            limiter.reserve_sync(TENANT_A, 1)
    assert limiter.usage_sync(TENANT_A) == 100


def test_tenants_have_independent_budgets(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=1_000)
    limiter.reserve_sync(TENANT_A, 1_000)
    limiter.reserve_sync(TENANT_B, 1_000)
    assert limiter.usage_sync(TENANT_A) == 1_000
    assert limiter.usage_sync(TENANT_B) == 1_000
    with pytest.raises(QuotaExceeded):
        limiter.reserve_sync(TENANT_B, 1)


def test_window_boundary_is_exact_and_expired_tokens_free_capacity(tmp_path: Path) -> None:
    clock = Clock()
    limiter = build(tmp_path, clock=clock, budget=1_000)
    limiter.reserve_sync(TENANT_A, 1_000)
    clock.advance(59.9)
    assert limiter.usage_sync(TENANT_A) == 1_000
    with pytest.raises(QuotaExceeded):
        limiter.reserve_sync(TENANT_A, 1)
    # The window is half-open: an event exactly `window` old has left it.
    clock.advance(0.1)
    assert limiter.usage_sync(TENANT_A) == 0
    limiter.reserve_sync(TENANT_A, 1_000)


def test_the_window_slides_rather_than_resetting(tmp_path: Path) -> None:
    clock = Clock()
    limiter = build(tmp_path, clock=clock, budget=1_000)
    limiter.reserve_sync(TENANT_A, 600)
    clock.advance(30)
    limiter.reserve_sync(TENANT_A, 400)
    with pytest.raises(QuotaExceeded):
        limiter.reserve_sync(TENANT_A, 1)
    clock.advance(30.1)
    # Only the older half aged out; the newer reservation still occupies the window.
    assert limiter.usage_sync(TENANT_A) == 400
    limiter.reserve_sync(TENANT_A, 600)
    with pytest.raises(QuotaExceeded):
        limiter.reserve_sync(TENANT_A, 1)


def rows(limiter: TokenWindowLimiter) -> int:
    connection = sqlite3.connect(limiter.database_path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0])
    finally:
        connection.close()


def test_admission_evicts_stale_rows_of_other_inactive_tenants(tmp_path: Path) -> None:
    clock = Clock()
    limiter = build(tmp_path, clock=clock, budget=1_000)
    for _ in range(5):
        limiter.reserve_sync(TENANT_B, 100)
    assert rows(limiter) == 5
    clock.advance(120)
    limiter.reserve_sync(TENANT_A, 10)
    # Tenant B stopped sending traffic; its rows are still removed.
    assert rows(limiter) == 1
    assert limiter.usage_sync(TENANT_B) == 0


def test_rejected_admission_still_commits_eviction(tmp_path: Path) -> None:
    clock = Clock()
    limiter = build(tmp_path, clock=clock, budget=1_000)
    limiter.reserve_sync(TENANT_B, 500)
    clock.advance(120)
    limiter.reserve_sync(TENANT_A, 1_000)
    assert rows(limiter) == 1
    with pytest.raises(QuotaExceeded):
        limiter.reserve_sync(TENANT_A, 1)
    assert rows(limiter) == 1


def test_explicit_purge_removes_only_expired_rows(tmp_path: Path) -> None:
    clock = Clock()
    limiter = build(tmp_path, clock=clock, budget=1_000)
    limiter.reserve_sync(TENANT_A, 100)
    clock.advance(30)
    limiter.reserve_sync(TENANT_A, 100)
    clock.advance(31)
    assert limiter.purge_sync() == 1
    assert rows(limiter) == 1
    assert limiter.usage_sync(TENANT_A) == 100


def test_state_survives_reopening_the_database(tmp_path: Path) -> None:
    clock = Clock()
    first = build(tmp_path, clock=clock, budget=1_000)
    first.reserve_sync(TENANT_A, 700)
    second = TokenWindowLimiter(
        tmp_path / "quota.sqlite3", token_budget=1_000, window_seconds=60.0, clock=clock
    )
    assert second.usage_sync(TENANT_A) == 700
    with pytest.raises(QuotaExceeded):
        second.reserve_sync(TENANT_A, 301)
    second.reserve_sync(TENANT_A, 300)
    assert first.usage_sync(TENANT_A) == 1_000


def test_reconciliation_lowers_a_reservation_to_reported_usage(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=1_000)
    reservation = limiter.reserve_sync(TENANT_A, 500)
    assert limiter.reconcile_sync(reservation, 120) == 120
    assert limiter.usage_sync(TENANT_A) == 120
    limiter.reserve_sync(TENANT_A, 880)


@pytest.mark.parametrize("reported", [600, 10_000, -5])
def test_reconciliation_never_charges_more_than_the_reservation(
    tmp_path: Path, reported: int
) -> None:
    limiter = build(tmp_path, budget=1_000)
    reservation = limiter.reserve_sync(TENANT_A, 500)
    charged = limiter.reconcile_sync(reservation, reported)
    assert charged == (500 if reported > 0 else 0)
    assert limiter.usage_sync(TENANT_A) == charged


def test_reconciling_an_expired_reservation_is_harmless(tmp_path: Path) -> None:
    clock = Clock()
    limiter = build(tmp_path, clock=clock, budget=1_000)
    reservation = limiter.reserve_sync(TENANT_A, 500)
    clock.advance(120)
    limiter.purge_sync()
    assert limiter.reconcile_sync(reservation, 10) == 10
    assert limiter.usage_sync(TENANT_A) == 0


def test_raw_tenant_keys_are_never_written_to_the_database(tmp_path: Path) -> None:
    raw = "tenant-a-key"
    limiter = build(tmp_path, budget=1_000)
    limiter.reserve_sync(fingerprint(raw), 100)
    stored = limiter.database_path.read_bytes()
    assert raw.encode() not in stored
    assert fingerprint(raw).encode() in stored


def test_missing_schema_or_unusable_path_fails_closed(tmp_path: Path) -> None:
    empty = TokenWindowLimiter(tmp_path / "absent.sqlite3", token_budget=10)
    with pytest.raises(QuotaStorageError):
        empty.reserve_sync(TENANT_A, 1)
    with pytest.raises(QuotaStorageError):
        empty.usage_sync(TENANT_A)
    with pytest.raises(QuotaStorageError):
        empty.reconcile_sync(Reservation(id=1, tokens=1), 1)
    with pytest.raises(QuotaStorageError):
        empty.purge_sync()


def test_directory_in_place_of_a_database_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "quota.sqlite3").mkdir()
    limiter = TokenWindowLimiter(tmp_path / "quota.sqlite3", token_budget=10)
    with pytest.raises(QuotaStorageError):
        limiter.initialize_sync()


def test_negative_reservations_and_invalid_settings_are_rejected(tmp_path: Path) -> None:
    limiter = build(tmp_path)
    with pytest.raises(ValueError, match="negative"):
        limiter.reserve_sync(TENANT_A, -1)
    for kwargs in ({"token_budget": 0}, {"window_seconds": 0}, {"timeout_seconds": 0}):
        with pytest.raises(ValueError, match="positive"):
            TokenWindowLimiter(tmp_path / "quota.sqlite3", **kwargs)


def test_zero_token_reservations_are_admitted_without_consuming_budget(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=10)
    limiter.reserve_sync(TENANT_A, 10)
    limiter.reserve_sync(TENANT_A, 0)
    assert limiter.usage_sync(TENANT_A) == 10


CONCURRENT_WRITERS = 16
CONCURRENT_TOKENS = 5_000


def test_simultaneous_writers_on_separate_connections_cannot_oversubscribe(
    tmp_path: Path,
) -> None:
    """Every worker reserves against its own connection after a shared barrier."""
    limiter = build(tmp_path, budget=50_000)
    barrier = threading.Barrier(CONCURRENT_WRITERS)

    def attempt(_: int) -> int:
        barrier.wait(timeout=10)
        try:
            return limiter.reserve_sync(TENANT_A, CONCURRENT_TOKENS).tokens
        except QuotaExceeded:
            return 0

    with ThreadPoolExecutor(max_workers=CONCURRENT_WRITERS) as pool:
        granted = list(pool.map(attempt, range(CONCURRENT_WRITERS)))
    assert sum(granted) == 50_000
    assert granted.count(CONCURRENT_TOKENS) == 10
    assert limiter.usage_sync(TENANT_A) == 50_000


@pytest.mark.asyncio
async def test_concurrent_async_admissions_stay_within_the_budget(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=50_000)
    results = await asyncio.gather(
        *(limiter.reserve(TENANT_A, CONCURRENT_TOKENS) for _ in range(CONCURRENT_WRITERS)),
        return_exceptions=True,
    )
    granted = [item for item in results if isinstance(item, Reservation)]
    rejected = [item for item in results if isinstance(item, QuotaExceeded)]
    assert len(granted) == 10
    assert len(rejected) == CONCURRENT_WRITERS - 10
    assert await limiter.usage(TENANT_A) == 50_000


@pytest.mark.asyncio
async def test_concurrent_tenants_do_not_block_each_other_out(tmp_path: Path) -> None:
    limiter = build(tmp_path, budget=10_000)
    tenants = [fingerprint(f"tenant-{index}") for index in range(8)]
    await asyncio.gather(*(limiter.reserve(tenant, 10_000) for tenant in tenants))
    usage = await asyncio.gather(*(limiter.usage(tenant) for tenant in tenants))
    assert usage == [10_000] * 8


@pytest.mark.asyncio
async def test_async_helpers_mirror_the_blocking_api(tmp_path: Path) -> None:
    clock = Clock()
    limiter = build(tmp_path, clock=clock, budget=1_000)
    await limiter.initialize()
    reservation = await limiter.reserve(TENANT_A, 400)
    assert await limiter.reconcile(reservation, 50) == 50
    assert await limiter.usage(TENANT_A) == 50
    clock.advance(61)
    assert await limiter.purge() == 1

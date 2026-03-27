#!/usr/bin/env python
"""
Benchmark: Memory isolation via ProcessPoolExecutor for Parquet writes.

Proves that heavy arro3/Parquet workloads cause permanent RSS bloat in the
calling process when run via asyncio.to_thread() (in-process thread), and
that dispatching to a child process via ProcessPoolExecutor keeps the parent
process's RSS stable.

No Django required — runs standalone with just arro3 installed.

Usage:
    uv run python benchmarks/bench_process_isolation.py

The script runs the same workload in three modes:
  1. BASELINE: No work at all — measures overhead of measurement itself.
  2. IN-PROCESS (to_thread): arro3 Parquet writes in a thread (current pattern).
  3. CHILD PROCESS (ProcessPoolExecutor): Same writes in a spawned child process.

Each mode runs ROUNDS iterations and reports RSS before, peak, and after
each round — including after gc.collect() + malloc_trim().

Success criteria:
  - In-process mode: RSS grows with each round and does NOT fully return.
  - Child process mode: Parent RSS stays flat (±2 MB) across all rounds.
"""

import asyncio
import ctypes
import gc
import io
import os
import resource
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

# ---------------------------------------------------------------------------
# Configuration — tune these to control workload intensity
# ---------------------------------------------------------------------------

# Number of rows per Parquet write. 100k rows with 9 columns ≈ 50-80 MB in
# Arrow + Parquet buffers, enough to fragment glibc arenas.
ROWS_PER_WRITE = 100_000

# Number of rounds (each round does one full Parquet write cycle).
ROUNDS = 5

# Number of separate Parquet files per round (simulates per-org writes).
FILES_PER_ROUND = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_rss_mb() -> float:
    """Get current process RSS in MB via /proc/self/statm (Linux) or resource module."""
    try:
        with open("/proc/self/statm") as f:
            # statm fields: size resident shared text lib data dt (in pages)
            pages = int(f.read().split()[1])
            return pages * resource.getpagesize() / (1024 * 1024)
    except (OSError, IndexError):
        # Fallback for non-Linux
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_maxrss / 1024  # ru_maxrss is in KB on Linux


def malloc_trim():
    """Ask glibc to return freed heap pages to the OS."""
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass


def cleanup():
    """Aggressive cleanup: gc + malloc_trim."""
    gc.collect()
    gc.collect()  # Second pass for weak refs / weak ref callbacks
    malloc_trim()


# ---------------------------------------------------------------------------
# Workload: simulate arro3 Parquet write (matches promotion.py pattern)
# ---------------------------------------------------------------------------


def _generate_parquet_bytes(n_rows: int) -> bytes:
    """
    Build an Arrow RecordBatch and write it to an in-memory Parquet buffer.

    This mirrors the _write_chunk_parquet() function in promotion.py:
    - Builds Arrow arrays from Python lists
    - Creates a RecordBatch
    - Writes compressed Parquet to a BytesIO buffer

    Returns the Parquet bytes (discarded by caller — we care about memory, not output).
    """
    import arro3.core as ac
    import arro3.io as aio

    # Simulate realistic column data matching SPAN_PARQUET_COLUMN_TYPES
    org_ids = [1] * n_rows
    project_ids = [i % 50 + 1 for i in range(n_rows)]
    txn_names = [f"/api/v1/endpoint-{i % 200}/" for i in range(n_rows)]
    span_ids = [f"span-{i:012d}" for i in range(n_rows)]
    txn_ids = [f"txn-{i % 5000:012d}" for i in range(n_rows)]
    ops = [["db", "http.client", "cache.get", "template.render"][i % 4] for i in range(n_rows)]
    descriptions = [f"SELECT * FROM table_{i % 100}" for i in range(n_rows)]
    durations = [float(i % 1000) + 0.5 for i in range(n_rows)]
    # Timestamps as int64 microseconds (matches the arro3 timestamp workaround)
    base_ts = 1700000000_000_000  # ~2023-11 in microseconds
    timestamps_us = [base_ts + i * 1000 for i in range(n_rows)]

    batch = ac.RecordBatch.from_arrays(
        [
            ac.Array(org_ids, type=ac.DataType.int32()),
            ac.Array(project_ids, type=ac.DataType.int32()),
            ac.Array(txn_names, type=ac.DataType.utf8()),
            ac.Array(span_ids, type=ac.DataType.utf8()),
            ac.Array(txn_ids, type=ac.DataType.utf8()),
            ac.Array(ops, type=ac.DataType.utf8()),
            ac.Array(descriptions, type=ac.DataType.utf8()),
            ac.Array(durations, type=ac.DataType.float64()),
            ac.Array(timestamps_us, type=ac.DataType.int64()).cast(
                ac.DataType.timestamp("us")
            ),
        ],
        names=[
            "organization_id",
            "project_id",
            "transaction_name",
            "span_id",
            "transaction_id",
            "op",
            "description",
            "duration",
            "timestamp",
        ],
    )

    buf = io.BytesIO()
    aio.write_parquet(
        batch,
        buf,
        compression="zstd(3)",
        max_row_group_size=min(n_rows, 100_000),
    )
    result = buf.getvalue()

    # Explicitly delete large intermediates (matches production pattern)
    del batch, buf, org_ids, project_ids, txn_names, span_ids, txn_ids
    del ops, descriptions, durations, timestamps_us

    return result


def do_parquet_work(n_rows: int, n_files: int) -> dict:
    """
    Run the full Parquet write workload.

    This is the function dispatched to either a thread or child process.
    Returns stats about what it did (for logging).
    """
    total_bytes = 0
    for i in range(n_files):
        data = _generate_parquet_bytes(n_rows)
        total_bytes += len(data)
        del data

    return {
        "files": n_files,
        "rows_per_file": n_rows,
        "total_parquet_bytes": total_bytes,
        "pid": os.getpid(),
    }


# ---------------------------------------------------------------------------
# Async runners matching the vtasks execution patterns
# ---------------------------------------------------------------------------


async def run_in_thread(n_rows: int, n_files: int) -> dict:
    """Current pattern: asyncio.to_thread() — work happens in-process."""
    return await asyncio.to_thread(do_parquet_work, n_rows, n_files)


async def run_in_process(executor: ProcessPoolExecutor, n_rows: int, n_files: int) -> dict:
    """Proposed pattern: ProcessPoolExecutor — work happens in child process."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, do_parquet_work, n_rows, n_files)


# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------


async def bench_mode(mode: str, rounds: int, rows: int, files: int) -> list[dict]:
    """Run `rounds` iterations of a workload mode and collect RSS measurements."""
    results = []
    executor = None

    if mode == "process":
        # max_workers=1: one child at a time, clean exit between rounds
        executor = ProcessPoolExecutor(max_workers=1)

    try:
        for r in range(1, rounds + 1):
            cleanup()
            rss_before = get_rss_mb()

            t0 = time.monotonic()

            if mode == "baseline":
                # No work — just measure overhead
                await asyncio.sleep(0.01)
                stats = {"files": 0, "rows_per_file": 0, "total_parquet_bytes": 0, "pid": os.getpid()}
            elif mode == "thread":
                stats = await run_in_thread(rows, files)
            elif mode == "process":
                stats = await run_in_process(executor, rows, files)
            else:
                raise ValueError(f"Unknown mode: {mode}")

            elapsed = time.monotonic() - t0
            rss_after_work = get_rss_mb()

            # Cleanup and measure post-cleanup RSS
            cleanup()
            rss_after_cleanup = get_rss_mb()

            result = {
                "round": r,
                "rss_before_mb": round(rss_before, 1),
                "rss_after_work_mb": round(rss_after_work, 1),
                "rss_after_cleanup_mb": round(rss_after_cleanup, 1),
                "rss_retained_mb": round(rss_after_cleanup - rss_before, 1),
                "elapsed_s": round(elapsed, 2),
                "parquet_mb": round(stats["total_parquet_bytes"] / (1024 * 1024), 1),
                "worker_pid": stats["pid"],
            }
            results.append(result)
    finally:
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)

    return results


def print_results(mode: str, results: list[dict]):
    """Pretty-print benchmark results for one mode."""
    header = (
        f"{'Round':>5}  {'Before':>8}  {'AfterWork':>10}  {'AfterGC':>8}  "
        f"{'Retained':>9}  {'Time':>6}  {'Parquet':>8}  {'PID':>7}"
    )
    units = (
        f"{'':>5}  {'(MB)':>8}  {'(MB)':>10}  {'(MB)':>8}  "
        f"{'(MB)':>9}  {'(s)':>6}  {'(MB)':>8}  {'':>7}"
    )

    print(f"\n{'='*78}")
    print(f"  MODE: {mode.upper()}")
    print(f"{'='*78}")
    print(header)
    print(units)
    print("-" * 78)

    for r in results:
        retained_str = f"{r['rss_retained_mb']:+.1f}"
        pid_str = str(r['worker_pid'])
        if r['worker_pid'] != os.getpid():
            pid_str += " (child)"

        print(
            f"{r['round']:>5}  {r['rss_before_mb']:>8.1f}  {r['rss_after_work_mb']:>10.1f}  "
            f"{r['rss_after_cleanup_mb']:>8.1f}  {retained_str:>9}  "
            f"{r['elapsed_s']:>6.2f}  {r['parquet_mb']:>8.1f}  {pid_str:>7}"
        )

    # Summary
    first_before = results[0]["rss_before_mb"]
    last_after = results[-1]["rss_after_cleanup_mb"]
    total_growth = last_after - first_before
    print("-" * 78)
    print(f"  Total RSS growth over {len(results)} rounds: {total_growth:+.1f} MB")
    print(f"  (first before: {first_before:.1f} MB → last after cleanup: {last_after:.1f} MB)")


async def main():
    parent_pid = os.getpid()
    print(f"Parent PID: {parent_pid}")
    print(f"Config: {ROWS_PER_WRITE} rows/file × {FILES_PER_ROUND} files/round × {ROUNDS} rounds")
    print(f"Python: {sys.version}")

    # Check THP status
    try:
        with open("/sys/kernel/mm/transparent_hugepage/enabled") as f:
            thp = f.read().strip()
            print(f"THP: {thp}")
    except OSError:
        print("THP: unknown (not Linux or no access)")

    # Check malloc implementation
    try:
        libc = ctypes.CDLL("libc.so.6")
        print("libc: glibc (malloc_trim available)")
    except Exception:
        print("libc: non-glibc (malloc_trim unavailable)")

    print()

    # --- Baseline ---
    baseline = await bench_mode("baseline", ROUNDS, ROWS_PER_WRITE, FILES_PER_ROUND)
    print_results("baseline (no work)", baseline)

    # Force cleanup between modes
    cleanup()

    # --- In-process (thread) ---
    thread_results = await bench_mode("thread", ROUNDS, ROWS_PER_WRITE, FILES_PER_ROUND)
    print_results("in-process (asyncio.to_thread)", thread_results)

    # Force cleanup between modes
    cleanup()

    # --- Child process (ProcessPoolExecutor) ---
    process_results = await bench_mode("process", ROUNDS, ROWS_PER_WRITE, FILES_PER_ROUND)
    print_results("child process (ProcessPoolExecutor)", process_results)

    # --- Comparison ---
    print(f"\n{'='*78}")
    print("  COMPARISON SUMMARY")
    print(f"{'='*78}")

    thread_growth = thread_results[-1]["rss_after_cleanup_mb"] - thread_results[0]["rss_before_mb"]
    process_growth = process_results[-1]["rss_after_cleanup_mb"] - process_results[0]["rss_before_mb"]
    baseline_growth = baseline[-1]["rss_after_cleanup_mb"] - baseline[0]["rss_before_mb"]

    print(f"  Baseline RSS growth:    {baseline_growth:+.1f} MB (noise floor)")
    print(f"  In-process RSS growth:  {thread_growth:+.1f} MB")
    print(f"  Child process growth:   {process_growth:+.1f} MB")
    print()

    avg_thread_time = sum(r["elapsed_s"] for r in thread_results) / len(thread_results)
    avg_process_time = sum(r["elapsed_s"] for r in process_results) / len(process_results)
    overhead_pct = ((avg_process_time - avg_thread_time) / avg_thread_time) * 100 if avg_thread_time > 0 else 0

    print(f"  Avg time per round (thread):  {avg_thread_time:.2f}s")
    print(f"  Avg time per round (process): {avg_process_time:.2f}s")
    print(f"  Process overhead: {overhead_pct:+.1f}%")
    print()

    if thread_growth > 5 and process_growth < thread_growth * 0.3:
        print("  RESULT: Process isolation EFFECTIVE — RSS stays bounded in parent.")
    elif thread_growth <= 5:
        print("  RESULT: In-process RSS growth too small to measure with this workload.")
        print("          Try increasing ROWS_PER_WRITE or FILES_PER_ROUND.")
    else:
        print("  RESULT: Process isolation had limited effect.")
        print("          RSS growth may be from non-arro3 allocations in the parent.")


if __name__ == "__main__":
    asyncio.run(main())

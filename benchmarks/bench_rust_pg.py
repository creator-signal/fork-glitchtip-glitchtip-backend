"""Compare the gt_rust PostgreSQL driver against psycopg3 (async).

Usage (inside the `web` container):
    python benchmarks/bench_rust_pg.py
    python benchmarks/bench_rust_pg.py --rounds 50 --rows 5000

What it measures for each workload:
    - wall-clock mean/p50/p95 over N rounds (after a warmup)
    - RSS delta (MB) from before→after the workload
    - total rows returned

Workloads:
    roundtrip     one SELECT 1 (connection+protocol floor)
    int_rows      SELECT N int rows (wide read, integer path)
    jsonb_rows    SELECT N rows with a JSONB column (JSON decode cost)
    param_ins     INSERT N rows one statement at a time (worst-case write)
    bulk_unnest   INSERT N rows via one UNNEST statement (Django bulk_create)

Honours the benchmark-realism rule: if DB_LATENCY_MS is unset, the script
warns that results don't reflect production. Pair this script with tc netem
(see benchmarks/run_ingest_bench.sh) when you want latency realism.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import os
import resource
import statistics
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

try:
    from gt_rust import RustPgDriver
except ImportError as e:
    print(f"ERROR: gt_rust not importable: {e}", file=sys.stderr)
    print("Build it with `uv sync` (invokes maturin) or rebuild the image.")
    sys.exit(2)


DB_URL = os.environ.get(
    "DATABASE_URL", "postgres://postgres:postgres@postgres:5432/postgres"
)


def rss_mb() -> float:
    # ru_maxrss is KB on Linux; approximate current RSS via /proc.
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * (resource.getpagesize() / 1024 / 1024)
    except OSError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _parse_db_url(url: str) -> dict[str, Any]:
    from urllib.parse import urlparse

    p = urlparse(url)
    return {
        "host": p.hostname,
        "port": p.port or 5432,
        "dbname": (p.path or "/").lstrip("/"),
        "user": p.username,
        "password": p.password or "",
    }


@asynccontextmanager
async def psycopg_pool():
    conninfo = DB_URL.replace("postgres://", "postgresql://")
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.set_autocommit(True)
        yield conn


async def ensure_fixtures(conn: psycopg.AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute("DROP TABLE IF EXISTS gt_bench_int")
        await cur.execute("DROP TABLE IF EXISTS gt_bench_json")
        await cur.execute("DROP TABLE IF EXISTS gt_bench_ins")
        await cur.execute("CREATE TABLE gt_bench_int (id bigint, val bigint)")
        await cur.execute("CREATE TABLE gt_bench_json (id bigint, data jsonb)")
        await cur.execute(
            "CREATE TABLE gt_bench_ins (id bigint, name text, data jsonb)"
        )
        await cur.execute(
            "INSERT INTO gt_bench_int SELECT g, g*2 FROM generate_series(1, 10000) g"
        )
        await cur.execute(
            "INSERT INTO gt_bench_json "
            "SELECT g, jsonb_build_object("
            "'i', g, 'msg', repeat('x', 32), 'tags', jsonb_build_array('a','b','c')"
            ") FROM generate_series(1, 10000) g"
        )


def percentiles(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    return {
        "mean": statistics.fmean(values),
        "p50": values[len(values) // 2],
        "p95": values[max(0, int(len(values) * 0.95) - 1)],
    }


def fmt_row(name: str, stats: dict[str, float], rss_delta: float) -> str:
    return (
        f"  {name:<14} "
        f"mean {stats['mean']*1000:7.3f} ms  "
        f"p50 {stats['p50']*1000:7.3f} ms  "
        f"p95 {stats['p95']*1000:7.3f} ms  "
        f"ΔRSS {rss_delta:+.1f} MB"
    )


async def bench_psycopg(rounds: int, rows: int) -> list[tuple[str, dict, float]]:
    results = []
    async with psycopg_pool() as conn:
        # roundtrip
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
        gc.collect()
        rss0 = rss_mb()
        ts: list[float] = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
            ts.append(time.perf_counter() - t0)
        results.append(("roundtrip", percentiles(ts), rss_mb() - rss0))

        # int_rows
        sql = f"SELECT id, val FROM gt_bench_int LIMIT {rows}"
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(sql)
            await cur.fetchall()
        gc.collect()
        rss0 = rss_mb()
        ts = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            async with conn.cursor(row_factory=tuple_row) as cur:
                await cur.execute(sql)
                await cur.fetchall()
            ts.append(time.perf_counter() - t0)
        results.append(("int_rows", percentiles(ts), rss_mb() - rss0))

        # jsonb_rows — psycopg3 decodes JSONB to dict by default
        sql = f"SELECT id, data FROM gt_bench_json LIMIT {rows}"
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(sql)
            await cur.fetchall()
        gc.collect()
        rss0 = rss_mb()
        ts = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            async with conn.cursor(row_factory=tuple_row) as cur:
                await cur.execute(sql)
                await cur.fetchall()
            ts.append(time.perf_counter() - t0)
        results.append(("jsonb_rows", percentiles(ts), rss_mb() - rss0))

        # param_ins — N sequential INSERTs. This is the worst case for
        # both drivers (one roundtrip per row).
        await conn.execute("TRUNCATE gt_bench_ins")
        # psycopg3 requires explicit Jsonb() wrapping for dict→jsonb.
        # gt_rust auto-detects dict as JSONB, so its payload is simpler.
        payload = [
            (i, f"name-{i}", Jsonb({"i": i, "tags": ["a", "b"]}))
            for i in range(rows)
        ]
        gc.collect()
        rss0 = rss_mb()
        ts = []
        for _ in range(rounds):
            await conn.execute("TRUNCATE gt_bench_ins")
            t0 = time.perf_counter()
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO gt_bench_ins (id, name, data) VALUES (%s, %s, %s)",
                    payload,
                )
            ts.append(time.perf_counter() - t0)
        results.append(("param_ins", percentiles(ts), rss_mb() - rss0))

        # bulk_unnest — one INSERT with array params. Django's fast-path
        # for bulk_create on Postgres 14+ emits exactly this shape.
        ids = list(range(rows))
        names = [f"name-{i}" for i in range(rows)]
        datas = [Jsonb({"i": i, "tags": ["a", "b"]}) for i in range(rows)]
        bulk_sql = (
            "INSERT INTO gt_bench_ins (id, name, data) "
            "SELECT * FROM UNNEST(%s::bigint[], %s::text[], %s::jsonb[])"
        )
        gc.collect()
        rss0 = rss_mb()
        ts = []
        for _ in range(rounds):
            await conn.execute("TRUNCATE gt_bench_ins")
            t0 = time.perf_counter()
            async with conn.cursor() as cur:
                await cur.execute(bulk_sql, (ids, names, datas))
            ts.append(time.perf_counter() - t0)
        results.append(("bulk_unnest", percentiles(ts), rss_mb() - rss0))
    return results


async def bench_rust(rounds: int, rows: int) -> list[tuple[str, dict, float]]:
    kw = _parse_db_url(DB_URL)
    driver = RustPgDriver.connect(
        host=kw["host"],
        port=kw["port"],
        dbname=kw["dbname"],
        user=kw["user"],
        password=kw["password"],
        pool_size=4,
        sslmode="disable",
        prepared_statements=True,
    )

    results = []
    # roundtrip
    await driver.query("SELECT 1", [])
    gc.collect()
    rss0 = rss_mb()
    ts: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        await driver.query("SELECT 1", [])
        ts.append(time.perf_counter() - t0)
    results.append(("roundtrip", percentiles(ts), rss_mb() - rss0))

    # int_rows
    sql = f"SELECT id, val FROM gt_bench_int LIMIT {rows}"
    await driver.query(sql, [])
    gc.collect()
    rss0 = rss_mb()
    ts = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        await driver.query(sql, [])
        ts.append(time.perf_counter() - t0)
    results.append(("int_rows", percentiles(ts), rss_mb() - rss0))

    # jsonb_rows
    sql = f"SELECT id, data FROM gt_bench_json LIMIT {rows}"
    await driver.query(sql, [])
    gc.collect()
    rss0 = rss_mb()
    ts = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        await driver.query(sql, [])
        ts.append(time.perf_counter() - t0)
    results.append(("jsonb_rows", percentiles(ts), rss_mb() - rss0))

    # param_ins — N sequential INSERTs via query_many (one pinned
    # connection, sequential prepared executes). Matches psycopg3's
    # executemany semantics.
    await driver.execute("TRUNCATE gt_bench_ins", [])
    insert_sql = "INSERT INTO gt_bench_ins (id, name, data) VALUES ($1, $2, $3)"
    batch = [
        (insert_sql, [i, f"name-{i}", {"i": i, "tags": ["a", "b"]}])
        for i in range(rows)
    ]
    gc.collect()
    rss0 = rss_mb()
    ts = []
    for _ in range(rounds):
        await driver.execute("TRUNCATE gt_bench_ins", [])
        t0 = time.perf_counter()
        await driver.query_many(batch)
        ts.append(time.perf_counter() - t0)
    results.append(("param_ins", percentiles(ts), rss_mb() - rss0))

    # bulk_unnest — single INSERT ... UNNEST. Mirrors Django bulk_create.
    ids = list(range(rows))
    names = [f"name-{i}" for i in range(rows)]
    datas = [{"i": i, "tags": ["a", "b"]} for i in range(rows)]
    bulk_sql = (
        "INSERT INTO gt_bench_ins (id, name, data) "
        "SELECT * FROM UNNEST($1::bigint[], $2::text[], $3::jsonb[])"
    )
    gc.collect()
    rss0 = rss_mb()
    ts = []
    for _ in range(rounds):
        await driver.execute("TRUNCATE gt_bench_ins", [])
        t0 = time.perf_counter()
        await driver.execute(bulk_sql, [ids, names, datas])
        ts.append(time.perf_counter() - t0)
    results.append(("bulk_unnest", percentiles(ts), rss_mb() - rss0))
    return results


def format_section(title: str, rows: list[tuple[str, dict, float]]) -> str:
    out = [f"\n{title}"]
    for name, stats, rss_delta in rows:
        out.append(fmt_row(name, stats, rss_delta))
    return "\n".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--rows", type=int, default=1000)
    args = parser.parse_args()

    if not os.environ.get("DB_LATENCY_MS"):
        print(
            "NOTE: DB_LATENCY_MS is unset. Measured deltas reflect a local "
            "unix-socket/loopback PG with sub-ms RTT, not production. Use "
            "benchmarks/run_ingest_bench.sh for latency-injected runs."
        )

    async with psycopg_pool() as conn:
        await ensure_fixtures(conn)

    print(f"\nrounds={args.rounds}  rows={args.rows}  db={DB_URL}")
    psyco = await bench_psycopg(args.rounds, args.rows)
    rust = await bench_rust(args.rounds, args.rows)

    print(format_section("psycopg3 (async)", psyco))
    print(format_section("gt_rust", rust))

    print("\nspeedup (psycopg3 mean / gt_rust mean)")
    by_name = {n: s["mean"] for n, s, _ in psyco}
    for name, stats, _ in rust:
        p = by_name.get(name)
        if p:
            print(f"  {name:<14} {p/stats['mean']:.2f}x")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

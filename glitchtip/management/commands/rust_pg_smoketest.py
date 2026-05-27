"""Smoke-test the gt_rust PostgreSQL driver.

Opens a connection pool using Django's DATABASES settings, runs a trivial
query, and prints the round-trip time. Intended for verifying the PyO3
extension is built and wired up on a new host before running benchmarks.
"""

import asyncio
import time

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run a single SELECT via the gt_rust driver and print timings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sql",
            default="SELECT 1",
            help="SQL to execute (default: SELECT 1)",
        )
        parser.add_argument(
            "--iterations",
            type=int,
            default=5,
            help="Number of times to run the query (default: 5)",
        )

    def handle(self, *args, sql, iterations, **options):
        try:
            from gt_rust import driver_from_django_settings
        except ImportError as e:
            raise CommandError(
                f"gt_rust extension not importable: {e}. "
                "Build it with `uv sync` (which invokes maturin)."
            ) from e

        async def run():
            driver = driver_from_django_settings()
            # Warmup
            await driver.query(sql, [])
            timings = []
            for _ in range(iterations):
                t0 = time.perf_counter()
                await driver.query(sql, [])
                timings.append((time.perf_counter() - t0) * 1000)
            return timings

        timings = asyncio.run(run())
        self.stdout.write(self.style.SUCCESS("gt_rust driver OK"))
        for i, ms in enumerate(timings, 1):
            self.stdout.write(f"  iter {i}: {ms:.3f} ms")
        self.stdout.write(
            f"  mean: {sum(timings) / len(timings):.3f} ms  "
            f"min: {min(timings):.3f} ms  "
            f"max: {max(timings):.3f} ms"
        )

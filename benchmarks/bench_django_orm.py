"""End-to-end Django ORM benchmark: psycopg3 backend vs gt_rust backend.

This is the benchmark that matters for GlitchTip's primary goal of
"more work for less resources". It runs identical ORM workloads
against the same Postgres database using two different Django DB
backends — psycopg3 (the stock postgresql backend) and gt_rust — and
reports wall-clock, RSS delta, and RSS high-water.

Run it twice, once per backend, then diff::

    DJANGO_SETTINGS_MODULE=glitchtip.settings \\
        python benchmarks/bench_django_orm.py --label psycopg3

    GLITCHTIP_USE_RUST_PG=true \\
        python benchmarks/bench_django_orm.py --label gt_rust

Or use ``benchmarks/run_orm_bench.sh`` which wraps both runs and
diffs them side-by-side with ``tc netem`` latency injection.

Workloads:
    count               Issue.objects.count()
    select_one          Issue.objects.get(id=...)
    select_list         Issue.objects.all()[:100]
    filter_annotate     Issue.objects.filter(...).annotate(...)[:50]
    bulk_create_50      Issue.objects.bulk_create([...50 objs...])
    individual_save_50  50x Issue(...).save()
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path


def rss_mb() -> float:
    """Current RSS in MB, using /proc/self/statm for accuracy."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * (resource.getpagesize() / 1024 / 1024)
    except OSError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def setup_django():
    import django

    django.setup()


def ensure_fixtures(n_issues: int):
    """Create a dedicated bench org/project and populate ``n_issues``
    rows if the counts are below target. Idempotent across runs."""
    from django.db import transaction

    from apps.issue_events.constants import EventStatus
    from apps.issue_events.models import Issue
    from apps.organizations_ext.models import Organization
    from apps.projects.models import Project
    from apps.users.models import User

    User.objects.get_or_create(
        email="bench@example.com",
        defaults={"password": "!", "is_active": True},
    )
    org, _ = Organization.objects.get_or_create(
        slug="bench-orm", defaults={"name": "bench-orm"}
    )
    project = Project.objects.filter(organization=org, slug="bench-project").first()
    if project is None:
        project = Project.objects.create(
            organization=org,
            slug="bench-project",
            name="bench-project",
            platform="python",
        )

    # Partitions for today/tomorrow are created up front by the
    # run_orm_bench.sh wrapper (``manage.py maintain_partitions``).
    have = Issue.objects.filter(project=project).count()
    if have >= n_issues:
        return
    to_add = n_issues - have
    issues = [
        Issue(
            project=project,
            title=f"bench issue {i}",
            type=0,
            level=4,
            status=EventStatus.UNRESOLVED,
            metadata={"title": f"bench issue {i}"},
        )
        for i in range(to_add)
    ]
    with transaction.atomic():
        Issue.objects.bulk_create(issues, batch_size=500)
    print(f"  fixtures: added {to_add} issues under project={project.slug}")


def percentiles(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    return {
        "mean": statistics.fmean(values),
        "p50": values[len(values) // 2],
        "p95": values[max(0, int(len(values) * 0.95) - 1)],
    }


def run_workload(
    name: str, fn, rounds: int, warmup: int = 2
) -> tuple[str, dict[str, float], float, float]:
    """Run ``fn`` ``rounds`` times, return (name, percentiles, ΔRSS, peak RSS)."""
    for _ in range(warmup):
        fn()
    gc.collect()
    rss_before = rss_mb()
    peak = rss_before
    ts: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
        cur = rss_mb()
        if cur > peak:
            peak = cur
    return name, percentiles(ts), rss_mb() - rss_before, peak


def bench(n_issues: int, rounds: int) -> list[dict]:

    from apps.issue_events.constants import EventStatus
    from apps.issue_events.models import Issue
    from apps.projects.models import Project

    project = (
        Project.objects.filter(
            organization__slug="bench-orm", slug="bench-project"
        )
        .first()
    )
    any_issue = Issue.objects.filter(project=project).first()
    if any_issue is None:
        raise RuntimeError("fixtures missing; pass --setup to create them")

    workloads: list[tuple[str, callable]] = []

    def count():
        Issue.objects.filter(project=project).count()

    workloads.append(("count", count))

    pk = any_issue.pk

    def select_one():
        Issue.objects.get(pk=pk)

    workloads.append(("select_one", select_one))

    def select_list():
        list(Issue.objects.filter(project=project).order_by("-id")[:100])

    workloads.append(("select_list", select_list))

    def filter_annotate():
        from django.db.models import Count

        list(
            Issue.objects.filter(project=project, is_deleted=False)
            .annotate(n=Count("issueevent"))
            .order_by("-last_seen")[:50]
        )

    workloads.append(("filter_annotate", filter_annotate))

    def bulk_create_50():
        from django.db import transaction

        objs = [
            Issue(
                project=project,
                title=f"bench bulk {i}",
                type=0,
                level=4,
                status=EventStatus.UNRESOLVED,
                metadata={"title": f"bench bulk {i}"},
            )
            for i in range(50)
        ]
        with transaction.atomic():
            Issue.objects.bulk_create(objs)
            # Roll back so we don't accumulate rows across iterations.
            transaction.set_rollback(True)

    workloads.append(("bulk_create_50", bulk_create_50))

    def individual_save_50():
        from django.db import transaction

        with transaction.atomic():
            for i in range(50):
                Issue(
                    project=project,
                    title=f"bench single {i}",
                    type=0,
                    level=4,
                    status=EventStatus.UNRESOLVED,
                    metadata={"title": f"bench single {i}"},
                ).save()
            transaction.set_rollback(True)

    workloads.append(("individual_save_50", individual_save_50))

    results = []
    for name, fn in workloads:
        name, stats, delta, peak = run_workload(name, fn, rounds=rounds)
        results.append(
            {
                "name": name,
                "mean_ms": stats["mean"] * 1000,
                "p50_ms": stats["p50"] * 1000,
                "p95_ms": stats["p95"] * 1000,
                "rss_delta_mb": delta,
                "rss_peak_mb": peak,
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="unlabeled", help="tag for JSON output")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--issues", type=int, default=500)
    parser.add_argument("--json", help="write results as JSON to this path")
    parser.add_argument(
        "--setup", action="store_true", help="only ensure fixtures then exit"
    )
    args = parser.parse_args()

    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        print("ERROR: set DJANGO_SETTINGS_MODULE before running", file=sys.stderr)
        return 2

    setup_django()
    # Each bench run ensures fixtures — both backends point at the same
    # Postgres, so data persists across runs.
    ensure_fixtures(args.issues)
    if args.setup:
        return 0

    if not os.environ.get("DB_LATENCY_MS"):
        print(
            "NOTE: DB_LATENCY_MS is unset. Results reflect local loopback RTT,"
            " not production. See benchmarks/run_orm_bench.sh for latency injection."
        )

    gc.collect()
    rss_baseline = rss_mb()
    from django.db import connection

    display = connection.display_name
    print(
        f"\nlabel={args.label}  backend={display}  rounds={args.rounds}  "
        f"issues={args.issues}  rss_baseline={rss_baseline:.1f}MB"
    )

    rows = bench(args.issues, args.rounds)
    print(
        f"  {'workload':<20} {'mean':>10} {'p50':>10} {'p95':>10} "
        f"{'ΔRSS':>8} {'peak':>8}"
    )
    for r in rows:
        print(
            f"  {r['name']:<20} {r['mean_ms']:>8.2f}ms {r['p50_ms']:>8.2f}ms "
            f"{r['p95_ms']:>8.2f}ms {r['rss_delta_mb']:>+6.1f}MB "
            f"{r['rss_peak_mb']:>6.1f}MB"
        )

    out = {
        "label": args.label,
        "backend": display,
        "rss_baseline_mb": rss_baseline,
        "results": rows,
    }
    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

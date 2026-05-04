#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Django ORM benchmark: psycopg3 backend vs gt_rust backend.
#
# Runs bench_django_orm.py twice (once per backend) against the same
# Postgres, with tc netem latency injection and production malloc
# tuning, then prints a side-by-side comparison.
#
# Usage:
#   bash benchmarks/run_orm_bench.sh [BENCH_ARGS...]
#
# Env:
#   DB_LATENCY_MS    One-way latency (default: 5; RTT ≈ 2×)
#   DB_JITTER_MS     Latency jitter (default: 1)
# ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

DB_LATENCY_MS="${DB_LATENCY_MS:-5}"
DB_JITTER_MS="${DB_JITTER_MS:-1}"

CONTAINER_NAME="gt_orm_bench_$$"

cleanup() {
    echo "» Cleaning up container $CONTAINER_NAME"
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p /tmp/gt_orm_bench
JSON_PSYCOPG=/tmp/gt_orm_bench/psycopg3.json
JSON_GTRUST=/tmp/gt_orm_bench/gt_rust.json

echo "» Starting supporting services"
docker compose up -d postgres valkey >/dev/null

echo "» Launching bench container (NET_ADMIN, root user)"
docker run -d --rm \
    --name "$CONTAINER_NAME" \
    --network glitchtip-backend_default \
    --cap-add NET_ADMIN \
    --user 0:0 \
    -v "$REPO_ROOT:/code" \
    -e PYTHONPATH=/code \
    -e DATABASE_URL="postgres://postgres:postgres@postgres:5432/postgres" \
    -e VALKEY_URL="redis://valkey:6379" \
    -e DB_LATENCY_MS="$DB_LATENCY_MS" \
    -e MALLOC_MMAP_THRESHOLD_=65536 \
    -e MALLOC_TRIM_THRESHOLD_=65536 \
    -e MALLOC_ARENA_MAX=4 \
    -w /code \
    glitchtip-backend-web:latest \
    sleep 600 >/dev/null

# Migrate+fixture once before latency injection so the one-time writes
# don't dominate the bench.
echo "» Setting up schema + fixtures"
docker exec "$CONTAINER_NAME" python manage.py migrate --no-input --skip-checks >/dev/null
docker exec "$CONTAINER_NAME" python manage.py maintain_partitions >/dev/null 2>&1 || true
docker exec "$CONTAINER_NAME" \
    env DJANGO_SETTINGS_MODULE=glitchtip.settings \
    python benchmarks/bench_django_orm.py --setup --issues 500

echo "» Injecting ${DB_LATENCY_MS}ms (±${DB_JITTER_MS}ms) latency"
docker exec "$CONTAINER_NAME" bash -c "
    apt-get -qq update >/dev/null 2>&1
    apt-get -qq install -y iproute2 >/dev/null 2>&1 || true
    tc qdisc add dev eth0 root netem delay ${DB_LATENCY_MS}ms ${DB_JITTER_MS}ms
" || { echo "» ERROR: tc netem injection failed"; exit 1; }

echo ""
echo "════════════════════ psycopg3 backend ════════════════════"
docker exec "$CONTAINER_NAME" \
    env DJANGO_SETTINGS_MODULE=glitchtip.settings \
    python benchmarks/bench_django_orm.py \
    --label psycopg3 --json "$JSON_PSYCOPG" "$@"

echo ""
echo "════════════════════ gt_rust backend ════════════════════"
docker exec "$CONTAINER_NAME" \
    env GLITCHTIP_USE_RUST_PG=true \
    python benchmarks/bench_django_orm.py \
    --label gt_rust --json "$JSON_GTRUST" "$@"

echo ""
echo "════════════════════ comparison ════════════════════"
docker exec -i "$CONTAINER_NAME" python - /tmp/gt_orm_bench/psycopg3.json /tmp/gt_orm_bench/gt_rust.json <<'PY'
import json
import sys

a = json.loads(open(sys.argv[1]).read())
b = json.loads(open(sys.argv[2]).read())

name_a = a["label"]
name_b = b["label"]
print(f"{'workload':<20} {name_a+' mean':>16} {name_b+' mean':>16} {'speedup':>10}")
print("-" * 80)

by_name_a = {r["name"]: r for r in a["results"]}
for rb in b["results"]:
    ra = by_name_a.get(rb["name"])
    if ra is None:
        continue
    speedup = ra["mean_ms"] / rb["mean_ms"] if rb["mean_ms"] > 0 else float("inf")
    print(
        f"{rb['name']:<20} "
        f"{ra['mean_ms']:>12.2f}ms   "
        f"{rb['mean_ms']:>12.2f}ms   "
        f"{speedup:>6.2f}x"
    )

print()
print(f"rss_baseline  {name_a}: {a['rss_baseline_mb']:.1f}MB   "
      f"{name_b}: {b['rss_baseline_mb']:.1f}MB   "
      f"diff: {b['rss_baseline_mb'] - a['rss_baseline_mb']:+.1f}MB")

peak_a = max(r["rss_peak_mb"] for r in a["results"])
peak_b = max(r["rss_peak_mb"] for r in b["results"])
print(f"rss_peak      {name_a}: {peak_a:.1f}MB   "
      f"{name_b}: {peak_b:.1f}MB   "
      f"diff: {peak_b - peak_a:+.1f}MB")
PY

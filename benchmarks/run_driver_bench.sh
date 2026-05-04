#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Driver Benchmark with injected DB latency (tc netem).
#
# Compares gt_rust vs psycopg3 on the same workloads. Latency injection
# is required to get meaningful async results — at sub-ms local RTTs
# there's nothing for the event loop to overlap.
#
# Usage:
#   bash benchmarks/run_driver_bench.sh [BENCH_ARGS...]
#
# Examples:
#   bash benchmarks/run_driver_bench.sh
#   bash benchmarks/run_driver_bench.sh --rounds 50 --rows 5000
#
# Environment variables:
#   DB_LATENCY_MS   One-way latency in ms (default: 5; RTT is 2×this).
#   DB_JITTER_MS    Jitter (default: 1)
# ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

DB_LATENCY_MS="${DB_LATENCY_MS:-5}"
DB_JITTER_MS="${DB_JITTER_MS:-1}"

CONTAINER_NAME="gt_driver_bench_$$"

cleanup() {
    echo "» Cleaning up container $CONTAINER_NAME"
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "» Starting web container with NET_ADMIN (for tc netem)"
docker compose up -d postgres valkey >/dev/null

# Launch a long-lived web container with NET_ADMIN — tc netem needs
# CAP_NET_ADMIN inside the container's net namespace.
docker run -d --rm \
    --name "$CONTAINER_NAME" \
    --network glitchtip-backend_default \
    --cap-add NET_ADMIN \
    --user 0:0 \
    -v "$REPO_ROOT:/code" \
    -e DATABASE_URL="postgres://postgres:postgres@postgres:5432/postgres" \
    -e DB_LATENCY_MS="$DB_LATENCY_MS" \
    -e MALLOC_MMAP_THRESHOLD_=65536 \
    -e MALLOC_TRIM_THRESHOLD_=65536 \
    -w /code \
    glitchtip-backend-web:latest \
    sleep 300 >/dev/null

# Verify connectivity first.
if ! docker exec "$CONTAINER_NAME" python -c "import psycopg; psycopg.connect('postgresql://postgres:postgres@postgres:5432/postgres').close()" 2>&1; then
    echo "» ERROR: cannot reach postgres from the benchmark container"
    exit 1
fi

# Install tc and inject latency on the egress path to postgres (and its
# response path). netem applies to all egress, which is fine — valkey is
# idle during this bench.
echo "» Injecting ${DB_LATENCY_MS}ms (±${DB_JITTER_MS}ms) latency via tc netem"
docker exec "$CONTAINER_NAME" bash -c "
    apt-get -qq update >/dev/null
    apt-get -qq install -y iproute2 >/dev/null 2>&1 || true
    tc qdisc add dev eth0 root netem delay ${DB_LATENCY_MS}ms ${DB_JITTER_MS}ms
" || {
    echo "» ERROR: failed to install tc netem"
    exit 1
}

echo "» Running benchmark"
docker exec "$CONTAINER_NAME" python benchmarks/bench_rust_pg.py "$@"

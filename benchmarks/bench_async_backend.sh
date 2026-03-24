#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# A/B Benchmark: django-async-backend vs stock Django DB backend
#
# Runs the mixed-workload benchmark on the current branch (async backend),
# then on master (stock backend), and prints a side-by-side comparison.
#
# Usage:
#   bash benchmarks/bench_async_backend.sh
#
# The script uses compose.bench.yml which already sets DEBUG=false,
# granian ASGI, uvloop, and single worker — production-like settings.
# ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/compose.bench.yml"

CONCURRENCY=200
REQUESTS_PER_WAVE=1000
WAVES=5
PAUSE=3

cd "$REPO_ROOT"

CURRENT_BRANCH=$(git branch --show-current)

run_benchmark() {
    local label="$1"
    local outfile="$2"

    echo ""
    echo "============================================================"
    echo "  BENCHMARK: $label"
    echo "============================================================"
    echo ""

    # Rebuild to pick up code changes
    echo ">>> Building..."
    $COMPOSE build web 2>&1 | tail -3
    echo ""

    # Restart web (keeps postgres/valkey data)
    echo ">>> Restarting web..."
    $COMPOSE up -d postgres valkey 2>&1 | tail -3
    $COMPOSE up -d --force-recreate web bench 2>&1 | tail -3
    echo ""

    # Wait for web server
    echo ">>> Waiting for web server..."
    for i in $(seq 1 300); do
        result=$($COMPOSE exec -T bench python -c "
import httpx
try:
    r = httpx.get('http://web:8000/_health/', timeout=3)
    print('ok' if r.status_code < 500 else 'fail')
except Exception as e:
    print(f'err:{e}')
" 2>&1 || echo "container_not_ready")
        if [ "$result" = "ok" ]; then
            echo "    Ready after ${i}s"
            break
        fi
        if [ "$i" -eq 300 ]; then
            echo "ERROR: Web server not ready. Logs:"
            $COMPOSE logs web --tail 20
            exit 1
        fi
        sleep 1
    done
    echo ""

    # Capture baseline memory
    WEB_CONTAINER=$($COMPOSE ps -q web)
    local baseline_mem
    baseline_mem=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo "0")

    # Start memory monitor
    local memlog
    memlog=$(mktemp /tmp/bench_ab_mem_XXXXXX.csv)
    echo "timestamp_s,mem_bytes" > "$memlog"
    (
        while true; do
            mem=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo "0")
            echo "$(date +%s.%N),$mem" >> "$memlog"
            sleep 0.5
        done
    ) &
    local monitor_pid=$!

    # Run the benchmark
    echo ">>> Running mixed workload: ${CONCURRENCY} concurrent, ${REQUESTS_PER_WAVE} req/wave, ${WAVES} waves"
    echo ""

    local start_time
    start_time=$(date +%s.%N)

    $COMPOSE exec -T bench python benchmarks/bench_ingest_memory.py \
        --mode mixed \
        -c "$CONCURRENCY" \
        -n "$REQUESTS_PER_WAVE" \
        -w "$WAVES" \
        --pause "$PAUSE" 2>&1 | tee "$outfile"

    local end_time
    end_time=$(date +%s.%N)

    # Stop monitor
    kill "$monitor_pid" 2>/dev/null && wait "$monitor_pid" 2>/dev/null || true

    # Post-benchmark memory
    local final_mem
    final_mem=$(docker exec "$WEB_CONTAINER" cat /sys/fs/cgroup/memory.current 2>/dev/null || echo "0")

    # Calculate peak from memlog
    local peak_mem
    peak_mem=$(awk -F',' 'NR>1 && $2+0 > max { max=$2+0 } END { print max+0 }' "$memlog")

    # Calculate total wall time and total requests
    local wall_time
    wall_time=$(echo "$end_time - $start_time" | bc)
    local total_requests=$((REQUESTS_PER_WAVE * WAVES))

    # Write summary to a parseable file
    local summary="${outfile}.summary"
    cat > "$summary" <<EOFSUM
label="$label"
baseline_mb=$(echo "scale=1; $baseline_mem / 1048576" | bc)
peak_mb=$(echo "scale=1; $peak_mem / 1048576" | bc)
final_mb=$(echo "scale=1; $final_mem / 1048576" | bc)
growth_mb=$(echo "scale=1; ($peak_mem - $baseline_mem) / 1048576" | bc)
total_requests=$total_requests
wall_time_s=$(printf "%.1f" "$wall_time")
rps=$(echo "scale=1; $total_requests / $wall_time" | bc)
memlog=$memlog
EOFSUM

    echo ""
    echo ">>> $label Summary:"
    echo "    Baseline: $(echo "scale=1; $baseline_mem / 1048576" | bc) MB"
    echo "    Peak:     $(echo "scale=1; $peak_mem / 1048576" | bc) MB"
    echo "    Final:    $(echo "scale=1; $final_mem / 1048576" | bc) MB"
    echo "    Growth:   $(echo "scale=1; ($peak_mem - $baseline_mem) / 1048576" | bc) MB"
    echo "    Requests: $total_requests in $(printf "%.1f" "$wall_time")s"
    echo "    Throughput: $(echo "scale=1; $total_requests / $wall_time" | bc) req/s"
    echo ""
}

# ── Run on current branch (async backend) ────────────────────────
ASYNC_OUT=$(mktemp /tmp/bench_async_XXXXXX.txt)
run_benchmark "async-backend ($CURRENT_BRANCH)" "$ASYNC_OUT"

# ── Run on master (stock backend) ────────────────────────────────
echo ""
echo ">>> Switching to master for baseline..."
git stash --include-untracked -q || true
git checkout master -q

BASELINE_OUT=$(mktemp /tmp/bench_baseline_XXXXXX.txt)
run_benchmark "stock (master)" "$BASELINE_OUT"

# ── Switch back ──────────────────────────────────────────────────
echo ">>> Switching back to $CURRENT_BRANCH..."
git checkout "$CURRENT_BRANCH" -q
git stash pop -q 2>/dev/null || true

# ── Comparison ───────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  A/B COMPARISON"
echo "============================================================"
echo ""

# Parse summaries
source "${ASYNC_OUT}.summary"
async_baseline=$baseline_mb
async_peak=$peak_mb
async_final=$final_mb
async_growth=$growth_mb
async_rps=$rps
async_wall=$wall_time_s

source "${BASELINE_OUT}.summary"
stock_baseline=$baseline_mb
stock_peak=$peak_mb
stock_final=$final_mb
stock_growth=$growth_mb
stock_rps=$rps
stock_wall=$wall_time_s

printf "  %-24s %12s %12s %12s\n" "Metric" "Stock" "Async" "Delta"
printf "  %-24s %12s %12s %12s\n" "------------------------" "------------" "------------" "------------"
printf "  %-24s %10.1f MB %10.1f MB %+.1f MB\n" "Baseline Memory" "$stock_baseline" "$async_baseline" "$(echo "$async_baseline - $stock_baseline" | bc)"
printf "  %-24s %10.1f MB %10.1f MB %+.1f MB\n" "Peak Memory" "$stock_peak" "$async_peak" "$(echo "$async_peak - $stock_peak" | bc)"
printf "  %-24s %10.1f MB %10.1f MB %+.1f MB\n" "Final Memory" "$stock_final" "$async_final" "$(echo "$async_final - $stock_final" | bc)"
printf "  %-24s %10.1f MB %10.1f MB %+.1f MB\n" "Memory Growth" "$stock_growth" "$async_growth" "$(echo "$async_growth - $stock_growth" | bc)"
printf "  %-24s %8.1f r/s %8.1f r/s %+.1f r/s\n" "Throughput" "$stock_rps" "$async_rps" "$(echo "$async_rps - $stock_rps" | bc)"
printf "  %-24s %10.1f s %10.1f s %+.1f s\n" "Wall Time" "$stock_wall" "$async_wall" "$(echo "$async_wall - $stock_wall" | bc)"

# Percentage changes
if [ "$(echo "$stock_rps > 0" | bc)" -eq 1 ]; then
    rps_pct=$(echo "scale=1; ($async_rps - $stock_rps) / $stock_rps * 100" | bc)
    printf "\n  Throughput change: %+.1f%%\n" "$rps_pct"
fi
if [ "$(echo "$stock_peak > 0" | bc)" -eq 1 ]; then
    mem_pct=$(echo "scale=1; ($async_peak - $stock_peak) / $stock_peak * 100" | bc)
    printf "  Peak memory change: %+.1f%%\n" "$mem_pct"
fi

echo ""
echo "  Raw output: $ASYNC_OUT (async), $BASELINE_OUT (stock)"
echo "============================================================"

# Clean up compose stack
echo ""
echo ">>> Stopping bench stack..."
$COMPOSE down 2>&1 | tail -3

#!/usr/bin/env bash
# Thermal / throttling check.
#
# Runs the same bench workload N times back-to-back against an already-up
# (or auto-launched) bench stack and reports per-iteration throughput,
# wall-clock, max CPU package temp, and min observed CPU frequency.
#
# Throttling signature:
#   - throughput trends DOWN across iterations
#   - tcpu_max climbs and saturates near the package limit (~95-100°C)
#   - freq_min drops well below base clock (~1-2 GHz on a throttled core)
#
# If the 0.9-CPU caps mitigate throttling you should see roughly flat
# throughput and tcpu_max plateauing at a moderate value (<85°C).
#
# Env knobs (defaults reflect the thermal-safe preset for laptops):
#   ITERS=5          number of back-to-back runs
#   MODE=probe-realistic
#   CONCURRENCY=200  -c
#   REQS_PER_WAVE=1000   -n
#   WAVES=5          -w
#   PAUSE=0.1        --pause
#   WEB_CPUS=0.9 PG_CPUS=0.9 VALKEY_CPUS=0.9 BENCH_CPUS=0.9
#   TCPU_ZONE=/sys/class/thermal/thermal_zone6/temp     (TCPU package)
#   FREQ_PATH=/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq
#
# Usage:
#   bash benchmarks/run_thermal_check.sh
#   ITERS=10 CONCURRENCY=300 bash benchmarks/run_thermal_check.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="docker compose -f $SCRIPT_DIR/compose.bench.yml"

ITERS="${ITERS:-5}"
MODE="${MODE:-probe-realistic}"
CONCURRENCY="${CONCURRENCY:-200}"
REQS_PER_WAVE="${REQS_PER_WAVE:-1000}"
WAVES="${WAVES:-5}"
PAUSE="${PAUSE:-0.1}"
WEB_CPUS="${WEB_CPUS:-0.9}"
PG_CPUS="${PG_CPUS:-0.9}"
VALKEY_CPUS="${VALKEY_CPUS:-0.9}"
BENCH_CPUS="${BENCH_CPUS:-0.9}"
RESULTS_DIR="${RESULTS_DIR:-$(mktemp -d /tmp/gt_thermal_XXXXXX)}"
TCPU_ZONE="${TCPU_ZONE:-/sys/class/thermal/thermal_zone6/temp}"
FREQ_PATH="${FREQ_PATH:-/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq}"

read_tcpu() {
    awk '{print int($1/1000)}' "$TCPU_ZONE" 2>/dev/null || echo 0
}
read_freq_mhz() {
    awk '{print int($1/1000)}' "$FREQ_PATH" 2>/dev/null || echo 0
}

# Bring stack up if web isn't already running on the bench compose project.
if ! $COMPOSE ps --status running --services 2>/dev/null | grep -q "^web$"; then
    echo ">>> bringing up bench stack at WEB=${WEB_CPUS} PG=${PG_CPUS} VALKEY=${VALKEY_CPUS} BENCH=${BENCH_CPUS} CPUs"
    WEB_CPUS=$WEB_CPUS PG_CPUS=$PG_CPUS VALKEY_CPUS=$VALKEY_CPUS BENCH_CPUS=$BENCH_CPUS \
        $COMPOSE up -d --build
    echo ">>> waiting for web /_health/ (up to 300s)"
    for i in $(seq 1 300); do
        s=$($COMPOSE exec -T bench python -c "
import httpx
try: r=httpx.get('http://web:8000/_health/',timeout=3); print('ok' if r.status_code<500 else 'fail')
except: print('err')
" 2>/dev/null)
        [ "$s" = "ok" ] && { echo "  ready after ${i}s"; break; }
        sleep 1
    done
fi

# Background temp/freq sampler
sampler_log="$RESULTS_DIR/temps.csv"
echo "ts,tcpu_c,freq_mhz" > "$sampler_log"
( while true; do
    echo "$(date +%s),$(read_tcpu),$(read_freq_mhz)" >> "$sampler_log"
    sleep 1
done ) &
SAMPLER_PID=$!
trap 'kill $SAMPLER_PID 2>/dev/null; wait $SAMPLER_PID 2>/dev/null || true' EXIT

echo ""
echo "============================================================"
echo "  THERMAL CHECK"
echo "  Iters:       $ITERS x ($MODE c=$CONCURRENCY n=$REQS_PER_WAVE w=$WAVES)"
echo "  CPU caps:    web=$WEB_CPUS pg=$PG_CPUS valkey=$VALKEY_CPUS bench=$BENCH_CPUS"
echo "  Results:     $RESULTS_DIR"
echo "  Idle TCPU:   $(read_tcpu)°C  freq=$(read_freq_mhz)MHz"
echo "============================================================"

printf "\n%-5s %-8s %-10s %-9s %-9s %-9s\n" "iter" "wall_s" "thr_req/s" "tcpu_max" "tcpu_post" "freq_min"
printf "%-5s %-8s %-10s %-9s %-9s %-9s\n" "----" "------" "---------" "--------" "---------" "--------"

for i in $(seq 1 "$ITERS"); do
    log="$RESULTS_DIR/iter${i}.log"
    ts_start=$(date +%s)

    $COMPOSE exec -T bench python benchmarks/bench_ingest_memory.py \
        --mode "$MODE" -c "$CONCURRENCY" -n "$REQS_PER_WAVE" -w "$WAVES" --pause "$PAUSE" \
        --host http://web:8000 \
        > "$log" 2>&1 || true

    ts_end=$(date +%s)
    tcpu_post=$(read_tcpu)
    dur=$((ts_end - ts_start))

    # Per-wave table rows: "  N    success    error    p50    p95    p99"
    total_succ=$(awk '/^ +[0-9]+ +[0-9]+ +[0-9]+ +[0-9]/ {s+=$2} END{print s+0}' "$log")
    thr=$(awk -v t="$total_succ" -v d="$dur" 'BEGIN{if(d>0) printf "%.1f",t/d; else print "0.0"}')

    tcpu_max=$(awk -F',' -v s="$ts_start" -v e="$ts_end" 'NR>1 && $1>=s && $1<=e && $2>m{m=$2} END{print m+0}' "$sampler_log")
    freq_min=$(awk -F',' -v s="$ts_start" -v e="$ts_end" 'NR>1 && $1>=s && $1<=e && (m==0||$3<m){m=$3} END{print m+0}' "$sampler_log")

    printf "%-5s %-8s %-10s %-9s %-9s %-9s\n" "$i" "$dur" "$thr" "${tcpu_max}C" "${tcpu_post}C" "${freq_min}MHz"
done

echo ""
echo "Per-iteration logs:  $RESULTS_DIR/iterN.log"
echo "Temp/freq trace:     $sampler_log"

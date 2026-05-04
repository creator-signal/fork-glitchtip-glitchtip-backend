#!/usr/bin/env python3
"""Multi-iteration backend / latency / concurrency comparison runner.

Replaces single-shot interpretation of ``run_concurrency_bench.sh``
with statistical aggregation: each (backend, latency, concurrency)
cell runs ``--iters`` times, the warmup is dropped, and results are
reported as median + IQR with RSS peak and req/s-per-MB derived.

Run from the repo root:

    python benchmarks/bench_compare.py \\
        --backends async,rust \\
        --latencies 0,2 \\
        --concurrency 200,500 \\
        --iters 7

The script orchestrates docker compose. It brings the bench stack up
fresh for each ``backend`` (because BACKEND_VARIANT switches DJANGO
settings), applies tc netem latency for each ``latency`` cell, and
runs ``bench_ingest_memory.py --mode probe-realistic`` against the
running web container ``--iters`` times per ``concurrency`` value.
RSS peak is sampled from the web container's cgroup memory.current.

Output is a per-cell summary table to stdout, plus a JSON dump (one
row per cell) to ``--out`` if given. The JSON is the input format we
will plot from later.
"""

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "benchmarks" / "compose.bench.yml"
COMPOSE = ["docker", "compose", "-f", str(COMPOSE_FILE)]


@dataclass
class IterResult:
    wall_s: float
    total_success: int
    total_error: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    rss_baseline_mb: float
    rss_peak_mb: float

    @property
    def thr_req_s(self) -> float:
        return self.total_success / self.wall_s if self.wall_s > 0 else 0.0


@dataclass
class CellResult:
    backend: str
    latency_ms: int
    concurrency: int
    iters_kept: int
    iters_total: int
    median_thr: float
    iqr_thr: float
    median_p95: float
    median_rss_peak_mb: float
    efficiency_req_s_per_mb: float  # median_thr / median_rss_peak_mb
    raw: list[IterResult] = field(default_factory=list)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, **kw)


def compose_run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return run(COMPOSE + args, **kw)


def bring_up(backend: str, cpu_caps: dict[str, str]) -> None:
    """Start the bench stack with BACKEND_VARIANT=backend and given caps.

    Always teardown-then-up so settings.py picks up env changes.
    """
    print(f">>> bring down (clean state)", flush=True)
    compose_run(["down", "-v"], capture_output=True)

    env = os.environ.copy()
    env["BACKEND_VARIANT"] = backend
    env.update(cpu_caps)
    print(
        f">>> bring up BACKEND_VARIANT={backend} caps={cpu_caps}",
        flush=True,
    )
    compose_run(
        ["up", "-d", "--build"],
        env=env,
        capture_output=True,
    )

    # Health check — bench_ingest_memory.py also waits, but we
    # short-circuit on early failure here.
    print(">>> waiting for /_health/ (up to 300s)", flush=True)
    for i in range(1, 301):
        r = compose_run(
            [
                "exec",
                "-T",
                "bench",
                "python",
                "-c",
                "import httpx,sys\n"
                "try: r=httpx.get('http://web:8000/_health/',timeout=3); "
                "sys.stdout.write('ok' if r.status_code<500 else 'fail')\n"
                "except: sys.stdout.write('err')",
            ],
            capture_output=True,
        )
        if r.stdout.strip() == "ok":
            print(f"    ready after {i}s", flush=True)
            return
        time.sleep(1)
    raise RuntimeError("web never became ready")


def apply_latency(latency_ms: int, jitter_ms: int = 0) -> None:
    """Apply tc netem latency to the web container's egress to PG/valkey.

    Idempotent: removes prior qdiscs first.
    """
    cmds = [
        # Install tc if missing.
        [
            "exec",
            "-T",
            "web",
            "bash",
            "-c",
            "which tc >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq iproute2 >/dev/null 2>&1)",
        ],
    ]
    for c in cmds:
        compose_run(c, capture_output=True)

    # Reset
    compose_run(
        ["exec", "-T", "web", "bash", "-c", "tc qdisc del dev eth0 root 2>/dev/null || true"],
        capture_output=True,
    )

    if latency_ms <= 0:
        return  # no shaping requested

    pg_ip = compose_run(
        ["exec", "-T", "web", "getent", "hosts", "postgres"],
        capture_output=True,
    ).stdout.split()[0]
    valkey_ip = compose_run(
        ["exec", "-T", "web", "getent", "hosts", "valkey"],
        capture_output=True,
    ).stdout.split()[0]

    qdisc_cmds = [
        f"tc qdisc add dev eth0 root handle 1: prio priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0",
        f"tc qdisc add dev eth0 parent 1:2 handle 20: netem delay {latency_ms}ms {jitter_ms}ms",
        f"tc filter add dev eth0 parent 1:0 protocol ip u32 match ip dst {pg_ip}/32 flowid 1:2",
        f"tc filter add dev eth0 parent 1:0 protocol ip u32 match ip dst {valkey_ip}/32 flowid 1:2",
    ]
    for c in qdisc_cmds:
        compose_run(["exec", "-T", "web", "bash", "-c", c], capture_output=True)


def web_container_id() -> str:
    r = compose_run(["ps", "-q", "web"], capture_output=True)
    return r.stdout.strip()


def read_rss_bytes(container_id: str) -> int:
    if not container_id:
        return 0
    r = run(
        [
            "docker",
            "exec",
            container_id,
            "cat",
            "/sys/fs/cgroup/memory.current",
        ],
        capture_output=True,
    )
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def parse_bench_output(
    stdout: str,
) -> tuple[int, int, list[float], list[float], list[float]]:
    """Sum success/error counts + collect per-wave p95s from bench output.

    Bench prints a per-wave table:
        Wave      Success    Error    p50ms    p95ms    p99ms
        -----------------------------------------------------
          1           100        0     92.3    120.9    147.5
          ...
    """
    total_succ = 0
    total_err = 0
    p95_list: list[float] = []
    p50_list: list[float] = []
    p99_list: list[float] = []
    row_re = re.compile(
        r"^\s*\d+\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$"
    )
    for line in stdout.splitlines():
        m = row_re.match(line)
        if m:
            succ, err, p50, p95, p99 = m.groups()
            total_succ += int(succ)
            total_err += int(err)
            p50_list.append(float(p50))
            p95_list.append(float(p95))
            p99_list.append(float(p99))
    return total_succ, total_err, p50_list, p95_list, p99_list


def median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def iqr(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    q = statistics.quantiles(xs, n=4)
    return q[2] - q[0]  # Q3 - Q1


def run_one_iter(
    concurrency: int,
    reqs_per_wave: int,
    waves: int,
    pause: float,
    mode: str,
) -> IterResult:
    web = web_container_id()
    rss_baseline = read_rss_bytes(web)

    # Sample web RSS from the host every 0.5 s while the bench is
    # in flight. Cgroup memory.current is the right number — it
    # already includes file-backed pages we can't dismiss.
    rss_samples: list[int] = []
    sampler_done = False

    def sample_rss():
        while not sampler_done:
            rss_samples.append(read_rss_bytes(web))
            time.sleep(0.5)

    import threading

    sampler = threading.Thread(target=sample_rss, daemon=True)
    sampler.start()

    t0 = time.monotonic()
    bench_cmd = [
        "exec",
        "-T",
        "bench",
        "python",
        "benchmarks/bench_ingest_memory.py",
        "--mode",
        mode,
        "-c",
        str(concurrency),
        "-n",
        str(reqs_per_wave),
        "-w",
        str(waves),
        "--pause",
        str(pause),
        "--host",
        "http://web:8000",
    ]
    r = compose_run(bench_cmd, capture_output=True)
    wall = time.monotonic() - t0

    sampler_done = True
    sampler.join(timeout=2)

    succ, err, p50s, p95s, p99s = parse_bench_output(r.stdout)
    rss_peak = max(rss_samples) if rss_samples else rss_baseline
    return IterResult(
        wall_s=wall,
        total_success=succ,
        total_error=err,
        p50_ms=median(p50s),
        p95_ms=median(p95s),
        p99_ms=median(p99s),
        rss_baseline_mb=rss_baseline / (1024 * 1024),
        rss_peak_mb=rss_peak / (1024 * 1024),
    )


def run_cell(
    backend: str,
    latency_ms: int,
    concurrency: int,
    iters: int,
    reqs_per_wave: int,
    waves: int,
    pause: float,
    mode: str,
) -> CellResult:
    apply_latency(latency_ms)

    raw: list[IterResult] = []
    for i in range(1, iters + 1):
        print(
            f"  iter {i}/{iters} (backend={backend} lat={latency_ms}ms c={concurrency})",
            flush=True,
        )
        r = run_one_iter(concurrency, reqs_per_wave, waves, pause, mode)
        print(
            f"    wall={r.wall_s:.1f}s succ={r.total_success} err={r.total_error} "
            f"p95={r.p95_ms:.0f}ms rss_peak={r.rss_peak_mb:.0f}MB "
            f"thr={r.thr_req_s:.1f}req/s",
            flush=True,
        )
        raw.append(r)

    # Drop the warmup iter
    kept = raw[1:] if len(raw) > 1 else raw
    thrs = [k.thr_req_s for k in kept]
    p95s = [k.p95_ms for k in kept]
    rss_peaks = [k.rss_peak_mb for k in kept]

    med_thr = median(thrs)
    med_rss = median(rss_peaks)
    return CellResult(
        backend=backend,
        latency_ms=latency_ms,
        concurrency=concurrency,
        iters_kept=len(kept),
        iters_total=iters,
        median_thr=med_thr,
        iqr_thr=iqr(thrs),
        median_p95=median(p95s),
        median_rss_peak_mb=med_rss,
        efficiency_req_s_per_mb=med_thr / med_rss if med_rss > 0 else 0.0,
        raw=raw,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--backends",
        default="async,rust",
        help="Comma-separated BACKEND_VARIANTs to compare",
    )
    ap.add_argument(
        "--latencies",
        default="0,2",
        help="Comma-separated tc netem latencies in ms (per-cell)",
    )
    ap.add_argument(
        "--concurrency",
        default="200,500",
        help="Comma-separated concurrency levels (per-cell)",
    )
    ap.add_argument("--iters", type=int, default=7,
                    help="Iterations per cell (warmup discarded)")
    ap.add_argument("--reqs-per-wave", type=int, default=500)
    ap.add_argument("--waves", type=int, default=5)
    ap.add_argument("--pause", type=float, default=0.1)
    ap.add_argument("--mode", default="probe-realistic",
                    choices=["ingest", "mixed", "probe", "probe-realistic"])
    ap.add_argument("--web-cpus", default="1",
                    help="Per-pod granian recommendation; default 1")
    ap.add_argument("--pg-cpus", default="1")
    ap.add_argument("--valkey-cpus", default="0.5")
    ap.add_argument("--bench-cpus", default="0.9")
    ap.add_argument("--out", help="Optional JSON dump of all cell results")
    ap.add_argument("--no-teardown", action="store_true",
                    help="Leave the stack up at the end of the run")
    args = ap.parse_args()

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    latencies = [int(x) for x in args.latencies.split(",") if x.strip()]
    concurrencies = [int(x) for x in args.concurrency.split(",") if x.strip()]

    cpu_caps = {
        "WEB_CPUS": args.web_cpus,
        "PG_CPUS": args.pg_cpus,
        "VALKEY_CPUS": args.valkey_cpus,
        "BENCH_CPUS": args.bench_cpus,
    }

    print(f"=== bench_compare.py ===")
    print(f"  backends:      {backends}")
    print(f"  latencies_ms:  {latencies}")
    print(f"  concurrency:   {concurrencies}")
    print(f"  iters/cell:    {args.iters} (warmup dropped)")
    print(f"  workload:      {args.mode} n={args.reqs_per_wave} w={args.waves}")
    print(f"  cpu caps:      {cpu_caps}")
    print()

    cells: list[CellResult] = []
    try:
        for backend in backends:
            bring_up(backend, cpu_caps)
            for latency_ms in latencies:
                for concurrency in concurrencies:
                    cell = run_cell(
                        backend=backend,
                        latency_ms=latency_ms,
                        concurrency=concurrency,
                        iters=args.iters,
                        reqs_per_wave=args.reqs_per_wave,
                        waves=args.waves,
                        pause=args.pause,
                        mode=args.mode,
                    )
                    cells.append(cell)
    finally:
        if not args.no_teardown:
            print(">>> bringing down stack")
            compose_run(["down", "-v"], capture_output=True)

    # Print summary table.
    print()
    print("=" * 100)
    print(
        f"{'backend':<10} {'lat_ms':>6} {'conc':>5} "
        f"{'thr_med':>9} {'thr_iqr':>9} {'p95_med':>9} "
        f"{'rss_peak':>9} {'eff':>9}  {'iters':>6}"
    )
    print(
        f"{'-'*10} {'-'*6:>6} {'-'*5:>5} "
        f"{'-'*9:>9} {'-'*9:>9} {'-'*9:>9} "
        f"{'-'*9:>9} {'-'*9:>9}  {'-'*6:>6}"
    )
    for c in cells:
        print(
            f"{c.backend:<10} {c.latency_ms:>6} {c.concurrency:>5} "
            f"{c.median_thr:>9.1f} {c.iqr_thr:>9.1f} {c.median_p95:>9.1f} "
            f"{c.median_rss_peak_mb:>9.0f} {c.efficiency_req_s_per_mb:>9.3f}  "
            f"{c.iters_kept:>3}/{c.iters_total:<3}"
        )
    print("=" * 100)
    print(
        "\nthr_med req/s (median across kept iters), thr_iqr = Q3-Q1, "
        "rss_peak in MB, eff = thr_med / rss_peak_med (req/s/MB)"
    )

    if args.out:
        Path(args.out).write_text(
            json.dumps([asdict(c) for c in cells], indent=2, default=str),
        )
        print(f"\nJSON dump: {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

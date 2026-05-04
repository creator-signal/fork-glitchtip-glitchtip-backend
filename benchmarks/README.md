# GlitchTip benchmarking guide

This directory holds the throughput / memory benches for the ingest path
and DB drivers, plus the methodology we apply when interpreting them.
Benchmark results decide architectural questions (Rust driver, GIL
release, batching changes), so the methodology matters at least as much
as the scripts.

## What we are optimizing for

> **GlitchTip should get more work done with less resources at any
> scale.** A change is acceptable only if it improves the
> _resource × throughput_ product on the workloads operators actually
> run.

Two operating points anchor the discussion:

| operating point | target hardware | DB RTT | scaling pattern |
|-----------------|-----------------|--------|------------------|
| single-VPS, low scale | one box, modest cores | ~0 ms (loopback) | scale up to a single saturated worker |
| k8s, high scale | many small pods, managed PG | 1–2 ms (internal cloud network, never WAN) | scale out via more pods |

Decisions need to clear both bars. A patch that wins at 0 ms but
regresses at 2 ms (or vice-versa) is suspect — it usually means we are
trading off a fixed cost against a different fixed cost rather than
removing real work.

GlitchTip's DB and web pods always live on the same internal network.
There's no design point at 50–100 ms RTT; don't tune for it and don't
benchmark with it.

## The fragmentation problem

This is the most visible production pain and it shapes how we measure.

A fresh granian worker on a real install starts at ~300 MB RSS. Over
hours-to-days under high concurrent ingest, the same worker bloats to
**3 GB+**. The work being done has not changed; the RSS has. End users
observe this as "GlitchTip is bloated" even though the first hours of
the same install were lean.

Root cause: glibc's per-thread malloc arenas + Python heap interaction
under high asyncio concurrency. Free'd allocations are held in arenas
and never returned to the OS. Mitigations we already apply:

- `bin/tune-malloc.sh` — `MALLOC_ARENA_MAX = 2 × CPU quota`,
  `MALLOC_MMAP_THRESHOLD_=65536`, `MALLOC_TRIM_THRESHOLD_=65536`. This
  alone saved ~260 MB of baseline RSS in earlier benches.
- Periodic `gc.collect()` + `malloc_trim(0)` after maintenance steps.
- granian worker restart on a schedule (blunt, but effective).

We have not found further wins inside Python. **The remaining lever is
moving hot-path code to Rust** — Rust uses jemalloc/mimalloc and
returns memory to the OS predictably, so work that today produces
fragmentation in Python's heap stops producing fragmentation at all.

This is the strategic motivation behind the `gt_rust` driver and,
later, a Rust ingest binary that reuses the same pool.

## What we measure — and why both

Throughput alone is misleading because Python's malloc behaviour means
"faster" can shift fragmentation patterns and *raise* steady-state
RSS. Operators care about cost, not req/s. Cost = RAM × pods.

| metric | meaning | why it matters |
|--------|---------|-----------------|
| **req/s sustained** | throughput at saturation | fewer pods if higher |
| **RSS peak** | high-water mark during run | drives pod sizing |
| **req/s per MB RSS peak** | derived efficiency | the cost-of-ownership number |
| **p50 / p95 / p99** | tail behaviour under saturation | user-visible latency |
| **events written / sec** (ingest only) | post-batch write rate | what the actual queue drains at — req/s alone misleads because the embedded ASGI worker batches writes |
| **RSS slope under sustained load** | fragmentation rate (MB/min) | predicts the 300 MB → 3 GB blow-up |

`req/s per MB` is the headline number for any "should we ship this?"
decision. A patch that adds 20 % req/s at 30 % more RSS is *worse* at
the k8s operating point because it implies more pods.

## Standard run configuration

Same on every comparison run:

```
WEB_CPUS=1                  # always — granian's recommended pattern is one
                            # worker per pod, scale out via pods
PG_CPUS=1
VALKEY_CPUS=0.5
BENCH_CPUS=0.9
DB_LATENCY_MS=2             # primary target; also run 0 for the VPS case
DATABASE_POOL_MIN_SIZE=5
DATABASE_POOL_MAX_SIZE=40
```

`WEB_CPUS=1` does double duty: it matches granian's recommended
deployment pattern (a pod is one worker; horizontal scaling adds
pods), and it constrains scheduler noise so run-to-run variance comes
down.

### Concurrency sweep

500 concurrent in-flight requests is the primary saturation point —
that's what the hot-path optimisations target. Always also run lower
(50, 200) to see whether wins are saturation-only and higher (1000+)
to find the saturation cliff. The goal is "supports any config", so
we look at the full curve, not one number.

## Bench matrix

Run every comparison across these axes. The runner emits one line per
combination so the result is a table you can diff.

| axis | values |
|------|--------|
| backend | `psycopg3 + django-async-backend` (baseline), `gt_rust` |
| DB latency | 0 ms, 2 ms (10 ms only for stress) |
| workload | driver-only (`bench_rust_pg.py`), realistic probe (`probe-realistic`), mixed ingest (`bench_ingest_memory.py --mode mixed`) |
| concurrency | 50, 200, 500, 1000 |

## Methodology

Single-shot numbers lie. Apply this, every time:

1. **≥ 7 iterations per cell, report median + IQR.** Never mean
   (outlier-sensitive). Discard run 1 as warmup.
2. **0 ms _and_ ≥ 1 ms latency, same session.** A change that wins
   only at one of the two is suspect — flag in the report and
   investigate before merging. GIL-release in particular tends to
   favour latency conditions.
3. **Same hardware, same session, same temp envelope.** Don't compare
   numbers from different days unless thermals are controlled (e.g.
   desktop, AC'd room).
4. **Calibrate the noise floor first.** Run the same backend 7 times
   in a row; the IQR of those runs is your noise floor. A measured
   "improvement" of 5 % at a 15 % noise floor is not an improvement.
5. **Throughput AND RSS peak together.** If a patch raises req/s but
   also raises RSS peak, compute req/s-per-MB. If that ratio doesn't
   improve, the patch is neutral or worse.
6. **For ingest, separate request-rate from event-rate.** The ASGI
   worker batches; req/s and events-written/sec can diverge.
7. **Sustain long enough for fragmentation to show.** A 30-second run
   doesn't reveal the 300 MB → 3 GB curve. For the soak test (see
   tooling below) run ≥ 30 minutes and report RSS slope.

## Anti-illusion checks

A measured "win" is suspect when any of these hold:

- Wins only at 0 ms (or only at 2 ms) — usually a fixed-cost shuffle.
- req/s up but RSS peak up by ≥ same percent — operators pay for both.
- Wins at one concurrency level but not adjacent ones — saturation
  artifact, not a real path improvement.
- Wins disappear when the noise-floor calibration is repeated.
- Wins at p50 but not p95/p99 — best-case improvement, tail still
  drives user perception.

## Acceptance bar for `gt_rust`

To advance the Rust driver out of draft and replace `psycopg3 +
django-async-backend` as the default, all of the following must hold
on the 2 ms latency, mixed-ingest, saturated workload (the closest
proxy to k8s production):

- **req/s ≥ psycopg parity** (within noise floor).
- **RSS peak ≤ psycopg + 5 %.** Strict — the whole point is to fix
  fragmentation.
- **req/s per MB RSS strictly higher** than psycopg.
- **30-min soak: RSS slope strictly lower** than psycopg. This is the
  acceptance criterion that maps directly to the production pain.

Secondary checks at 0 ms loopback (single-VPS):

- req/s within −5 % of psycopg.
- RSS peak no higher.

Driver-only `roundtrip` and `int_rows` benches inform diagnosis but
are not gates: at 0 ms the FFI cost dominates and rust loses; at 2 ms
the gap closes. The gate is the realistic workload.

## Future direction: Rust calls Rust

Once the driver clears the bar, the next step is reusing the same
`gt_rust` pool from a Rust ingest binary — no Python in the hot
path. Bench: Rust ingest end-to-end vs Python ingest end-to-end,
identical pool, identical DB. That's a separate MR; the gate to start
it is the driver acceptance bar above.

---

# Per-tool reference

## `bench_ingest_memory.py` — memory growth under sustained load

Measures web server memory growth under sustained load with artificial
database latency. Uses `tc netem` to inject round-trip delay on the
web container's connections to Postgres and Valkey, then fires
high-concurrency traffic in waves while sampling cgroup memory.

**Why:** In production, GlitchTip's web process grows from ~300 MB to
~3 GB over several days. This benchmark compresses that pattern into
minutes by combining forced backend latency with high concurrency.

### Quick start

```bash
# Default: ingest-only, 500 concurrent, 5 waves of 2000 events, 2ms DB latency
bash benchmarks/run_ingest_bench.sh

# Mixed workload (ingest + API reads + uptime checks), 15 waves
bash benchmarks/run_ingest_bench.sh --mode mixed -c 500 -n 2000 -w 15

# Quick smoke test
bash benchmarks/run_ingest_bench.sh -c 50 -n 200 -w 2 --pause 1
```

### What it does

1. Starts a dedicated `compose.bench.yml` stack (web + postgres + valkey + bench)
2. Waits for web to finish migrations and bootstrap
3. Installs `iproute2` in the web container and applies `tc netem` delay
4. Runs `bench_ingest_memory.py` from the bench container
5. Samples `memory.current` from the web container's cgroup every 0.5 s
6. Prints a memory timeline and summary showing baseline, peak, and growth

### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `DB_LATENCY_MS` | `2` | Artificial round-trip latency to Postgres/Valkey |
| `DB_JITTER_MS` | `0` | Latency jitter |

All arguments after `run_ingest_bench.sh` are forwarded to the Python script:

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `ingest` | `ingest`, `mixed`, `probe`, or `probe-realistic` |
| `-c` | `500` | Concurrent requests |
| `-n` | `2000` | Requests per wave |
| `-w` | `5` | Number of waves |
| `--pause` | `3.0` | Seconds between waves |

### Mixed workload weights

The `--mode mixed` option distributes requests across endpoint types:

| Type | Weight | Endpoint |
|------|--------|----------|
| `ingest` | 60 | `POST /api/{id}/envelope/` |
| `uptime` | 20 | `GET /api/0/organizations/{slug}/heartbeat_check/{uuid}/` |
| `list_issues` | 8 | `GET /api/0/organizations/{slug}/issues/` |
| `list_projects` | 4 | `GET /api/0/projects/` |
| `get_org` | 4 | `GET /api/0/organizations/{slug}/` |
| `get_project` | 4 | `GET /api/0/projects/{org}/{proj}/` |

### Notes

- The compose stack runs with `DEBUG=false` so Django's query logging
  and debug middleware don't pollute memory measurements.
- The web container has `cap_add: NET_ADMIN` to allow `tc` traffic shaping.
- First run on a fresh DB takes longer (~2-3 min) for migrations and
  partition creation. Subsequent runs reuse the existing DB.
- The bench container stays running (`sleep infinity`) between runs so
  you can re-run the Python script manually without restarting the stack.
- Per-pod memory limits are in `compose.bench.yml`. Adjust if you want
  longer soak runs.

## `run_concurrency_bench.sh` — back-to-back backend comparison

Wraps `bench_ingest_memory.py` with `tc netem` and runs the same
workload across each backend variant in `BACKENDS` (default: `async`).
Single-shot per backend — for one-off looks. Use `bench_compare.py`
(see below; not yet built) for statistical comparisons.

## `run_thermal_check.sh` — noise floor / throttling calibration

Runs the same workload N times back-to-back against the bench stack
and reports per-iteration throughput plus host CPU temp/freq. Use
this:

- before any comparison session, to verify the box isn't
  throttling under the chosen CPU caps;
- to calibrate the noise floor — IQR across iterations is the smallest
  difference you can claim is real on this hardware.

```bash
ITERS=10 bash benchmarks/run_thermal_check.sh
```

## `bench_rust_pg.py` — driver-only

Compares the gt_rust driver against psycopg3 (async) at the protocol
level only. No Django, no ORM. Useful for diagnosis ("is the regression
in the driver or in our usage?"), not a gate — see the acceptance bar
above.

## `bench_django_orm.py` — ORM dispatch overhead

Identical ORM workloads against the same DB via different ENGINEs.
The "more work for less resources" number you can bring into a
discussion with someone who cares about Django specifically.

## `bench_cold_storage.py` — archive/cleanup at various volumes

```bash
docker compose run --rm web python manage.py shell \
    -c "exec(open('benchmarks/bench_cold_storage.py').read())"
```

## Wanted but not yet built

In priority order:

1. **`bench_compare.py`** — multi-iteration runner (median + IQR,
   RSS peak, req/s-per-MB) across the full bench matrix. Replaces
   single-shot interpretation. This is the gate-quality tool.
2. **`bench_compare.py --plot`** — 2D scatter (RSS peak × req/s) per
   backend per latency. Visual fragmentation/efficiency picture.
3. **Soak runner** — 30-minute sustained load, RSS over time, slope
   reported. The fragmentation gate for the gt_rust acceptance bar.

## Reproducing a baseline

Always paste the runner banner (CPU caps, latency, pool sizing) into
any result you share so future readers can reproduce.

```sh
# Quick sanity (single-shot, no statistics)
bash benchmarks/run_concurrency_bench.sh

# Thermal / noise-floor calibration
ITERS=10 bash benchmarks/run_thermal_check.sh

# Once bench_compare.py exists:
#   bash benchmarks/bench_compare.py --backends async,rust \
#       --latencies 0,2 --concurrency 50,200,500 --iters 7
```

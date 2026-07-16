# Ingest A/B findings: Python vs Rust envelope path

Recorded baselines from `run_ingest_ab.sh` (see [README](./README.md) for
the harness design). These are the Phase 3b gates from the rust-ingest
plan: the Rust arm must beat the Python arm on **CPU per 10k accepted
envelopes** and **RSS behavior** before default-on (R2), and neither may
regress arm-over-arm as later phases land.

## Method notes

- Both arms boot through `bin/run-all-in-one.sh` — production entrypoint:
  tune-malloc, embedded vtasks worker, granian 1 worker — from the same
  image, differing only in `GLITCHTIP_RUST_INGEST`. Separate postgres and
  valkey databases per arm so workers cannot drain each other's queues.
- CPU and RSS come from `/metrics` (`process_cpu_seconds_total`,
  `process_resident_memory_bytes`) of the single granian worker. A segment
  ends only after the embedded worker drains (CPU-idle detection), so
  worker processing cost is attributed to the segment that enqueued it.
  Worker processing is identical Python in both arms — it dilutes the
  relative delta but not the absolute CPU/10k difference; the true
  ingest-path-only delta is larger than the numbers below.
- Request lists are reseeded per (workload, segment) so both arms receive
  byte-identical workloads; segments alternate arm order (A/B then B/A) so
  DB growth and cache drift cancel across the run. Segments that see a
  granian worker respawn are excluded (the harness flags them).
- The settled-RSS slope over a handful of segments is the noisiest metric
  (glibc arena growth makes settled RSS wander, especially on the Python
  arm). The `oversized` segment RSS and the `burst` peak/settled numbers
  are the robust memory signals.
- `header_dsn` is a known behavioral divergence, not a defect: Python
  never implemented envelope-header-DSN auth (403), Rust accepts (200).
  Its CPU is normalized per request, not per accepted envelope.

## Baseline — 2026-07-15, glitchtip-rust 0.6.0

Environment: Intel Core Ultra 7 258V; `WEB_CPUS=2 WEB_MEM=2g` (defaults);
`--segments 6 -n 2000 -c 100` (defaults otherwise); backend @ 7fda5a3a
with glitchtip-rust 0.6.0; run stamp `baseline_gtr060.json`.

| workload | metric | python | rust | delta |
|----------|--------|-------:|-----:|------:|
| prodmix | CPU s / 10k accepted | 41.9 | 25.1 | **−40%** |
| prodmix | RSS MB / 10k accepted (slope) | 28.1 | 11.5 | −59% |
| prodmix | mean RPS | 282 | 420 | +49% |
| junk | CPU s / 10k requests | 22.8 | 11.2 | **−51%** |
| oversized | CPU s / 10k requests | 238 | 163 | −32% |
| oversized | settled RSS during segments (MB) | 680–1057 | 249–270 | — |
| header_dsn | CPU s / 10k requests | 28.0 | 21.6 | −23% |
| burst | peak RSS MB | 513 | 239 | — |
| burst | settled RSS MB (post-burst) | 439 | 229 | — |

Observations:

- **Every workload's CPU is lower on the Rust arm**, including the
  embedded worker's identical Python processing cost in both arms. On
  `header_dsn` the Rust arm does *full ingest* of 2000 events per segment
  at lower CPU than the Python arm spends rejecting them with 403s.
- **The streaming story is the memory story.** Buffering 6 MiB oversized
  bodies at c=100 pushes the Python arm's settled RSS to ~0.7–1.0 GiB
  (payloads traverse the Python heap and glibc arenas never fully
  return); the Rust arm stays at ~250 MB. Same shape in the burst
  workload: Python peaks at 513 MB and settles 108 MB above the Rust arm.
- **Reject-path status parity is exact**: junk segments produce identical
  200/400/403 counts on both arms (byte-identical request lists).
- Open observations (not gates): the Rust arm shows ~0.1% client-side
  connection errors on junk segments (early reject closes the connection
  while the client is mid-send — candidate for Phase 4 graceful
  early-reject draining) and its junk-flood p95 latency is higher while
  its CPU is half — worth a look when Phase 4 touches backpressure.

Re-record after each phase lands (Rust MR → wheel tag → backend bump) and
append a dated section; keep prior baselines for arm-over-arm comparison.

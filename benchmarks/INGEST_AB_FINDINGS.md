# Ingest A/B findings: Python vs Rust envelope path

Recorded baselines from `run_ingest_ab.sh` (see [README](./README.md) for
the harness design). These are the Phase 3b gates from the rust-ingest
plan: the Rust arm must beat the Python arm on **CPU per 10k accepted
events** and **RSS behavior** before default-on (R2), and neither may
regress arm-over-arm as later phases land.

## Method notes

- Both arms boot through `bin/run-all-in-one.sh` — production entrypoint:
  tune-malloc, embedded vtasks worker, granian 1 worker — from the same
  image, differing only in `GLITCHTIP_RUST_INGEST`. Each arm has its own
  postgres database and its own valkey instance. Two deliberate departures
  from production shape for determinism: uptime dispatch (1s schedule) and
  the hourly jittered gc+malloc_trim pass are disabled — both otherwise
  land in random segments' CPU/RSS windows.
- CPU and RSS come from `/metrics` (`process_cpu_seconds_total`,
  `process_resident_memory_bytes`) of the single granian worker. A segment
  ends only after the embedded worker drains (CPU-idle detection), so
  worker processing cost is attributed to the segment that enqueued it.
  Worker processing is identical Python in both arms — it dilutes the
  relative delta but not the absolute CPU difference; the true
  ingest-path-only delta is larger than the numbers below.
- Request lists are reseeded per (workload, segment) so both arms receive
  byte-identical workloads; segments alternate arm order (A/B then B/A);
  a per-run nonce keeps rerun event ids out of the server's dedupe window.
  Segments with a worker respawn or a drain timeout are excluded.
- CPU/10k is reported as total-based and per-segment median; the two
  agreeing (they do below, within 2%) means no segment was contaminated.
- The settled-RSS slope is the noisiest metric: glibc arena growth arrives
  in occasional steps, and a least-squares line over 5 points reads one
  step as slope. Report it with R² and the raw endpoints; the burst
  peak/settled and oversized-segment RSS are the robust memory signals.
- `header_dsn` is a known behavioral divergence, not a defect: Python
  never implemented envelope-header-DSN auth (403), Rust accepts (200).
  Its CPU is normalized per request, not per accepted event.

## Baseline — 2026-07-15, glitchtip-rust 0.6.0

Environment: Intel Core Ultra 7 258V; `WEB_CPUS=2 WEB_MEM=2g` (defaults);
`--segments 6 -n 2000 -c 100` (defaults otherwise); fresh stack; backend
branch `bench/ingest-ab-harness` with glitchtip-rust 0.6.0; run stamp
`baseline_gtr060_v2.json`.

| workload | metric | python | rust | delta |
|----------|--------|-------:|-----:|------:|
| prodmix | CPU s / 10k accepted events | 46.9 | 26.3 | **−44%** |
| prodmix | …per-segment median | 46.8 | 26.5 | −43% |
| prodmix | settled RSS first→last (MB) | 290→320 | 213→243 | — |
| prodmix | RPS | 295 | 447 | +52% |
| junk | CPU s / 10k requests | 24.4 | 10.0 | **−59%** |
| oversized | CPU s / 10k requests | 237 | 167 | −30% |
| oversized | settled RSS during segments (MB) | 659–1259 | 258–284 | — |
| header_dsn | CPU s / 10k requests | 27.3 | 19.2 | −30% |
| burst | peak RSS MB | 492 | 241 | — |
| burst | settled RSS MB (post-burst) | 448 | 238 | — |

Observations:

- **Every workload's CPU is lower on the Rust arm**, with total and median
  agreeing within 2% (no contaminated segments). On `header_dsn` the Rust
  arm does *full ingest* of 2000 events per segment at ~30% less CPU than
  the Python arm spends rejecting them with 403s.
- **The streaming story is the memory story.** Buffering 6 MiB oversized
  bodies at c=100 pushes the Python arm's settled RSS to 0.66–1.26 GiB
  (payloads traverse the Python heap; arenas never fully return); the Rust
  arm stays at ~260–285 MB. Burst: Python peaks +155 MB and stays +111 MB
  above its pre-burst baseline after 45s idle; the Rust arm's peak is
  +3 MB and it settles back exactly to baseline (237.9→241.2→237.8 MB).
- Both arms' prodmix RSS grew ~30 MB over 12k envelopes (~10.2k
  event-carrying). The slope fits (py 16.3, rust 49.9 MB/10k; R² 0.88 /
  0.80) disagree with the identical endpoints because the Rust arm's
  growth arrived as one ~25 MB arena step at segment 3 and the line reads
  the step as slope — treat endpoints + burst as the memory gate, slope
  as a trend indicator across releases.
- **Reject-path status parity is exact**: junk segments produce identical
  200/400/403 counts on both arms (byte-identical request lists).
- Open observations (not gates): the Rust arm shows ~0.1% client-side
  connection errors on junk segments (early reject closes the connection
  while the client is mid-send — candidate for Phase 4 graceful
  early-reject draining) and its junk-flood p95 latency is higher while
  its CPU is less than half — revisit when Phase 4 touches backpressure.

## 2026-07-16 — master checkpoint (granian 2.7.9, glitchtip-rust 0.6.1, django-vtasks 3.0.0)

Environment: same laptop (Intel Core Ultra 7 258V); `WEB_CPUS=2 WEB_MEM=2g`;
`--segments 4 -n 2000 -c 100`; fresh stack; master `66b85f88`; run stamp
`baseline_gtr061_granian279.json`. Comparator: 2026-07-15 pre-train master
(granian 2.7.3, gt-rust 0.6.0, vtasks 2.x), stamp `baseline_gtr060_lock.json`,
same parameters. **Attribution caveat:** the image moved in four ways at once
(granian 2.7.3→2.7.9 incl. the 2.7.7 async-leak fix, django-vtasks 3.0.0
embedded worker, gt-rust 0.6.1, new ASGI dispatcher + lifespan wiring) — this
is a master-to-master checkpoint, not a granian isolate.

Noise note: `py.prodmix` seg 1 was contaminated by host activity (18.1 s CPU
vs 8.2–10.0 s in the other segments; 109 rps; p95 1674 ms — the rust half of
the same segment was normal). Python junk segments are bimodal (4.5 vs 7.0 s)
for the same reason. Rust segments were stable throughout (junk CPU spread
2.17–2.21 s). Python figures below quote the median (and seg-1-excluded mean
where noted); this run is the argument for moving the rig to a quiet desktop.

| workload | metric | python | rust | delta |
|----------|--------|-------:|-----:|------:|
| prodmix | CPU s / 10k accepted events (median) | 57.4 (54.3 excl. seg 1) | 28.9 | **−50%** |
| prodmix | settled RSS first→last (MB) | 278→285 | 211→228 | — |
| prodmix | RPS | 195 | 393 | +101% |
| junk | CPU s / 10k requests (median) | 28.3 | 10.9 | **−61%** |
| oversized | CPU s / 10k requests (median) | 235 | 167 | −29% |
| oversized | settled RSS during segments (MB) | 645–1030 | 240–261 | — |
| header_dsn | CPU s / 10k requests | 24.3 | 17.8 | −27% |
| burst | base → peak → settled RSS (MB) | 284→501→439 | 224→233→222 | — |

Arm-over-arm observations vs the 2.7.3 image:

- **All Phase 3b gates hold**; CPU deltas match the prior baseline within
  noise. The Rust burst profile improved: peak-over-base +9.4 MB (was
  +25.3) and it settles marginally *below* its pre-burst baseline (was
  +11.7 above).
- **Absolute resting RSS is higher in both arms** (+32 MB rust, +64 MB py
  at the final segments) and the old run's idle-time decay is gone — the
  2.7.3 image released ~50 MB during the burst workload's 45 s idle
  windows (rust 228→174 MB) and decayed further over the tail (py
  882→350 MB); the 2.7.9 image holds a flat plateau instead. Consistent
  with the granian 2.7.7 leak fix changing free/reuse patterns (memory
  retained in arenas and reused rather than churned and trimmed). Not a
  leak signal: within-run endpoints are flat and burst settles to base.
- **Python burst throughput +40%** (154→216 rps); py oversized RPS also
  +38% — the allocation-heaviest paths benefit most from the leak fix.
  (py header_dsn RPS −27%, but its segments sit in this run's noise band.)
- **A small per-request CPU uptick on the rust junk path**: mean
  1.79→2.19 s/segment (≈ +0.2 ms/request), and the new run's spread is
  tight (2.17–2.21 s), so it's real within this run. The py arm's cheap
  paths don't confirm a matching constant (junk clean-segment median is
  flat; header_dsn is +0.5 ms/req but noise-contaminated), so arm
  attribution is open — candidates are the new ASGI dispatcher hop and
  vtasks-3.0 worker drain. Re-measure on a quiet host before chasing.
  **Resolved 2026-07-16 — measurement artifact, no regression.** Re-run
  junk-only (6 segments) on a quiet dedicated desktop (AMD 6C/12T,
  podman rootless, same harness/params): old 229b4265 image vs new
  master image land within 1% (rust median 3.52 vs 3.55 cpu-s/10k,
  segment spread 0.70–0.74 vs 0.70–0.76 s; py 17.62 vs 17.02). The
  laptop delta was clock-frequency drift: `process_cpu_seconds_total`
  counts time, not cycles, and the power-limited laptop sustains
  different effective clocks run-to-run (±20%) while staying thermally
  stable *within* a run — hence the deceptively tight per-run spread.
  Consequence for this doc: on the laptop only within-run interleaved
  A/B deltas are trustworthy; cross-run CPU and RPS comparisons (e.g.
  the py burst/oversized RPS gains above, and their granian-leak-fix
  attribution) carry that ±20% band. The RSS observations are
  unaffected (bytes don't scale with clocks). Absolute CPU numbers are
  ~3–5× lower on the desktop and not comparable across machines;
  future cross-run baselines move to the desktop rig.
- Known opens unchanged (Phase 4): rust junk ~0.1% client resets (9/8000);
  rust junk p95 higher than py while its CPU is under half.

Re-record after each phase lands (Rust MR → wheel tag → backend bump) and
append a dated section; keep prior baselines for arm-over-arm comparison.

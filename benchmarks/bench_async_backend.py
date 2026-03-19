#!/usr/bin/env python3
"""
Benchmark: Django default backend vs django-async-backend.

Tests whether true async ORM (no sync_to_async threads for reads)
reduces memory growth compared to Django's default sync-wrapped ORM.

Requires django-async-backend to be pip-installed in the Docker image.
Uses a Dockerfile overlay that installs it and patches settings.

Usage:
  uv run python benchmarks/bench_async_backend.py
"""

import argparse
import concurrent.futures
import json
import random
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    print("ERROR: httpx required")
    sys.exit(1)

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
BASE_URL = "http://localhost:8000"
API_TOKEN = "d" * 64

# Settings patch: override DATABASE ENGINE and add to INSTALLED_APPS
# Injected as a Python file that gets appended to settings via
# DJANGO_SETTINGS_MODULE pointing to a wrapper.
SETTINGS_PATCH = """\
# Patch: use django-async-backend for async ORM reads
DATABASES["default"]["ENGINE"] = "django_async_backend.db.backends.postgresql"
if "django_async_backend" not in INSTALLED_APPS:
    INSTALLED_APPS.append("django_async_backend")
"""

# Dockerfile that adds django-async-backend on top of the main image
DOCKERFILE_ASYNC = """\
FROM glitchtip-bench-base
RUN pip install django-async-backend==0.0.3
# Patch settings to use async backend
RUN echo '{patch}' > /code/glitchtip/settings_async_patch.py
# Create a wrapper settings module that imports original + patch
RUN echo 'from glitchtip.settings import *\\nexec(open("/code/glitchtip/settings_async_patch.py").read())' > /code/glitchtip/settings_async.py
"""

_SHARED_ENV = """\
      DATABASE_URL: postgres://postgres:postgres@postgres:5432/postgres
      VALKEY_URL: redis://valkey:6379
      SECRET_KEY: change_me
      ENABLE_ORGANIZATION_CREATION: "true"
      ENABLE_TEST_API: "true"
      DEBUG: "false"
      EMAIL_BACKEND: "django.core.mail.backends.console.EmailBackend"
      VTASKS_CONCURRENCY: "4"
      LOG_LEVEL: WARNING
      GLITCHTIP_BOOTSTRAP_DEV: "false"
      GLITCHTIP_ENABLE_LOGS: "true"
      GLITCHTIP_ENABLE_MCP: "false"
      GLITCHTIP_EMBED_WORKER: "true"
"""

COMPOSE_DEFAULT = """\
services:
  web:
    build: {project_root}
    volumes:
      - {project_root}:/code
    command: ./bin/run-all-in-one.sh
    cpus: 2
    cap_add:
      - NET_ADMIN
    environment:
""" + _SHARED_ENV + """\
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - valkey
  postgres:
    image: postgres:18
    environment:
      POSTGRES_HOST_AUTH_METHOD: "trust"
  valkey:
    image: valkey/valkey:9
"""

COMPOSE_ASYNC = """\
services:
  web:
    build:
      context: {project_root}
      dockerfile: Dockerfile.async-backend-bench
    volumes:
      - {project_root}:/code
    command: ./bin/run-all-in-one.sh
    cpus: 2
    cap_add:
      - NET_ADMIN
    environment:
""" + _SHARED_ENV + """\
      DJANGO_SETTINGS_MODULE: glitchtip.settings_async
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - valkey
  postgres:
    image: postgres:18
    environment:
      POSTGRES_HOST_AUTH_METHOD: "trust"
  valkey:
    image: valkey/valkey:9
"""


# ---------------------------------------------------------------------------
# Envelope builders (same as bench_jemalloc.py)
# ---------------------------------------------------------------------------

def _envelope(event_id, item_type, event):
    header = json.dumps({"event_id": event_id, "sent_at": "2025-01-15T10:30:00Z"})
    item_header = json.dumps({"type": item_type, "content_type": "application/json"})
    payload = json.dumps(event)
    return (header + "\n" + item_header + "\n" + payload + "\n").encode()


def build_small_error():
    eid = uuid.uuid4().hex
    event = {
        "event_id": eid,
        "timestamp": "2025-01-15T10:30:00.123456Z",
        "level": "error",
        "platform": "python",
        "environment": random.choice(["production", "staging"]),
        "release": f"app@{random.randint(1, 50)}.0.0",
        "transaction": random.choice(["/api/users", "/api/orders", "/health"]),
        "exception": {
            "values": [{
                "type": random.choice(["ValueError", "KeyError", "TypeError"]),
                "value": f"Error {random.randint(1, 10000)}",
                "mechanism": {"type": "generic", "handled": False},
                "stacktrace": {
                    "frames": [
                        {"filename": f"app/mod_{i}.py", "function": f"fn_{i}",
                         "lineno": random.randint(1, 500), "in_app": True}
                        for i in range(random.randint(2, 5))
                    ]
                },
            }]
        },
        "tags": {"server": f"web-{random.randint(1, 4):02d}"},
        "sdk": {"name": "sentry.python", "version": "1.40.0"},
    }
    return _envelope(eid, "event", event)


def build_large_error():
    eid = uuid.uuid4().hex
    num_frames = random.randint(15, 30)
    frames = [{
        "filename": f"app/deep/module_{i}.py",
        "function": f"handler_{i}",
        "lineno": random.randint(1, 1000),
        "in_app": i > num_frames // 3,
        "vars": {f"var_{j}": f"val_{'x' * random.randint(10, 100)}" for j in range(random.randint(3, 8))},
        "pre_context": [f"    line {k}" for k in range(5)],
        "context_line": f"    raise Error('err {i}')",
        "post_context": [f"    line {k}" for k in range(5)],
    } for i in range(num_frames)]
    event = {
        "event_id": eid,
        "timestamp": "2025-01-15T10:30:00.123456Z",
        "level": "error",
        "platform": "python",
        "environment": "production",
        "exception": {"values": [{"type": "RuntimeError", "value": f"Deep {'.' * 100}",
                                   "stacktrace": {"frames": frames}}]},
        "tags": {"browser": "Chrome 120"},
        "sdk": {"name": "sentry.python", "version": "1.40.0"},
    }
    return _envelope(eid, "event", event)


def random_envelope():
    return build_small_error() if random.random() < 0.7 else build_large_error()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, check=True):
    return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True)


def compose(args, compose_file, check=True):
    result = run(f"docker compose -f {compose_file} -p glitchtip-bench {args}", check=False)
    if result.returncode != 0 and check:
        if result.stderr:
            print(f"  [stderr] {result.stderr.strip()[:200]}", file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode, args)
    return result


def get_web_cid(compose_file):
    result = compose("ps -q web", compose_file, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split("\n")[0]
    return None


def inject_latency(compose_file, delay_ms=2):
    cid = get_web_cid(compose_file)
    if not cid:
        return
    run(f"docker exec -u root {cid} sh -c '"
        f"apt-get update -qq && apt-get install -y -qq iproute2 > /dev/null 2>&1 && "
        f"tc qdisc add dev eth0 root netem delay {delay_ms}ms'", check=False)
    print(f"  Injected {delay_ms}ms latency")


def wait_for_healthy(timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{BASE_URL}/api/settings/", timeout=5)
            if r.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError):
            pass
        time.sleep(3)
    raise TimeoutError("Server did not become healthy")


def get_dsn():
    r = httpx.get(f"{BASE_URL}/api/0/projects/org/project/keys/",
                  headers={"Authorization": f"Bearer {API_TOKEN}"}, timeout=10)
    r.raise_for_status()
    return r.json()[0]["dsn"]["public"]


def get_rss_mb(compose_file):
    cid = get_web_cid(compose_file)
    if not cid:
        return 0.0
    r = run(f"docker exec {cid} cat /sys/fs/cgroup/memory.current 2>/dev/null", check=False)
    if r.returncode == 0 and r.stdout.strip():
        try:
            return int(r.stdout.strip()) / (1024 * 1024)
        except ValueError:
            pass
    return 0.0


# ---------------------------------------------------------------------------
# Load test
# ---------------------------------------------------------------------------

@dataclass
class RoundResult:
    round_num: int
    pre_rss_mb: float = 0
    peak_rss_mb: float = 0
    post_rss_mb: float = 0
    throughput: float = 0
    accepted: int = 0


@dataclass
class BenchResult:
    name: str
    baseline_mb: float = 0
    rounds: list = field(default_factory=list)
    final_mb: float = 0


def run_round(store_url, compose_file, num_events, concurrency, round_num):
    result = RoundResult(round_num=round_num)
    result.pre_rss_mb = get_rss_mb(compose_file)
    rss_samples = []
    stop = threading.Event()
    accepted = 0

    def sampler():
        while not stop.is_set():
            mb = get_rss_mb(compose_file)
            if mb > 0:
                rss_samples.append(mb)
            time.sleep(0.3)

    def send():
        nonlocal accepted
        try:
            r = httpx.post(store_url, content=random_envelope(),
                           headers={"Content-Type": "application/x-sentry-envelope"}, timeout=30)
            if r.status_code == 200:
                accepted += 1
        except Exception:
            pass

    # Also do API reads interleaved
    def api_read():
        try:
            httpx.get(f"{BASE_URL}/api/0/organizations/org/issues/?query=is:unresolved",
                      headers={"Authorization": f"Bearer {API_TOKEN}"}, timeout=10)
        except Exception:
            pass

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        tasks = []
        for i in range(num_events):
            tasks.append(pool.submit(send))
            if i % 5 == 0:
                tasks.append(pool.submit(api_read))
        concurrent.futures.wait(tasks)
    elapsed = time.monotonic() - t0
    time.sleep(8)
    stop.set()
    t.join(timeout=2)

    result.throughput = num_events / elapsed if elapsed else 0
    result.accepted = accepted
    result.peak_rss_mb = max(rss_samples) if rss_samples else result.pre_rss_mb
    result.post_rss_mb = get_rss_mb(compose_file)
    return result


def run_topology(name, compose_content, rounds, events_per_round, concurrency):
    compose_file = f"/tmp/bench-{name}.yml"
    with open(compose_file, "w") as f:
        f.write(compose_content.format(project_root=PROJECT_ROOT))

    compose("down -v --remove-orphans 2>/dev/null", compose_file, check=False)

    try:
        print(f"\n  [{name}] Building...")
        compose("up -d --build", compose_file)
        print(f"  [{name}] Waiting for server...")
        wait_for_healthy()

        cid = get_web_cid(compose_file)
        run(f"docker exec -e DEBUG=True {cid} python manage.py bootstrap_dev", check=True)

        dsn = get_dsn()
        parsed = urlparse(dsn)
        pid = parsed.path.strip("/")
        store_url = f"http://{parsed.hostname}:{parsed.port}/api/{pid}/envelope/?sentry_key={parsed.username}"

        # Warmup
        print(f"  [{name}] Warming up...")
        run_round(store_url, compose_file, 50, 10, 0)
        time.sleep(5)

        inject_latency(compose_file, delay_ms=2)
        time.sleep(2)

        bench = BenchResult(name=name)
        bench.baseline_mb = get_rss_mb(compose_file)
        print(f"  [{name}] Baseline RSS: {bench.baseline_mb:.0f} MB")

        for rnd in range(1, rounds + 1):
            print(f"  [{name}] Round {rnd}/{rounds}...", end=" ", flush=True)
            rr = run_round(store_url, compose_file, events_per_round, concurrency, rnd)
            bench.rounds.append(rr)
            print(f"pre={rr.pre_rss_mb:.0f} peak={rr.peak_rss_mb:.0f} post={rr.post_rss_mb:.0f} MB ({rr.throughput:.0f} ev/s)")
            time.sleep(5)

        time.sleep(15)
        bench.final_mb = get_rss_mb(compose_file)
        print(f"  [{name}] Final: {bench.final_mb:.0f} MB")
        return bench
    finally:
        compose("down -v --remove-orphans", compose_file, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--events-per-round", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=150)
    parser.add_argument("--only", choices=["default", "async"])
    args = parser.parse_args()

    print("=" * 70)
    print("  DEFAULT vs ASYNC-BACKEND ORM")
    print(f"  {args.rounds} rounds × {args.events_per_round} events @ {args.concurrency} concurrency")
    print("=" * 70)

    results = {}
    to_run = [args.only] if args.only else ["default", "async"]

    for i, name in enumerate(to_run):
        if i > 0:
            print("\n  Cooling down 15s...")
            time.sleep(15)
        compose_tmpl = COMPOSE_DEFAULT if name == "default" else COMPOSE_ASYNC
        results[name] = run_topology(name, compose_tmpl, args.rounds, args.events_per_round, args.concurrency)

    # Summary
    print(f"\n{'=' * 70}")
    print("  RESULTS")
    print(f"{'=' * 70}")

    for name, b in results.items():
        peak = max(r.peak_rss_mb for r in b.rounds) if b.rounds else 0
        growth = b.final_mb - b.baseline_mb
        avg_tp = sum(r.throughput for r in b.rounds) / len(b.rounds) if b.rounds else 0
        print(f"\n  {name}: baseline={b.baseline_mb:.0f} peak={peak:.0f} final={b.final_mb:.0f} growth={growth:+.0f} MB, {avg_tp:.0f} ev/s")
        print(f"  Staircase: ", " → ".join(f"{r.post_rss_mb:.0f}" for r in b.rounds))

    if len(results) == 2:
        d, a = results["default"], results["async"]
        dg = d.final_mb - d.baseline_mb
        ag = a.final_mb - a.baseline_mb
        print(f"\n  Growth: default={dg:.0f} MB, async={ag:.0f} MB ({(1 - ag/dg)*100:+.0f}% change)" if dg > 0 else "")


if __name__ == "__main__":
    main()

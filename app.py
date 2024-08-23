"""
All-in-one GlitchTip process
Do not scale this beyond 1 instance. Instead use bin/run-* scripts
"""

import os
import signal
import threading
import time

import django
import uvicorn
from django.core.management import call_command

from glitchtip.celery import app

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
django.setup()


def run_celery_worker():
    worker = app.Worker(pool="threads", loglevel="info")
    worker.start()


def run_celery_beat():
    app.Beat().run()


def run_django_server():
    uvicorn.run(
        "glitchtip.asgi:application",
        workers=int(os.environ.get("WEB_CONCURRENCY", 1)),
        host="0.0.0.0",
        port=8000,
        log_level="info",
        lifespan="off",
    )


def run_init():
    call_command("migrate", no_input=True)


def run_pgpartition(stop_event: threading.Event):
    """Run every 12 hours. Handle sigterms cleanly"""
    while not stop_event.is_set():
        call_command("pgpartition", yes=True)
        for _ in range(12 * 60 * 60):
            if stop_event.is_set():
                break
            time.sleep(1)


def signal_handler(sig: int, frame: object | None, stop_event: threading.Event):
    stop_event.set()
    exit(0)


def main() -> None:
    run_init()

    stop_event = threading.Event()
    signal.signal(
        signal.SIGTERM, lambda sig, frame: signal_handler(sig, frame, stop_event)
    )
    signal.signal(
        signal.SIGINT, lambda sig, frame: signal_handler(sig, frame, stop_event)
    )

    # Run celery
    worker_thread = threading.Thread(target=run_celery_worker)
    worker_thread.daemon = True
    beat_thread = threading.Thread(target=run_celery_beat)
    beat_thread.daemon = True
    pgparition_thread = threading.Thread(target=run_pgpartition, args=(stop_event,))

    worker_thread.start()
    beat_thread.start()
    pgparition_thread.start()

    run_django_server()


if __name__ == "__main__":
    main()

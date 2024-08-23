"""
All-in-one GlitchTip process
Do not scale this beyond 1 instance. Instead use bin/run-* scripts
"""

import os
import signal
import sys
import threading
import time

import django
import uvicorn
from django.core.management import call_command

from glitchtip.celery import app

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
django.setup()


# def run_celery_worker():
#     try:
#         app.worker_main(argv=["worker", "--loglevel=info", "--pool=threads"])
#     except KeyboardInterrupt:
#         pass
#     finally:
#         print("exit celery")


def run_celery_worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            app.worker_main(argv=["worker", "--loglevel=info", "--pool=threads"])
        except Exception as e:
            if not stop_event.is_set():
                print(f"Worker crashed with error: {e}. Restarting...")
            else:
                break


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


def run_pgpartition():
    while True:
        call_command("pgpartition", yes=True)
        time.sleep(12 * 60 * 60)  # 12 hours


def signal_handler(sig: int, frame: object | None):
    print("Signal received, shutting down gracefully...")
    sys.exit(0)


def main() -> None:
    run_init()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # pgpartition_thread = threading.Thread(
    #     target=run_pgpartition, name="PgPartitionThread"
    # )
    # pgpartition_thread.start()

    worker_thread = threading.Thread(
        target=run_celery_worker, name="CeleryWorkerThread"
    )
    worker_thread.start()

    # beat_thread = threading.Thread(target=run_celery_beat, name="CeleryBeatThread")
    # beat_thread.start()

    run_django_server()


if __name__ == "__main__":
    main()

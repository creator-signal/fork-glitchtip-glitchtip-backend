"""
All-in-one GlitchTip process
Fine to scale beyond 1 instance
Larger instances should consider dedicated resources, using bin/run-* scripts
"""

import logging
import os

import django
from django.conf import settings
from django.core.management import call_command
from granian import Granian

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
django.setup()
log = logging.getLogger(__name__)


def run_init():
    call_command("migrate", no_input=True, skip_checks=True)
    if "django.contrib.sessions" in settings.INSTALLED_APPS:
        call_command("createcachetable")
    call_command("pgpartition", yes=True)


def main():
    run_init()

    # Enable embedded worker in glitchtip/asgi.py
    os.environ["GLITCHTIP_EMBED_WORKER"] = "true"

    # Run Granian
    Granian(
        "glitchtip.asgi:application",
        address="0.0.0.0",
        port=8000,
        workers=int(os.environ.get("WEB_CONCURRENCY", 1)),
        log_level="info",
        interface="asgi",
        websockets=False,
    ).serve()


if __name__ == "__main__":
    main()

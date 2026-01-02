#!/usr/bin/env sh
set -e

# Run initialization commands
python manage.py migrate --no-input --skip-checks
python manage.py pgpartition --yes

# Create cache table if django.contrib.sessions is installed
python -c "import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'glitchtip.settings'); django.setup(); from django.conf import settings; from django.core.management import call_command; call_command('createcachetable') if 'django.contrib.sessions' in settings.INSTALLED_APPS else None"

# Enable embedded worker
export GLITCHTIP_EMBED_WORKER=true

# Granian settings

WORKERS=${WEB_CONCURRENCY:-1}

HOST=${GRANIAN_HOST:-0.0.0.0}

PORT=${GRANIAN_PORT:-8000}

G_LOG_LEVEL=${GRANIAN_LOG_LEVEL:-INFO}



# Run Granian

exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --log-level $G_LOG_LEVEL --no-ws

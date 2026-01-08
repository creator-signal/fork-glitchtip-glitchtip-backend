#!/usr/bin/env sh
set -e

# Workers: support both Heroku-style WEB_CONCURRENCY and GRANIAN_WORKERS
# GRANIAN_WORKERS takes precedence if set
WORKERS=${GRANIAN_WORKERS:-${WEB_CONCURRENCY:-1}}

# Host and port with defaults (could also just use GRANIAN_HOST/GRANIAN_PORT directly)
HOST=${GRANIAN_HOST:-0.0.0.0}
PORT=${GRANIAN_PORT:-8000}

# Use async by default
USE_ASYNC_SERVER=${USE_ASYNC_SERVER:-true}

# Serve static files by default
export GRANIAN_STATIC_PATH_MOUNT=${GRANIAN_STATIC_PATH_MOUNT:-static}

if [ "$USE_ASYNC_SERVER" = "true" ]; then
    echo "Start GlitchTip with ${WORKERS} granian worker(s) (ASGI)"
    exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS
else
    echo "Start GlitchTip with ${WORKERS} granian worker(s) (WSGI)"
    exec granian --interface wsgi glitchtip.wsgi:application --host $HOST --port $PORT --workers $WORKERS
fi

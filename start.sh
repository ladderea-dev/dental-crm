#!/bin/sh
# POSIX sh so this works on slim Alpine/Debian base images without bash.
set -eu
cd "$(dirname "$0")"

echo "🚀 Starting OutboundAI..."

# VPS / container env vars are the single source of truth.
# .env is used ONLY for local development, and only for variables the host has not already set.
if [ -f ".env" ]; then
    echo "   Found local .env — loading ONLY unset variables (VPS env always wins)"
    # shellcheck disable=SC2046
    set -a
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        key="${line%%=*}"
        # Only export if currently unset
        if [ -z "$(printenv "$key" || true)" ]; then
            export "$line"
        fi
    done < .env
    set +a
fi

echo "📋 Configuration:"
echo "   LiveKit:  ${LIVEKIT_URL:-<missing>}"
echo "   Gemini:   ${GEMINI_MODEL:-gemini-3.1-flash-live-preview}"
DB_BACKEND="${CRM_DB_BACKEND:-${AIRZEN_DB_BACKEND:-supabase}}"
if [ "$DB_BACKEND" = "mysql" ]; then
    echo "   Database: MySQL ($MYSQL_HOST:${MYSQL_PORT:-3306}/$MYSQL_DATABASE)"
else
    echo "   Database: Supabase (${SUPABASE_URL:-<missing>})"
fi

SERVER_PID=""
AGENT_PID=""

echo "🌐 Starting FastAPI on port ${PORT:-8000}..."
uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}" &
SERVER_PID=$!

# Propagate shutdown signals to both the web server and the agent worker
trap 'kill -TERM "$SERVER_PID" 2>/dev/null || true; kill -TERM "$AGENT_PID" 2>/dev/null || true; wait' INT TERM

sleep 2

# Only start LiveKit agent if voice is enabled and LiveKit credentials exist
ENABLE_VOICE="${ENABLE_VOICE_AGENT:-true}"
if [ "${OUTBOUND_CALLING_ENABLED:-true}" = "false" ] || [ "$ENABLE_VOICE" = "false" ] || [ -z "${LIVEKIT_URL:-}" ]; then
    echo "ℹ️ Outbound voice agent disabled or LiveKit not configured. Running in WhatsApp-only / Web CRM mode."
    wait "$SERVER_PID"
    exit 0
fi

echo "🤖 Starting LiveKit agent worker..."
python agent.py start &
AGENT_PID=$!

# Portable watchdog: exit as soon as the uvicorn server dies.
# Automatically restart the LiveKit agent worker if it exits.
while kill -0 "$SERVER_PID" 2>/dev/null; do
    if ! kill -0 "$AGENT_PID" 2>/dev/null; then
        echo "⚠️ LiveKit agent worker exited — restarting in 5 seconds..."
        sleep 5
        python agent.py start &
        AGENT_PID=$!
    fi
    sleep 2
done

# Reap whichever child is still alive
kill -TERM "$SERVER_PID" 2>/dev/null || true
kill -TERM "$AGENT_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
wait "$AGENT_PID"  2>/dev/null || true

echo "❌ FastAPI server exited — stopping container so the orchestrator can restart it."
exit 1

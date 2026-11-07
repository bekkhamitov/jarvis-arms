#!/usr/bin/env bash
# Opens an SSH tunnel to the station's agent API (NUCK-9:8091 -> localhost:8091), then runs Block Buddy.
#   ./run.sh ui               dashboard + voice agent: open http://localhost:8792 and talk there
#   ./run.sh console          voice agent only, through this Mac's mic and speaker
#   ./run.sh download-files   once: fetch the turn-detector model
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
STATION_HOST="${STATION_HOST:?set STATION_HOST=user@station-ip in .env}"
KEY="${STATION_KEY:-$HOME/.ssh/makermods_station}"

if ! curl -s -m 2 localhost:8091/status >/dev/null; then
  ssh -i "$KEY" -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -L 8091:localhost:8091 "$STATION_HOST"
fi

PY=(uv run --no-project --python 3.12 --with 'livekit-agents~=1.8' --with 'livekit-plugins-ai-coustics~=0.3' --with 'python-dotenv>=1' --with aiohttp python)

if [[ "${1:-}" == "ui" ]]; then
  "${PY[@]}" agent.py dev &
  AGENT=$!
  trap 'kill $AGENT 2>/dev/null' EXIT
  "${PY[@]}" server.py
else
  exec "${PY[@]}" agent.py "$@"
fi

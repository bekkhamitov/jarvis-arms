#!/usr/bin/env bash
# Runs ON THE STATION. Restarts Dream's station agent with camera snapshots (station_frames.py).
# 1. walks the arms home (powered), 2. stops the agent (`dreamcli robot down`: arms go limp at home),
# 3. starts station_frames.py exactly as `dreamcli robot up` starts robots.station, detached, same pid/log files.
set -euo pipefail
source ~/dream-env/bin/activate
API=http://localhost:8091

if curl -s -m 3 $API/status >/dev/null; then
  echo "== walking the arms home"
  curl -s -m 10 -X POST $API/stop >/dev/null || true
  curl -s -m 60 -X POST $API/home >/dev/null || true
  for _ in $(seq 1 30); do
    state=$(curl -s -m 3 $API/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
    [ "$state" = idle ] && break; sleep 0.5
  done
  echo "== stopping the station agent"
  dreamcli robot down
fi

echo "== starting the station agent with camera snapshots"
cd ~/block-buddy
setsid nohup ~/dream-env/bin/python station_frames.py --config ~/.dreamlabs/station.yaml \
  --arms-lock ~/.dreamlabs/arms.lock >> ~/.dreamlabs/station.log 2>&1 < /dev/null &
echo $! > ~/.dreamlabs/station.pid
for _ in $(seq 1 60); do
  curl -s -m 2 $API/status >/dev/null && break; sleep 1
done
curl -s $API/status | python3 -c 'import json,sys; s=json.load(sys.stdin); print("station:", s["state"], s["arms"])'
curl -s $API/frames

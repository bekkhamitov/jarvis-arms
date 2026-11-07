# jarvis-arms

Talk to a pair of robot arms like JARVIS. Built at the MakerMods Physical AI Hackathon (Nov 2026).

You speak in the browser, a LiveKit voice agent turns it into one-block tasks ("put the red block on top of the green block"), and Dream Labs' Dream-1 model drives two YAM arms through Dream's station agent. A tactical web dashboard shows the live cameras, arm health, the conversation and a STOP that pins the arms where they are.

## Pieces

| File | What it does |
|---|---|
| `agent.py` | LiveKit voice agent ("JARVIS"): speech → one-block tasks, stop, home, status |
| `server.py` | Dashboard backend: polls the station, proxies cameras, fast STOP, 60 s auto-stop, LiveKit tokens |
| `index.html` | Dashboard: camera HUD, systems, voice transcript, activity log |
| `station/station_frames.py` | Runs on the station: Dream's station agent plus camera snapshots, `/freeze` (stop + pin arms at their measured pose) and an interruptible, slower home move |
| `run.sh` | Opens the SSH tunnel to the station and starts the dashboard + agent |

## Run

```bash
cp .env.example .env        # LiveKit Cloud keys + STATION_HOST
./run.sh ui                 # then open http://localhost:8792
```

On the station: `bash station/restart_with_cameras.sh` (homes the arms, then restarts the station agent with the add-ons).

Keep a hand on the physical e-stop. Software stop is not a substitute.

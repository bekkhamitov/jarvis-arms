"""Block Buddy dashboard: one page to watch the station, control it, and talk to the voice agent.

    ./run.sh ui        # starts this server and the voice agent, then open http://localhost:8792

The server polls the station agent (through run.sh's SSH tunnel) twice a second, keeps an activity log of every
change, stops any run that goes past RUN_LIMIT_S, proxies the page's buttons to the station, and mints LiveKit
tokens that bring the voice agent ("block-buddy") into the page's room.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import secrets
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from livekit import api

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dashboard")

STATION = os.environ.get("STATION_URL", "http://localhost:8091").rstrip("/")
RUN_LIMIT_S = float(os.environ.get("RUN_LIMIT_S", "60"))
PORT = int(os.environ.get("DASHBOARD_PORT", "8792"))
AGENT_NAME = "block-buddy"


class Hub:
    def __init__(self) -> None:
        self.status: dict | None = None
        self.status_at = 0.0
        self.error: str | None = None
        self.events: collections.deque[dict] = collections.deque(maxlen=500)
        self.next_id = 1
        self.http: aiohttp.ClientSession | None = None
        self.stop_http: aiohttp.ClientSession | None = None
        self.last_stop = 0.0  # wall time of the last stop from anywhere (page, safety limit)

    def event(self, source: str, text: str, level: str = "info") -> None:
        self.events.append({"id": self.next_id, "t": time.time(), "source": source, "text": text, "level": level})
        self.next_id += 1
        log.info("[%s] %s", source, text)

    async def station(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        assert self.http
        async with self.http.request(method, STATION + path, json=body) as reply:
            return reply.status, await reply.json(content_type=None)

    async def poll(self) -> None:
        while True:
            try:
                _, status = await self.station("GET", "/status")
                if self.error:
                    self.event("station", "reconnected to the station")
                self.error = None
                self.track(self.status, status)
                self.status, self.status_at = status, time.time()
                if status.get("state") == "running" and (status.get("run_elapsed_s") or 0) > RUN_LIMIT_S:
                    self.event("safety", f"run passed {int(RUN_LIMIT_S)} s: stopping it", "warn")
                    self.last_stop = time.time()
                    code, _ = await self.station("POST", "/freeze")
                    if code == 404:
                        await self.station("POST", "/stop")
            except Exception as error:  # the tunnel or the station agent is down
                message = f"cannot reach the station: {error.__class__.__name__}"
                if self.error != message:
                    self.event("station", message + " (is the SSH tunnel up and `dreamcli robot up` running?)", "error")
                self.error = message
            await asyncio.sleep(0.5)

    def track(self, old: dict | None, new: dict) -> None:
        if old is None:
            self.event("station", f"connected: {new.get('state')}, model {new.get('checkpoint')}")
            return
        if new.get("state") != old.get("state"):
            text = f"{old.get('state')} → {new.get('state')}"
            if new.get("state") == "running" and new.get("prompt"):
                text += f": “{new['prompt']}”"
            self.event("station", text, "error" if new.get("state") == "error" else "info")
        elif new.get("prompt") != old.get("prompt") and new.get("prompt"):
            self.event("station", f"task changed: “{new['prompt']}”")
        for name, ok in (new.get("arms") or {}).items():
            if (old.get("arms") or {}).get(name) != ok:
                self.event("station", f"{name} arm {'answering' if ok else 'NOT answering'}", "info" if ok else "error")
        err = new.get("last_error")
        if err and err != old.get("last_error"):
            self.event("station", f"error: {err.get('message')}", "error")


hub = Hub()


async def index(_: web.Request) -> web.Response:
    html = (HERE / "index.html").read_text().replace("__BUILD__", page_build())
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


def page_build() -> str:
    """Changes whenever index.html changes, so an old open tab reloads itself instead of running stale code."""
    return str(int((HERE / "index.html").stat().st_mtime))


async def get_status(_: web.Request) -> web.Response:
    return web.json_response({"station": hub.status, "age_s": time.time() - hub.status_at if hub.status else None,
                              "error": hub.error, "run_limit_s": RUN_LIMIT_S, "build": page_build(),
                              "last_stop_age_s": time.time() - hub.last_stop if hub.last_stop else None})


async def get_events(request: web.Request) -> web.Response:
    after = int(request.query.get("after", "0"))
    return web.json_response([e for e in hub.events if e["id"] > after])


async def post_log(request: web.Request) -> web.Response:
    body = await request.json()
    hub.event(str(body.get("source", "voice"))[:20], str(body.get("text", ""))[:500])
    return web.json_response({"ok": True})


def command(path: str, label: str):
    async def handler(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        prompt = (body or {}).get("prompt")
        if path in ("/start", "/prompt") and not prompt:
            return web.json_response({"error": "say what to do"}, status=400)
        hub.event("page", f"{label}" + (f": “{prompt}”" if prompt else ""))
        try:
            route = path
            if path == "/start" and hub.status and hub.status.get("state") == "running":
                route = "/prompt"  # a running run takes a new task as a fresh episode
            code, reply = await hub.station("POST", route, {"prompt": prompt} if prompt else None)
        except Exception as error:
            hub.event("page", f"{label} failed: {error}", "error")
            return web.json_response({"error": str(error)}, status=502)
        if code != 200:
            hub.event("page", f"{label} refused: {reply.get('error')}", "error")
        return web.json_response(reply, status=code)
    return handler


CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


async def stop(_: web.Request) -> web.Response:
    """Stop, on its own connection with a short timeout and retries, so nothing else can queue in front of it.

    The page calls this on a different host name than everything else (localhost vs 127.0.0.1), so the browser's
    six-connections-per-host limit, filled by camera and status polling, can never hold a STOP back.
    """
    was = (hub.status or {}).get("state")
    hub.last_stop = time.time()
    started = time.monotonic()
    last_error = "no attempt"
    for attempt in range(3):
        try:
            async with hub.stop_http.post(STATION + "/freeze") as reply:  # stop + pin arms at their measured pose
                if reply.status == 404:  # station started without station_frames.py: plain stop
                    raise LookupError("no /freeze")
                data = await reply.json(content_type=None)
                if reply.status == 200:
                    ms = round((time.monotonic() - started) * 1000)
                    gaps = {k: v.get("gap_to_old_target_rad") for k, v in (data.get("frozen") or {}).items()}
                    hub.event("page", (f"STOP: run stopped, arms pinned where they are ({ms} ms; gap to old target {gaps})"
                                       if was == "running" else "STOP: arms pinned where they are (nothing was running)"))
                    return web.json_response({**data, "was": was, "ms": ms}, headers=CORS)
                last_error = data.get("error", reply.status)
        except LookupError:
            async with hub.stop_http.post(STATION + "/stop") as reply:
                data = await reply.json(content_type=None)
                if reply.status == 200:
                    hub.event("page", "STOP (plain stop: freeze not available on the station)", "warn")
                    return web.json_response({**data, "was": was}, headers=CORS)
                last_error = data.get("error", reply.status)
        except Exception as error:
            last_error = f"{error.__class__.__name__}: {error}"
        await asyncio.sleep(0.2)
    hub.event("page", f"STOP FAILED after 3 tries: {last_error}. USE THE E-STOP", "error")
    return web.json_response({"error": str(last_error)}, status=502, headers=CORS)


async def stop_home(_: web.Request) -> web.Response:
    """Stop any run, wait for the station to settle, then walk the arms home."""
    try:
        code, _ = await hub.station("POST", "/freeze")
        if code == 404:
            await hub.station("POST", "/stop")
        for _ in range(40):
            _, status = await hub.station("GET", "/status")
            if status.get("state") in ("idle", "error"):
                break
            await asyncio.sleep(0.25)
        code, reply = await hub.station("POST", "/home")
    except Exception as error:
        hub.event("page", f"stop & home failed: {error}", "error")
        return web.json_response({"error": str(error)}, status=502)
    hub.event("page", "stop & home: arms walking home" if code == 200 else f"home refused: {reply.get('error')}",
              "info" if code == 200 else "error")
    return web.json_response(reply, status=code)


async def frame(request: web.Request) -> web.Response:
    """The station's newest frame of one camera (needs station_frames.py running on the station)."""
    assert hub.http
    try:
        async with hub.http.get(f"{STATION}/frame/{request.match_info['name']}.jpg",
                                timeout=aiohttp.ClientTimeout(total=3)) as reply:
            body = await reply.read()
            return web.Response(body=body, status=reply.status, content_type=reply.content_type,
                                headers={"Cache-Control": "no-store"})
    except Exception as error:
        return web.json_response({"error": str(error)}, status=502)


VOICE_ROOMS: list[str] = []  # one voice session at a time: two open tabs meant two agents hearing you


async def close_old_voice_rooms() -> None:
    """Deletes EVERY earlier Block Buddy voice room in the project, which disconnects old tabs (even ones from before
    a restart or on an old port) and their agents, so exactly one tab holds the mic and one agent listens."""
    try:
        async with api.LiveKitAPI(os.environ["LIVEKIT_URL"].replace("wss://", "https://"),
                                  os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]) as lk:
            for room in (await lk.room.list_rooms(api.ListRoomsRequest())).rooms:
                if room.name.startswith("block-buddy-"):
                    await lk.room.delete_room(api.DeleteRoomRequest(room=room.name))
                    hub.event("page", f"closed an old voice session ({room.name})")
        VOICE_ROOMS.clear()
    except Exception as error:
        log.warning("could not close old voice rooms: %s", error)


async def token(_: web.Request) -> web.Response:
    await close_old_voice_rooms()
    room = f"block-buddy-{secrets.token_hex(3)}"
    VOICE_ROOMS.append(room)
    jwt = (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
           .with_identity(f"you-{secrets.token_hex(2)}").with_name("You")
           .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
           .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]))
           .to_jwt())
    hub.event("page", f"joined voice room {room}")
    return web.json_response({"url": os.environ["LIVEKIT_URL"], "token": jwt, "room": room})


async def on_startup(app: web.Application) -> None:
    hub.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=65))
    # STOP gets its own connection pool and a short timeout: it never waits behind frames or status calls.
    hub.stop_http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4),
                                          connector=aiohttp.TCPConnector(limit=4, force_close=True))
    app["poller"] = asyncio.create_task(hub.poll())


async def on_cleanup(app: web.Application) -> None:
    app["poller"].cancel()
    if hub.http:
        await hub.http.close()
    if hub.stop_http:
        await hub.stop_http.close()


def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", get_status)
    app.router.add_get("/api/events", get_events)
    app.router.add_post("/api/log", post_log)
    app.router.add_post("/api/start", command("/start", "start"))
    app.router.add_post("/api/stop", stop)
    app.router.add_get("/api/stop", stop)  # also reachable from a bookmark or `curl localhost:8790/api/stop`
    app.router.add_post("/api/stop-home", stop_home)
    app.router.add_get("/api/frame/{name}", frame)
    app.router.add_post("/api/home", command("/home", "home"))
    app.router.add_post("/api/reset", command("/reset", "reset"))
    app.router.add_post("/api/token", token)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None, print=lambda *_: log.info("dashboard on http://localhost:%s", PORT))


if __name__ == "__main__":
    main()

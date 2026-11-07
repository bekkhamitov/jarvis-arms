"""Dream's station agent plus camera snapshots, for the Block Buddy dashboard. Runs ON THE STATION.

    ~/dream-env/bin/python ~/block-buddy/station_frames.py --config ~/.dreamlabs/station.yaml \
        --arms-lock ~/.dreamlabs/arms.lock

This is exactly `python -m robots.station` (what `dreamcli robot up` starts) with two read-only GET routes added to
its HTTP API. Nothing about the arms, the runs or the other routes changes:

    GET /frames             {"cameras": ["top", "left", "right"]}
    GET /frame/<name>.jpg   the camera's newest frame, JPEG, at most FRAME_WIDTH pixels wide

The frames are the ones the station's own camera readers already hold (StationController.frames), so no camera is
opened a second time.
"""
from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np

from robots import home, station, station_api, types

FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "640"))
JPEG_QUALITY = int(os.environ.get("FRAME_QUALITY", "70"))

MIN_INTERVAL_S = float(os.environ.get("FRAME_MIN_INTERVAL_S", "0.15"))  # at most ~7 encodes per camera per second

_original_get = station_api._Handler.do_GET
_cache: dict[str, tuple[float, int, bytes]] = {}  # camera -> (encoded at, id of the frame, jpeg)
_lock = threading.Lock()


def _encoded(name: str, frame) -> bytes | None:
    """The camera's newest frame as JPEG, re-encoded at most every MIN_INTERVAL_S however many viewers poll."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and (hit[1] == id(frame) or now - hit[0] < MIN_INTERVAL_S):
            return hit[2]
        rgb = frame.data
        height, width = rgb.shape[:2]
        if width > FRAME_WIDTH:
            rgb = cv2.resize(rgb, (FRAME_WIDTH, round(height * FRAME_WIDTH / width)), interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            return None
        _cache[name] = (now, id(frame), jpeg.tobytes())
        return _cache[name][2]


def _do_get(self: station_api._Handler) -> None:
    route = self.path.split("?", 1)[0]
    if route == "/frames":
        self._reply(200, {"cameras": sorted(self.server.api.controller.frames)})
        return
    if route.startswith("/frame/"):
        name = route[len("/frame/"):].removesuffix(".jpg")
        frame = self.server.api.controller.frames.get(name)
        if frame is None:
            self._reply(404, {"error": f"no frame from camera {name!r}"})
            return
        data = _encoded(name, frame)
        if data is None:
            self._reply(500, {"error": "could not encode the frame"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return
    _original_get(self)


station_api._Handler.do_GET = _do_get

_original_post = station_api._Handler.do_POST


def _freeze(controller) -> dict:
    """Pin every arm to where it physically IS right now (measured joints and gripper), on its PD.

    Dream's stop holds the arm at the last *commanded* target. While a policy is moving, that target can be ahead of
    the arm (the tracker leads it), so after a stop the arm keeps travelling to it. Freeze re-commands the measured
    pose instead, twice, so the arm stops where you see it.
    """
    return _freeze_arms(controller._arms)


def _freeze_arms(arms) -> dict:
    report = {}
    for _ in range(2):
        for name, arm in arms.items():
            state = arm.read()
            gripper = min(max(float(state.gripper), 0.0), 1.0)
            arm.write(types.ArmCommand(positions_rad=np.asarray(state.positions_rad, dtype=float), gripper=gripper))
            commanded = getattr(state, "commanded_rad", None)
            gap = None if commanded is None else float(np.max(np.abs(np.asarray(commanded) - state.positions_rad)))
            report[name] = {"pinned_at_rad": [round(float(x), 4) for x in state.positions_rad],
                            "gap_to_old_target_rad": None if gap is None else round(gap, 4)}
        time.sleep(0.05)
    return report


HOME_MAX_RAD_S = float(os.environ.get("HOME_MAX_RAD_S", "0.5"))  # Dream's home: 3 s whatever the distance
_abort_home = threading.Event()
_original_move_home = home.move_home


class _HomeAborted(Exception):
    pass


def _move_home(arms, *, duration_s=home.HOME_DURATION_S, steps=home.HOME_STEPS, clock=time.monotonic, sleep=time.sleep):
    """Dream's home move, but slow (at most HOME_MAX_RAD_S on any joint) and abortable by /freeze.

    Dream's own home move always takes 3 s (2 rad in 3 s was seen) and refuses every stop until it ends.
    """
    _abort_home.clear()
    probe: dict[str, str] = {}
    origins = home._read_origins(arms, probe)
    delta = max((float(np.max(np.abs(o.positions_rad - arms[n].home_rad))) for n, o in origins.items()), default=0.0)
    duration = max(duration_s, delta / HOME_MAX_RAD_S)
    steps = max(steps, int(duration * 40))

    def abortable_sleep(seconds: float) -> None:
        if _abort_home.wait(max(0.0, seconds)):
            raise _HomeAborted()

    try:
        return _original_move_home(arms, duration_s=duration, steps=steps, clock=clock, sleep=abortable_sleep)
    except _HomeAborted:
        _freeze_arms(arms)
        return {}  # the station goes back to idle; the arms are pinned where they were


home.move_home = _move_home


def _do_post(self: station_api._Handler) -> None:
    if self.path.split("?", 1)[0] != "/freeze":
        _original_post(self)
        return
    api = self.server.api
    ctl = api.controller
    started = time.monotonic()
    if ctl.state == "homing":
        _abort_home.set()  # the patched home move stops at its next step (25 ms) and pins the arms
    else:
        try:
            api.call(ctl.stop())  # cancel the run; the loop leaves the arms holding
        except Exception:
            pass  # already stopping, or a reset: wait below, then pin
    while ctl.state in ("homing", "stopping") and time.monotonic() - started < 3.0:
        time.sleep(0.02)
    status = ctl.status()
    try:
        arms = _freeze(api.controller)
    except Exception as error:
        self._reply(500, {"error": f"stopped, but could not pin the arms: {error}", **status})
        return
    self._reply(200, {**api.controller.status(), "frozen": arms,
                      "freeze_ms": round((time.monotonic() - started) * 1000)})


station_api._Handler.do_POST = _do_post

if __name__ == "__main__":
    station.main()

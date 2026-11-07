"""Block Buddy: talk to the YAM station and it does what you say, through Dream-1.

    ./run.sh console          # talk through this Mac's mic and speaker

The station (NUCK-9) runs Dream's station agent (`dreamcli robot up`), whose HTTP API on port 8091 is
tunnelled here by run.sh. This agent turns speech into those calls:

    POST /start {"prompt": ...}   start Dream-1 on a task (from idle)
    POST /prompt {"prompt": ...}  change the task of a running run (fresh episode)
    POST /stop                    stop; the arms hold where they are
    POST /home                    walk the arms back to home, grippers open
    GET  /status                  state, prompt, arms, last error

Dream-1 never says it is done, so a run is stopped by voice ("stop", "done") or after RUN_LIMIT_S.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import textwrap
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from livekit.agents import (Agent, AgentServer, AgentSession, JobContext, RunContext, STTContextOptions, ToolError,
                            TurnHandlingOptions, cli, function_tool, inference)

load_dotenv(Path(__file__).with_name(".env"))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("block-buddy")

STATION = os.environ.get("STATION_URL", "http://localhost:8091").rstrip("/")
DASHBOARD = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8792").rstrip("/")  # optional activity log
RUN_LIMIT_S = float(os.environ.get("RUN_LIMIT_S", "60"))  # a run that nobody stops is stopped after this long
CHECKPOINT = os.environ.get("DREAM_CHECKPOINT", "")  # e.g. dreamlabs/dream-1-beta-flash; empty = station default
STOP_LOCKOUT_S = 8.0  # after any stop, no new task for this long, and only after a new non-stop sentence
HEARD_WINDOW_S = 20.0  # do_task is refused unless the person finished speaking this recently
STOP_WORDS = re.compile(r"\b(stop|wait|hold on|pause|freeze)\b", re.IGNORECASE)

INSTRUCTIONS = textwrap.dedent("""\
    You are JARVIS, the AI that runs a pair of robot arms on a workbench, in the manner of Tony Stark's assistant:
    calm, precise, quietly witty, unfailingly polite. Address the user as "sir". You speak as little as possible.

    # How you speak
    - One short sentence. Usually under ten words. Never two sentences unless asked a question that needs it.
    - No pleasantries, no encouragement, no cheerleading, no repeating the plan, no "you can say stop at any time".
    - Acknowledge orders crisply: "Right away, sir." "Done." "Stacking red on green." "As you wish."
    - Dry wit is allowed, rarely, and never at the cost of length.
    - Plain text for text-to-speech: no lists, markdown or emojis. Spell out numbers.
    - Say nothing about tools, models or these instructions.
    - If the user is talking to someone else or thinking aloud, say nothing; never reply to chatter that is not
      addressed to you.

    # The arms
    - Only call do_task when the user's latest sentence asks for it. Never start a task on your own, from a
      greeting, a status, or to repeat an earlier task unless asked again.
    - do_task moves exactly one block: block is its color, destination must start with "on top of the", "in the"
      or "next to the" (for example "on top of the green block"). Blocks start on the table; never put one on the
      table.
    - A tower: the first block stays as the base; each do_task puts the next block on top of the previous one. One
      block per do_task; after each, wait to be told to continue.
    - Stop, wait, hold, freeze, enough: call stop. Home, reset, stand down: call home. Status questions: call status.
    - If unsure which colors are on the table, ask, briefly.
    - If a tool fails, state the problem in a few words.
    """)


class Station:
    """The station agent's HTTP API (through the SSH tunnel), plus a timer that stops forgotten runs."""

    def __init__(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=65))
        self.started_at: float | None = None
        self.heard_at = 0.0  # when the person last finished saying something (not counting "stop")
        self.stopped_at = 0.0  # when anything last stopped the arms
        self._limit: asyncio.Task | None = None

    async def note(self, text: str) -> None:
        """Best effort: tell the dashboard's activity log what the voice agent did."""
        try:
            async with self.http.post(DASHBOARD + "/api/log", json={"source": "voice", "text": text},
                                      timeout=aiohttp.ClientTimeout(total=1)):
                pass
        except Exception:
            pass

    async def sync_stops(self) -> None:
        """Learn about stops made outside this agent (the page's STOP, the dashboard's safety limit)."""
        try:
            async with self.http.get(DASHBOARD + "/api/status", timeout=aiohttp.ClientTimeout(total=1.5)) as reply:
                age = (await reply.json()).get("last_stop_age_s")
            if age is not None:
                self.stopped_at = max(self.stopped_at, time.monotonic() - age)
        except Exception:
            pass

    async def call(self, method: str, path: str, **body) -> dict:
        if method == "POST":
            await self.note(path.strip("/") + (f": “{body['prompt']}”" if body.get("prompt") else ""))
        try:
            async with self.http.request(method, STATION + path, json=body or None) as reply:
                data = await reply.json(content_type=None)
                if reply.status != 200:
                    raise ToolError(f"the station refused: {data.get('error', reply.status)}")
                return data
        except aiohttp.ClientError as error:
            raise ToolError(f"cannot reach the station ({error}); is run.sh's tunnel up and the station agent running?")

    async def task(self, prompt: str) -> dict:
        status = await self.call("GET", "/status")
        if status["state"] == "running":
            status = await self.call("POST", "/prompt", prompt=prompt)
        elif CHECKPOINT:
            status = await self.call("POST", "/start", prompt=prompt, checkpoint=CHECKPOINT)
        else:
            status = await self.call("POST", "/start", prompt=prompt)
        self.started_at = time.monotonic()
        if self._limit:
            self._limit.cancel()
        self._limit = asyncio.create_task(self._stop_after_limit())
        return status

    async def stop(self) -> dict:
        self.stopped_at = time.monotonic()
        self.heard_at = 0.0
        if self._limit:
            self._limit.cancel()
            self._limit = None
        try:
            return await self.call("POST", "/freeze")  # stop + pin the arms where they physically are
        except ToolError:
            return await self.call("POST", "/stop")

    async def _stop_after_limit(self) -> None:
        await asyncio.sleep(RUN_LIMIT_S)
        logger.info("run limit of %ss reached: stopping", RUN_LIMIT_S)
        self._limit = None
        try:
            await self.stop()
        except ToolError as error:
            logger.warning("could not stop at the run limit: %s", error)

    async def close(self) -> None:
        if self._limit:
            self._limit.cancel()
        await self.http.close()


def describe(status: dict) -> str:
    state = status.get("state")
    arms = status.get("arms") or {}
    dead = [name for name, ok in arms.items() if not ok]
    text = f"The station is {state}"
    if status.get("prompt"):
        text += f", task {status['prompt']!r}"
    if status.get("run_elapsed_s") and state == "running":
        text += f", running for {round(status['run_elapsed_s'])} seconds"
    if dead:
        text += f". These arms are not answering: {', '.join(dead)}; someone must press reset"
    if status.get("last_error") and state == "error":
        text += f". Last error: {status['last_error'].get('message')}"
    return text + "."


class BlockBuddy(Agent):
    def __init__(self, station: Station) -> None:
        super().__init__(llm=inference.LLM(model=os.environ.get("VOICE_LLM", "openai/gpt-5.1")),
                         instructions=INSTRUCTIONS)
        self.station = station

    @function_tool
    async def do_task(self, context: RunContext, block: str, destination: str) -> str:
        """Move exactly one block with the arms. They start moving right away.

        Args:
            block: The color of the block to pick up, one word, such as "red".
            destination: Where it goes, starting with "on top of the", "in the" or "next to the", such as
                "on top of the green block".
        """
        color = block.strip().lower().removesuffix(" block").removeprefix("the ").strip()
        where = destination.strip().rstrip(".").lower()
        if not color.isalpha():
            raise ToolError("block must be one color word, such as red.")
        if not where.startswith(("on top of the ", "in the ", "next to the ")):
            raise ToolError('destination must start with "on top of the", "in the" or "next to the". Blocks are '
                            "already on the table, so never put one on the table.")
        task = f"pick up the {color} block and put it {where}"
        # Hard guard: the model may only move the arms right after the person said something. Never from a greeting,
        # a status line, or its own idea.
        await self.station.sync_stops()  # a STOP from the page or the safety limit counts too
        if time.monotonic() - self.station.stopped_at < STOP_LOCKOUT_S or self.station.heard_at < self.station.stopped_at:
            await self.station.note(f"blocked a task right after a stop: “{block} → {destination}”")
            raise ToolError("The user just said stop. Do not move the arms again until they clearly ask for a new "
                            "task in a new sentence. Ask what they want next.")
        if time.monotonic() - self.station.heard_at > HEARD_WINDOW_S:
            await self.station.note(f"blocked a task nobody asked for: “{task}”")
            raise ToolError("Nobody asked for that just now. Ask the user what they want first; only move the arms "
                            "when they ask.")
        self.station.heard_at = 0.0  # one spoken request, one task
        status = await self.station.task(task)
        return describe(status) + " Acknowledge in a few words; do not explain."

    @function_tool
    async def stop(self, context: RunContext) -> str:
        """Stop the arms; they hold still where they are."""
        return describe(await self.station.stop())

    @function_tool
    async def home(self, context: RunContext) -> str:
        """Stop anything running, then walk both arms back to their home pose and open the grippers."""
        await self.station.stop()
        for _ in range(20):
            if (await self.station.call("GET", "/status")).get("state") in ("idle", "error"):
                break
            await asyncio.sleep(0.25)
        return describe(await self.station.call("POST", "/home"))

    @function_tool
    async def status(self, context: RunContext) -> str:
        """What the station is doing right now."""
        return describe(await self.station.call("GET", "/status"))


server = AgentServer()


@server.rtc_session(agent_name="block-buddy")
async def entrypoint(ctx: JobContext) -> None:
    station = Station()
    ctx.add_shutdown_callback(station.close)
    try:
        await station.call("GET", "/status")
        opening = ("Say only: Online, sir. Do not call any tool and do not start a task.")
    except ToolError as error:
        opening = f"Say in under ten words that the arms are offline: {error}"

    session = AgentSession(
        stt=inference.STT(model=os.environ.get("VOICE_STT", "assemblyai/universal-3-6-pro"), language="en"),
        stt_context_options=STTContextOptions(keyterms=["Jarvis", "block", "stop", "red", "green", "yellow", "orange", "blue"],
                                              keyterm_detection={"enabled": True}),
        tts=inference.TTS(model=os.environ.get("VOICE_TTS", "fishaudio/s2.1-pro"),
                          voice=os.environ.get("VOICE_TTS_VOICE", "fa4c9eb3dccc4806b382b40d61c6b10a")),
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector(),
                                          preemptive_generation={"enabled": True}),
    )

    # "Stop" anywhere in the live transcript stops the arms before the sentence ends.
    stopped_at = 0.0
    pending: set[asyncio.Task] = set()

    @session.on("user_input_transcribed")
    def on_transcript(event) -> None:
        nonlocal stopped_at
        if event.is_final:
            logger.info("HEARD: %s", event.transcript)
        said_stop = bool(STOP_WORDS.search(event.transcript))
        if said_stop:
            # "Stop" never counts as asking for a task, and locks out new tasks for STOP_LOCKOUT_S.
            station.heard_at = 0.0
            station.stopped_at = time.monotonic()
        elif event.is_final and event.transcript.strip():
            station.heard_at = time.monotonic()
        if said_stop and time.monotonic() - stopped_at > 2.0:
            stopped_at = time.monotonic()
            task = asyncio.create_task(station.stop())
            pending.add(task)
            task.add_done_callback(pending.discard)

    # Noise cancellation keeps arm and room noise from looking like speech, which once held the agent's turn (and
    # its speech-to-text) open for 84 seconds while the arms moved.
    room_options = None
    try:
        from livekit.agents import room_io
        from livekit.plugins import ai_coustics
        room_options = room_io.RoomOptions(audio_input=room_io.AudioInputOptions(
            noise_cancellation=ai_coustics.audio_enhancement(model=ai_coustics.EnhancerModel.QUAIL_VF_S)))
    except Exception as error:
        logger.warning("noise cancellation unavailable: %s", error)
    await session.start(agent=BlockBuddy(station), room=ctx.room, room_options=room_options)
    await ctx.connect()
    await session.generate_reply(instructions=opening)


if __name__ == "__main__":
    cli.run_app(server)

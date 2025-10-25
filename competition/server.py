from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import suppress
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

try:  # pragma: no cover - fallback for direct script execution
    from .agent_runner import run_agent
    from . import race_manager
except ImportError:  # pragma: no cover - fallback for direct script execution
    from agent_runner import run_agent  # type: ignore
    import race_manager  # type: ignore


class TaskRequest(BaseModel):
    task: str = Field(..., min_length=2, description="Task instruction to hand off to the agent")


class HumanSubmissionRequest(BaseModel):
    submission: Optional[str] = Field(default=None, description="Human race submission text (if applicable)")


class RunState:
    def __init__(self, run_id: str, task: str, race_id: Optional[str] = None) -> None:
        self.run_id = run_id
        self.task = task
        self.race_id = race_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.agent_task: asyncio.Task[None] | None = None
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._buffer: list[dict[str, Any]] = []
        self._dispatch_task: asyncio.Task[None] | None = asyncio.create_task(self._dispatch_loop())
        self._dispatch_done = asyncio.Event()

    async def _dispatch_loop(self) -> None:
        try:
            while True:
                event = await self.queue.get()
                if self.race_id:
                    await race_manager.handle_run_event(self.run_id, event)
                self._buffer.append(event)
                for listener in list(self._listeners):
                    listener.put_nowait(event)
                if event.get("type") == "complete":
                    break
        finally:
            self._dispatch_done.set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        listener: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in self._buffer:
            listener.put_nowait(event)
        if not self._dispatch_done.is_set():
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(listener)

    async def wait_for_dispatch(self) -> None:
        if self._dispatch_task is None:
            return
        await self._dispatch_done.wait()
        with suppress(asyncio.CancelledError):
            await self._dispatch_task

    def cancel_dispatch(self) -> None:
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()


run_states: dict[str, RunState] = {}

app = FastAPI(title="Competition Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _launch_run(task: str, *, race_id: Optional[str] = None) -> dict[str, str]:
    run_id = uuid.uuid4().hex
    state = RunState(run_id=run_id, task=task, race_id=race_id)
    run_states[run_id] = state

    if race_id:
        try:
            race_manager.register_agent_run(race_id, run_id)
        except Exception:
            state.cancel_dispatch()
            run_states.pop(run_id, None)
            raise

    async def runner() -> None:
        try:
            await run_agent(task, state.queue)
        finally:
            await state.wait_for_dispatch()
            run_states.pop(run_id, None)

    state.agent_task = asyncio.create_task(runner())
    return {"run_id": run_id}


@app.post("/run")
async def start_run(request: TaskRequest) -> dict[str, str]:
    return await _launch_run(request.task)


@app.get("/run/{run_id}/events")
async def stream_events(run_id: str) -> StreamingResponse:
    state = run_states.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown run id")

    listener_queue = state.subscribe()

    async def event_generator() -> Any:
        try:
            while True:
                event = await listener_queue.get()
                payload = json.dumps(event)
                event_type = event.get("type", "message")
                yield f"event: {event_type}\ndata: {payload}\n\n"
                if event.get("type") == "complete":
                    break
        except asyncio.CancelledError:  # pragma: no cover - consumer disconnect
            raise
        finally:
            state.unsubscribe(listener_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/run/{run_id}")
async def get_status(run_id: str) -> dict[str, Any]:
    state = run_states.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown run id")
    task_state = "running"
    if state.agent_task and state.agent_task.done():
        task_state = "error" if state.agent_task.exception() else "completed"
    return {"run_id": run_id, "task": state.task, "state": task_state}


@app.post("/race")
async def create_race() -> Dict[str, Any]:
    race = await race_manager.create_race()
    return race_manager.race_summary(race)


@app.get("/race/{race_id}")
async def get_race(race_id: str) -> Dict[str, Any]:
    try:
        race = race_manager.get_race(race_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown race id") from None
    return race_manager.race_summary(race)


@app.post("/race/{race_id}/agent/start")
async def start_race_agent(race_id: str) -> Dict[str, Any]:
    try:
        race = race_manager.get_race(race_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown race id") from None

    launch = await _launch_run(race.task.agent_instructions, race_id=race_id)
    return {**race_manager.race_summary(race), **launch}


@app.post("/race/{race_id}/human/start")
async def start_human_attempt(race_id: str) -> Dict[str, Any]:
    try:
        race = race_manager.mark_human_started(race_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown race id") from None
    return race_manager.race_summary(race)


@app.post("/race/{race_id}/human/submit")
async def submit_human_attempt(race_id: str, request: HumanSubmissionRequest) -> Dict[str, Any]:
    try:
        race = await race_manager.record_human_submission(race_id, request.submission)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown race id") from None
    return race_manager.race_summary(race)


if __name__ == "__main__":
    port = int(os.getenv("AGENT_SERVER_PORT", "8000"))
    host = os.getenv("AGENT_SERVER_HOST", "0.0.0.0")
    reload = os.getenv("AGENT_SERVER_RELOAD", "false").lower() == "true"
    uvicorn.run(app, host=host, port=port, reload=reload)

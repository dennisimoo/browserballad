from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:  # pragma: no cover - support package and script execution styles
    from . import llm
except ImportError:  # pragma: no cover - fallback when executed as a script
    import llm  # type: ignore


@dataclass
class RaceTask:
    title: str
    summary: str
    human_instructions: str
    agent_instructions: str
    task_type: str
    success_criteria: str
    expected_output_description: str
    evaluation_guidelines: list[str]

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RaceTask":
        return cls(
            title=payload["title"],
            summary=payload["summary"],
            human_instructions=payload["human_instructions"],
            agent_instructions=payload["agent_instructions"],
            task_type=payload["task_type"],
            success_criteria=payload["success_criteria"],
            expected_output_description=payload["expected_output_description"],
            evaluation_guidelines=list(payload.get("evaluation_guidelines", [])),
        )


@dataclass
class ParticipantState:
    status: str = "pending"
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    result: Optional[str] = None
    live_url: Optional[str] = None


@dataclass
class RaceState:
    race_id: str
    task: RaceTask
    created_at: float = field(default_factory=lambda: time.time())
    status: str = "ready"
    agent_run_id: Optional[str] = None
    agent: ParticipantState = field(default_factory=lambda: ParticipantState(status="pending"))
    human: ParticipantState = field(default_factory=lambda: ParticipantState(status="pending"))
    human_submission: Optional[str] = None
    verdict: Optional[Dict[str, Any]] = None
    finalizing: bool = False

    def to_response(self) -> Dict[str, Any]:
        return {
            "race_id": self.race_id,
            "status": self.status,
            "task": {
                "title": self.task.title,
                "summary": self.task.summary,
                "human_instructions": self.task.human_instructions,
                "agent_instructions": self.task.agent_instructions,
                "task_type": self.task.task_type,
                "success_criteria": self.task.success_criteria,
                "expected_output_description": self.task.expected_output_description,
                "evaluation_guidelines": self.task.evaluation_guidelines,
            },
            "agent": _participant_to_dict(self.agent, include_live_url=True, include_result=True),
            "human": _participant_to_dict(self.human, include_result=self.task.task_type == "text_entry"),
            "verdict": self.verdict,
        }


RACE_STATES: Dict[str, RaceState] = {}
RUN_TO_RACE: Dict[str, str] = {}
_race_lock = asyncio.Lock()


def _participant_to_dict(participant: ParticipantState, *, include_live_url: bool = False, include_result: bool = False) -> Dict[str, Any]:
    payload = {
        "status": participant.status,
        "started_at": _ts_to_iso(participant.started_at),
        "completed_at": _ts_to_iso(participant.completed_at),
        "duration_seconds": participant.duration_seconds,
    }
    if include_result:
        payload["result"] = participant.result
    if include_live_url:
        payload["live_url"] = participant.live_url
    return payload


def _ts_to_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def create_race() -> RaceState:
    async with _race_lock:
        task_payload = await llm.generate_race_task()
        race_id = uuid.uuid4().hex
        task = RaceTask.from_payload(task_payload)
        race = RaceState(race_id=race_id, task=task)
        RACE_STATES[race_id] = race
        return race


def get_race(race_id: str) -> RaceState:
    race = RACE_STATES.get(race_id)
    if not race:
        raise KeyError(f"Unknown race id {race_id}")
    return race


def register_agent_run(race_id: str, run_id: str) -> RaceState:
    race = get_race(race_id)
    if race.agent_run_id is not None:
        raise RuntimeError(f"Agent run already registered for race {race_id}")
    race.agent_run_id = run_id
    RUN_TO_RACE[run_id] = race_id
    now = time.time()
    race.status = "running"
    race.agent.status = "starting"
    race.agent.started_at = race.agent.started_at or now
    return race


async def handle_run_event(run_id: str, event: Dict[str, Any]) -> None:
    race_id = RUN_TO_RACE.get(run_id)
    if not race_id:
        return
    race = get_race(race_id)

    event_type = event.get("type")
    if event_type == "status":
        status = event.get("status")
        if status:
            race.agent.status = status
            if status == "running" and race.agent.started_at is None:
                race.agent.started_at = time.time()
    elif event_type == "live_url":
        url = event.get("url")
        if url:
            race.agent.live_url = url
    elif event_type == "result":
        result = event.get("result")
        if result is not None:
            race.agent.result = result
            race.agent.completed_at = race.agent.completed_at or time.time()
            if race.agent.started_at:
                race.agent.duration_seconds = race.agent.completed_at - race.agent.started_at
    elif event_type == "error":
        race.agent.status = "error"
    elif event_type == "complete":
        race.agent.status = "completed"
        race.agent.completed_at = race.agent.completed_at or time.time()
        if race.agent.started_at and race.agent.duration_seconds is None:
            race.agent.duration_seconds = race.agent.completed_at - race.agent.started_at
        if race.status != "judging" and race.status != "completed":
            race.status = "awaiting_human" if race.human.status != "completed" else race.status
        RUN_TO_RACE.pop(run_id, None)

    await _maybe_finalize(race)


async def record_human_submission(race_id: str, submission: Optional[str]) -> RaceState:
    race = get_race(race_id)

    if race.human.status == "pending":
        race.human.status = "running"
        race.human.started_at = race.human.started_at or time.time()

    race.human.status = "completed"
    race.human.completed_at = race.human.completed_at or time.time()
    if race.human.started_at and race.human.duration_seconds is None:
        race.human.duration_seconds = race.human.completed_at - race.human.started_at

    if race.task.task_type == "text_entry":
        race.human_submission = submission or ""
        race.human.result = race.human_submission
    else:
        race.human_submission = submission or ""

    await _maybe_finalize(race)
    return race


async def _maybe_finalize(race: RaceState) -> None:
    if race.verdict is not None or race.finalizing:
        return

    agent_ready = race.agent.result is not None or race.agent.status == "error"
    if race.task.task_type == "text_entry":
        human_ready = race.human_submission is not None
    else:
        human_ready = race.human.status == "completed"

    if not (agent_ready and human_ready):
        return

    race.finalizing = True
    race.status = "judging"

    async def _run_judgement() -> None:
        try:
            verdict = await llm.judge_race(
                task=asdict(race.task),
                agent_result=race.agent.result,
                human_submission=race.human_submission,
                agent_duration=race.agent.duration_seconds,
                human_duration=race.human.duration_seconds,
            )
            race.verdict = verdict
            race.status = "completed"
        except Exception as exc:  # pragma: no cover - surfaced via API
            race.verdict = {
                "winner": "tie",
                "reasoning": f"Judging failed: {exc}",
                "agent_score": 0.0,
                "human_score": 0.0,
            }
            race.status = "completed"
        finally:
            race.finalizing = False

    asyncio.create_task(_run_judgement())


async def get_or_create_race(race_id: str) -> RaceState:
    async with _race_lock:
        return get_race(race_id)


def clear_race(race_id: str) -> None:
    RACE_STATES.pop(race_id, None)
    run_ids = [run_id for run_id, rid in RUN_TO_RACE.items() if rid == race_id]
    for run_id in run_ids:
        RUN_TO_RACE.pop(run_id, None)


def race_summary(race: RaceState) -> Dict[str, Any]:
    return {"race": race.to_response()}


def mark_human_started(race_id: str) -> RaceState:
    race = get_race(race_id)
    if race.human.status == "completed":
        return race
    if race.human.status == "pending":
        race.human.status = "running"
        race.human.started_at = race.human.started_at or time.time()
        if race.status == "ready":
            race.status = "running"
    return race

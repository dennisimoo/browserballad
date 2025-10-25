from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Generator

import pytest  # type: ignore[import-not-found]
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from competition import race_manager, server  # type: ignore[import-not-found]


@pytest.fixture(autouse=True)
def reset_race_state() -> Generator[None, None, None]:
    race_manager.RACE_STATES.clear()
    race_manager.RUN_TO_RACE.clear()
    yield
    race_manager.RACE_STATES.clear()
    race_manager.RUN_TO_RACE.clear()


async def _fake_generate_race_task() -> Dict[str, Any]:
    return {
        "title": "Collect a homepage headline",
        "summary": "Both participants must capture the main headline on example.com.",
        "human_instructions": "Navigate to https://example.com and copy the main headline text.",
        "agent_instructions": "Visit https://example.com and extract the primary heading text.",
        "task_type": "text_entry",
        "success_criteria": "Captures the same headline text displayed on the page.",
        "expected_output_description": "A short string containing the headline.",
        "evaluation_guidelines": [
            "Confirm the headline text matches the site.",
            "Reward faster completion when accuracy ties.",
        ],
    }


async def _fake_judge_race(**_: Any) -> Dict[str, Any]:
    await asyncio.sleep(0)
    return {
        "winner": "human",
        "reasoning": "The human provided the exact headline with quicker completion time.",
        "agent_score": 6.0,
        "human_score": 8.5,
    }


async def _fake_run_agent(task: str, queue: asyncio.Queue[Dict[str, Any]]) -> None:  # pragma: no cover - exercised via test
    await queue.put({"type": "status", "status": "running", "task": task})
    await queue.put({"type": "live_url", "url": "https://live.browser-use.com/test"})
    await queue.put({"type": "result", "result": "Example headline captured"})
    await queue.put({"type": "complete"})


def _wait_for(
    predicate: Callable[[Dict[str, Any]], bool],
    client: TestClient,
    race_id: str,
    attempts: int = 20,
    delay: float = 0.05,
) -> Dict[str, Any]:
    for _ in range(attempts):
        payload = client.get(f"/race/{race_id}").json()["race"]
        if predicate(payload):
            return payload
        time.sleep(delay)
    raise AssertionError("Condition not met within timeout")


def test_race_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.race_manager.llm, "generate_race_task", _fake_generate_race_task)
    monkeypatch.setattr(server.race_manager.llm, "judge_race", _fake_judge_race)
    monkeypatch.setattr(server, "run_agent", _fake_run_agent)

    with TestClient(server.app) as client:
        race_payload = client.post("/race").json()["race"]
        race_id = race_payload["race_id"]

        start_agent_response = client.post(f"/race/{race_id}/agent/start").json()
        run_id = start_agent_response["run_id"]
        assert run_id

        agent_state = _wait_for(lambda payload: payload["agent"]["status"] == "completed", client, race_id)
        assert agent_state["agent"]["result"] == "Example headline captured"
        assert agent_state["agent"]["live_url"] == "https://live.browser-use.com/test"

        client.post(f"/race/{race_id}/human/start")
        client.post(f"/race/{race_id}/human/submit", json={"submission": "Example Domain"})

        final_state = _wait_for(lambda payload: payload["verdict"] is not None and payload["status"] == "completed", client, race_id)
        verdict = final_state["verdict"]
        assert verdict["winner"] == "human"
        assert verdict["agent_score"] == pytest.approx(6.0)
        assert verdict["human_score"] == pytest.approx(8.5)
        assert final_state["human"]["status"] == "completed"
from __future__ import annotations

import json
import os
import random
from typing import Any, Dict

from openai import AsyncOpenAI

_TASK_MODEL = os.getenv("RACE_TASK_MODEL", "gpt-4.1-mini")
_JUDGE_MODEL = os.getenv("RACE_JUDGE_MODEL", "gpt-5-mini")

# A local pool of race tasks while AI generation is disabled.
# Feel free to edit or extend this list to suit new challenges.
STATIC_TASKS: list[Dict[str, Any]] = [
    {
        "title": "Wikipedia Spider-Man to Captain America Navigation",
        "summary": "Navigate from Spider-Man's Wikipedia page to Captain America's Wikipedia page and extract specific information.",
        "human_instructions": (
            "Start at the Wikipedia article for Spider-Man (https://en.wikipedia.org/wiki/Spider-Man). "
            "Navigate to the Captain America Wikipedia article by clicking links within Wikipedia pages. "
            "Once there, find and provide Captain America's real name."
        ),
        "agent_instructions": (
            "Navigate to https://en.wikipedia.org/wiki/Spider-Man, then follow internal Wikipedia links "
            "to reach the Captain America article. Extract and return Captain America's real name from the page."
        ),
        "task_type": "text_entry",
        "success_criteria": "Must provide Captain America's real name (Steve Rogers) found on the Wikipedia page.",
        "expected_output_description": "Captain America's real name.",
        "evaluation_guidelines": [
            "Verify the answer is 'Steve Rogers' or 'Steven Rogers'.",
            "Confirm navigation was done through Wikipedia links (not direct URL entry).",
            "Information must be extracted from the Captain America Wikipedia article.",
        ],
    },
]

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing OPENAI_API_KEY environment variable")
        _client = AsyncOpenAI(api_key=api_key)
    return _client


def _extract_json_from_response(content: str) -> Dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        # Remove Markdown code fences if present.
        content = content.strip("`")
        newline_idx = content.find("\n")
        if newline_idx != -1:
            content = content[newline_idx + 1 :]
    return json.loads(content)


async def _invoke_response(client: AsyncOpenAI, *, model: str, messages: list[Dict[str, Any]], json_mode: bool) -> Any:
    payload: Dict[str, Any] = {
        "model": model,
        "input": messages,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        return await client.responses.create(**payload)
    except TypeError:
        if json_mode:
            payload.pop("response_format", None)
            return await client.responses.create(**payload)
        raise


def _response_to_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text

    output = getattr(response, "output", None)
    if not output:
        raise RuntimeError("LLM response missing content")

    parts: list[str] = []
    for item in output:
        content_items = getattr(item, "content", None)
        if not content_items:
            continue
        for chunk in content_items:
            value = None
            if isinstance(chunk, dict):
                if "text" in chunk and chunk["text"]:
                    maybe_text = chunk["text"]
                    if isinstance(maybe_text, dict):
                        value = maybe_text.get("value") or maybe_text.get("text")
                    elif isinstance(maybe_text, str):
                        value = maybe_text
                elif "output_text" in chunk and chunk["output_text"]:
                    value = str(chunk["output_text"])
            else:
                text_obj = getattr(chunk, "text", None)
                if text_obj:
                    value = getattr(text_obj, "value", None) or getattr(text_obj, "text", None)
                    if value is None and isinstance(text_obj, str):
                        value = text_obj
                if value is None:
                    maybe_output = getattr(chunk, "output_text", None)
                    if maybe_output:
                        value = str(maybe_output)
            if value:
                parts.append(str(value))

    combined = "".join(parts).strip()
    if not combined:
        raise RuntimeError("LLM response missing textual output")
    return combined


async def generate_race_task() -> Dict[str, Any]:
    if STATIC_TASKS:
        # Return a copy so callers can mutate without affecting the template list.
        return json.loads(json.dumps(random.choice(STATIC_TASKS)))

    return await _generate_task_via_ai()


async def _generate_task_via_ai() -> Dict[str, Any]:
    client = _get_client()

    system_prompt = (
        "You design short competitive tasks where a human races an autonomous browser agent. "
        "Each assignment must take place in a standard web browser, require navigating live websites, and be solvable in under 3 minutes. "
        "Favor objectives like visiting a specific site, running a search, collecting top-N results, extracting contact details, or confirming on-page facts. "
        "Return strict JSON with keys: title, summary, human_instructions, agent_instructions, "
        "task_type (text_entry or confirmation), success_criteria, expected_output_description, "
        "and evaluation_guidelines (array of bullet strings). Avoid Markdown and explanations."
    )

    user_prompt = (
        "Create a creative but fair browser-based race task. "
        "Ensure human instructions clearly describe the steps (e.g., 'Go to example.com and list the top 5 resources about X'). "
        "Agent instructions should precisely describe the browsing actions and required output. "
        "Avoid tasks that require logging in, payments, or unsafe behavior. "
        "For confirmation tasks, success should hinge on verifying something visible on the page rather than free-form text."
    )

    response = await _invoke_response(
        client,
        model=_TASK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        json_mode=True,
    )

    content = _response_to_text(response)
    if not content:
        raise RuntimeError("Task generation returned no content")

    task_payload = _extract_json_from_response(content)

    required_keys = {
        "title",
        "summary",
        "human_instructions",
        "agent_instructions",
        "task_type",
        "success_criteria",
        "expected_output_description",
        "evaluation_guidelines",
    }
    missing = required_keys.difference(task_payload)
    if missing:
        raise RuntimeError(f"Task generation missing keys: {', '.join(sorted(missing))}")

    task_type = task_payload["task_type"].strip().lower()
    if task_type not in {"text_entry", "confirmation"}:
        raise RuntimeError(f"Unsupported task type from generator: {task_type}")

    return task_payload


async def judge_race(
    task: Dict[str, Any],
    agent_result: str | None,
    human_submission: str | None,
    agent_duration: float | None,
    human_duration: float | None,
) -> Dict[str, Any]:
    client = _get_client()

    system_prompt = (
        "You are an impartial adjudicator comparing a human participant to an autonomous browser agent. "
        "You must analyze accuracy, completeness, adherence to success criteria, and speed. "
        "Respond strictly with JSON containing the keys winner (agent, human, or tie), reasoning, agent_score, and human_score. "
        "Scores must be floating point numbers between 0 and 10. Do not include any additional commentary fields such as feedback."
    )

    evaluation_context = {
        "task": {
            "title": task.get("title"),
            "summary": task.get("summary"),
            "success_criteria": task.get("success_criteria"),
            "expected_output_description": task.get("expected_output_description"),
            "evaluation_guidelines": task.get("evaluation_guidelines"),
            "task_type": task.get("task_type"),
        },
        "agent": {
            "result": agent_result or "",
            "duration_seconds": agent_duration,
        },
        "human": {
            "submission": human_submission or "",
            "duration_seconds": human_duration,
        },
    }

    user_prompt = (
        "Evaluate the race participants using the provided context. If an entry is missing or empty,"
        " penalize accordingly. Consider speed but prioritize task success."
        " Respond with JSON only."
    )

    response = await _invoke_response(
        client,
        model=_JUDGE_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(evaluation_context)},
            {"role": "user", "content": user_prompt},
        ],
        json_mode=True,
    )

    content = _response_to_text(response)
    if not content:
        raise RuntimeError("Judge returned no content")

    verdict = _extract_json_from_response(content)

    required_keys = {"winner", "reasoning", "agent_score", "human_score"}
    missing = required_keys.difference(verdict)
    if missing:
        raise RuntimeError(f"Judge response missing keys: {', '.join(sorted(missing))}")

    normalized_verdict = {key: verdict[key] for key in required_keys}

    winner = str(normalized_verdict["winner"]).lower()
    if winner not in {"agent", "human", "tie"}:
        normalized_verdict["winner"] = "tie"
    else:
        normalized_verdict["winner"] = winner

    for score_key in ("agent_score", "human_score"):
        try:
            normalized_verdict[score_key] = float(normalized_verdict[score_key])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Judge response {score_key} is not numeric: {normalized_verdict[score_key]!r}") from exc

    return normalized_verdict

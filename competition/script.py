import argparse
import asyncio
from typing import Any

from competition.agent_runner import run_agent


async def run_cli(task: str) -> list[dict[str, Any]]:
    """Run the agent for a single task and print streamed events."""

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    agent_task = asyncio.create_task(run_agent(task, queue))
    events: list[dict[str, Any]] = []

    while True:
        event = await queue.get()
        events.append(event)
        print(event)
        if event.get("type") == "complete":
            break

    await agent_task
    return events


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the browser-use agent for an ad-hoc task")
    parser.add_argument(
        "task",
        nargs="?",
        default="find the email of Pranav Sekhar in San Francisco",
        help="Task instruction passed to the agent.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_cli(args.task))



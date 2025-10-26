from __future__ import annotations

import asyncio
import logging
import os
import re

import dotenv
from browser_use import Agent, Browser, ChatBrowserUse, Tools
from browser_use_sdk import BrowserUse

dotenv.load_dotenv()
os.environ["ANONYMIZED_TELEMETRY"] = "false"

_api_key = os.getenv("BROWSER_USE_API_KEY")
if not _api_key:
    raise RuntimeError("Missing BROWSER_USE_API_KEY environment variable")

client = BrowserUse(api_key=_api_key)

LIVE_URL_PATTERN = re.compile(r"(https://live\.browser-use\.com[^\s\x1b]+)", re.IGNORECASE)


class QueueLogHandler(logging.Handler):
    """Bridge logging records into an asyncio queue for streaming."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(level=logging.INFO)
        self._queue = queue
        self._loop = loop
        self._live_url_sent = False

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - logging infrastructure
        message = self.format(record)
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait,
            {"type": "log", "message": message},
        )

        if not self._live_url_sent:
            match = LIVE_URL_PATTERN.search(record.getMessage())
            if match:
                self._live_url_sent = True
                url = match.group(1).strip()
                self._loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    {"type": "live_url", "url": url},
                )


async def run_agent(task: str, queue: asyncio.Queue) -> None:
    """Execute the browser-use agent for the provided task and stream events."""

    loop = asyncio.get_running_loop()
    handler = QueueLogHandler(queue=queue, loop=loop)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    previous_level = root_logger.level
    if previous_level == logging.NOTSET or previous_level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    # also ensure our own logger is emitting INFO level messages
    module_logger = logging.getLogger("browser_use")
    module_prev_level = module_logger.level
    if module_prev_level == logging.NOTSET or module_prev_level > logging.INFO:
        module_logger.setLevel(logging.INFO)
    module_handler_attached = False
    if handler not in module_logger.handlers:
        module_logger.addHandler(handler)
        module_handler_attached = True

    await queue.put({"type": "status", "status": "starting", "task": task})

    session = client.sessions.create_session(
        profile_id="faf3ff86-030f-4b90-a5fc-97e1c08e03d0",
        proxy_country_code="us",
    )

    tools = Tools()
    agent_ref: dict[str, Agent | None] = {"agent": None}

    @tools.action(description="Ask the user to provide additional information via the console")  # type: ignore[arg-type]
    async def ask_user_input() -> str:
        await queue.put({"type": "log", "message": "Agent paused for manual user input."})
        agent = agent_ref["agent"]
        if agent:
            agent.pause()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: input("Please provide the required information and press Enter to continue: "),
            )
        finally:
            if agent:
                agent.resume()
        await queue.put({"type": "log", "message": f"Received manual input ({len(data)} characters)."})
        return data

    browser = Browser(use_cloud=True)
    agent = Agent(
        task=task,
        browser=browser,
        session=session,
        llm=ChatBrowserUse(),
        use_vision=True,
        thinking=False,
        flash_mode=True,
        tools=tools,
    )
    agent_ref["agent"] = agent

    try:
        await queue.put({"type": "status", "status": "running"})
        result = await agent.run()
        await queue.put({"type": "status", "status": "completed"})
        await queue.put({"type": "result", "result": str(result)})
    except Exception as exc:  # pragma: no cover - surfaced through queue events
        logging.exception("Agent run failed")
        await queue.put({"type": "error", "message": str(exc)})
        raise
    finally:
        root_logger.removeHandler(handler)
        if previous_level == logging.NOTSET:
            root_logger.setLevel(logging.NOTSET)
        else:
            root_logger.setLevel(previous_level)
        if module_handler_attached:
            module_logger.removeHandler(handler)
        if module_prev_level == logging.NOTSET:
            module_logger.setLevel(logging.NOTSET)
        else:
            module_logger.setLevel(module_prev_level)
        await queue.put({"type": "complete"})

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class IncomingMessage:
    message_id: str
    umo: str
    sender_id: str
    text: str
    segments: list[Any] = field(default_factory=list)
    event: Any = None

    def agent_text(self) -> str:
        from .message_codec import message_for_agent

        return message_for_agent(self.text, self.segments)


@dataclass(slots=True)
class Activity:
    activity_id: str
    umo: str
    scene_id: str
    owner_sender_id: str
    phase: str = "starting"
    event: Any = None
    runner: Any = None
    task: asyncio.Task | None = None
    wait_queue: asyncio.Queue[IncomingMessage] = field(default_factory=asyncio.Queue)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    follow_up_count: int = 0
    outbound_count: int = 0
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    final_sent: bool = False
    seen_message_ids: set[str] = field(default_factory=set)
    restored: bool = False
    resume_context: str = ""
    explicit_end: bool = False

    def stop(self, reason: str = "user") -> None:
        self.phase = "stopping"
        self.cancel_event.set()
        if self.runner is not None and hasattr(self.runner, "request_stop"):
            self.runner.request_stop()
        while not self.wait_queue.empty():
            try:
                self.wait_queue.get_nowait()
            except asyncio.QueueEmpty:
                break


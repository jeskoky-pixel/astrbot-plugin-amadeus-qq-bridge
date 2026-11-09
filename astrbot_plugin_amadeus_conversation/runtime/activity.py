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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    outbound_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    message_event: asyncio.Event = field(default_factory=asyncio.Event)
    wait_queue: asyncio.Queue[IncomingMessage] = field(default_factory=asyncio.Queue)
    event_by_message_id: dict[str, Any] = field(default_factory=dict)
    follow_up_tickets: dict[str, Any] = field(default_factory=dict)
    rich_followups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    waiter_active: bool = False
    wait_baseline_seq: int = 0
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
    aborted: bool = False
    deadline_at: float | None = None
    response_types: list[str] = field(default_factory=list)
    last_error: str | None = None

    def stop(self, reason: str = "user") -> None:
        self.phase = "stopping"
        self.explicit_end = True
        self.cancel_event.set()
        self.message_event.set()
        if self.runner is not None and hasattr(self.runner, "request_stop"):
            self.runner.request_stop()
    def record_response(self, response_type: Any) -> None:
        value = getattr(response_type, "value", response_type)
        if value is not None:
            self.response_types.append(str(value))


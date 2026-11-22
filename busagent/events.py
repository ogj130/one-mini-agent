"""Event stream shared by the loop, the CLI, and (later) OpenTelemetry.

The loop never prints. It emits typed events; each front end decides what to
do with them. That keeps the loop testable and makes a CLI, an SSE endpoint,
and a trace exporter equal citizens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[Event], None]


def null_sink(event: Event) -> None:
    """Default sink: drop everything."""


class EventLog:
    """Collects events for tests and for `trace`."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def text(self) -> str:
        return "".join(event.data.get("text", "") for event in self.events
                       if event.kind == "model.delta")

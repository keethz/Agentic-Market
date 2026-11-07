"""Transport abstraction mirroring a BAND room.

- dm(): a private message between two participants (still visible in the room
  timeline to anyone with room access — so never put secrets in it).
- post(): a room-wide message (status, approval requests).
LocalTransport runs everything in-process for dev/tests; BandTransport
(band_transport.py) implements the same interface against BAND.
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

from pydantic import BaseModel

from .models import now

Handler = Callable[["Message"], None]


class Message(BaseModel):
    sender: str
    recipient: Optional[str]  # None = whole room
    content: str
    at: str


class Transport:
    def subscribe(self, participant: str, handler: Handler) -> None: ...
    def dm(self, sender: str, recipient: str, content: str) -> None: ...
    def post(self, sender: str, content: str) -> None: ...
    def run_until_idle(self, max_messages: int = 500) -> None: ...

    @property
    def timeline(self) -> List[Message]:
        return []


class LocalTransport(Transport):
    def __init__(self):
        self._handlers: Dict[str, Handler] = {}
        self._queue: Deque[Tuple[str, Message]] = deque()
        self._timeline: List[Message] = []

    def subscribe(self, participant, handler):
        self._handlers[participant] = handler

    def dm(self, sender, recipient, content):
        m = Message(sender=sender, recipient=recipient, content=content, at=now())
        self._timeline.append(m)
        self._queue.append((recipient, m))

    def post(self, sender, content):
        self._timeline.append(Message(sender=sender, recipient=None, content=content, at=now()))

    def run_until_idle(self, max_messages=500):
        n = 0
        while self._queue and n < max_messages:
            to, m = self._queue.popleft()
            if to in self._handlers:
                self._handlers[to](m)
            n += 1

    @property
    def timeline(self):
        return list(self._timeline)

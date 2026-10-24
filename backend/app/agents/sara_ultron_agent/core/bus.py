"""Ultron event bus - one in-process publish/subscribe channel.

Ultron already had two ways of telling the browser something happened: an SSE
queue per run (``core/session.py``) and an HTTP poll for fleet health
(``core/fleet.py``). Both are correct for what they do, but neither can express
"a *new* thing appeared that nobody subscribed to beforehand" - a phone pairing
with the backend, a desktop runtime coming online, or the voice state machine
changing - which is exactly what a command center has to react to.

This is that missing channel. It is deliberately small and in-process:

* publishers call ``publish(topic, kind, **payload)`` from any thread;
* subscribers take a :class:`Subscription` over a topic prefix and drain their
  own bounded queue;
* every event carries a monotonic ``seq`` and a wall-clock ``t``, so a client
  can order events that arrived out of sequence and detect gaps.

It is *not* a message broker and does not pretend to be one: there is no
persistence to disk, no delivery across a restart, and no at-least-once
guarantee. ``recent()`` gives a short replay buffer for the common case of a
client that connects a moment after the interesting event, and the WebSocket
routes in ``api.py`` send that buffer first. Anything that must survive a
restart belongs in the session store or the memory store, not here.

Backpressure is handled by dropping the *oldest* event for a subscriber whose
queue is full. Dropping the oldest is the right choice for a live status
channel: the newest state is the one worth rendering, and a blocked consumer
must never stall the publisher (which here can be the desktop runtime's
heartbeat).
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger("sara.ultron.bus")

#: Events retained for late subscribers (per topic). Enough to cover a page
#: load, small enough that the bus cannot grow without bound.
HISTORY_PER_TOPIC = 40

#: Per-subscriber queue depth. A slow consumer loses history, never the newest
#: state, and never blocks the publisher.
SUBSCRIBER_QUEUE = 256


class Subscription:
    """One consumer's view of the bus. Use as a context manager."""

    def __init__(self, bus: "EventBus", prefix: str) -> None:
        self._bus = bus
        self.prefix = prefix
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(
            maxsize=SUBSCRIBER_QUEUE
        )
        self.dropped = 0
        self.closed = False

    def matches(self, topic: str) -> bool:
        return self.prefix == "*" or topic == self.prefix or topic.startswith(
            self.prefix + "."
        )

    def _offer(self, event: Dict[str, Any]) -> None:
        """Enqueue without ever blocking or raising."""
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            pass
        # Full: drop the oldest so the newest state always survives.
        try:
            self._queue.get_nowait()
            self._queue.put_nowait(event)
        except (queue.Empty, queue.Full):  # pragma: no cover - racy, harmless
            self.dropped += 1

    def get(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Next event, or ``None`` on timeout. ``None`` is also returned once
        the subscription is closed and drained."""
        if self.closed and self._queue.empty():
            return None
        try:
            return self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Everything queued right now, oldest first."""
        out: List[Dict[str, Any]] = []
        while len(out) < limit:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self) -> None:
        self._bus.unsubscribe(self)
        self.closed = True

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class EventBus:
    """Thread-safe topic bus with a bounded replay buffer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[Subscription] = []
        self._history: Dict[str, List[Dict[str, Any]]] = {}
        self._counter = itertools.count(1)

    # -- publishing -----------------------------------------------------
    def publish(self, topic: str, kind: str, /, **payload: Any) -> Dict[str, Any]:
        """Publish one event. Returns it, so a caller can log what it sent.

        ``topic`` and ``kind`` name the event and are positional-only. A
        payload may not reuse either name: silently letting ``kind="phone"``
        overwrite the event's own ``kind`` would mislabel every device event,
        so it is rejected instead. The regression suite caught exactly that.
        """
        for reserved in ("topic", "kind", "seq", "t"):
            if reserved in payload:
                raise ValueError(
                    f"Event payload may not contain {reserved!r}; it names the "
                    f"event itself. Rename the field (e.g. 'device_{reserved}')."
                )
        with self._lock:
            event: Dict[str, Any] = {
                "seq": next(self._counter),
                "t": time.time(),
                "topic": topic,
                "kind": kind,
                **payload,
            }
            buf = self._history.setdefault(topic, [])
            buf.append(event)
            if len(buf) > HISTORY_PER_TOPIC:
                del buf[: len(buf) - HISTORY_PER_TOPIC]
            targets = [s for s in self._subscribers if s.matches(topic)]
        # Delivered outside the lock: a subscriber's queue must never be able to
        # stall a publisher.
        for subscription in targets:
            subscription._offer(event)
        return event

    # -- subscribing ---------------------------------------------------
    def subscribe(self, prefix: str = "*") -> Subscription:
        subscription = Subscription(self, prefix)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    def recent(self, prefix: str = "*", limit: int = 50) -> List[Dict[str, Any]]:
        """Replay buffer, newest last, filtered by topic prefix.

        Ordered by ``seq`` rather than by insertion into the per-topic buffers,
        because two topics publishing at once interleave and a client stitching
        them together needs one total order.
        """
        with self._lock:
            events: List[Dict[str, Any]] = []
            for topic, buf in self._history.items():
                if prefix == "*" or topic == prefix or topic.startswith(prefix + "."):
                    events.extend(buf)
        events.sort(key=lambda e: e["seq"])
        return events[-limit:]

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def topics(self) -> List[str]:
        with self._lock:
            return sorted(self._history)

    def clear(self) -> None:
        """Drop history. Subscribers keep receiving live events."""
        with self._lock:
            self._history.clear()


#: Process-wide bus. One instance per backend process, which is the correct
#: scope: the desktop runtime and the phones reach it through the HTTP/WS
#: surface of the same process, never directly.
bus = EventBus()

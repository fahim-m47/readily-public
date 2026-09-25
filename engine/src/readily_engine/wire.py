"""The v1 wire's shared vocabulary, below both HTTP and the state it carries.

`server/wire.py` owns the HTTP surface — codes, messages, SSE framing. This
module owns what the rest of the Engine needs in order to *speak* v1 without
importing the server: the version stamped on every payload, the one error
envelope, and the observable-state idiom the SSE streams read from.
"""

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import TypedDict, cast

WIRE_VERSION = 1


class WireError(TypedDict):
    """The only error shape v1 carries, in response bodies and in snapshots.

    Fixed sentences only: internal detail stays in the Engine log.
    """

    version: int
    code: str
    message: str


def wire_error(code: str, message: str) -> WireError:
    """Build the v1 error envelope's inner object."""
    return {"version": WIRE_VERSION, "code": code, "message": message}


class Observable[SnapshotT: Mapping[str, object]]:
    """A lock-guarded snapshot, a revision counter, and an async stream that
    yields the snapshot whenever it changes.

    The narration worker and the download manager both publish state this
    way; hand-rolling it twice is what let their two copies drift. Snapshots
    are plain dicts typed as a TypedDict, because that is exactly what goes
    on the wire — `json.dumps` of the snapshot is the SSE payload.

    `lock` is deliberately public. A publisher whose own state has to move
    with the snapshot — the worker's generation counter, the download
    manager's active entry — needs its check-and-update to be one step, and
    that means the publisher's lock and this one being the same lock. It is
    reentrant so `update` can be called from inside such a block.
    """

    def __init__(
        self,
        initial: SnapshotT,
        *,
        poll_seconds: float,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        self.lock = threading.RLock()
        self._snapshot: dict[str, object] = dict(initial)
        self._revision = 0
        self._poll_seconds = poll_seconds
        self._heartbeat_seconds = heartbeat_seconds

    def snapshot(self) -> SnapshotT:
        """A copy of the current state, safe to serialize or mutate."""
        with self.lock:
            return cast(SnapshotT, dict(self._snapshot))

    def update(self, **changes: object) -> None:
        """Replace the named fields and advance the revision readers watch."""
        with self.lock:
            self._snapshot.update(changes)
            self._revision += 1

    async def events(
        self, live: Callable[[SnapshotT], SnapshotT] | None = None
    ) -> AsyncIterator[SnapshotT]:
        """Yield the snapshot on every change, plus a periodic repeat that
        keeps the SSE connection (and any proxy in front of it) alive.

        `live` patches in fields too fast-moving to be worth a revision
        each — download byte counts, read from staging on every poll. A
        patched snapshot that differs from the last one yielded counts as a
        change like any other, so the caller does not track its own.
        """
        seen = -1
        last: SnapshotT | None = None
        last_yield = 0.0
        while True:
            with self.lock:
                revision = self._revision
                snapshot = cast(SnapshotT, dict(self._snapshot))
            if live is not None:
                snapshot = live(snapshot)
            now = time.monotonic()
            if (
                revision != seen
                or snapshot != last
                or now - last_yield >= self._heartbeat_seconds
            ):
                seen = revision
                last = snapshot
                last_yield = now
                yield snapshot
            await asyncio.sleep(self._poll_seconds)

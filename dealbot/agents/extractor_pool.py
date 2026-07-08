"""ExtractorPool — async worker pool consuming a shared task queue.

    Explorers navigate marketplaces and drop (snapshot, marketplace, spec)
    tasks onto a shared queue. This pool spins up N async workers, each
    running the stateless Extractor on tasks they pop off the queue.
    Extraction happens in parallel with continued browsing.

Lifecycle:
    - `start()` spawns worker tasks.
    - `submit(snap, marketplace, spec)` enqueues a task; returns immediately.
    - `drain()` sends N poison pills and awaits worker completion; returns
      the accumulated list[Offer].

Ownership: caller owns the Extractor + WatchlistContext. Pool owns queue,
workers, and the results accumulator.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from dealbot.agents.perception import PageSnapshot
from dealbot.agents.workers.extractor import Extractor, Offer
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)


@dataclass
class _ExtractorTask:
    snap: PageSnapshot
    marketplace: str
    spec: WatchlistContext


# Sentinel for graceful worker shutdown. Sent once per worker.
_POISON = None


class ExtractorPool:
    """Async worker pool for Extractor invocations."""

    def __init__(self, extractor: Extractor, num_workers: int = 3) -> None:
        self.extractor = extractor
        self.num_workers = num_workers
        self._queue: asyncio.Queue = asyncio.Queue()
        self._results_lock = asyncio.Lock()
        self._results: list[Offer] = []
        self._workers: list[asyncio.Task] = []
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for _ in range(self.num_workers):
            self._workers.append(asyncio.create_task(self._worker_loop()))

    async def submit(
        self,
        snap: PageSnapshot,
        marketplace: str,
        spec: WatchlistContext,
    ) -> None:
        if not self._started:
            raise RuntimeError("ExtractorPool: submit() before start()")
        await self._queue.put(_ExtractorTask(
            snap=snap, marketplace=marketplace, spec=spec,
        ))

    async def drain(self) -> list[Offer]:
        """Send poison pills, wait for workers, return accumulated offers."""
        if not self._started:
            return []
        for _ in range(self.num_workers):
            await self._queue.put(_POISON)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._started = False
        return list(self._results)

    async def _worker_loop(self) -> None:
        while True:
            task = await self._queue.get()
            if task is _POISON:
                return
            try:
                offers = await self.extractor.extract_from_snapshot(
                    task.snap, task.marketplace, task.spec,
                )
            except Exception as exc:
                logger.warning(
                    "ExtractorPool worker: extract failed for %r: %s",
                    task.snap.url, exc,
                )
                offers = []
            if offers:
                async with self._results_lock:
                    self._results.extend(offers)

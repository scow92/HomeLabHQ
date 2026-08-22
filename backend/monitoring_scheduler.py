"""Small interval scheduler for independent, non-overlapping monitoring jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Callable


@dataclass
class ScheduledJob:
    name: str
    interval: float
    runner: Callable[[], None]
    next_run: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class IntervalScheduler:
    """Dispatch due jobs on worker threads while preventing same-job overlap."""

    def __init__(self, jobs, *, clock=time.monotonic, on_result=None):
        self.jobs = {
            job.name: job for job in jobs
        }
        self.clock = clock
        self.on_result = on_result
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()

    def _finished(self, worker: threading.Thread) -> None:
        with self._workers_lock:
            self._workers.discard(worker)

    def _execute(self, job: ScheduledJob) -> None:
        error = None
        try:
            job.runner()
        except Exception as exc:  # Scheduler isolation is intentional.
            error = exc
        finally:
            job.lock.release()
            try:
                if self.on_result:
                    self.on_result(job.name, error)
            finally:
                self._finished(threading.current_thread())

    def dispatch_due(self, now: float | None = None) -> list[str]:
        """Start every due unlocked job and return the names actually started."""
        now = self.clock() if now is None else now
        started = []
        for job in self.jobs.values():
            if now < job.next_run:
                continue
            job.next_run = now + max(0.01, float(job.interval))
            if not job.lock.acquire(blocking=False):
                continue
            worker = threading.Thread(
                target=self._execute,
                args=(job,),
                name=f"monitor-{job.name}",
                daemon=True,
            )
            with self._workers_lock:
                self._workers.add(worker)
            worker.start()
            started.append(job.name)
        return started

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            now = self.clock()
            self.dispatch_due(now)
            next_run = min((job.next_run for job in self.jobs.values()), default=now + 1)
            self.stop_event.wait(max(0.01, next_run - now))

    def start(self) -> threading.Thread:
        if self.thread and self.thread.is_alive():
            return self.thread
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self.run, name="monitoring-scheduler", daemon=True
        )
        self.thread.start()
        return self.thread

    def run(self) -> None:
        """Run in the current thread; useful when another lifecycle owns it."""
        self.thread = threading.current_thread()
        now = self.clock()
        for job in self.jobs.values():
            job.next_run = now
        self._loop()

    def wait_for_idle(self, timeout: float = 10) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            with self._workers_lock:
                workers = list(self._workers)
            if not workers:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            workers[0].join(min(remaining, 0.05))

    def stop(self, timeout: float = 10) -> bool:
        """Interrupt interval waits and join scheduler plus in-flight workers."""
        self.stop_event.set()
        deadline = time.monotonic() + max(0, timeout)
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0, deadline - time.monotonic()))
        idle = self.wait_for_idle(max(0, deadline - time.monotonic()))
        return idle and not (thread and thread.is_alive())

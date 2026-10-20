"""Shared worker-thread plumbing for login, defense and bump tasks.

Tk work stays on the main thread. A task owns one non-daemon thread, a cancel
event, a one-slot captcha bridge and an immutable state snapshot.
"""
from dataclasses import dataclass, field
from queue import Queue
from threading import Event, Lock, Thread
from time import monotonic


CAPTCHA_TIMEOUT_SECONDS = 300


@dataclass
class CaptchaRequest:
    image: bytes = field(repr=False)
    answer: str | None = field(default=None, repr=False)
    ready: Event = field(default_factory=Event, repr=False)


@dataclass(frozen=True)
class TaskState:
    stage: str = 'idle'
    stats: object = None
    reason: str | None = None
    updated_at: float = 0.0


class BackgroundTask:
    """Base for one explicitly started worker. Subclasses implement ``_work``."""

    reuse_message = '종료한 작업을 재사용할 수 없음'

    def __init__(self):
        self.cancel = Event()
        self.captchas = Queue(maxsize=1)
        self._lock = Lock()
        self._state = TaskState()
        self._thread = None

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _publish(self, stage, stats=None, reason=None):
        with self._lock:
            self._state = TaskState(stage, stats, reason, monotonic())

    def start(self):
        if self._thread is not None:
            raise RuntimeError(self.reuse_message)
        self._publish('starting')
        self._thread = Thread(target=self._work, daemon=False)
        self._thread.start()

    def stop(self):
        self.cancel.set()

    def _captcha(self, image):
        """Hand the image to the UI thread and wait for its answer or cancellation."""
        if self.cancel.is_set():
            return None
        request = CaptchaRequest(image)
        self._publish('captcha')
        self.captchas.put_nowait(request)
        deadline = monotonic() + CAPTCHA_TIMEOUT_SECONDS
        try:
            while not request.ready.wait(.1):
                if self.cancel.is_set() or monotonic() >= deadline:
                    return None
            return None if self.cancel.is_set() else request.answer
        finally:
            request.image = b''
            request.answer = None

    def _work(self):  # pragma: no cover - abstract
        raise NotImplementedError

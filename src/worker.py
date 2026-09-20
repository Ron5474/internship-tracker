import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from db import STAGE_CLOSED, STAGE_DELIVER, Evaluation, utcnow
from discord_client import DeliveryResult, format_link_only, send_message
from users import User

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 60, 300, 900, 3600)
DELIVERY_BUDGET = 10

_LINK_ONLY_NOTES = {
    "fetch_failed": "couldn't read the description",
    "score_failed": "couldn't read the description",
}


def backoff(attempt: int) -> int:
    """Seconds to wait after the given 1-based attempt number."""
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]


def message_for(ev: Evaluation) -> str:
    job = ev.job
    return format_link_only(job.company, job.role, job.location, job.url, note=_LINK_ONLY_NOTES.get(ev.outcome))


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker,
        users: list[User],
        send: Callable[..., DeliveryResult] = send_message,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._users = {u.id: u for u in users}
        self._send = send
        self._now = now
        # name -> resume time, or None for "until restart". Cleared by construction.
        self.paused: dict[str, datetime | None] = {}

    # -- pause map ----------------------------------------------------------

    def is_paused(self, name: str) -> bool:
        if name not in self.paused:
            return False
        resume_at = self.paused[name]
        if resume_at is None:
            return True
        if self._now() >= resume_at:
            del self.paused[name]
            return False
        return True

    def _pause(self, name: str, until: datetime | None, why: str) -> None:
        if name not in self.paused:
            log.error("Pausing %s (%s)%s", name, why, "" if until else " until restart")
        self.paused[name] = until

    # -- loop ---------------------------------------------------------------

    def run_forever(self, idle_sleep: float = 3.0) -> None:
        while True:
            try:
                did_work = self.run_once()
            except Exception:
                log.exception("Worker iteration failed")
                did_work = False
            if not did_work:
                time.sleep(idle_sleep)

    def run_once(self) -> bool:
        with self._sessions() as session:
            ev = self._next_deliverable(session)
            if ev is None:
                return False
            self.deliver(session, ev)
            session.commit()
            return True

    def _next_deliverable(self, session: Session) -> Evaluation | None:
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .filter(Evaluation.stage == STAGE_DELIVER, Evaluation.next_attempt_at <= now)
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    # -- deliver stage ------------------------------------------------------

    def deliver(self, session: Session, ev: Evaluation) -> None:
        user = self._users.get(ev.user_id)
        if user is None:
            ev.delivery_error = f"unknown user {ev.user_id!r}; not in users.yaml"
            ev.stage = STAGE_CLOSED
            log.error("Evaluation %d: %s", ev.id, ev.delivery_error)
            return

        result = self._send(user.discord_webhook, message_for(ev), ev.pdf_path)

        if result.ok:
            ev.delivery_attempts += 1
            ev.delivery_error = None
            ev.stage = STAGE_CLOSED
            log.info("Delivered to %s: %s — %s", ev.user_id, ev.job.company, ev.job.role)
            return

        if result.kind == "gone":
            # Discord says stop using this webhook. Whole destination waits for a fixed config + restart.
            self._pause(f"discord:{ev.user_id}", None, result.error or "webhook gone")
            return

        ev.delivery_attempts += 1
        ev.delivery_error = result.error
        delay = result.retry_after if result.retry_after is not None else backoff(ev.delivery_attempts)
        ev.next_attempt_at = self._now() + timedelta(seconds=delay)
        level = logging.ERROR if ev.delivery_attempts > DELIVERY_BUDGET else logging.WARNING
        log.log(level, "Delivery to %s failed (attempt %d, %s): %s; retry in %ss",
                ev.user_id, ev.delivery_attempts, result.kind, result.error, delay)

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from db import FETCH_FAILED, FETCH_OK, FETCH_PENDING, STAGE_CLOSED, STAGE_DELIVER, Evaluation, Job, utcnow
from discord_client import DeliveryResult, format_link_only, send_message
from fetcher import DESCRIPTION_CAP, FetchResult, fetch_description, has_requirements
from users import User

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 60, 300, 900, 3600)
DELIVERY_BUDGET = 10
FETCH_BUDGET = 5
FETCH_MAX_AGE_HOURS = 24
FETCH_GAP_SECONDS = 2

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
        fetch: Callable[[str], FetchResult] = fetch_description,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._users = {u.id: u for u in users}
        self._send = send
        self._fetch = fetch
        self._now = now
        self._fetch_not_before: datetime | None = None
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
            if until is None:
                log.error("Pausing %s (%s) until restart", name, why)
            else:
                log.error("Pausing %s (%s) until %s", name, why, until.isoformat(timespec="seconds"))
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
            job = self._next_fetchable(session)
            if job is not None:
                self.fetch(session, job)
                session.commit()
                return True
            ev = self._next_deliverable(session)
            if ev is None:
                return False
            self.deliver(session, ev)
            session.commit()
            return True

    def _next_fetchable(self, session: Session) -> Job | None:
        now = self._now()
        if self._fetch_not_before is not None and now < self._fetch_not_before:
            return None
        waiting = session.query(Evaluation.job_id).filter(Evaluation.stage != STAGE_CLOSED)
        return (
            session.query(Job)
            .filter(Job.fetch_status == FETCH_PENDING, Job.next_attempt_at <= now, Job.id.in_(waiting))
            .order_by(Job.next_attempt_at, Job.id)
            .first()
        )

    def _next_deliverable(self, session: Session) -> Evaluation | None:
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .join(Job)
            .filter(
                Evaluation.stage == STAGE_DELIVER,
                Evaluation.next_attempt_at <= now,
                Job.fetch_status != FETCH_PENDING,
            )
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    # -- fetch stage --------------------------------------------------------

    def fetch(self, session: Session, job: Job) -> None:
        now = self._now()
        if job.fetch_first_attempt_at is None:
            job.fetch_first_attempt_at = now
        job.fetch_attempts += 1
        # Lease: commit the attempt before the network call so a hard crash mid-fetch
        # (OOM, SIGKILL) still consumes budget instead of re-picking this job first on restart.
        job.next_attempt_at = now + timedelta(seconds=backoff(job.fetch_attempts))
        session.commit()
        try:
            result = self._fetch(job.url)
        except Exception as e:  # noqa: BLE001 — a fetcher bug is a failed attempt, not a dead worker
            log.exception("Fetcher raised for job %d", job.id)
            result = FetchResult(None, job.fetch_host or "", "none", "transient", f"{type(e).__name__}: {e}")
        self._fetch_not_before = self._now() + timedelta(seconds=FETCH_GAP_SECONDS)

        job.fetch_host = result.host
        job.fetch_strategy = result.strategy
        chars = len(result.text or "")
        reqs = has_requirements(result.text) if result.text else False
        log.info("fetch job=%d host=%s strategy=%s outcome=%s chars=%d requirements=%s%s",
                 job.id, result.host, result.strategy, result.kind, chars, "yes" if reqs else "no",
                 f" error={result.error}" if result.error else "")

        if result.ok:
            job.description = result.text
            job.description_truncated = chars > DESCRIPTION_CAP
            job.has_requirements = reqs
            job.fetch_status = FETCH_OK
            job.fetch_error = None
            return

        job.fetch_error = result.error
        age = now - job.fetch_first_attempt_at
        over_budget = job.fetch_attempts >= FETCH_BUDGET or age >= timedelta(hours=FETCH_MAX_AGE_HOURS)
        if result.kind == "permanent" or over_budget:
            if result.kind != "permanent":
                job.fetch_error = f"budget exhausted after {job.fetch_attempts} attempts: {result.error}"
            self._fail_fetch(session, job)
            return
        job.next_attempt_at = now + timedelta(seconds=backoff(job.fetch_attempts))

    def _fail_fetch(self, session: Session, job: Job) -> None:
        job.fetch_status = FETCH_FAILED
        for ev in job.evaluations:
            if ev.stage != STAGE_CLOSED and ev.outcome is None:
                ev.outcome = "fetch_failed"
        log.warning("Job %d (%s — %s) description unavailable: %s", job.id, job.company, job.role, job.fetch_error)

    # -- deliver stage ------------------------------------------------------

    def deliver(self, session: Session, ev: Evaluation) -> None:
        user = self._users.get(ev.user_id)
        if user is None:
            # Not a delivery outcome: the row waits for a fixed users.yaml + restart.
            self._pause(f"discord:{ev.user_id}", None, f"user {ev.user_id!r} not in users.yaml")
            return

        try:
            result = self._send(user.discord_webhook, message_for(ev), ev.pdf_path)
        except Exception as e:  # noqa: BLE001 — anything the sender raises is a failed attempt, not a crash
            log.exception("Sender raised for evaluation %d", ev.id)
            result = DeliveryResult("transient", None, f"{type(e).__name__}: {e}")

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
        if result.kind == "transient":
            # 5xx / connection / 429 says the destination is unwell: hold the user's other
            # rows too. "invalid" is one bad request and says nothing about the webhook.
            self._pause(f"discord:{ev.user_id}", ev.next_attempt_at, result.error or result.kind)

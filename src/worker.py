import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from cv import MasterCV, cv_to_text
from db import FETCH_FAILED, FETCH_OK, FETCH_PENDING, STAGE_CLOSED, STAGE_DELIVER, STAGE_SCORE, Evaluation, Job, utcnow
from discord_client import DeliveryResult, format_link_only, format_match, send_message
from fetcher import DESCRIPTION_CAP, FetchResult, fetch_description, has_requirements
from llm import LLMResult
from users import User

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 60, 300, 900, 3600)
DELIVERY_BUDGET = 10
FETCH_BUDGET = 5
FETCH_MAX_AGE_HOURS = 24
FETCH_GAP_SECONDS = 2
SCORE_BUDGET = 3
LLM_PAUSE_SECONDS = 900

_LINK_ONLY_NOTES = {
    "fetch_failed": "couldn't read the description",
    "score_failed": "couldn't score",
}


def backoff(attempt: int) -> int:
    """Seconds to wait after the given 1-based attempt number."""
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]


def message_for(ev: Evaluation) -> str:
    job = ev.job
    if ev.score is not None and ev.outcome in ("matched", "below_threshold"):
        return format_match(job.company, job.role, job.location, job.url, ev.score, ev.reasoning or "",
                            ev.missing_confirmed or [], ev.missing_unknown or [], matched=ev.outcome == "matched")
    return format_link_only(job.company, job.role, job.location, job.url, note=_LINK_ONLY_NOTES.get(ev.outcome))


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker,
        users: list[User],
        cvs: dict[str, dict] | None = None,
        llm=None,
        send: Callable[..., DeliveryResult] = send_message,
        fetch: Callable[[str], FetchResult] = fetch_description,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._users = {u.id: u for u in users}
        self._cvs = dict(cvs or {})
        self._llm = llm
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
        # Ready messages first, then the cheap fetch, then the slow LLM call.
        with self._sessions() as session:
            ev = self._next_deliverable(session)
            if ev is not None:
                self.deliver(session, ev)
                session.commit()
                return True
            job = self._next_fetchable(session)
            if job is not None:
                self.fetch(session, job)
                session.commit()
                return True
            ev = self._next_scoreable(session)
            if ev is not None:
                self.score(session, ev)
                session.commit()
                return True
            return False

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

    def _next_scoreable(self, session: Session) -> Evaluation | None:
        if self._llm is None or self.is_paused("llm"):
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .join(Job)
            .filter(Evaluation.stage == STAGE_SCORE, Evaluation.next_attempt_at <= now, Job.fetch_status == FETCH_OK)
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
        if job.fetch_attempts >= FETCH_BUDGET:
            # Crashed attempts can leave the job at the budget while still pending: no more requests.
            job.fetch_error = f"budget exhausted after {job.fetch_attempts} attempts"
            self._fail_fetch(session, job)
            return
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
        after = self._now()   # a fetch can take the full timeout; deadlines count from when it returned
        self._fetch_not_before = after + timedelta(seconds=FETCH_GAP_SECONDS)

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
        age = after - job.fetch_first_attempt_at
        over_budget = job.fetch_attempts >= FETCH_BUDGET or age >= timedelta(hours=FETCH_MAX_AGE_HOURS)
        if result.kind == "permanent" or over_budget:
            if result.kind != "permanent":
                job.fetch_error = f"budget exhausted after {job.fetch_attempts} attempts: {result.error}"
            self._fail_fetch(session, job)
            return
        job.next_attempt_at = after + timedelta(seconds=backoff(job.fetch_attempts))

    def _fail_fetch(self, session: Session, job: Job) -> None:
        job.fetch_status = FETCH_FAILED
        for ev in job.evaluations:
            if ev.stage == STAGE_CLOSED:
                continue
            if ev.outcome is None:
                ev.outcome = "fetch_failed"
            if ev.stage == STAGE_SCORE:
                ev.stage = STAGE_DELIVER   # nothing to score; deliver the link
        log.warning("Job %d (%s — %s) description unavailable: %s", job.id, job.company, job.role, job.fetch_error)

    # -- score stage --------------------------------------------------------

    def score(self, session: Session, ev: Evaluation) -> None:
        user = self._users.get(ev.user_id)
        if user is None or ev.user_id not in self._cvs:
            self._pause(f"discord:{ev.user_id}", None, f"user {ev.user_id!r} not in users.yaml / no CV loaded")
            return

        now = self._now()
        if ev.attempts >= SCORE_BUDGET:
            # Crashed attempts can leave the row at the budget with no result: no further call.
            self._give_up_scoring(ev, now, f"budget exhausted after {ev.attempts} attempts")
            return
        if ev.cv_snapshot is None:
            ev.cv_snapshot = self._cvs[ev.user_id]
        # Lease the attempt before the (slow, crash-prone) call, like fetch.
        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()

        description = (ev.job.description or "")[:DESCRIPTION_CAP]
        cv_text = cv_to_text(MasterCV.model_validate(ev.cv_snapshot))
        try:
            result = self._llm.score(description, cv_text)
        except Exception as e:  # noqa: BLE001 — a client bug is a failed attempt, not a dead worker
            log.exception("LLM client raised for evaluation %d", ev.id)
            result = LLMResult("transient", None, f"{type(e).__name__}: {e}", None, None, None, 0)
        after = self._now()   # deadlines below are measured from when the call came back

        usage = result.usage or {}
        log.info("score ev=%d user=%s model=%s outcome=%s score=%s ms=%d tokens=%s/%s%s",
                 ev.id, ev.user_id, result.model or self._llm_model_name(), self._score_outcome(result, user),
                 result.data.score if result.ok else "-", result.ms,
                 usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                 f" error={result.error}" if result.error else "")

        if result.ok:
            data = result.data
            ev.score, ev.reasoning = data.score, data.reasoning
            ev.missing_confirmed, ev.missing_unknown = data.missing_confirmed, data.missing_unknown
            ev.score_model, ev.score_usage = result.model, result.usage
            ev.last_error = None
            if data.score >= user.threshold:
                ev.outcome, ev.stage = "matched", STAGE_DELIVER
            else:
                ev.outcome = "below_threshold"
                ev.stage = STAGE_DELIVER if user.notify_below_threshold else STAGE_CLOSED
            ev.next_attempt_at = after
            return

        if result.kind == "unavailable":
            # Outage or config problem: not this row's fault. Give the lease back and hold every row.
            ev.attempts -= 1
            resume = after + timedelta(seconds=LLM_PAUSE_SECONDS)
            ev.next_attempt_at = resume
            self._pause("llm", resume, result.error or "unavailable")
            return

        ev.last_error = result.error
        delay = result.retry_after if result.retry_after is not None else backoff(ev.attempts)
        if result.kind == "transient":
            # A 429/5xx/timeout says the shared endpoint is unwell: hold every other row for the
            # backoff — even when this row is about to give up and be delivered link-only.
            self._pause("llm", after + timedelta(seconds=delay), result.error or result.kind)
        if ev.attempts >= SCORE_BUDGET:
            self._give_up_scoring(ev, after, result.error or result.kind)
            return
        ev.next_attempt_at = after + timedelta(seconds=delay)

    def _give_up_scoring(self, ev: Evaluation, when: datetime, why: str) -> None:
        ev.last_error = why
        ev.outcome, ev.stage = "score_failed", STAGE_DELIVER
        ev.next_attempt_at = when
        log.warning("Evaluation %d: scoring gave up after %d attempts: %s", ev.id, ev.attempts, why)

    def _llm_model_name(self) -> str:
        return getattr(self._llm, "_model", "?")

    @staticmethod
    def _score_outcome(result: LLMResult, user: User) -> str:
        if not result.ok:
            return result.kind
        return "matched" if result.data.score >= user.threshold else "below_threshold"

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

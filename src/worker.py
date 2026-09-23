import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from cv import MasterCV, cv_to_id_text, cv_to_text, validate_selection
from db import (
    FETCH_FAILED,
    FETCH_OK,
    FETCH_PENDING,
    STAGE_CLOSED,
    STAGE_DELIVER,
    STAGE_RENDER,
    STAGE_SCORE,
    STAGE_TAILOR,
    Evaluation,
    Job,
    utcnow,
)
from discord_client import DeliveryResult, format_link_only, format_match, send_message
from fetcher import DESCRIPTION_CAP, FetchResult, fetch_description, has_requirements
from llm import LLMResult
from render import output_path, render_pdf
from users import User

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 60, 300, 900, 3600)
DELIVERY_BUDGET = 10
FETCH_BUDGET = 5
FETCH_MAX_AGE_HOURS = 24
FETCH_GAP_SECONDS = 2
SCORE_BUDGET = 3
TAILOR_BUDGET = 3
RENDER_BUDGET = 2
LLM_PAUSE_SECONDS = 900
INVALID_STREAK_LIMIT = 3

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
        return format_match(
            job.company, job.role, job.location, job.url, ev.score, ev.reasoning or "",
            ev.missing_confirmed or [], ev.missing_unknown or [],
            matched=ev.outcome == "matched",
            overflow=bool(ev.page_overflow and ev.pdf_path),
            # Only a match promises a resume; a below-threshold notice never had one.
            resume_missing=ev.outcome == "matched" and ev.pdf_path is None and ev.resume_error is not None,
        )
    return format_link_only(job.company, job.role, job.location, job.url, note=_LINK_ONLY_NOTES.get(ev.outcome))


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker,
        users: list[User],
        cvs: dict[str, dict] | None = None,
        llm=None,
        tailor=None,
        render: Callable[..., "RenderResult"] = render_pdf,
        output_dir: str | None = None,
        max_bullets: int = 4,
        send: Callable[..., DeliveryResult] = send_message,
        fetch: Callable[[str], FetchResult] = fetch_description,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._users = {u.id: u for u in users}
        self._cvs = dict(cvs or {})
        self._llm = llm
        self._tailor = tailor
        self._render = render
        self._output_dir = output_dir
        self._max_bullets = max_bullets
        self._send = send
        self._fetch = fetch
        self._now = now
        self._fetch_not_before: datetime | None = None
        self._invalid_streak = 0   # consecutive "invalid" LLM replies; a run of them is a model/schema problem
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

    @property
    def _tailoring_enabled(self) -> bool:
        return self._tailor is not None and self._output_dir is not None

    @staticmethod
    def _model_name(client) -> str:
        """The alias this client was configured with — the only thing pause keys are built from."""
        return getattr(client, "model", "?")

    def _llm_paused(self, client) -> bool:
        # "llm" is the endpoint (a 429/5xx holds both stages); "llm:<alias>" is one bad model or key.
        return self.is_paused("llm") or self.is_paused(f"llm:{self._model_name(client)}")

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
        # Ready messages first, then cheap network, then local CPU, then the cheap LLM
        # call, then the expensive one.
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
            ev = self._next_renderable(session)
            if ev is not None:
                self.render(session, ev)
                session.commit()
                return True
            ev = self._next_scoreable(session)
            if ev is not None:
                self.score(session, ev)
                session.commit()
                return True
            ev = self._next_tailorable(session)
            if ev is not None:
                self.tailor(session, ev)
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
        if self._llm is None or self._llm_paused(self._llm):
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
        try:
            cv_text = cv_to_text(MasterCV.model_validate(ev.cv_snapshot))
        except ValidationError as e:
            # A hand-edited or pre-schema snapshot: no call would be meaningful, and no lease is owed.
            self._give_up_scoring(ev, now, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:300]}")
            return
        # Lease the attempt before the (slow, crash-prone) call, like fetch.
        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()

        description = (ev.job.description or "")[:DESCRIPTION_CAP]
        try:
            result = self._llm.score(description, cv_text)
        except Exception as e:  # noqa: BLE001 — a client bug is a failed attempt, not a dead worker
            log.exception("LLM client raised for evaluation %d", ev.id)
            result = LLMResult("transient", None, f"{type(e).__name__}: {e}", None, None, None, 0)
        after = self._now()   # deadlines below are measured from when the call came back

        usage = result.usage or {}
        outcome = self._score_outcome(result, user)
        log.info("score ev=%d user=%s model=%s outcome=%s score=%s ms=%d tokens=%s/%s%s",
                 ev.id, ev.user_id, result.model or self._model_name(self._llm), outcome,
                 result.data.score if outcome in ("matched", "below_threshold") else "-", result.ms,
                 usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                 f" error={result.error}" if result.error else "")

        if result.kind == "invalid":
            self._invalid_streak += 1
            if self._invalid_streak >= INVALID_STREAK_LIMIT:
                self._invalid_streak = 0
                self._pause(f"llm:{self._model_name(self._llm)}", after + timedelta(seconds=LLM_PAUSE_SECONDS),
                            f"{INVALID_STREAK_LIMIT} consecutive invalid replies — model/schema mismatch?")
        else:
            self._invalid_streak = 0

        if result.ok and result.data.posting_usable is False:
            # The model read an error page / login wall, not a posting: nothing to score, deliver the link.
            self._give_up_scoring(ev, after, "posting not usable per model")
            return

        if result.ok:
            data = result.data
            ev.score, ev.reasoning = data.score, data.reasoning
            ev.missing_confirmed, ev.missing_unknown = data.missing_confirmed, data.missing_unknown
            ev.score_model, ev.score_usage = result.model, result.usage
            ev.last_error = None
            if data.score >= user.threshold:
                ev.outcome = "matched"
                ev.stage = STAGE_TAILOR if self._tailoring_enabled else STAGE_DELIVER
                ev.attempts = 0            # the budget belongs to the stage, not the row
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
            self._pause(f"llm:{self._model_name(self._llm)}", resume, result.error or "unavailable")
            return

        ev.last_error = result.error
        delay = result.retry_after if result.retry_after is not None else backoff(ev.attempts)
        delay = max(delay, 1)   # Retry-After: 0 must not schedule an immediate retry
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

    @staticmethod
    def _score_outcome(result: LLMResult, user: User) -> str:
        if not result.ok:
            return result.kind
        if result.data.posting_usable is False:
            return "unusable"
        return "matched" if result.data.score >= user.threshold else "below_threshold"

    # -- render stage ---------------------------------------------------------

    def _next_renderable(self, session: Session) -> Evaluation | None:
        if self._output_dir is None:
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .filter(Evaluation.stage == STAGE_RENDER, Evaluation.next_attempt_at <= now)
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    def render(self, session: Session, ev: Evaluation) -> None:
        now = self._now()
        if ev.attempts >= RENDER_BUDGET:
            self._give_up_resume(ev, now, f"render budget exhausted after {ev.attempts} attempts")
            return
        try:
            cv = MasterCV.model_validate(ev.cv_snapshot)
        except ValidationError as e:
            self._give_up_resume(ev, now, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:200]}")
            return
        if not ev.tailored:
            self._give_up_resume(ev, now, "no tailored selection to render")
            return

        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()        # WeasyPrint can hang or be OOM-killed; the attempt must be durable

        out = output_path(self._output_dir, ev.user_id, ev.job_id, ev.job.company)
        try:
            result = self._render(cv, ev.tailored, out)
        except Exception as e:  # noqa: BLE001 — a bad glyph or a full disk is a failed attempt
            log.exception("Render failed for evaluation %d", ev.id)
            after = self._now()
            if ev.attempts >= RENDER_BUDGET:
                self._give_up_resume(ev, after, f"{type(e).__name__}: {e}")
            else:
                ev.last_error = f"{type(e).__name__}: {e}"
                ev.next_attempt_at = after + timedelta(seconds=backoff(ev.attempts))
            return

        after = self._now()
        ev.pdf_path, ev.page_overflow = result.path, result.overflow
        ev.stage, ev.attempts, ev.next_attempt_at = STAGE_DELIVER, 0, after
        log.info("render ev=%d user=%s pages=%d overflow=%s path=%s",
                 ev.id, ev.user_id, result.pages, result.overflow, result.path)

    # -- tailor stage ---------------------------------------------------------

    def _next_tailorable(self, session: Session) -> Evaluation | None:
        if self._tailor is None or self._llm_paused(self._tailor):
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .filter(Evaluation.stage == STAGE_TAILOR, Evaluation.next_attempt_at <= now)
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    def tailor(self, session: Session, ev: Evaluation) -> None:
        now = self._now()
        if ev.attempts >= TAILOR_BUDGET:
            # Crashed attempts can leave the row at the budget with no selection: no further call.
            self._give_up_resume(ev, now, f"tailor budget exhausted after {ev.attempts} attempts")
            return
        try:
            cv = MasterCV.model_validate(ev.cv_snapshot)
        except ValidationError as e:
            self._give_up_resume(ev, now, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:200]}")
            return

        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()        # lease before the slow call, exactly as score and fetch do

        try:
            result = self._tailor.tailor((ev.job.description or "")[:DESCRIPTION_CAP],
                                         cv_to_id_text(cv), self._max_bullets)
        except Exception as e:  # noqa: BLE001 — a client bug is a failed attempt, not a dead worker
            log.exception("Tailor client raised for evaluation %d", ev.id)
            result = LLMResult("transient", None, f"{type(e).__name__}: {e}", None, None, None, 0)
        after = self._now()

        log.info("tailor ev=%d user=%s model=%s outcome=%s ms=%d%s",
                 ev.id, ev.user_id, result.model or getattr(self._tailor, "model", "?"),
                 result.kind, result.ms, f" error={result.error}" if result.error else "")

        if result.ok:
            selection, warnings = validate_selection(cv, result.data.model_dump(), self._max_bullets)
            for w in warnings:
                log.warning("tailor ev=%d: %s", ev.id, w)
            ev.tailored = selection
            ev.tailor_model = result.model
            ev.stage, ev.attempts, ev.next_attempt_at = STAGE_RENDER, 0, after
            return

        if result.kind == "unavailable":
            ev.attempts -= 1                      # not this row's fault: hand the lease back
            resume = after + timedelta(seconds=LLM_PAUSE_SECONDS)
            ev.next_attempt_at = resume
            # The CONFIGURED alias, never result.model. On a re-ask whose first call succeeded and
            # whose second returned 401, LLMResult carries the backend's own model name — pausing
            # that would write a key `_llm_paused` never reads, and the cooldown would do nothing.
            self._pause(f"llm:{self._model_name(self._tailor)}", resume, result.error or "unavailable")
            return

        ev.last_error = result.error
        delay = max(result.retry_after if result.retry_after is not None else backoff(ev.attempts), 1)
        if result.kind == "transient":
            self._pause("llm", after + timedelta(seconds=delay), result.error or result.kind)
        if ev.attempts >= TAILOR_BUDGET:
            self._give_up_resume(ev, after, result.error or result.kind)
            return
        ev.next_attempt_at = after + timedelta(seconds=delay)

    def _give_up_resume(self, ev: Evaluation, when: datetime, why: str) -> None:
        """No PDF for this row. The score message still goes out; `outcome` stays as scored."""
        ev.resume_error = why
        ev.last_error = why
        # A re-scored row can still be carrying the PREVIOUS run's PDF. Attaching it here would
        # ship an old resume under a new score, and pdf_path being set would also suppress the
        # "couldn't generate resume" notice. Clear it: this row has no resume.
        ev.pdf_path, ev.page_overflow = None, False
        ev.stage, ev.attempts, ev.next_attempt_at = STAGE_DELIVER, 0, when
        log.warning("Evaluation %d: no tailored resume (%s)", ev.id, why)

    # -- deliver stage ------------------------------------------------------

    def deliver(self, session: Session, ev: Evaluation) -> None:
        user = self._users.get(ev.user_id)
        if user is None:
            # Not a delivery outcome: the row waits for a fixed users.yaml + restart.
            self._pause(f"discord:{ev.user_id}", None, f"user {ev.user_id!r} not in users.yaml")
            return

        if ev.pdf_path and not Path(ev.pdf_path).exists():
            # The volume was wiped or the file was cleaned up: send what we still have.
            log.warning("Evaluation %d: resume %s is gone; delivering without it", ev.id, ev.pdf_path)
            ev.resume_error = ev.resume_error or "resume file missing at delivery"
            ev.pdf_path = None

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

        if result.kind == "attachment":
            # No exists() check can cover permissions or a race with a cleanup. The message itself
            # is fine: drop the attachment and send it on the next pass rather than looping on a
            # file that will never open. No delivery attempt is counted — nothing was sent.
            log.warning("Evaluation %d: %s; delivering without the resume", ev.id, result.error)
            ev.resume_error = ev.resume_error or result.error
            ev.pdf_path, ev.page_overflow = None, False
            ev.next_attempt_at = self._now()
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

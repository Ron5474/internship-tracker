import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
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
    FeedPost,
    Job,
    drain_resume_stages,
    utcnow,
)
from discord_client import DeliveryResult, format_link_only, format_match, send_message
from fetcher import DESCRIPTION_CAP, FetchResult, fetch_description, has_requirements
from llm import LLMResult
from render import UNDERFILL_BELOW, RenderResult, attachment_name, fit_to_page, output_path
from users import User, webhook_key

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 60, 300, 900, 3600)
DELIVERY_BUDGET = 10
FEED_BUDGET = 10
FETCH_BUDGET = 5
FETCH_MAX_AGE_HOURS = 24
FETCH_GAP_SECONDS = 2
SCORE_BUDGET = 3
TAILOR_BUDGET = 3
RENDER_BUDGET = 2
LLM_PAUSE_SECONDS = 900
INVALID_STREAK_LIMIT = 3
# An "unavailable" reply (connection refused, 401/403/404) pauses that model: briefly for the
# first one — a LiteLLM restart is over in seconds — and for longer on each consecutive repeat,
# which is what a wrong key or alias looks like. Any other answer from the model resets it.
UNAVAILABLE_PAUSE_SECONDS = (60, 300, 900)

_LINK_ONLY_NOTES = {
    "fetch_failed": "couldn't read the description",
    "score_failed": "couldn't score",
}


def backoff(attempt: int) -> int:
    """Seconds to wait after the given 1-based attempt number."""
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]


def _underfill(ev: Evaluation) -> int | None:
    """The fill percentage, when the resume came out short enough to be worth saying."""
    if ev.outcome != "matched" or not ev.pdf_path or ev.page_fill is None:
        return None
    return ev.page_fill if ev.page_fill < UNDERFILL_BELOW * 100 else None


def message_for(ev: Evaluation) -> str:
    job = ev.job
    if ev.score is not None and ev.outcome in ("matched", "below_threshold"):
        return format_match(
            job.company, job.role, job.location, job.url, ev.score, ev.reasoning or "",
            ev.missing_confirmed or [], ev.missing_unknown or [],
            matched=ev.outcome == "matched",
            # Only a match ever has a resume; gate on outcome too so a stray manual UPDATE that
            # leaves page_overflow/pdf_path set on a non-matched row can't surface the note.
            overflow=bool(ev.page_overflow and ev.pdf_path and ev.outcome == "matched"),
            # Only a match promises a resume; a below-threshold notice never had one.
            resume_missing=ev.outcome == "matched" and ev.pdf_path is None and ev.resume_error is not None,
            underfill=_underfill(ev),
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
        render: Callable[..., "RenderResult"] = fit_to_page,
        output_dir: str | None = None,
        max_bullets: int = 4,
        send: Callable[..., DeliveryResult] = send_message,
        fetch: Callable[[str], FetchResult] = fetch_description,
        now: Callable[[], datetime] = utcnow,
        concurrency: int = 1,
    ) -> None:
        self._sessions = session_factory
        self._users = {u.id: u for u in users}
        # key -> URL, so a feed_posts row can be resolved without storing the secret.
        # Every public destination across every user and feed, so a row's key resolves to a
        # URL without the database ever holding one.
        self._feed_webhooks = {webhook_key(url): url
                               for u in users for url in u.feed_webhooks().values()}
        self._cvs = dict(cvs or {})
        self._llm = llm
        self._tailor = tailor
        self._render = render
        self._output_dir = output_dir
        self._max_bullets = max_bullets
        self._send = send
        self._fetch = fetch
        self._now = now
        self._orphan_feed_warned = False   # log once per process, not on every idle tick
        self._fetch_not_before: datetime | None = None
        self._invalid_streak = 0   # consecutive "invalid" LLM replies; a run of them is a model/schema problem
        self._tailor_invalid_streak = 0   # own counter — never shared with score()'s _invalid_streak
        self._unavailable_streak: dict[str, int] = {}   # alias -> consecutive unavailable replies
        # LLM calls in flight when concurrency > 1. Only the HTTP call leaves this thread: the
        # lease is committed before submission and the result written when the loop harvests
        # the finished call, so every database write stays here, in its own session. With
        # concurrency 1 there is no pool and the call completes inside run_once, as before.
        self._concurrency = max(1, concurrency)
        self._pool = ThreadPoolExecutor(self._concurrency, thread_name_prefix="llm") if concurrency > 1 else None
        self._inflight: dict[int, tuple[Future, str]] = {}   # ev.id -> (future, stage)
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

    def _pause_unavailable(self, client, after: datetime, why: str) -> datetime:
        """Pause this model's alias for the streak's step and return when it resumes."""
        alias = self._model_name(client)
        if self.is_paused(f"llm:{alias}") and self.paused[f"llm:{alias}"] is not None:
            # Already paused: this reply came from a call that was in flight when the first
            # one failed. Same incident, same pause — it must not climb the ladder.
            return self.paused[f"llm:{alias}"]
        streak = self._unavailable_streak.get(alias, 0) + 1
        self._unavailable_streak[alias] = streak
        step = UNAVAILABLE_PAUSE_SECONDS[min(streak, len(UNAVAILABLE_PAUSE_SECONDS)) - 1]
        resume = after + timedelta(seconds=step)
        self._pause(f"llm:{alias}", resume, why)
        return resume

    def _not_inflight(self):
        """A row whose call is in flight must not be picked again when its lease runs out — a
        36-second call outlives the 30-second first lease."""
        return Evaluation.id.notin_(list(self._inflight)) if self._inflight else True

    def _llm_paused(self, client) -> bool:
        # "llm" is the endpoint (a 429/5xx holds both stages); "llm:<alias>" is one bad model or key.
        return self.is_paused("llm") or self.is_paused(f"llm:{self._model_name(client)}")

    # -- loop ---------------------------------------------------------------

    def startup(self) -> None:
        """One-time work before the loop.

        Rows queued for tailoring or rendering by a process that had a tailor client would sit
        forever in one that does not. Move them to delivery so the score still reaches the user.

        `main.build()` always constructs a `tailor` client and `output_dir` together, so in
        production this drain guards a directly-constructed `Worker` (tests, one-off scripts) —
        not the Plan 4 -> Plan 3 rollback path, which the runbook SQL in docs/ops.md covers
        instead (an older Plan 3 image has no tailor/render selectors at all to drain into).
        """
        with self._sessions() as session:
            self._warn_orphan_feed_posts(session)

        if self._tailoring_enabled:
            return
        with self._sessions() as session:
            drained = drain_resume_stages(session)
            session.commit()
        if drained:
            log.warning("Tailoring not configured: %d queued row(s) will be delivered "
                        "with the score only", drained)

    def _warn_orphan_feed_posts(self, session: Session) -> None:
        """Unsent feed posts whose destination is no longer in users.yaml.

        `_next_feed_post` filters `webhook_key.in_(self._feed_webhooks)` in SQL, so a row whose
        destination fell out of the live config (discord_webhook repointed, or the user removed)
        is invisible to the worker forever, with no log. It is also invisible to the runbook's
        `attempts > 0` diagnostic, since these rows never get an attempt: attempts=0, error=NULL.
        deliver() handles the exact analogue loudly for a missing user; this matches it, once per
        process rather than on every idle tick.
        """
        if self._orphan_feed_warned:
            return
        self._orphan_feed_warned = True
        keys = [key for (key,) in session.query(FeedPost.webhook_key)
                .filter(FeedPost.sent_at.is_(None)).all()
                if key not in self._feed_webhooks]
        if not keys:
            return
        orphan_keys = sorted(set(keys))
        log.warning("%d unsent feed post(s) target %d destination(s) no longer in users.yaml: %s",
                    len(keys), len(orphan_keys), ", ".join(orphan_keys))

    def run_forever(self, idle_sleep: float = 3.0) -> None:
        self.startup()
        while True:
            try:
                did_work = self.run_once()
            except Exception:
                log.exception("Worker iteration failed")
                did_work = False
            if not did_work:
                self._idle(idle_sleep)

    def _idle(self, seconds: float) -> None:
        """Nothing to do right now. With calls in flight, wake as soon as one finishes."""
        if self._inflight:
            wait([f for f, _ in self._inflight.values()], timeout=seconds, return_when="FIRST_COMPLETED")
        else:
            time.sleep(seconds)

    def wait_inflight(self, timeout: float | None = None) -> None:
        """Block until every in-flight call has returned (the results still await a run_once)."""
        if self._inflight:
            wait([f for f, _ in self._inflight.values()], timeout=timeout)

    def run_once(self) -> bool:
        # Ready messages first (private results, then the public feed slot — one HTTP call,
        # so a posting reaches the shared channel within seconds), then cheap network, then
        # local CPU, then the LLM stages. Tailoring goes before scoring: a match is one call
        # from its resume, and finishing it beats starting another posting — with scoring
        # first, the first match of a batch waited for the whole batch to be scored.
        if self._harvest():
            return True
        with self._sessions() as session:
            ev = self._next_deliverable(session)
            if ev is not None:
                self.deliver(session, ev)
                session.commit()
                return True
            post = self._next_feed_post(session)
            if post is not None:
                self.post_feed(session, post)
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
            if self._pool is not None:
                return self._submit_next(session)
            ev = self._next_tailorable(session)
            if ev is not None:
                self.tailor(session, ev)
                session.commit()
                return True
            ev = self._next_scoreable(session)
            if ev is not None:
                self.score(session, ev)
                session.commit()
                return True
            return False

    # -- LLM calls in flight ------------------------------------------------------

    def _submit_next(self, session: Session) -> bool:
        """Lease the next LLM-stage row and hand its call to the pool. One row per tick."""
        if len(self._inflight) >= self._concurrency:
            return False
        for pick, begin, stage in ((self._next_tailorable, self._begin_tailor, STAGE_TAILOR),
                                   (self._next_scoreable, self._begin_score, STAGE_SCORE)):
            ev = pick(session)
            if ev is None:
                continue
            call = begin(session, ev)
            session.commit()
            if call is not None:     # None: the row was given up or parked without a call
                self._inflight[ev.id] = (self._pool.submit(call), stage)
            return True
        return False

    def _harvest(self) -> bool:
        """Write the results of finished calls, each in a session of its own."""
        done = [(ev_id, f, stage) for ev_id, (f, stage) in self._inflight.items() if f.done()]
        for ev_id, future, stage in done:
            del self._inflight[ev_id]
            finish = self._finish_tailor if stage == STAGE_TAILOR else self._finish_score
            with self._sessions() as session:
                ev = session.get(Evaluation, ev_id)
                if ev is not None:
                    finish(session, ev, future.result(), self._now())
                    session.commit()
        return bool(done)

    @staticmethod
    def _guarded(fn: Callable[[], LLMResult], ev_id: int, what: str) -> Callable[[], LLMResult]:
        """A client bug is a failed attempt, not a dead worker — in a pool thread or inline."""
        def call() -> LLMResult:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                log.exception("%s raised for evaluation %d", what, ev_id)
                return LLMResult("transient", None, f"{type(e).__name__}: {e}", None, None, None, 0)
        return call

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

    def _next_feed_post(self, session: Session) -> FeedPost | None:
        if not self._feed_webhooks:
            return None
        now = self._now()
        candidates = (
            session.query(FeedPost)
            .filter(FeedPost.sent_at.is_(None),
                    FeedPost.next_attempt_at <= now,
                    FeedPost.webhook_key.in_(list(self._feed_webhooks)))
            .order_by(FeedPost.next_attempt_at, FeedPost.id)
            .all()
        )
        for post in candidates:
            if not self.is_paused(f"feed:{post.webhook_key}"):
                return post
        return None

    def post_feed(self, session: Session, post: FeedPost) -> None:
        """Announce one job to one channel: the link, nothing about the candidate."""
        webhook = self._feed_webhooks[post.webhook_key]
        job = post.job
        content = format_link_only(job.company, job.role, job.location, job.url)
        try:
            result = self._send(webhook, content)
        except Exception as e:  # noqa: BLE001 — a sender bug is a failed attempt, not a dead worker
            log.exception("Sender raised for feed post %d", post.id)
            result = DeliveryResult("transient", None, f"{type(e).__name__}: {e}")

        if result.ok:
            post.sent_at = self._now()
            post.error = None
            log.info("feed post=%d job=%d: %s — %s", post.id, job.id, job.company, job.role)
            return

        if result.kind == "gone":
            self._pause(f"feed:{post.webhook_key}", None, result.error or "feed webhook gone")
            return

        post.attempts += 1
        post.error = result.error
        delay = result.retry_after if result.retry_after is not None else backoff(post.attempts)
        post.next_attempt_at = self._now() + timedelta(seconds=delay)
        level = logging.ERROR if post.attempts > FEED_BUDGET else logging.WARNING
        log.log(level, "Feed post %d failed (attempt %d, %s): %s; retry in %ss",
                post.id, post.attempts, result.kind, result.error, delay)
        if result.kind == "transient":
            self._pause(f"feed:{post.webhook_key}", post.next_attempt_at, result.error or result.kind)

    def _next_scoreable(self, session: Session) -> Evaluation | None:
        if self._llm is None or self._llm_paused(self._llm):
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .join(Job)
            .filter(Evaluation.stage == STAGE_SCORE, Evaluation.next_attempt_at <= now, Job.fetch_status == FETCH_OK,
                    self._not_inflight())
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
        """Serial path: lease, call, write — all inside this tick."""
        call = self._begin_score(session, ev)
        if call is not None:
            self._finish_score(session, ev, call(), self._now())

    def _begin_score(self, session: Session, ev: Evaluation) -> Callable[[], LLMResult] | None:
        """Validate, lease and commit; return the call to make, or None if the row is parked."""
        user = self._users.get(ev.user_id)
        if user is None or ev.user_id not in self._cvs:
            self._pause(f"discord:{ev.user_id}", None, f"user {ev.user_id!r} not in users.yaml / no CV loaded")
            return None

        now = self._now()
        if ev.attempts >= SCORE_BUDGET:
            # Crashed attempts can leave the row at the budget with no result: no further call.
            self._give_up_scoring(ev, now, f"budget exhausted after {ev.attempts} attempts")
            return None
        if ev.cv_snapshot is None:
            ev.cv_snapshot = self._cvs[ev.user_id]
        try:
            cv_text = cv_to_text(MasterCV.model_validate(ev.cv_snapshot))
        except ValidationError as e:
            # A hand-edited or pre-schema snapshot: no call would be meaningful, and no lease is owed.
            self._give_up_scoring(ev, now, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:300]}")
            return None
        # Lease the attempt before the (slow, crash-prone) call, like fetch.
        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()

        description = (ev.job.description or "")[:DESCRIPTION_CAP]
        return self._guarded(lambda: self._llm.score(description, cv_text), ev.id, "LLM client")

    def _finish_score(self, session: Session, ev: Evaluation, result: LLMResult, after: datetime) -> None:
        """Write the outcome of a scoring call. `after` is when the call came back: every
        deadline below counts from it, not from when the call was made."""
        user = self._users[ev.user_id]
        usage = result.usage or {}
        outcome = self._score_outcome(result, user)
        log.info("score ev=%d user=%s model=%s outcome=%s score=%s ms=%d tokens=%s/%s%s",
                 ev.id, ev.user_id, result.model or self._model_name(self._llm), outcome,
                 result.data.score if outcome in ("matched", "below_threshold") else "-", result.ms,
                 usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                 f" error={result.error}" if result.error else "")

        if result.kind != "unavailable":
            self._unavailable_streak.pop(self._model_name(self._llm), None)
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
                # A re-scored row (docs/ops.md's re-fetch path) can still be carrying a previous
                # run's tailored selection and PDF. A below-threshold row has no resume: clear
                # them the way _give_up_resume does, or the old resume ships under the new score.
                ev.tailored, ev.pdf_path, ev.page_overflow, ev.resume_error = None, None, False, None
                ev.stage = STAGE_DELIVER if user.notify_below_threshold else STAGE_CLOSED
            ev.next_attempt_at = after
            return

        if result.kind == "unavailable":
            # Outage or config problem: not this row's fault. Give the lease back and hold every row.
            ev.attempts -= 1
            ev.next_attempt_at = self._pause_unavailable(self._llm, after, result.error or "unavailable")
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
        if not self._tailoring_enabled:
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
        if result.selection is not None:
            # The fit trimmed or added content; store what was actually rendered, so a
            # re-render reproduces this page rather than the pre-fit selection.
            ev.tailored = result.selection
        ev.pdf_path, ev.page_overflow = result.path, result.overflow
        ev.page_fill = round(result.fill * 100)
        ev.stage, ev.attempts, ev.next_attempt_at = STAGE_DELIVER, 0, after
        log.info("render ev=%d user=%s pages=%d fill=%d%% overflow=%s path=%s",
                 ev.id, ev.user_id, result.pages, ev.page_fill, result.overflow, result.path)

    # -- tailor stage ---------------------------------------------------------

    def _next_tailorable(self, session: Session) -> Evaluation | None:
        if not self._tailoring_enabled or self._llm_paused(self._tailor):
            return None
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .filter(Evaluation.stage == STAGE_TAILOR, Evaluation.next_attempt_at <= now, self._not_inflight())
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    def tailor(self, session: Session, ev: Evaluation) -> None:
        """Serial path: lease, call, write — all inside this tick."""
        call = self._begin_tailor(session, ev)
        if call is not None:
            self._finish_tailor(session, ev, call(), self._now())

    def _begin_tailor(self, session: Session, ev: Evaluation) -> Callable[[], LLMResult] | None:
        now = self._now()
        if ev.attempts >= TAILOR_BUDGET:
            # Crashed attempts can leave the row at the budget with no selection: no further call.
            self._give_up_resume(ev, now, f"tailor budget exhausted after {ev.attempts} attempts")
            return None
        try:
            cv = MasterCV.model_validate(ev.cv_snapshot)
        except ValidationError as e:
            self._give_up_resume(ev, now, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:200]}")
            return None

        ev.attempts += 1
        ev.next_attempt_at = now + timedelta(seconds=backoff(ev.attempts))
        session.commit()        # lease before the slow call, exactly as score and fetch do

        description, id_text = (ev.job.description or "")[:DESCRIPTION_CAP], cv_to_id_text(cv)
        return self._guarded(lambda: self._tailor.tailor(description, id_text, self._max_bullets),
                             ev.id, "Tailor client")

    def _finish_tailor(self, session: Session, ev: Evaluation, result: LLMResult, after: datetime) -> None:
        log.info("tailor ev=%d user=%s model=%s outcome=%s ms=%d%s",
                 ev.id, ev.user_id, result.model or getattr(self._tailor, "model", "?"),
                 result.kind, result.ms, f" error={result.error}" if result.error else "")

        if result.kind != "unavailable":
            self._unavailable_streak.pop(self._model_name(self._tailor), None)
        if result.kind == "invalid":
            # A model that does not honour JSON mode costs two billed calls per _ask; without a
            # breaker here TAILOR_BUDGET burns the pricier model with no cooldown between rows.
            # A counter of its own — score()'s _invalid_streak must not be touched by tailor().
            self._tailor_invalid_streak += 1
            if self._tailor_invalid_streak >= INVALID_STREAK_LIMIT:
                self._tailor_invalid_streak = 0
                self._pause(f"llm:{self._model_name(self._tailor)}", after + timedelta(seconds=LLM_PAUSE_SECONDS),
                            f"{INVALID_STREAK_LIMIT} consecutive invalid replies — model/schema mismatch?")
        else:
            self._tailor_invalid_streak = 0

        if result.ok:
            try:
                cv = MasterCV.model_validate(ev.cv_snapshot)   # the snapshot _begin_tailor validated
            except ValidationError as e:
                self._give_up_resume(ev, after, f"cv_snapshot invalid: {type(e).__name__}: {str(e)[:200]}")
                return
            selection, warnings = validate_selection(cv, result.data.model_dump(), self._max_bullets)
            for w in warnings:
                log.warning("tailor ev=%d: %s", ev.id, w)
            ev.tailored = selection
            ev.tailor_model = result.model
            ev.stage, ev.attempts, ev.next_attempt_at = STAGE_RENDER, 0, after
            return

        if result.kind == "unavailable":
            # Unlike score() — where nothing has been produced yet and the lease goes back — a
            # wrong/renamed LLM_TAILOR_MODEL alias will not fix itself on retry. Keeping the lease
            # here is what lets the row reach TAILOR_BUDGET and degrade via _give_up_resume instead
            # of looping forever at attempts==0.
            # The CONFIGURED alias, never result.model. On a re-ask whose first call succeeded and
            # whose second returned 401, LLMResult carries the backend's own model name — pausing
            # that would write a key `_llm_paused` never reads, and the cooldown would do nothing.
            resume = self._pause_unavailable(self._tailor, after, result.error or "unavailable")
            if ev.attempts >= TAILOR_BUDGET:
                self._give_up_resume(ev, after, result.error or "unavailable")
                return
            ev.next_attempt_at = resume
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

    @staticmethod
    def _attachment_name(ev: Evaluation) -> str | None:
        """What the PDF is called in Discord. None leaves the file's own name, which is a
        worse download name but always correct — worth falling back to rather than guessing."""
        snapshot = ev.cv_snapshot if isinstance(ev.cv_snapshot, dict) else {}
        name = snapshot.get("name")
        if not name:
            return None
        return attachment_name(name, ev.job.company, ev.job.role)

    def deliver(self, session: Session, ev: Evaluation) -> None:
        user = self._users.get(ev.user_id)
        if user is None:
            # Not a delivery outcome: the row waits for a fixed users.yaml + restart.
            self._pause(f"discord:{ev.user_id}", None, f"user {ev.user_id!r} not in users.yaml")
            return

        # Only a match ever gets a resume attached — belt-and-braces against any manual UPDATE
        # (see docs/ops.md's re-fetch note) that leaves pdf_path set on a non-matched row.
        pdf_path = ev.pdf_path if ev.outcome == "matched" else None
        if pdf_path and not Path(pdf_path).exists():
            # The volume was wiped or the file was cleaned up: send what we still have.
            log.warning("Evaluation %d: resume %s is gone; delivering without it", ev.id, pdf_path)
            ev.resume_error = ev.resume_error or "resume file missing at delivery"
            ev.pdf_path = None
            pdf_path = None

        try:
            result = self._send(user.results_webhook, message_for(ev), pdf_path,
                                self._attachment_name(ev) if pdf_path else None)
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

from datetime import datetime, timedelta

import pytest

from db import FETCH_FAILED, FETCH_OK, FETCH_PENDING, STAGE_CLOSED, STAGE_DELIVER, Evaluation, Feed, Job
from discord_client import DeliveryResult
from fetcher import FetchResult
from users import User
from worker import (
    BACKOFF_SECONDS,
    DELIVERY_BUDGET,
    FETCH_BUDGET,
    FETCH_GAP_SECONDS,
    FETCH_MAX_AGE_HOURS,
    Worker,
    backoff,
    message_for,
)

T0 = datetime(2026, 9, 19, 12, 0, 0)

RON = User(id="ron", cv="/x", discord_webhook="https://d/ron", feeds=["internships"], sections=["software"])
COUSIN = User(id="cousin", cv="/x", discord_webhook="https://d/cousin", feeds=["internships"], sections=["software"])


class FakeSender:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, webhook, content, pdf_path=None):
        self.calls.append((webhook, content, pdf_path))
        return self.results.pop(0) if self.results else DeliveryResult("ok", None, None)


OK = DeliveryResult("ok", None, None)


def _seed(session, user_id="ron", **ev_kwargs):
    feed = session.query(Feed).filter_by(name="internships").first() or Feed(name="internships", repo="a/b", branch="dev")
    job = Job(feed=feed, url_key=f"https://x.com/{user_id}", url=f"https://x.com/{user_id}?utm_source=S",
              company="Stripe", role="SWE Intern", location="SF", section="software engineering internship roles",
              next_attempt_at=T0)
    # Pin next_attempt_at to the fake clock; the model default is the real utcnow().
    ev = Evaluation(job=job, user_id=user_id, **{"next_attempt_at": T0, "stage": STAGE_DELIVER, **ev_kwargs})
    session.add_all([feed, job, ev])
    session.commit()
    return ev


def _resolve(session, ev):
    ev.job.fetch_status = FETCH_OK
    session.commit()
    return ev


@pytest.fixture
def clock():
    state = {"now": T0}
    def now():
        return state["now"]
    now.advance = lambda seconds: state.__setitem__("now", state["now"] + timedelta(seconds=seconds))
    return now


def _worker(session_factory, sender, clock, users=(RON, COUSIN)):
    return Worker(session_factory, list(users), send=sender, now=clock)


# --- helpers ---------------------------------------------------------------

def test_backoff_schedule_saturates():
    assert [backoff(i) for i in range(1, 8)] == [30, 60, 300, 900, 3600, 3600, 3600]
    assert BACKOFF_SECONDS == (30, 60, 300, 900, 3600)


def test_message_for_plain_new_posting(session):
    ev = _seed(session)
    assert message_for(ev) == "🆕 **Stripe** — SWE Intern\n📍 SF\n🔗 https://x.com/ron?utm_source=S"


def test_message_for_fetch_failed_adds_note(session):
    ev = _seed(session, outcome="fetch_failed")
    assert "(couldn't read the description)" in message_for(ev)


def test_message_for_matched_row_uses_match_format(session):
    ev = _seed(session, outcome="matched", score=82, reasoning="Good.", missing_confirmed=["K8s"], missing_unknown=[])
    msg = message_for(ev)
    assert msg.startswith("🎯 82% — **Stripe** — SWE Intern") and "⚠️ Gaps: K8s" in msg


def test_message_for_below_threshold_row_uses_down_icon(session):
    ev = _seed(session, outcome="below_threshold", score=40, reasoning="Meh.")
    assert message_for(ev).startswith("📉 40% — **Stripe**")


def test_message_for_score_failed_is_link_only_with_note(session):
    ev = _seed(session, outcome="score_failed")
    assert "(couldn't score)" in message_for(ev) and "🎯" not in message_for(ev)


# --- deliver ---------------------------------------------------------------

def test_run_once_delivers_and_closes(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    sender = FakeSender(OK)
    w = _worker(session_factory, sender, clock)
    assert w.run_once() is True
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED
    assert ev.delivery_attempts == 1
    assert ev.delivery_error is None
    assert sender.calls[0][0] == "https://d/ron"
    assert sender.calls[0][2] is None


def test_run_once_returns_false_when_idle(session_factory, session, clock):
    w = _worker(session_factory, FakeSender(), clock)
    assert w.run_once() is False


def test_transient_failure_schedules_retry_with_backoff(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    w = _worker(session_factory, FakeSender(DeliveryResult("transient", None, "503")), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 1
    assert ev.delivery_error == "503"
    assert ev.next_attempt_at == T0 + timedelta(seconds=30)


def test_retry_after_overrides_backoff(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    w = _worker(session_factory, FakeSender(DeliveryResult("transient", 7.0, "rate limited")), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.next_attempt_at == T0 + timedelta(seconds=7)


def test_row_not_picked_before_next_attempt_at(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    sender = FakeSender(DeliveryResult("transient", None, "503"), OK)
    w = _worker(session_factory, sender, clock)
    w.run_once()
    assert w.run_once() is False          # still backing off
    clock.advance(30)
    assert w.run_once() is True
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED
    assert ev.delivery_attempts == 2


def test_gone_webhook_pauses_user_and_leaves_row_untouched(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    w = _worker(session_factory, FakeSender(DeliveryResult("gone", None, "404")), clock)
    w.run_once()
    session.refresh(ev)
    assert w.paused == {"discord:ron": None}
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 0
    assert ev.next_attempt_at == T0


def test_paused_user_blocks_all_their_rows_but_not_others(session_factory, session, clock):
    _resolve(session, _seed(session, "ron"))
    ron2 = _seed_second_ron(session)
    cousin_ev = _resolve(session, _seed(session, "cousin"))
    sender = FakeSender(DeliveryResult("gone", None, "404"), OK, OK)
    w = _worker(session_factory, sender, clock)
    w.run_once()                       # ron #1 → gone → paused
    w.run_once()                       # must skip ron #2, deliver cousin
    w.run_once()                       # nothing left runnable
    session.refresh(cousin_ev); session.refresh(ron2)
    assert cousin_ev.stage == STAGE_CLOSED
    assert ron2.stage == STAGE_DELIVER
    assert [c[0] for c in sender.calls] == ["https://d/ron", "https://d/cousin"]


def _seed_second_ron(session):
    feed = session.query(Feed).filter_by(name="internships").one()
    job = Job(feed=feed, url_key="https://x.com/ron2", url="https://x.com/ron2", company="Meta",
              role="SWE Intern", location="MP", section="software engineering internship roles",
              next_attempt_at=T0)
    ev = Evaluation(job=job, user_id="ron", next_attempt_at=T0, stage=STAGE_DELIVER)
    session.add_all([job, ev]); session.commit()
    return _resolve(session, ev)


def test_transient_failure_pauses_user_until_retry_time(session_factory, session, clock):
    # While one row backs off, the same user's other rows must not be attempted either.
    ev1 = _resolve(session, _seed(session, "ron"))
    ev2 = _seed_second_ron(session)
    sender = FakeSender(DeliveryResult("transient", None, "503"), OK, OK)
    w = _worker(session_factory, sender, clock)
    w.run_once()
    assert w.paused["discord:ron"] == T0 + timedelta(seconds=30)
    assert w.run_once() is False
    assert len(sender.calls) == 1
    clock.advance(30)
    assert w.run_once() is True
    assert len(sender.calls) == 2
    # Both rows are runnable now; the worker picks by next_attempt_at, so the row that
    # never failed (still due at T0) goes before the retried one (due at T0+30s).
    session.refresh(ev1); session.refresh(ev2)
    assert ev2.stage == STAGE_CLOSED
    assert ev1.stage == STAGE_DELIVER
    assert w.run_once() is True
    session.refresh(ev1)
    assert ev1.stage == STAGE_CLOSED
    assert ev1.delivery_attempts == 2


def test_invalid_result_does_not_pause_user(session_factory, session, clock):
    _resolve(session, _seed(session))
    w = _worker(session_factory, FakeSender(DeliveryResult("invalid", None, "HTTP 400: bad")), clock)
    w.run_once()
    assert "discord:ron" not in w.paused


def test_sender_exception_counts_as_transient_attempt(session_factory, session, clock):
    # e.g. the PDF vanished from disk: a failed attempt with backoff, not a worker crash.
    ev = _resolve(session, _seed(session))

    def boom(webhook, content, pdf_path=None):
        raise OSError("no such file")

    w = _worker(session_factory, boom, clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 1
    assert "OSError" in ev.delivery_error
    assert ev.next_attempt_at == T0 + timedelta(seconds=30)


def test_invalid_request_counts_attempt_and_backs_off(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    w = _worker(session_factory, FakeSender(DeliveryResult("invalid", None, "HTTP 400: bad")), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 1
    assert "400" in ev.delivery_error


def test_after_budget_keeps_retrying_hourly(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    fails = [DeliveryResult("transient", None, "503")] * (DELIVERY_BUDGET + 1)
    w = _worker(session_factory, FakeSender(*fails), clock)
    for _ in range(DELIVERY_BUDGET + 1):
        w.run_once()
        clock.advance(3600)
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == DELIVERY_BUDGET + 1
    # Backoff saturates at one hour: the last attempt scheduled its retry exactly one hour later,
    # which is where the clock stands now.
    assert ev.next_attempt_at == clock()


def test_unknown_user_pauses_destination_and_keeps_row(session_factory, session, clock):
    # A row for a user no longer in users.yaml waits for a fixed users.yaml + restart;
    # closed is reserved for confirmed sends and below-threshold scores.
    ev = _resolve(session, _seed(session, "ghost"))
    sender = FakeSender()
    w = _worker(session_factory, sender, clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 0
    assert w.paused == {"discord:ghost": None}
    assert sender.calls == []


def test_closed_rows_are_never_picked(session_factory, session, clock):
    _resolve(session, _seed(session, stage=STAGE_CLOSED))
    w = _worker(session_factory, FakeSender(), clock)
    assert w.run_once() is False


def test_restart_resumes_pending_delivery(session_factory, session, clock):
    ev = _resolve(session, _seed(session))
    w1 = _worker(session_factory, FakeSender(DeliveryResult("transient", None, "503")), clock)
    w1.run_once()
    clock.advance(30)
    w2 = _worker(session_factory, FakeSender(OK), clock)   # fresh worker = restart
    assert w2.run_once() is True
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED


# --- fetch -----------------------------------------------------------------

FETCH_OK_RESULT = FetchResult("Qualifications\n" + "x" * 400, "api.lever.co", "lever", "ok", None)
FETCH_TRANSIENT = FetchResult(None, "api.lever.co", "lever", "transient", "HTTP 503")
FETCH_PERMANENT = FetchResult(None, "careers.x.com", "page", "permanent", "HTTP 404")


class FakeFetcher:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return self.results.pop(0) if self.results else FETCH_OK_RESULT


def _worker_f(session_factory, fetcher, clock, sender=None, users=(RON, COUSIN)):
    return Worker(session_factory, list(users), send=sender or FakeSender(), fetch=fetcher, now=clock)


def test_new_jobs_are_pending_fetch_and_not_delivered_yet(session_factory, session, clock):
    ev = _seed(session)
    assert ev.job.fetch_status == FETCH_PENDING
    sender = FakeSender()
    w = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT), clock, sender)
    w.run_once()                              # fetch attempt, transient
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert sender.calls == []                 # gated on fetch


def test_fetch_ok_stores_description_and_then_delivers(session_factory, session, clock):
    ev = _seed(session)
    fetcher = FakeFetcher(FETCH_OK_RESULT)
    sender = FakeSender(OK)
    w = _worker_f(session_factory, fetcher, clock, sender)
    assert w.run_once() is True               # fetch
    session.refresh(ev); session.refresh(ev.job)
    job = ev.job
    assert job.fetch_status == FETCH_OK
    assert job.description.startswith("Qualifications")
    assert job.description_truncated is False
    assert job.fetch_host == "api.lever.co"
    assert job.fetch_strategy == "lever"
    assert job.has_requirements is True
    assert job.fetch_attempts == 1
    assert fetcher.calls == [job.url]
    clock.advance(FETCH_GAP_SECONDS)
    assert w.run_once() is True               # deliver
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED and ev.outcome is None
    assert "couldn't read" not in sender.calls[0][1]


def test_fetch_marks_truncated_when_over_cap(session_factory, session, clock):
    from fetcher import DESCRIPTION_CAP
    ev = _seed(session)
    big = FetchResult("Requirements " + "y" * (DESCRIPTION_CAP + 10), "h", "page", "ok", None)
    w = _worker_f(session_factory, FakeFetcher(big), clock)
    w.run_once()
    session.refresh(ev.job)
    assert ev.job.description_truncated is True
    assert len(ev.job.description) > DESCRIPTION_CAP     # full text kept


def test_fetch_transient_backs_off_and_records_first_attempt(session_factory, session, clock):
    ev = _seed(session)
    w = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT), clock)
    w.run_once()
    session.refresh(ev.job)
    job = ev.job
    assert job.fetch_status == FETCH_PENDING
    assert job.fetch_attempts == 1
    assert job.fetch_first_attempt_at == T0
    assert job.fetch_error == "HTTP 503"
    assert job.next_attempt_at == T0 + timedelta(seconds=30)


def test_fetch_permanent_fails_job_and_stamps_evaluations(session_factory, session, clock):
    ev_ron = _seed(session, "ron")
    ev_cousin = _seed(session, "cousin")     # different job; untouched
    w = _worker_f(session_factory, FakeFetcher(FETCH_PERMANENT), clock)
    w.run_once()
    session.refresh(ev_ron); session.refresh(ev_ron.job); session.refresh(ev_cousin)
    assert ev_ron.job.fetch_status == FETCH_FAILED
    assert ev_ron.job.fetch_error == "HTTP 404"
    assert ev_ron.outcome == "fetch_failed" and ev_ron.stage == STAGE_DELIVER
    assert ev_cousin.outcome is None


def test_fetch_failed_delivers_link_only_with_note(session_factory, session, clock):
    ev = _seed(session)
    sender = FakeSender(OK)
    w = _worker_f(session_factory, FakeFetcher(FETCH_PERMANENT), clock, sender)
    w.run_once()                              # fetch → failed
    clock.advance(FETCH_GAP_SECONDS)
    w.run_once()                              # deliver
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED and ev.outcome == "fetch_failed"
    assert "(couldn't read the description)" in sender.calls[0][1]


def test_fetch_budget_exhausted_by_attempts(session_factory, session, clock):
    ev = _seed(session)
    w = _worker_f(session_factory, FakeFetcher(*[FETCH_TRANSIENT] * FETCH_BUDGET), clock)
    for _ in range(FETCH_BUDGET):
        assert w.run_once() is True
        clock.advance(3600)
    session.refresh(ev); session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_FAILED
    assert ev.job.fetch_attempts == FETCH_BUDGET
    assert "budget" in ev.job.fetch_error
    assert ev.outcome == "fetch_failed"


def test_fetch_budget_exhausted_by_age(session_factory, session, clock):
    ev = _seed(session)
    w = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT, FETCH_TRANSIENT), clock)
    w.run_once()                              # attempt 1 at T0
    clock.advance(FETCH_MAX_AGE_HOURS * 3600 + 1)
    w.run_once()                              # attempt 2, now older than the age limit
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_FAILED
    assert ev.job.fetch_attempts == 2


def test_fetch_skips_jobs_nobody_is_waiting_on(session_factory, session, clock):
    ev = _seed(session, stage=STAGE_CLOSED)   # only evaluation already closed
    fetcher = FakeFetcher()
    w = _worker_f(session_factory, fetcher, clock)
    assert w.run_once() is False
    assert fetcher.calls == []
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_PENDING


def test_fetch_respects_gap_between_fetches(session_factory, session, clock):
    _seed(session, "ron")
    _seed(session, "cousin")                  # two jobs pending
    fetcher = FakeFetcher(FETCH_OK_RESULT, FETCH_OK_RESULT)
    w = _worker_f(session_factory, fetcher, clock)
    assert w.run_once() is True               # fetch #1
    assert w.run_once() is True               # deliver #1 (ready messages go before fetches)
    assert w.run_once() is False              # fetch #2 must wait for the gap
    assert len(fetcher.calls) == 1
    clock.advance(FETCH_GAP_SECONDS)
    assert w.run_once() is True               # fetch #2
    assert len(fetcher.calls) == 2


def test_fetcher_exception_is_a_transient_attempt(session_factory, session, clock):
    ev = _seed(session)

    def boom(url):
        raise RuntimeError("parser exploded")

    w = _worker_f(session_factory, boom, clock)
    w.run_once()
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_PENDING
    assert ev.job.fetch_attempts == 1
    assert "RuntimeError" in ev.job.fetch_error


def test_restart_resumes_pending_fetch(session_factory, session, clock):
    ev = _seed(session)
    w1 = _worker_f(session_factory, FakeFetcher(FETCH_TRANSIENT), clock)
    w1.run_once()
    clock.advance(30)
    w2 = _worker_f(session_factory, FakeFetcher(FETCH_OK_RESULT), clock)   # restart
    assert w2.run_once() is True
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_OK and ev.job.fetch_attempts == 2


class _Crash(BaseException):
    """Escapes the worker's `except Exception` guard, like SIGKILL/OOM would."""


def test_fetch_lease_survives_crash_mid_fetch(session_factory, session, clock):
    ev = _seed(session)

    def crash(url):
        raise _Crash()

    w1 = _worker_f(session_factory, crash, clock)
    with pytest.raises(_Crash):
        w1.run_once()
    session.refresh(ev.job)
    assert ev.job.fetch_attempts == 1                       # lease persisted
    assert ev.job.fetch_first_attempt_at == T0
    assert ev.job.next_attempt_at == T0 + timedelta(seconds=30)

    w2 = _worker_f(session_factory, FakeFetcher(FETCH_OK_RESULT), clock)   # restart
    assert w2.run_once() is False                           # not first in line until the lease expires
    clock.advance(30)
    assert w2.run_once() is True
    session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_OK and ev.job.fetch_attempts == 2


def test_fail_fetch_stamps_only_open_evaluations_without_outcome(session_factory, session, clock):
    ev_ron = _seed(session, "ron")
    ev_cousin = Evaluation(job=ev_ron.job, user_id="cousin", next_attempt_at=T0,
                            stage=STAGE_DELIVER, outcome="score_failed")
    ev_ghost = Evaluation(job=ev_ron.job, user_id="ghost", next_attempt_at=T0, stage=STAGE_CLOSED)
    ev_dana = Evaluation(job=ev_ron.job, user_id="dana", next_attempt_at=T0,
                          stage=STAGE_SCORE, outcome="score_failed")
    session.add_all([ev_cousin, ev_ghost, ev_dana]); session.commit()

    w = _worker_f(session_factory, FakeFetcher(FETCH_PERMANENT), clock)
    assert w.run_once() is True
    session.refresh(ev_ron); session.refresh(ev_cousin); session.refresh(ev_ghost); session.refresh(ev_dana)
    assert ev_ron.outcome == "fetch_failed"
    assert ev_cousin.stage == STAGE_DELIVER and ev_cousin.outcome == "score_failed"   # pre-set outcome untouched
    assert ev_ghost.outcome is None and ev_ghost.stage == STAGE_CLOSED
    # A pre-set outcome does not block the score → deliver move.
    assert ev_dana.stage == STAGE_DELIVER and ev_dana.outcome == "score_failed"


def test_fetch_over_budget_on_restart_fails_without_a_request(session_factory, session, clock):
    # Crashes can leave attempts == budget with the job still pending; no further request.
    ev = _seed(session)
    ev.job.fetch_attempts = FETCH_BUDGET
    ev.job.fetch_first_attempt_at = T0
    session.commit()
    fetcher = FakeFetcher(FETCH_OK_RESULT)
    w = _worker_f(session_factory, fetcher, clock)
    assert w.run_once() is True
    assert fetcher.calls == []
    session.refresh(ev); session.refresh(ev.job)
    assert ev.job.fetch_status == FETCH_FAILED and "budget" in ev.job.fetch_error
    assert ev.outcome == "fetch_failed"


def test_fetch_backoff_is_measured_from_after_the_call(session_factory, session, clock):
    ev = _seed(session)

    def slow(url):
        clock.advance(14)                     # a near-timeout fetch
        return FETCH_TRANSIENT

    w = _worker_f(session_factory, slow, clock)
    w.run_once()
    session.refresh(ev.job)
    assert ev.job.next_attempt_at == T0 + timedelta(seconds=14 + 30)


from db import STAGE_SCORE


def test_fail_fetch_moves_score_rows_to_deliver(session_factory, session, clock):
    ev = _seed(session, stage=STAGE_SCORE)
    w = _worker_f(session_factory, FakeFetcher(FETCH_PERMANENT), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "fetch_failed"
    assert ev.stage == STAGE_DELIVER


# --- score -----------------------------------------------------------------

from cv import load_cv
from llm import LLMResult, ScoreResponse
from worker import LLM_PAUSE_SECONDS, SCORE_BUDGET

CV_DICT = load_cv("tests/fixtures/cv_sample.yaml").model_dump()
CVS = {"ron": CV_DICT, "cousin": CV_DICT}

def _llm_ok(score=82):
    return LLMResult("ok", ScoreResponse(score=score, reasoning="Because.", missing_confirmed=["K8s"], missing_unknown=["visa"]),
                     None, None, "deepseek-v4-flash", {"prompt_tokens": 10, "completion_tokens": 5}, 42)

LLM_TRANSIENT = LLMResult("transient", None, "HTTP 503", None, None, None, 5)
LLM_INVALID = LLMResult("invalid", None, "schema", None, None, None, 5)
LLM_DOWN = LLMResult("unavailable", None, "ConnectionError: refused", None, None, None, 5)


class FakeLLM:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def score(self, description, cv_text):
        self.calls.append((description, cv_text))
        return self.results.pop(0) if self.results else _llm_ok()


def _worker_s(session_factory, llm, clock, sender=None, users=(RON, COUSIN)):
    return Worker(session_factory, list(users), cvs=CVS, llm=llm, send=sender or FakeSender(), fetch=FakeFetcher(), now=clock)


def _seed_scoreable(session, user_id="ron", description="Requirements\nPython, FastAPI.", **kw):
    ev = _seed(session, user_id, stage=STAGE_SCORE, **kw)
    ev.job.fetch_status = FETCH_OK
    ev.job.description = description
    session.commit()
    return ev


def test_score_snapshots_cv_before_calling_llm(session_factory, session, clock):
    ev = _seed_scoreable(session)
    llm = FakeLLM(_llm_ok())
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True
    session.refresh(ev)
    assert ev.cv_snapshot == CV_DICT
    assert "Test Person" in llm.calls[0][1] and "Requirements" in llm.calls[0][0]


def test_score_match_stores_fields_and_moves_to_deliver(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(_llm_ok(82)), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "matched" and ev.stage == STAGE_DELIVER
    assert ev.score == 82 and ev.reasoning == "Because."
    assert ev.missing_confirmed == ["K8s"] and ev.missing_unknown == ["visa"]
    assert ev.score_model == "deepseek-v4-flash" and ev.score_usage["prompt_tokens"] == 10
    assert ev.attempts == 1 and ev.last_error is None


def test_score_below_threshold_closes_silently(session_factory, session, clock):
    ev = _seed_scoreable(session)
    sender = FakeSender()
    w = _worker_s(session_factory, FakeLLM(_llm_ok(59)), clock, sender)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "below_threshold" and ev.stage == STAGE_CLOSED and ev.score == 59
    assert w.run_once() is False and sender.calls == []


def test_score_below_threshold_delivers_when_user_opted_in(session_factory, session, clock):
    ev = _seed_scoreable(session)
    opted = RON.model_copy(update={"notify_below_threshold": True})
    sender = FakeSender(OK)
    w = _worker_s(session_factory, FakeLLM(_llm_ok(59)), clock, sender, users=(opted, COUSIN))
    w.run_once()                       # score
    session.refresh(ev)
    assert ev.outcome == "below_threshold" and ev.stage == STAGE_DELIVER
    w.run_once()                       # deliver
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED and sender.calls[0][1].startswith("📉 59%")


def test_score_uses_user_threshold(session_factory, session, clock):
    ev = _seed_scoreable(session, "cousin")
    strict = COUSIN.model_copy(update={"threshold": 90})
    w = _worker_s(session_factory, FakeLLM(_llm_ok(82)), clock, users=(RON, strict))
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "below_threshold"


def test_score_at_threshold_is_a_match(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(_llm_ok(60)), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.outcome == "matched"


def test_score_uses_capped_description(session_factory, session, clock):
    from fetcher import DESCRIPTION_CAP
    ev = _seed_scoreable(session, description="R" * (DESCRIPTION_CAP + 500))
    llm = FakeLLM(_llm_ok())
    _worker_s(session_factory, llm, clock).run_once()
    assert len(llm.calls[0][0]) == DESCRIPTION_CAP


class SlowLLM(FakeLLM):
    """Advances the fake clock during the call, like a real 120 s request would."""
    def __init__(self, clock, seconds, *results):
        super().__init__(*results)
        self._clock, self._seconds = clock, seconds

    def score(self, description, cv_text):
        self._clock.advance(self._seconds)
        return super().score(description, cv_text)


def test_score_transient_leases_attempt_backs_off_and_cools_down_llm(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(LLM_TRANSIENT), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_SCORE and ev.attempts == 1 and ev.last_error == "HTTP 503"
    assert ev.next_attempt_at == T0 + timedelta(seconds=30)
    assert w.paused["llm"] == T0 + timedelta(seconds=30)      # shared cooldown


def test_score_transient_cooldown_holds_other_rows(session_factory, session, clock):
    _seed_scoreable(session, "ron")
    _seed_scoreable(session, "cousin")
    llm = FakeLLM(LLMResult("transient", None, "429", 60.0, None, None, 1), _llm_ok(), _llm_ok())
    w = _worker_s(session_factory, llm, clock)
    w.run_once()                        # ron: 429, Retry-After 60
    assert w.run_once() is False        # cousin must NOT hit the endpoint during the cooldown
    assert len(llm.calls) == 1
    clock.advance(60)
    assert w.run_once() is True
    assert len(llm.calls) == 2


def test_score_final_attempt_429_still_cools_down_llm(session_factory, session, clock):
    ev_a = _seed_scoreable(session, "ron", attempts=SCORE_BUDGET - 1)   # third attempt is the last
    _seed_scoreable(session, "cousin")
    llm = FakeLLM(LLMResult("transient", None, "429", 60.0, None, None, 1), _llm_ok())
    sender = FakeSender(OK)
    w = _worker_s(session_factory, llm, clock, sender)
    assert w.run_once() is True         # A: 429 on its final attempt → score_failed, cooldown recorded
    session.refresh(ev_a)
    assert ev_a.outcome == "score_failed" and ev_a.stage == STAGE_DELIVER
    assert w.paused["llm"] == T0 + timedelta(seconds=60)
    assert w.run_once() is True         # A is delivered link-only right away
    assert len(sender.calls) == 1 and "(couldn't score)" in sender.calls[0][1]
    assert w.run_once() is False        # B waits out the cooldown
    assert len(llm.calls) == 1
    clock.advance(60)
    assert w.run_once() is True and len(llm.calls) == 2


def test_score_invalid_does_not_cool_down_llm(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, FakeLLM(LLM_INVALID), clock)
    w.run_once()
    assert "llm" not in w.paused


def test_score_deadlines_are_measured_from_after_the_call(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, SlowLLM(clock, 120, LLMResult("transient", None, "429", 7.0, None, None, 1)), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.next_attempt_at == T0 + timedelta(seconds=120 + 7)   # not 113 s in the past
    assert w.paused["llm"] == T0 + timedelta(seconds=120 + 7)


def test_score_unavailable_resume_is_measured_from_after_the_call(session_factory, session, clock):
    ev = _seed_scoreable(session)
    w = _worker_s(session_factory, SlowLLM(clock, 120, LLM_DOWN), clock)
    w.run_once()
    session.refresh(ev)
    assert w.paused["llm"] == T0 + timedelta(seconds=120 + LLM_PAUSE_SECONDS)
    assert ev.next_attempt_at == w.paused["llm"]


def test_score_over_budget_on_restart_goes_straight_to_fallback(session_factory, session, clock):
    ev = _seed_scoreable(session, attempts=SCORE_BUDGET)   # three crashed attempts persisted
    llm = FakeLLM(_llm_ok())
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True
    assert llm.calls == []
    session.refresh(ev)
    assert ev.outcome == "score_failed" and ev.stage == STAGE_DELIVER and ev.attempts == SCORE_BUDGET


def test_ready_deliveries_go_before_scoring(session_factory, session, clock):
    ready = _resolve(session, _seed(session, "ron", stage=STAGE_DELIVER))
    _seed_scoreable(session, "cousin")
    llm = FakeLLM(_llm_ok())
    sender = FakeSender(OK)
    w = _worker_s(session_factory, llm, clock, sender)
    assert w.run_once() is True
    assert len(sender.calls) == 1 and llm.calls == []      # delivered first
    session.refresh(ready)
    assert ready.stage == STAGE_CLOSED
    assert w.run_once() is True and len(llm.calls) == 1    # then scored


def test_score_budget_exhausted_falls_back_to_link_only(session_factory, session, clock):
    ev = _seed_scoreable(session)
    sender = FakeSender(OK)
    w = _worker_s(session_factory, FakeLLM(*[LLM_INVALID] * SCORE_BUDGET), clock, sender)
    for _ in range(SCORE_BUDGET):
        assert w.run_once() is True
        clock.advance(3600)
    session.refresh(ev)
    assert ev.outcome == "score_failed" and ev.stage == STAGE_DELIVER and ev.attempts == SCORE_BUDGET
    w.run_once()                       # deliver
    assert "(couldn't score)" in sender.calls[0][1]


def test_score_unavailable_pauses_llm_without_consuming_attempt(session_factory, session, clock):
    ev_ron = _seed_scoreable(session, "ron")
    ev_cousin = _seed_scoreable(session, "cousin")
    llm = FakeLLM(LLM_DOWN, _llm_ok(), _llm_ok())
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True         # ron: unavailable
    session.refresh(ev_ron)
    assert ev_ron.attempts == 0 and ev_ron.stage == STAGE_SCORE
    assert w.paused["llm"] == T0 + timedelta(seconds=LLM_PAUSE_SECONDS)
    assert ev_ron.next_attempt_at == T0 + timedelta(seconds=LLM_PAUSE_SECONDS)
    assert w.run_once() is False        # cousin waits too; nothing deliverable
    assert len(llm.calls) == 1
    clock.advance(LLM_PAUSE_SECONDS)
    assert w.run_once() is True         # scoring resumes
    assert len(llm.calls) == 2


def test_score_waits_for_fetch(session_factory, session, clock):
    ev = _seed(session, stage=STAGE_SCORE)         # fetch still pending
    llm = FakeLLM()
    w = _worker_s(session_factory, llm, clock)
    w._fetch = FakeFetcher(FETCH_TRANSIENT)
    w.run_once()                        # fetch attempt, not a score
    assert llm.calls == []
    session.refresh(ev)
    assert ev.stage == STAGE_SCORE


def test_score_llm_exception_is_transient_attempt(session_factory, session, clock):
    ev = _seed_scoreable(session)

    class Boom:
        def score(self, d, c):
            raise RuntimeError("kaboom")

    w = _worker_s(session_factory, Boom(), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.attempts == 1 and "RuntimeError" in ev.last_error and ev.stage == STAGE_SCORE


def test_score_lease_survives_crash(session_factory, session, clock):
    ev = _seed_scoreable(session)

    class Crash:
        def score(self, d, c):
            raise _Crash()

    w1 = _worker_s(session_factory, Crash(), clock)
    with pytest.raises(_Crash):
        w1.run_once()
    session.refresh(ev)
    assert ev.attempts == 1 and ev.cv_snapshot == CV_DICT and ev.next_attempt_at == T0 + timedelta(seconds=30)


def test_score_reuses_snapshot_on_retry_even_if_cv_changes(session_factory, session, clock):
    ev = _seed_scoreable(session)
    llm = FakeLLM(LLM_TRANSIENT, _llm_ok())
    w = _worker_s(session_factory, llm, clock)
    w.run_once()
    w._cvs["ron"] = {**CV_DICT, "name": "Someone Else"}   # "edited on disk" between attempts
    clock.advance(30)
    w.run_once()
    assert "Test Person" in llm.calls[1][1] and "Someone Else" not in llm.calls[1][1]


def test_score_unknown_user_pauses_destination(session_factory, session, clock):
    ev = _seed_scoreable(session, "ghost")
    llm = FakeLLM()
    w = _worker_s(session_factory, llm, clock)
    assert w.run_once() is True          # the pause is the unit of work
    assert llm.calls == []
    assert w.paused == {"discord:ghost": None}
    assert w.run_once() is False         # paused rows are skipped from now on
    session.refresh(ev)
    assert ev.stage == STAGE_SCORE and ev.attempts == 0


def test_score_without_llm_configured_is_skipped(session_factory, session, clock):
    _seed_scoreable(session)
    w = Worker(session_factory, [RON, COUSIN], cvs=CVS, llm=None, send=FakeSender(), fetch=FakeFetcher(), now=clock)
    assert w.run_once() is False

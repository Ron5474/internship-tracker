from datetime import datetime, timedelta

import pytest

from db import STAGE_CLOSED, STAGE_DELIVER, Evaluation, Feed, Job
from discord_client import DeliveryResult
from users import User
from worker import BACKOFF_SECONDS, DELIVERY_BUDGET, Worker, backoff, message_for

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
              company="Stripe", role="SWE Intern", location="SF", section="software engineering internship roles")
    # Pin next_attempt_at to the fake clock; the model default is the real utcnow().
    ev = Evaluation(job=job, user_id=user_id, **{"next_attempt_at": T0, **ev_kwargs})
    session.add_all([feed, job, ev])
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


# --- deliver ---------------------------------------------------------------

def test_run_once_delivers_and_closes(session_factory, session, clock):
    ev = _seed(session)
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
    ev = _seed(session)
    w = _worker(session_factory, FakeSender(DeliveryResult("transient", None, "503")), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 1
    assert ev.delivery_error == "503"
    assert ev.next_attempt_at == T0 + timedelta(seconds=30)


def test_retry_after_overrides_backoff(session_factory, session, clock):
    ev = _seed(session)
    w = _worker(session_factory, FakeSender(DeliveryResult("transient", 7.0, "rate limited")), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.next_attempt_at == T0 + timedelta(seconds=7)


def test_row_not_picked_before_next_attempt_at(session_factory, session, clock):
    ev = _seed(session)
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
    ev = _seed(session)
    w = _worker(session_factory, FakeSender(DeliveryResult("gone", None, "404")), clock)
    w.run_once()
    session.refresh(ev)
    assert w.paused == {"discord:ron": None}
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 0
    assert ev.next_attempt_at == T0


def test_paused_user_blocks_all_their_rows_but_not_others(session_factory, session, clock):
    _seed(session, "ron")
    ron2 = _seed_second_ron(session)
    cousin_ev = _seed(session, "cousin")
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
              role="SWE Intern", location="MP", section="software engineering internship roles")
    ev = Evaluation(job=job, user_id="ron", next_attempt_at=T0)
    session.add_all([job, ev]); session.commit()
    return ev


def test_invalid_request_counts_attempt_and_backs_off(session_factory, session, clock):
    ev = _seed(session)
    w = _worker(session_factory, FakeSender(DeliveryResult("invalid", None, "HTTP 400: bad")), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_DELIVER
    assert ev.delivery_attempts == 1
    assert "400" in ev.delivery_error


def test_after_budget_keeps_retrying_hourly(session_factory, session, clock):
    ev = _seed(session)
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


def test_unknown_user_id_closes_with_error(session_factory, session, clock):
    # A row for a user no longer in users.yaml can never be delivered; mark it and move on.
    ev = _seed(session, "ghost")
    w = _worker(session_factory, FakeSender(), clock)
    w.run_once()
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED
    assert "unknown user" in ev.delivery_error


def test_closed_rows_are_never_picked(session_factory, session, clock):
    _seed(session, stage=STAGE_CLOSED)
    w = _worker(session_factory, FakeSender(), clock)
    assert w.run_once() is False


def test_restart_resumes_pending_delivery(session_factory, session, clock):
    ev = _seed(session)
    w1 = _worker(session_factory, FakeSender(DeliveryResult("transient", None, "503")), clock)
    w1.run_once()
    clock.advance(30)
    w2 = _worker(session_factory, FakeSender(OK), clock)   # fresh worker = restart
    assert w2.run_once() is True
    session.refresh(ev)
    assert ev.stage == STAGE_CLOSED

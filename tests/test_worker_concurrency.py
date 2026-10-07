"""LLM_CONCURRENCY > 1: several score/tailor calls in flight at once.

Only the HTTP call leaves the main thread. The lease is taken before submission and the
result is written when the main loop harvests the finished call, so every database write
still happens on the worker's own thread, in its own session, exactly as in serial mode.
"""
import threading
from datetime import timedelta

from db import STAGE_CLOSED, STAGE_DELIVER, STAGE_RENDER, STAGE_SCORE, STAGE_TAILOR, Evaluation
from llm import LLMResult
from tests.test_worker import (  # noqa: F401 — `clock` is a fixture
    COUSIN, clock, CVS, DANA, LLM_DOWN, RON, FakeFetcher, FakeRenderer, FakeSender, FakeTailor, _llm_ok,
    _seed_scoreable, _seed_tailorable, _tailor_ok, _worker_s,
)
from worker import UNAVAILABLE_PAUSE_SECONDS, Worker


class GatedLLM:
    """Every call blocks until `release()` — the shape of a 36-second request."""
    model = "flash"

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.started = threading.Semaphore(0)
        self.gate = threading.Event()

    def score(self, description, cv_text):
        self.calls.append((description, cv_text))
        self.started.release()
        self.gate.wait(timeout=5)
        return self.results.pop(0) if self.results else _llm_ok()

    def tailor(self, description, cv_id_text, max_bullets):
        return self.score(description, cv_id_text)

    def wait_started(self, n):
        for _ in range(n):
            assert self.started.acquire(timeout=5), "call did not start"

    def release(self):
        self.gate.set()


def _worker_c(session_factory, llm, clock, concurrency, tailor=None, output_dir=None, users=(RON, COUSIN, DANA)):
    return Worker(session_factory, list(users), cvs=CVS, llm=llm, tailor=tailor,
                  output_dir=str(output_dir) if output_dir else None, render=FakeRenderer(1),
                  send=FakeSender(), fetch=FakeFetcher(), now=clock, concurrency=concurrency)


def _drain(w, llm):
    """Harvest everything in flight: release the gate, then loop until the worker is idle."""
    llm.release()
    while True:
        w.wait_inflight(timeout=5)
        if not w.run_once():
            break


def test_calls_up_to_the_concurrency_run_at_once(session_factory, session, clock):
    rows = [_seed_scoreable(session, uid) for uid in ("ron", "cousin", "dana")]
    llm = GatedLLM()
    w = _worker_c(session_factory, llm, clock, concurrency=2)
    assert w.run_once() is True          # first call submitted
    assert w.run_once() is True          # second call submitted alongside it
    llm.wait_started(2)
    assert len(llm.calls) == 2
    assert w.run_once() is False         # both slots busy, the third row waits, nothing else to do
    for ev in rows[:2]:
        session.refresh(ev)
        assert ev.attempts == 1          # leased before the call, as in serial mode
    _drain(w, llm)
    for ev in rows:
        session.refresh(ev)
        assert ev.score == 82 and ev.stage == STAGE_CLOSED      # scored, then delivered by the drain
    assert len(llm.calls) == 3


def test_a_row_in_flight_is_not_picked_again_when_its_lease_expires(session_factory, session, clock):
    ev = _seed_scoreable(session, "ron")
    llm = GatedLLM()
    w = _worker_c(session_factory, llm, clock, concurrency=2)
    assert w.run_once() is True
    llm.wait_started(1)
    clock.advance(3600)                  # far past the 30 s lease
    assert w.run_once() is False
    assert len(llm.calls) == 1
    _drain(w, llm)
    session.refresh(ev)
    assert ev.score == 82


def test_tailor_calls_share_the_slots_and_go_first(session_factory, session, clock, tmp_path):
    _seed_scoreable(session, "ron")
    tailored = _seed_tailorable(session, "cousin")
    llm, tailor = GatedLLM(), GatedLLM(_tailor_ok())
    tailor.model = "pro"
    w = _worker_c(session_factory, llm, clock, concurrency=2, tailor=tailor, output_dir=tmp_path)
    assert w.run_once() is True
    tailor.wait_started(1)
    assert llm.calls == []               # the first slot went to the tailor row
    llm.release()
    _drain(w, tailor)
    session.refresh(tailored)
    assert tailored.stage == STAGE_CLOSED and tailored.pdf_path


def test_unavailable_replies_from_one_incident_count_once(session_factory, session, clock):
    # Two calls in flight when LiteLLM restarts both come back "unavailable". That is one
    # blip, not two: the pause stays at the first step rather than escalating.
    for uid in ("ron", "cousin"):
        _seed_scoreable(session, uid)
    llm = GatedLLM(LLM_DOWN, LLM_DOWN)
    w = _worker_c(session_factory, llm, clock, concurrency=2)
    w.run_once(); w.run_once()
    llm.wait_started(2)
    _drain(w, llm)
    assert w.paused["llm:flash"] == clock() + timedelta(seconds=UNAVAILABLE_PAUSE_SECONDS[0])
    rows = session.query(Evaluation).all()
    assert all(ev.attempts == 0 and ev.stage == STAGE_SCORE for ev in rows)   # leases handed back


def test_a_client_that_raises_in_flight_is_a_failed_attempt(session_factory, session, clock):
    ev = _seed_scoreable(session, "ron")

    class Boom(GatedLLM):
        def score(self, description, cv_text):
            super().score(description, cv_text)
            raise RuntimeError("socket closed")

    llm = Boom()
    w = _worker_c(session_factory, llm, clock, concurrency=2)
    w.run_once()
    llm.wait_started(1)
    _drain(w, llm)
    session.refresh(ev)
    assert ev.attempts == 1 and ev.stage == STAGE_SCORE and "RuntimeError" in ev.last_error


def test_concurrency_of_one_calls_inline(session_factory, session, clock):
    # The serial path is unchanged: no pool, the call completes inside run_once.
    ev = _seed_scoreable(session, "ron")
    llm = GatedLLM()
    llm.release()
    w = _worker_c(session_factory, llm, clock, concurrency=1)
    assert w.run_once() is True
    session.refresh(ev)
    assert ev.score == 82

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import main
import state
from config import FEEDS, Settings
from db import FETCH_OK, STAGE_CLOSED, STAGE_DELIVER, STAGE_SCORE, Evaluation, Feed, Job
from llm import LLMResult, ScoreResponse
from tests.test_poller import README_V1, README_V2
from users import User

RON = User(id="ron", cv="tests/fixtures/cv_sample.yaml", discord_webhook="https://d/ron",
           feeds=["internships", "new-grad"], sections=["software engineering"])


class FakeLLM:
    def __init__(self, score=82):
        self._score = score
        self.calls = 0

    def score(self, description, cv_text):
        self.calls += 1
        return LLMResult("ok", ScoreResponse(score=self._score, reasoning="Fits.", missing_confirmed=[], missing_unknown=[]),
                         None, None, "fake", {"prompt_tokens": 1, "completion_tokens": 1}, 1)


def _readme(internships_readme):
    """poll_all polls every feed; give the internships repo the README under test
    and leave the new-grad repo empty so it contributes no rows."""
    return lambda repo, sha, token=None: internships_readme if repo == FEEDS["internships"].repo else ""


class _StopLoop(BaseException):
    """Sentinel used to break out of _poll_loop after the desired number of iterations.

    Subclasses BaseException (not Exception) so it is NOT swallowed by the
    `except Exception` guard inside _poll_loop that this test is verifying.
    """


def test_poll_loop_survives_exception_and_continues(monkeypatch):
    calls = []

    def fake_poll_all(session_factory, users, settings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        # Second call: stop the loop cleanly once we've proven it continued.
        raise _StopLoop()

    def fake_sleep(seconds):
        return None

    monkeypatch.setattr(main, "poll_all", fake_poll_all)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)

    settings = SimpleNamespace(poll_interval=0)

    with pytest.raises(_StopLoop):
        main._poll_loop(session_factory=None, users=[], settings=settings)

    assert len(calls) == 2


# --- end to end: build → poll → poll → deliver --------------------------------

def test_poll_all_then_run_once_delivers_new_posting(tmp_path):
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_timeout=5)
    users = [RON]
    session_factory, worker = main.build(settings, users, llm=FakeLLM())

    # First poll seeds the feed: jobs, no evaluations.
    with patch("main.get_latest_sha", return_value="s1") as sha, \
         patch("main.get_readme_content", side_effect=_readme(README_V1)):
        main.poll_all(session_factory, users, settings)
    assert sha.call_args.kwargs["token"] == "tok"
    with session_factory() as session:
        assert session.query(Job).count() > 0
        assert session.query(Evaluation).count() == 0

    # Second poll sees one new SWE row → exactly one evaluation for ron.
    with patch("main.get_latest_sha", return_value="s2"), \
         patch("main.get_readme_content", side_effect=_readme(README_V2)):
        main.poll_all(session_factory, users, settings)
    with session_factory() as session:
        ev = session.query(Evaluation).one()
        assert ev.stage == STAGE_SCORE
        assert ev.user_id == "ron"
        ev_id = ev.id
        # Delivery is gated on the description fetch; resolve it here so this test
        # keeps exercising poll → score → deliver without touching the network.
        ev.job.fetch_status = FETCH_OK
        session.commit()

    # Worker scores it, then delivers it through the real discord_client.
    ok = Mock(status_code=200, headers={}, json=lambda: {"id": "1"})
    with patch("discord_client.requests.post", return_value=ok) as post:
        assert worker.run_once() is True   # score
        assert post.call_count == 0
        assert worker.run_once() is True   # deliver
    assert post.call_args.args[0] == "https://d/ron"
    assert post.call_args.kwargs["params"] == {"wait": "true"}
    assert post.call_args.kwargs["json"]["content"].startswith("🎯 82%")
    with session_factory() as session:
        ev = session.get(Evaluation, ev_id)
        assert ev.stage == STAGE_CLOSED
        assert ev.outcome == "matched"


def test_poll_fetch_score_deliver_end_to_end(tmp_path):
    from fetcher import FetchResult
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_timeout=5)
    users = [User(id="ron", cv="tests/fixtures/cv_sample.yaml", discord_webhook="https://d/ron", feeds=["internships"],
                  sections=["software engineering"])]
    session_factory, worker = main.build(settings, users, llm=FakeLLM())
    worker._fetch = lambda url: FetchResult("Qualifications\n" + "z" * 400, "api.lever.co", "lever", "ok", None)

    with patch("main.get_latest_sha", return_value="s1"), \
         patch("main.get_readme_content", side_effect=lambda repo, sha, token=None: README_V1 if "Internships" in repo else ""):
        main.poll_all(session_factory, users, settings)
    with patch("main.get_latest_sha", return_value="s2"), \
         patch("main.get_readme_content", side_effect=lambda repo, sha, token=None: README_V2 if "Internships" in repo else ""):
        main.poll_all(session_factory, users, settings)

    post = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: {"id": "1"}))
    with patch("discord_client.requests.post", post):
        assert worker.run_once() is True      # fetch
        assert post.call_count == 0
        worker._fetch_not_before = None       # skip the politeness gap
        assert worker.run_once() is True      # score
        assert post.call_count == 0
        assert worker.run_once() is True      # deliver
    assert post.call_count == 1

    with session_factory() as s:
        ev = s.query(Evaluation).one()
        assert ev.stage == "closed"
        assert ev.job.fetch_status == "ok"
        assert ev.job.fetch_strategy == "lever"
        assert ev.job.has_requirements is True
        assert ev.score == 82
        assert ev.cv_snapshot["name"] == "Test Person"
        assert ev.score_model == "fake"
        assert post.call_args.kwargs["json"]["content"].startswith("🎯 82%")


def test_build_fails_fast_on_missing_cv(tmp_path):
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_timeout=5)
    users = [User(id="ron", cv=str(tmp_path / "missing.yaml"), discord_webhook="https://d/ron",
                  feeds=["internships"], sections=["software engineering"])]
    with pytest.raises(ValueError, match="missing.yaml"):
        main.build(settings, users, llm=FakeLLM())


def test_below_threshold_end_to_end_closes_without_post(tmp_path):
    from fetcher import FetchResult
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_timeout=5)
    users = [User(id="ron", cv="tests/fixtures/cv_sample.yaml", discord_webhook="https://d/ron", feeds=["internships"],
                  sections=["software engineering"])]
    session_factory, worker = main.build(settings, users, llm=FakeLLM(score=30))
    worker._fetch = lambda url: FetchResult("Qualifications\n" + "z" * 400, "api.lever.co", "lever", "ok", None)

    with patch("main.get_latest_sha", return_value="s1"), \
         patch("main.get_readme_content", side_effect=lambda repo, sha, token=None: README_V1 if "Internships" in repo else ""):
        main.poll_all(session_factory, users, settings)
    with patch("main.get_latest_sha", return_value="s2"), \
         patch("main.get_readme_content", side_effect=lambda repo, sha, token=None: README_V2 if "Internships" in repo else ""):
        main.poll_all(session_factory, users, settings)

    post = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: {"id": "1"}))
    with patch("discord_client.requests.post", post):
        assert worker.run_once() is True      # fetch
        assert post.call_count == 0
        worker._fetch_not_before = None       # skip the politeness gap
        assert worker.run_once() is True      # score
        assert post.call_count == 0
        assert worker.run_once() is False     # nothing left to deliver
    assert post.call_count == 0

    with session_factory() as s:
        ev = s.query(Evaluation).one()
        assert ev.stage == "closed"
        assert ev.outcome == "below_threshold"


def test_build_imports_legacy_state(tmp_path):
    state.write_known_urls(str(tmp_path), {"https://a.com/1", "https://b.com/2"})
    state.write_last_sha(str(tmp_path), "abc1234")
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token=None, llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_timeout=5)

    session_factory, _worker = main.build(settings, [RON])

    with session_factory() as session:
        feed = session.query(Feed).filter_by(name="internships").one()
        assert session.query(Job).filter_by(feed_id=feed.id).count() == 2
        assert feed.last_sha == "abc1234"
        assert session.query(Evaluation).count() == 0

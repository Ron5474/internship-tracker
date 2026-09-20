from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import main
import state
from config import FEEDS, Settings
from db import STAGE_CLOSED, STAGE_DELIVER, Evaluation, Feed, Job
from tests.test_poller import README_V1, README_V2
from users import User

RON = User(id="ron", cv="/x", discord_webhook="https://d/ron",
           feeds=["internships", "new-grad"], sections=["software engineering"])


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
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok")
    users = [RON]
    session_factory, worker = main.build(settings, users)

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
        assert ev.stage == STAGE_DELIVER
        assert ev.user_id == "ron"
        ev_id = ev.id

    # Worker delivers it through the real discord_client.
    ok = Mock(status_code=200, headers={}, json=lambda: {"id": "1"})
    with patch("discord_client.requests.post", return_value=ok) as post:
        assert worker.run_once() is True
    assert post.call_args.args[0] == "https://d/ron"
    assert post.call_args.kwargs["params"] == {"wait": "true"}
    with session_factory() as session:
        assert session.get(Evaluation, ev_id).stage == STAGE_CLOSED


def test_build_imports_legacy_state(tmp_path):
    state.write_known_urls(str(tmp_path), {"https://a.com/1", "https://b.com/2"})
    state.write_last_sha(str(tmp_path), "abc1234")
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token=None)

    session_factory, _worker = main.build(settings, [RON])

    with session_factory() as session:
        feed = session.query(Feed).filter_by(name="internships").one()
        assert session.query(Job).filter_by(feed_id=feed.id).count() == 2
        assert feed.last_sha == "abc1234"
        assert session.query(Evaluation).count() == 0

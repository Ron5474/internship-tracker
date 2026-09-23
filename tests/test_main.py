import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import main
import state
from config import FEEDS, Settings, load_settings
from db import (
    FETCH_OK,
    STAGE_CLOSED,
    STAGE_DELIVER,
    STAGE_RENDER,
    STAGE_SCORE,
    STAGE_TAILOR,
    Evaluation,
    Feed,
    Job,
    ensure_feeds,
)
from main import build
from tests.test_poller import README_V1, README_V2
from tests.test_worker import OK, CV_SNAPSHOT, FakeLLM, FakeSender, FakeTailor, _llm_ok
from users import User, load_users
from worker import Worker

FIXTURES = Path(__file__).parent / "fixtures"

RON = User(id="ron", cv=str(FIXTURES / "cv_sample.yaml"), discord_webhook="https://d/ron",
           feeds=["internships", "new-grad"], sections=["software engineering"])


def _readme(internships_readme):
    """poll_all polls every feed; give the internships repo the README under test
    and leave the new-grad repo empty so it contributes no rows."""
    return lambda repo, sha, token=None: internships_readme if repo == FEEDS["internships"].repo else ""


# A feed with no known jobs yet is always seeded silently, whatever the SHA says (poller.py) — so
# the first poll needs *some* row to prime "known" before a second poll can produce a fresh
# evaluation. This decoy (a section RON isn't subscribed to) supplies that, and is deleted below
# once it has done its job, so exactly one Job — the one with an Evaluation — remains.
README_DECOY = """\
## 💰 Quantitative Finance Internship Roles
<table><tbody>
<tr><td><strong>Jane</strong></td><td>Quant Intern</td><td>NY</td>
<td><div align="center"><a href="https://jane.com/q"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>
"""

README_DECOY_PLUS_POSTING = README_DECOY + """
## 💻 Software Engineering Internship Roles
<table><tbody>
<tr><td><strong>Stripe</strong></td><td>SWE Intern</td><td>SF</td>
<td><div align="center"><a href="https://stripe.com/j?gh_jid=1&utm_source=Simplify"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>
"""


def _poll_one_posting(session_factory, users, monkeypatch):
    """Two polls with GitHub calls monkeypatched: prime the feed with a decoy posting (so the
    second poll isn't itself treated as seeding), then land exactly one new SWE posting.
    Leaves one Evaluation at STAGE_SCORE with its Job still fetch-pending; the decoy is removed."""
    settings = SimpleNamespace(github_token="tok")
    with session_factory() as session:
        ensure_feeds(session, FEEDS.values())
        session.commit()
    monkeypatch.setattr(main, "get_latest_sha", lambda *a, **k: "s1")
    monkeypatch.setattr(main, "get_readme_content", _readme(README_DECOY))
    main.poll_all(session_factory, users, settings)
    monkeypatch.setattr(main, "get_latest_sha", lambda *a, **k: "s2")
    monkeypatch.setattr(main, "get_readme_content", _readme(README_DECOY_PLUS_POSTING))
    main.poll_all(session_factory, users, settings)
    with session_factory() as session:
        session.query(Job).filter(Job.company == "Jane").delete()
        session.commit()


def _write_users_and_cv(tmp_path):
    """A users.yaml plus the CV file it points to, both under tmp_path."""
    cv_path = tmp_path / "cv.yaml"
    cv_path.write_text((FIXTURES / "cv_sample.yaml").read_text())
    (tmp_path / "users.yaml").write_text(
        f"- id: ron\n  cv: {cv_path}\n  discord_webhook: https://d/ron\n"
        "  feeds: [internships]\n  sections: [software engineering]\n"
    )


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
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_tailor_model="pro", max_bullets_per_entry=4, llm_timeout=5)
    users = [RON]
    session_factory, worker = main.build(settings, users, llm=FakeLLM(_llm_ok()), tailor=FakeTailor())

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
        # keeps exercising poll → score → tailor → render → deliver without touching the network.
        ev.job.fetch_status = FETCH_OK
        session.commit()

    # Worker scores it, tailors and renders a resume, then delivers it through the real discord_client.
    ok = Mock(status_code=200, headers={}, json=lambda: {"id": "1"})
    with patch("discord_client.requests.post", return_value=ok) as post:
        assert worker.run_once() is True   # score
        assert post.call_count == 0
        for _ in range(3):                 # tailor, render, deliver
            assert worker.run_once() is True
    assert post.call_args.args[0] == "https://d/ron"
    assert post.call_args.kwargs["params"] == {"wait": "true"}
    # A resume is attached, so delivery went out as multipart (payload_json), not a plain json= body.
    payload = json.loads(post.call_args.kwargs["data"]["payload_json"])
    assert payload["content"].startswith("🎯 82%")
    with session_factory() as session:
        ev = session.get(Evaluation, ev_id)
        assert ev.stage == STAGE_CLOSED
        assert ev.outcome == "matched"


def test_poll_fetch_score_deliver_end_to_end(tmp_path, monkeypatch):
    from fetcher import FetchResult
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_tailor_model="pro", max_bullets_per_entry=4, llm_timeout=5)
    users = [User(id="ron", cv=str(FIXTURES / "cv_sample.yaml"), discord_webhook="https://d/ron", feeds=["internships"],
                  sections=["software engineering"])]
    session_factory, worker = main.build(settings, users, llm=FakeLLM(_llm_ok()), tailor=FakeTailor())
    worker._fetch = lambda url: FetchResult("Qualifications\n" + "z" * 400, "api.lever.co", "lever", "ok", None)

    _poll_one_posting(session_factory, users, monkeypatch)

    post = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: {"id": "1"}))
    with patch("discord_client.requests.post", post):
        assert worker.run_once() is True      # fetch
        assert post.call_count == 0
        worker._fetch_not_before = None       # skip the politeness gap
        assert worker.run_once() is True      # score
        assert post.call_count == 0
        for _ in range(3):                    # tailor, render, deliver
            assert worker.run_once() is True
    assert post.call_count == 1

    with session_factory() as s:
        ev = s.query(Evaluation).one()
        assert ev.stage == "closed"
        assert ev.job.fetch_status == "ok"
        assert ev.job.fetch_strategy == "lever"
        assert ev.job.has_requirements is True
        assert ev.score == 82
        assert ev.cv_snapshot["name"] == "Test Person"
        assert ev.score_model == "deepseek-v4-flash"
        payload = json.loads(post.call_args.kwargs["data"]["payload_json"])
        assert payload["content"].startswith("🎯 82%")


def test_build_fails_fast_on_missing_cv(tmp_path):
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_tailor_model="pro", max_bullets_per_entry=4, llm_timeout=5)
    users = [User(id="ron", cv=str(tmp_path / "missing.yaml"), discord_webhook="https://d/ron",
                  feeds=["internships"], sections=["software engineering"])]
    with pytest.raises(ValueError, match="missing.yaml"):
        main.build(settings, users, llm=FakeLLM())


def test_main_loads_cvs_before_migration(monkeypatch, tmp_path):
    # A missing CV must stop startup before the migration loop, which retries GitHub forever.
    (tmp_path / "users.yaml").write_text(
        f"- id: ron\n  cv: {tmp_path / 'missing.yaml'}\n  discord_webhook: https://d/ron\n"
        "  feeds: [internships]\n  sections: [software engineering]\n")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("LLM_SCORE_MODEL", "m")
    monkeypatch.setenv("LLM_TAILOR_MODEL", "pro")
    migrate = Mock(return_value=True)
    monkeypatch.setattr(main, "migrate_state", migrate)
    with pytest.raises(ValueError, match="missing"):
        main.main()
    migrate.assert_not_called()


def test_below_threshold_end_to_end_closes_without_post(tmp_path):
    from fetcher import FetchResult
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token="tok", llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_tailor_model="pro", max_bullets_per_entry=4, llm_timeout=5)
    users = [User(id="ron", cv=str(FIXTURES / "cv_sample.yaml"), discord_webhook="https://d/ron", feeds=["internships"],
                  sections=["software engineering"])]
    session_factory, worker = main.build(settings, users, llm=FakeLLM(_llm_ok(30)), tailor=FakeTailor())
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
        assert worker.run_once() is False     # below threshold: nothing left to deliver
    assert post.call_count == 0

    with session_factory() as s:
        ev = s.query(Evaluation).one()
        assert ev.stage == "closed"
        assert ev.outcome == "below_threshold"


def test_build_imports_legacy_state(tmp_path):
    state.write_known_urls(str(tmp_path), {"https://a.com/1", "https://b.com/2"})
    state.write_last_sha(str(tmp_path), "abc1234")
    settings = Settings(data_dir=str(tmp_path), poll_interval=1, github_token=None, llm_base_url="http://llm", llm_api_key=None, llm_score_model="m", llm_tailor_model="pro", max_bullets_per_entry=4, llm_timeout=5)

    session_factory, _worker = main.build(settings, [RON])

    with session_factory() as session:
        feed = session.query(Feed).filter_by(name="internships").one()
        assert session.query(Job).filter_by(feed_id=feed.id).count() == 2
        assert feed.last_sha == "abc1234"
        assert session.query(Evaluation).count() == 0


# --- full pipeline: everything external faked, everything else real -----------

def _pipeline(tmp_path, session_factory, users, cvs):
    """Everything external faked; the real cv, render, worker and discord formatting code runs."""
    from render import render_pdf
    llm, tailor, sender = FakeLLM(_llm_ok()), FakeTailor(), FakeSender(OK)
    worker = Worker(session_factory, users, cvs=cvs, llm=llm, tailor=tailor,
                    output_dir=str(tmp_path / "output"), max_bullets=4,
                    send=sender, render=render_pdf)
    return worker, llm, tailor, sender


def test_poll_to_pdf_delivery(tmp_path, session_factory, monkeypatch):
    """One posting, all the way: poll → fetch → score → tailor → render → deliver → closed."""
    users = [RON]
    cvs = {"ron": CV_SNAPSHOT}
    _poll_one_posting(session_factory, users, monkeypatch)   # the existing test_main poll helper
    with session_factory() as session:
        job = session.query(Job).one()
        job.description, job.fetch_status = "We need a Python engineer.", FETCH_OK
        session.commit()

    worker, llm, tailor, sender = _pipeline(tmp_path, session_factory, users, cvs)
    for _ in range(4):          # score, tailor, render, deliver
        assert worker.run_once() is True
    assert worker.run_once() is False

    with session_factory() as session:
        ev = session.query(Evaluation).one()
        assert ev.stage == STAGE_CLOSED and ev.outcome == "matched" and ev.resume_error is None
        assert ev.tailored["experience"] and ev.cv_snapshot == CV_SNAPSHOT
        assert Path(ev.pdf_path).read_bytes().startswith(b"%PDF")
        content, pdf = sender.calls[0][1], sender.calls[0][2]
        assert content.startswith("🎯") and pdf == ev.pdf_path
        assert "Couldn't generate resume" not in content


def test_restart_between_every_stage_resumes_without_repeating(tmp_path, session_factory, monkeypatch):
    """A fresh Worker for each iteration, sharing the fakes: no stage runs twice."""
    users = [RON]
    _poll_one_posting(session_factory, users, monkeypatch)
    with session_factory() as session:
        job = session.query(Job).one()
        job.description, job.fetch_status = "We need a Python engineer.", FETCH_OK
        session.commit()

    from render import render_pdf
    llm, tailor, sender = FakeLLM(_llm_ok(), _llm_ok()), FakeTailor(), FakeSender(OK, OK)
    renders = []

    def counting_render(cv, selection, out_path):
        renders.append(out_path)
        return render_pdf(cv, selection, out_path)

    stages = []
    for _ in range(4):
        # A brand-new Worker each time: only the database carries progress forward.
        w = Worker(session_factory, users, cvs={"ron": CV_SNAPSHOT}, llm=llm, tailor=tailor,
                   output_dir=str(tmp_path / "output"), max_bullets=4,
                   send=sender, render=counting_render)
        assert w.run_once() is True
        with session_factory() as session:
            stages.append(session.query(Evaluation).one().stage)

    assert stages == [STAGE_TAILOR, STAGE_RENDER, STAGE_DELIVER, STAGE_CLOSED]
    assert len(llm.calls) == 1 and len(tailor.calls) == 1 and len(renders) == 1 and len(sender.calls) == 1


def test_build_constructs_both_clients_with_their_own_models(tmp_path, monkeypatch):
    _write_users_and_cv(tmp_path)        # the existing test_main data-dir helper
    settings = load_settings({
        "DATA_DIR": str(tmp_path), "LLM_BASE_URL": "http://x/v1",
        "LLM_SCORE_MODEL": "flash", "LLM_TAILOR_MODEL": "pro", "MAX_BULLETS_PER_ENTRY": "3",
    })
    users = load_users(str(tmp_path / "users.yaml"))
    _, worker = build(settings, users)
    assert worker._llm.model == "flash"
    assert worker._tailor.model == "pro"
    assert worker._max_bullets == 3
    assert worker._output_dir == str(tmp_path / "output")

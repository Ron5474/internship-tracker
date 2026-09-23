"""Tests for scripts/ab_score.py.

The script's first live run happens on the server, against an endpoint this suite cannot
reach — so everything that does not require the network is tested here: which rows it picks,
what request it builds from them, how it reads a reply, and what it reports. The transport
is the only thing stubbed.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ab_score  # noqa: E402
from cv import load_cv  # noqa: E402
from db import Evaluation, Feed, Job, init_db, make_engine, make_session_factory  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "cv_sample.yaml")


@pytest.fixture
def db_path(tmp_path):
    """A database holding one scored evaluation, one unscored, and one with no description."""
    path = str(tmp_path / "tracker.db")
    engine = make_engine(path)
    init_db(engine)
    snapshot = load_cv(FIXTURE).model_dump()
    with make_session_factory(engine)() as session:
        feed = Feed(name="internships", repo="a/b", branch="dev")
        scored = Job(feed=feed, url_key="k1", url="https://x/1", company="Stripe", role="SWE Intern",
                     location="SF", section="software", description="We need a Python engineer.")
        unscored = Job(feed=feed, url_key="k2", url="https://x/2", company="Acme", role="Analyst",
                       location="NY", section="software", description="Some other posting.")
        undescribed = Job(feed=feed, url_key="k3", url="https://x/3", company="Globex", role="Dev",
                          location="LA", section="software")
        session.add_all([
            feed, scored, unscored, undescribed,
            Evaluation(job=scored, user_id="ron", score=82, cv_snapshot=snapshot),
            Evaluation(job=unscored, user_id="ron"),                      # never scored
            Evaluation(job=undescribed, user_id="ron", score=70, cv_snapshot=snapshot),  # no text
        ])
        session.commit()
    return path


def test_load_rows_skips_evaluations_that_cannot_be_rescored(db_path):
    # Re-scoring needs all three: a stored score to compare against, a description to send,
    # and the snapshot that produced the original score.
    rows = ab_score.load_rows(db_path, limit=10, ids=None)
    assert [r.company for r in rows] == ["Stripe"]
    assert rows[0].stored_score == 82


def test_load_rows_builds_the_real_scoring_request(db_path):
    row = ab_score.load_rows(db_path, limit=10, ids=None)[0]
    system, user = row.messages
    assert system["content"] == ab_score.SCORE_SYSTEM
    assert "We need a Python engineer." in user["content"]
    assert load_cv(FIXTURE).name in user["content"]      # the CV text went in too
    assert user["content"].rstrip().endswith("Return the JSON object now.")


def test_load_rows_honours_explicit_ids(db_path):
    rows = ab_score.load_rows(db_path, limit=10, ids=[999])
    assert rows == []


def _reply(monkeypatch, status=200, body=None, boom=None):
    class FakeResponse:
        status_code = status

        def json(self):
            return body

    def fake_post(url, headers=None, json=None, timeout=None):
        if boom:
            raise boom
        return FakeResponse()

    monkeypatch.setattr(ab_score.requests, "post", fake_post)


def _ok_body(score=77, reasoning_tokens=None, completion=120):
    details = {"reasoning_tokens": reasoning_tokens} if reasoning_tokens is not None else {}
    return {
        "choices": [{"message": {"content": json.dumps({
            "score": score, "reasoning": "why", "missing_confirmed": [], "missing_unknown": [],
        })}}],
        "usage": {"completion_tokens": completion, "completion_tokens_details": details},
    }


def test_ask_parses_a_score_and_the_reasoning_tokens(monkeypatch):
    _reply(monkeypatch, body=_ok_body(score=77, reasoning_tokens=4990))
    result = ab_score.ask("http://x/chat/completions", {}, "flash", [], {}, 10)
    assert result.score == 77 and result.reasoning_tokens == 4990 and result.error is None


def test_ask_reports_a_missing_reasoning_tokens_key_as_none(monkeypatch):
    # This is how "reasoning is off" shows up: the key disappears rather than going to zero.
    _reply(monkeypatch, body=_ok_body(reasoning_tokens=None))
    assert ab_score.ask("http://x", {}, "flash", [], {}, 10).reasoning_tokens is None


def test_ask_sends_the_variant_overrides(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(json)

        class R:
            status_code = 200

            def json(self):
                return _ok_body()

        return R()

    monkeypatch.setattr(ab_score.requests, "post", fake_post)
    ab_score.ask("http://x", {}, "flash", [{"role": "user", "content": "hi"}],
                 {"reasoning_effort": "none"}, 10)
    assert seen["reasoning_effort"] == "none"
    assert seen["temperature"] == 0 and seen["model"] == "flash"


def test_ask_turns_a_bad_status_into_a_result_not_an_exception(monkeypatch):
    _reply(monkeypatch, status=503, body={})
    result = ab_score.ask("http://x", {}, "flash", [], {}, 10)
    assert result.score is None and "503" in result.error


def test_ask_turns_a_malformed_reply_into_a_result(monkeypatch):
    _reply(monkeypatch, body={"choices": [{"message": {"content": "not json"}}], "usage": {}})
    result = ab_score.ask("http://x", {}, "flash", [], {}, 10)
    assert result.score is None and result.error


def test_ask_survives_a_connection_failure(monkeypatch):
    _reply(monkeypatch, boom=ab_score.requests.ConnectionError("down"))
    assert ab_score.ask("http://x", {}, "flash", [], {}, 10).error == "ConnectionError"


def test_report_shows_each_variant_and_counts_threshold_flips(capsys):
    row = ab_score.Row(165, 85, "RTX", "SWE Intern", [])
    row.results = [ab_score.Result(84, 900, 10, 120), ab_score.Result(40, 800, None, 120)]
    flipped = ab_score.Row(177, 84, "Excellus", "Analytics Intern", [])
    flipped.results = [ab_score.Result(83, 900, 10, 120), ab_score.Result(35, 700, None, 120)]

    ab_score.report([row, flipped], [{}, {"reasoning_effort": "none"}])
    out = capsys.readouterr().out

    assert "165" in out and "RTX — SWE Intern" in out
    # The repeat variant barely moved; the reasoning-off variant crossed the threshold on both.
    assert "0/2 crossed the 60 threshold" in out
    assert "2/2 crossed the 60 threshold" in out
    assert "no reasoning tokens reported" in out


def test_report_survives_a_variant_that_failed_everywhere(capsys):
    row = ab_score.Row(1, 80, "Acme", "Dev", [])
    row.results = [ab_score.Result(error="HTTP 401")]
    ab_score.report([row], [{"reasoning_effort": "none"}])
    assert "every call failed" in capsys.readouterr().out

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from db import (
    FETCH_PENDING,
    STAGE_DELIVER,
    Evaluation,
    Feed,
    Job,
    utcnow,
)


def _job(feed, key="https://x.com/j?gh_jid=1"):
    return Job(
        feed=feed, url_key=key, company="X", role="SWE", location="NY",
        url=key + "&utm_source=Simplify", section="software engineering",
    )


def test_utcnow_is_naive():
    assert utcnow().tzinfo is None
    assert isinstance(utcnow(), datetime)


def test_tables_roundtrip(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    ev = Evaluation(job=job, user_id="ron")
    session.add_all([feed, job, ev])
    session.commit()

    loaded = session.get(Evaluation, ev.id)
    assert loaded.job.company == "X"
    assert loaded.job.feed.name == "internships"


def test_job_defaults(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    session.add_all([feed, job])
    session.commit()
    assert job.fetch_status == FETCH_PENDING
    assert job.fetch_attempts == 0
    assert job.description is None
    assert job.description_truncated is False
    assert job.created_at is not None


def test_evaluation_defaults(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    ev = Evaluation(job=job, user_id="ron")
    session.add_all([feed, job, ev])
    session.commit()
    assert ev.stage == STAGE_DELIVER
    assert ev.outcome is None
    assert ev.attempts == 0
    assert ev.delivery_attempts == 0
    assert ev.next_attempt_at is not None
    assert ev.updated_at is not None


def test_job_unique_per_feed(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    session.add_all([feed, _job(feed), _job(feed)])
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_url_key_allowed_in_different_feeds(session):
    f1 = Feed(name="internships", repo="a/b", branch="dev")
    f2 = Feed(name="new-grad", repo="a/c", branch="dev")
    session.add_all([f1, f2, _job(f1), _job(f2)])
    session.commit()
    assert session.query(Job).count() == 2


def test_evaluation_unique_per_job_and_user(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    session.add_all([feed, job, Evaluation(job=job, user_id="ron"), Evaluation(job=job, user_id="ron")])
    with pytest.raises(IntegrityError):
        session.commit()

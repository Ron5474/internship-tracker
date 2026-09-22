import tempfile
from datetime import datetime

import pytest
from sqlalchemy import Column, Integer, MetaData, String, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase

from config import FEEDS
from db import (
    FETCH_OK,
    FETCH_PENDING,
    STAGE_DELIVER,
    Evaluation,
    Feed,
    Job,
    ensure_columns,
    ensure_feeds,
    import_legacy_state,
    init_db,
    make_engine,
    utcnow,
)
from state import write_known_urls, write_last_sha


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


def test_ensure_feeds_creates_rows_once(session):
    ensure_feeds(session, FEEDS.values())
    ensure_feeds(session, FEEDS.values())
    session.commit()
    names = sorted(f.name for f in session.query(Feed).all())
    assert names == ["internships", "new-grad"]


def test_import_legacy_seeds_internships_and_keeps_sha(session):
    with tempfile.TemporaryDirectory() as d:
        write_known_urls(d, {"https://a.com/1", "https://b.com/2?gh_jid=9"})
        write_last_sha(d, "abc1234")
        ensure_feeds(session, FEEDS.values())
        session.commit()

        assert import_legacy_state(session, d) == 2
        session.commit()

    feed = session.query(Feed).filter_by(name="internships").one()
    assert feed.last_sha == "abc1234"
    jobs = session.query(Job).all()
    assert {j.url_key for j in jobs} == {"https://a.com/1", "https://b.com/2?gh_jid=9"}
    assert all(j.feed_id == feed.id for j in jobs)
    assert all(j.fetch_status == FETCH_OK for j in jobs)
    assert session.query(Evaluation).count() == 0


def test_import_legacy_skipped_when_jobs_exist(session):
    with tempfile.TemporaryDirectory() as d:
        write_known_urls(d, {"https://a.com/1"})
        write_last_sha(d, "abc1234")
        ensure_feeds(session, FEEDS.values())
        feed = session.query(Feed).filter_by(name="internships").one()
        session.add(_job(feed, "https://already.com/x"))
        session.commit()

        assert import_legacy_state(session, d) == 0
        session.commit()

    assert session.query(Job).count() == 1
    assert session.query(Feed).filter_by(name="internships").one().last_sha is None


def test_import_legacy_noop_on_fresh_data_dir(session):
    with tempfile.TemporaryDirectory() as d:
        ensure_feeds(session, FEEDS.values())
        session.commit()
        assert import_legacy_state(session, d) == 0
    assert session.query(Job).count() == 0


def test_job_has_fetch_strategy_and_has_requirements(session):
    feed = Feed(name="internships", repo="a/b", branch="dev")
    job = _job(feed)
    session.add_all([feed, job])
    session.commit()
    assert job.fetch_strategy is None
    assert job.has_requirements is None
    job.fetch_strategy = "lever"
    job.has_requirements = True
    session.commit()
    assert session.get(Job, job.id).fetch_strategy == "lever"


def test_ensure_columns_adds_missing_columns_to_existing_db(tmp_path):
    engine = make_engine(str(tmp_path / "old.db"))
    init_db(engine)
    # Simulate a database created before this plan: drop the two new columns.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN fetch_strategy"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN has_requirements"))
    assert "fetch_strategy" not in {c["name"] for c in inspect(engine).get_columns("jobs")}

    added = ensure_columns(engine)

    assert sorted(added) == ["jobs.fetch_strategy", "jobs.has_requirements"]
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"fetch_strategy", "has_requirements"} <= cols


def test_ensure_columns_is_noop_when_current(tmp_path):
    engine = make_engine(str(tmp_path / "new.db"))
    init_db(engine)
    assert ensure_columns(engine) == []


def test_ensure_columns_refuses_non_nullable_column(tmp_path):
    class ProbeBase(DeclarativeBase):
        pass

    class Probe(ProbeBase):
        __tablename__ = "probe"
        id: int = Column(Integer, primary_key=True)
        must: str = Column(String, nullable=False)

    # Create a database with only the id column (simulating pre-plan schema).
    engine = make_engine(str(tmp_path / "probe.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))

    # Attempt to ensure the non-nullable column exists should raise.
    with pytest.raises(RuntimeError, match="nullable"):
        ensure_columns(engine, metadata=ProbeBase.metadata)

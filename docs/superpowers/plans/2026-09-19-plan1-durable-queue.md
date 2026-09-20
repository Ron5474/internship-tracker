# Plan 1: Durable Queue + Link-Only Delivery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-file polling script with a SQLite-backed poller + worker that tracks two feeds for multiple users and delivers a link-only Discord message for every new posting in each user's subscribed sections — nothing lost across restarts, broken webhooks paused, legacy state imported.

**Architecture:** One process, two threads. The poller thread diffs each GitHub feed against the `jobs` table and creates one `evaluations` row per interested user, in one transaction per feed. The worker thread drains `evaluations` whose `stage` names a runnable action; in this plan the only action is `deliver`. Every row's state is persisted before advancing, so a restart resumes exactly where it left off. Later plans add `fetch`, `score`, `tailor` and `render` in front of `deliver` without changing the queue.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (SQLite), Pydantic 2, PyYAML, requests, pytest. Existing `github_client.py`, `parser.py`, `state.py`, `migration.py` are reused unchanged.

**Spec:** `docs/superpowers/specs/2026-09-19-job-match-pipeline-design.md` — sections "Architecture", "Data model", "Users", "Error classification and retries", "Delivery", "Configuration", "Module layout", "Build order" step 2.

## Global Constraints

- Python 3.12, image `python:3.12-slim`. Dependencies pinned in `requirements.txt`.
- All datetimes stored naive UTC (SQLite has no timezone). Use `db.utcnow()` everywhere; never `datetime.now()` directly in production code.
- `stage` is always the **next action** to run: `deliver | closed` in this plan (`score | tailor | render` added later). `outcome` is written once at a fallback decision and never overwritten.
- A row reaches `closed` only when Discord returns HTTP 200 with a message body (`?wait=true`), or when `outcome=below_threshold` (not in this plan).
- Discord: content ≤ 2,000 characters; 429 honors `Retry-After`; 404/401 pause the destination until restart; the old 204-accepting `send_notification` is removed.
- Transient backoff schedule (seconds, by attempt index, capped at last value): `30, 60, 300, 900, 3600`.
- Delivery budget: 10 attempts, then keep retrying hourly and log at ERROR each time.
- Job identity: unique on `(feed_id, url_key)`. `url_key` from `parser.py` (PR #1 semantics: drops `utm_*` and `ref` only).
- First poll of a feed with no `last_sha` seeds jobs with **no** evaluations.
- Legacy import: `known_urls.json` → seeded `internships` jobs, `last_sha.txt` → `feeds.last_sha`; runs only when `jobs` is empty; `migrate_state` from PR #1 runs first.
- Tests: pytest, `pythonpath = [".", "src"]` (already set). **New tests import bare module names** (`from db import ...`, `patch("discord_client.requests.post")`), the same way `src/` modules import each other, so there is exactly one module object per file — importing `src.db` in a test and `db` in production code would create two SQLAlchemy registries for the same tables. The pre-existing tests keep their `src.parser` style; those modules are stateless so it does not matter. External calls mocked; DB tests use in-memory SQLite.
- Commit after every task. Do not push.

---

## File structure

| File | Responsibility |
|---|---|
| `src/config.py` | Read env into a `Settings` dataclass; `FEEDS` registry. Nothing else reads `os.environ`. |
| `src/db.py` | SQLAlchemy models (`Feed`, `Job`, `Evaluation`), engine/session factory, `utcnow()`, `import_legacy_state()`. |
| `src/users.py` | `User` Pydantic model, `load_users(path)`. |
| `src/discord_client.py` | Rewritten: `send_message()` returning a classified `DeliveryResult`, `format_link_only()`, `cap_content()`. |
| `src/poller.py` | `poll_feed()` — one feed, one transaction. |
| `src/worker.py` | `Worker` — pause map, backoff, `run_once()`, `deliver()`. |
| `src/main.py` | Rewritten: build settings, run migrations, start threads. |
| `src/state.py`, `src/migration.py`, `src/parser.py`, `src/github_client.py` | Unchanged. |
| `tests/test_config.py`, `tests/test_db.py`, `tests/test_users.py`, `tests/test_discord_client.py` (rewritten), `tests/test_poller.py`, `tests/test_worker.py`, `tests/conftest.py` | Tests. |
| `requirements.txt`, `.env.example`, `docker-compose.yml`, `.github/workflows/docker-publish.yml` | Deployment. |

`parser.find_new_rows` becomes unused by the pipeline; leave it and its tests in place (harmless) — removal is a separate cleanup.

---

### Task 1: Dependencies and `config.py`

**Files:**
- Modify: `requirements.txt`
- Create: `src/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.FeedSpec(name: str, repo: str, branch: str)`; `config.FEEDS: dict[str, FeedSpec]` with keys `"internships"` and `"new-grad"`; `config.Settings(data_dir: str, poll_interval: int, github_token: str | None)`; `config.load_settings(env: Mapping[str, str]) -> Settings`.

- [ ] **Step 1: Add dependencies**

Replace `requirements.txt` with:

```
requests==2.32.3
python-dotenv==1.0.1
SQLAlchemy==2.0.54
pydantic==2.13.5
PyYAML==6.0.3
```

Run: `pip install -r requirements.txt -r requirements-dev.txt`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_config.py`:

```python
import pytest

from config import FEEDS, FeedSpec, load_settings


def test_feeds_registry_has_both_feeds():
    assert FEEDS["internships"] == FeedSpec("internships", "SimplifyJobs/Summer2026-Internships", "dev")
    assert FEEDS["new-grad"] == FeedSpec("new-grad", "SimplifyJobs/New-Grad-Positions", "dev")


def test_load_settings_defaults():
    s = load_settings({})
    assert s.data_dir == "/data"
    assert s.poll_interval == 300
    assert s.github_token is None


def test_load_settings_reads_env():
    s = load_settings({"DATA_DIR": "/tmp/x", "POLL_INTERVAL_SECONDS": "60", "GITHUB_TOKEN": "ghp_1"})
    assert s.data_dir == "/tmp/x"
    assert s.poll_interval == 60
    assert s.github_token == "ghp_1"


def test_load_settings_treats_blank_token_as_none():
    assert load_settings({"GITHUB_TOKEN": ""}).github_token is None


def test_load_settings_rejects_non_integer_interval():
    with pytest.raises(ValueError):
        load_settings({"POLL_INTERVAL_SECONDS": "soon"})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.config'`

- [ ] **Step 4: Implement `src/config.py`**

```python
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FeedSpec:
    name: str
    repo: str
    branch: str


FEEDS: dict[str, FeedSpec] = {
    "internships": FeedSpec("internships", "SimplifyJobs/Summer2026-Internships", "dev"),
    "new-grad": FeedSpec("new-grad", "SimplifyJobs/New-Grad-Positions", "dev"),
}


@dataclass(frozen=True)
class Settings:
    data_dir: str
    poll_interval: int
    github_token: str | None


def load_settings(env: Mapping[str, str]) -> Settings:
    return Settings(
        data_dir=env.get("DATA_DIR", "/data"),
        poll_interval=int(env.get("POLL_INTERVAL_SECONDS", "300")),
        github_token=env.get("GITHUB_TOKEN") or None,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add requirements.txt src/config.py tests/test_config.py
git commit -m "feat: add config module and pipeline dependencies"
```

---

### Task 2: Database models and session factory

**Files:**
- Create: `src/db.py`
- Create: `tests/conftest.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `db.utcnow() -> datetime` (naive UTC)
  - `db.make_engine(path: str | None) -> Engine` — `None` gives in-memory SQLite shared across sessions (tests)
  - `db.init_db(engine) -> None` — creates tables
  - `db.make_session_factory(engine) -> sessionmaker`
  - Models: `Feed(id, name, repo, branch, last_sha)`, `Job(id, feed_id, url_key, company, role, location, url, section, description, description_truncated, fetch_status, fetch_host, fetch_error, fetch_attempts, fetch_first_attempt_at, next_attempt_at, created_at)`, `Evaluation(id, job_id, user_id, stage, outcome, cv_snapshot, score, reasoning, missing_confirmed, missing_unknown, tailored, pdf_path, page_overflow, attempts, next_attempt_at, last_error, delivery_attempts, delivery_error, updated_at)`; `Evaluation.job` relationship; `Job.feed` relationship.
  - Constants: `STAGE_DELIVER = "deliver"`, `STAGE_CLOSED = "closed"`, `FETCH_PENDING = "pending"`, `FETCH_OK = "ok"`, `FETCH_FAILED = "failed"`.

- [ ] **Step 1: Write the shared test fixture**

Create `tests/conftest.py`:

```python
import pytest

from db import init_db, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine(None)
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.close()
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_db.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.db'`

- [ ] **Step 4: Implement `src/db.py`**

```python
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

STAGE_DELIVER = "deliver"
STAGE_CLOSED = "closed"

FETCH_PENDING = "pending"
FETCH_OK = "ok"
FETCH_FAILED = "failed"


def utcnow() -> datetime:
    """Naive UTC — SQLite stores no timezone, so keep every datetime naive and UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Feed(Base):
    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    repo: Mapped[str] = mapped_column(String)
    branch: Mapped[str] = mapped_column(String)
    last_sha: Mapped[str | None] = mapped_column(String, nullable=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="feed")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("feed_id", "url_key", name="uq_job_feed_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"))
    url_key: Mapped[str] = mapped_column(String)
    company: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    location: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    section: Mapped[str] = mapped_column(String)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    fetch_status: Mapped[str] = mapped_column(String, default=FETCH_PENDING)
    fetch_host: Mapped[str | None] = mapped_column(String, nullable=True)
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    fetch_first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    feed: Mapped[Feed] = relationship(back_populates="jobs")
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="job")


class Evaluation(Base):
    __tablename__ = "evaluations"
    __table_args__ = (UniqueConstraint("job_id", "user_id", name="uq_eval_job_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    user_id: Mapped[str] = mapped_column(String)

    # Next action to run. Never "the last thing that happened".
    stage: Mapped[str] = mapped_column(String, default=STAGE_DELIVER)
    # Written once at a fallback decision; never overwritten.
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)

    cv_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_confirmed: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_unknown: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tailored: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    page_overflow: Mapped[bool] = mapped_column(Boolean, default=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    job: Mapped[Job] = relationship(back_populates="evaluations")


def make_engine(path: str | None) -> Engine:
    """SQLite engine. ``None`` gives one shared in-memory DB for tests."""
    if path is None:
        return create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        # WAL lets the poller and worker threads read/write concurrently.
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: add SQLite models for feeds, jobs and evaluations"
```

---

### Task 3: `users.py`

**Files:**
- Create: `src/users.py`
- Test: `tests/test_users.py`

**Interfaces:**
- Consumes: `config.FEEDS`
- Produces: `users.User` (Pydantic) with fields `id: str`, `cv: str`, `discord_webhook: str`, `feeds: list[str]`, `sections: list[str]` (lower-cased), `threshold: int = 60`; method `User.wants(feed_name: str, section: str) -> bool`; `users.load_users(path: str) -> list[User]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_users.py`:

```python
import tempfile
from pathlib import Path

import pytest

from users import User, load_users

VALID = """\
- id: ron
  cv: /data/cvs/ron.yaml
  discord_webhook: https://discord.com/api/webhooks/1/a
  feeds: [internships, new-grad]
  sections: [Software Engineering, data science]
  threshold: 60
- id: cousin
  cv: /data/cvs/cousin.yaml
  discord_webhook: https://discord.com/api/webhooks/2/b
  feeds: [new-grad]
  sections: [software engineering]
"""


def _write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "users.yaml"
    p.write_text(text)
    return str(p)


def test_load_users_parses_both_users():
    users = load_users(_write(VALID))
    assert [u.id for u in users] == ["ron", "cousin"]


def test_sections_are_lowercased():
    users = load_users(_write(VALID))
    assert users[0].sections == ["software engineering", "data science"]


def test_threshold_defaults_to_60():
    users = load_users(_write(VALID))
    assert users[1].threshold == 60


def test_wants_matches_feed_and_section_substring():
    ron = load_users(_write(VALID))[0]
    assert ron.wants("internships", "software engineering internship roles")
    assert ron.wants("new-grad", "data science, ai & machine learning new grad roles")


def test_wants_rejects_unsubscribed_feed():
    cousin = load_users(_write(VALID))[1]
    assert not cousin.wants("internships", "software engineering internship roles")


def test_wants_rejects_unsubscribed_section():
    cousin = load_users(_write(VALID))[1]
    assert not cousin.wants("new-grad", "hardware engineering new grad roles")


def test_unknown_feed_name_rejected():
    bad = VALID.replace("feeds: [new-grad]", "feeds: [phd-positions]")
    with pytest.raises(ValueError, match="phd-positions"):
        load_users(_write(bad))


def test_duplicate_user_id_rejected():
    bad = VALID.replace("id: cousin", "id: ron")
    with pytest.raises(ValueError, match="duplicate"):
        load_users(_write(bad))


def test_missing_webhook_rejected():
    bad = VALID.replace("  discord_webhook: https://discord.com/api/webhooks/2/b\n", "")
    with pytest.raises(ValueError):
        load_users(_write(bad))


def test_empty_file_rejected():
    with pytest.raises(ValueError, match="no users"):
        load_users(_write(""))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_users.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.users'`

- [ ] **Step 3: Implement `src/users.py`**

```python
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from config import FEEDS


class User(BaseModel):
    id: str
    cv: str
    discord_webhook: str
    feeds: list[str] = Field(min_length=1)
    sections: list[str] = Field(min_length=1)
    threshold: int = 60

    @field_validator("feeds")
    @classmethod
    def _known_feeds(cls, feeds: list[str]) -> list[str]:
        unknown = [f for f in feeds if f not in FEEDS]
        if unknown:
            raise ValueError(f"unknown feed(s): {', '.join(unknown)}")
        return feeds

    @field_validator("sections")
    @classmethod
    def _lowercase(cls, sections: list[str]) -> list[str]:
        return [s.strip().lower() for s in sections]

    def wants(self, feed_name: str, section: str) -> bool:
        """Same matching rule the old FILTER_SECTIONS used: substring on the normalized heading."""
        return feed_name in self.feeds and any(s in section for s in self.sections)


def load_users(path: str) -> list[User]:
    raw = yaml.safe_load(Path(path).read_text()) or []
    if not raw:
        raise ValueError(f"{path}: no users defined")
    try:
        users = [User.model_validate(item) for item in raw]
    except ValidationError as e:
        raise ValueError(f"{path}: {e}") from e
    ids = [u.id for u in users]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"{path}: duplicate user id(s): {', '.join(sorted(dupes))}")
    return users
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_users.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/users.py tests/test_users.py
git commit -m "feat: load per-user subscriptions from users.yaml"
```

---

### Task 4: Legacy state import

**Files:**
- Modify: `src/db.py` (append)
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Consumes: `state.read_known_urls`, `state.read_last_sha` (existing), `config.FEEDS`
- Produces: `db.ensure_feeds(session, feeds: Iterable[FeedSpec]) -> None`; `db.import_legacy_state(session, data_dir: str) -> int` (number of jobs seeded; 0 when skipped).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
import tempfile

from config import FEEDS
from db import ensure_feeds, import_legacy_state, FETCH_OK
from state import write_known_urls, write_last_sha


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'ensure_feeds' from 'src.db'`

- [ ] **Step 3: Implement**

Append to `src/db.py`:

```python
from collections.abc import Iterable  # noqa: E402  (keep imports at top in the real file)

from sqlalchemy.orm import Session  # noqa: E402

from config import FeedSpec  # noqa: E402
from state import read_known_urls, read_last_sha  # noqa: E402


def ensure_feeds(session: Session, feeds: Iterable[FeedSpec]) -> None:
    existing = {f.name for f in session.query(Feed).all()}
    for spec in feeds:
        if spec.name not in existing:
            session.add(Feed(name=spec.name, repo=spec.repo, branch=spec.branch))
    session.flush()


def import_legacy_state(session: Session, data_dir: str) -> int:
    """One-time import of the pre-pipeline known_urls.json / last_sha.txt.

    Runs only while the jobs table is empty. Seeds every known URL as an
    already-seen internships job (no evaluations, so nothing is notified) and
    carries the SHA over so the next poll diffs from where the old tracker left off.
    """
    if session.query(Job.id).first() is not None:
        return 0
    known = read_known_urls(data_dir)
    if not known:
        return 0
    feed = session.query(Feed).filter_by(name="internships").one()
    for key in sorted(known):
        session.add(Job(
            feed=feed, url_key=key, url=key, company="", role="", location="",
            section="", fetch_status=FETCH_OK,
        ))
    feed.last_sha = read_last_sha(data_dir)
    session.flush()
    return len(known)
```

Move the four `import` lines to the top of `src/db.py` with the other imports (the `noqa` comments are only there because this snippet is shown as an append).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat: import legacy known_urls/last_sha state into the jobs table"
```

---

### Task 5: Discord client rewrite

**Files:**
- Rewrite: `src/discord_client.py`
- Rewrite: `tests/test_discord_client.py`

**Interfaces:**
- Produces:
  - `discord_client.DeliveryResult(kind: str, retry_after: float | None, error: str | None)` with `kind in {"ok", "transient", "gone", "invalid"}` and property `ok -> bool`
  - `discord_client.send_message(webhook_url: str, content: str, pdf_path: str | None = None) -> DeliveryResult`
  - `discord_client.cap_content(header: str, lists: list[str], tail: str = "") -> str` — assembles `header + lists + tail`, truncating list lines first, then `tail`, so the result is ≤ 2000 characters
  - `discord_client.format_link_only(company: str, role: str, location: str, url: str, note: str | None = None) -> str`
  - `discord_client.MAX_CONTENT = 2000`
- Removes: `format_message`, `send_notification` (no remaining callers after Task 8).

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_discord_client.py` with:

```python
from unittest.mock import Mock, patch

import requests

from discord_client import (
    MAX_CONTENT,
    DeliveryResult,
    cap_content,
    format_link_only,
    send_message,
)

WEBHOOK = "https://discord.com/api/webhooks/1/abc"


def _resp(status, body=None, headers=None):
    r = Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body if body is not None else {}
    r.text = "" if body is None else str(body)
    return r


# --- format_link_only -------------------------------------------------------

def test_format_link_only_basic():
    msg = format_link_only("Stripe", "SWE Intern", "SF", "https://stripe.com/j?gh_jid=1")
    assert msg == "🆕 **Stripe** — SWE Intern\n📍 SF\n🔗 https://stripe.com/j?gh_jid=1"


def test_format_link_only_with_note():
    msg = format_link_only("Stripe", "SWE Intern", "SF", "https://x", note="couldn't read the description")
    assert msg.startswith("🆕 **Stripe** — SWE Intern (couldn't read the description)")


# --- cap_content -----------------------------------------------------------

def test_cap_content_leaves_short_message_alone():
    assert cap_content("head", ["⚠️ Gaps: a, b"], "tail") == "head\n⚠️ Gaps: a, b\ntail"


def test_cap_content_truncates_lists_before_tail():
    long_list = "⚠️ Gaps: " + ", ".join(f"req{i}" for i in range(600))
    out = cap_content("head", [long_list], "tail")
    assert len(out) <= MAX_CONTENT
    assert out.endswith("tail")
    assert "…" in out


def test_cap_content_truncates_tail_when_lists_already_minimal():
    out = cap_content("head", [], "x" * 5000)
    assert len(out) <= MAX_CONTENT
    assert out.startswith("head\n")


# --- send_message ----------------------------------------------------------

def test_send_uses_wait_true_and_succeeds_on_200_with_body():
    with patch("discord_client.requests.post", return_value=_resp(200, {"id": "1"})) as post:
        result = send_message(WEBHOOK, "hi")
    assert result.ok
    assert post.call_args.kwargs["params"] == {"wait": "true"}
    assert post.call_args.kwargs["json"] == {"content": "hi"}


def test_send_204_is_not_success():
    # wait=true should never yield 204; if it does, Discord did not confirm the message.
    with patch("discord_client.requests.post", return_value=_resp(204)):
        result = send_message(WEBHOOK, "hi")
    assert not result.ok
    assert result.kind == "transient"


def test_send_429_is_transient_with_retry_after():
    with patch("discord_client.requests.post", return_value=_resp(429, {}, {"Retry-After": "7"})):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert result.retry_after == 7.0


def test_send_5xx_is_transient():
    with patch("discord_client.requests.post", return_value=_resp(503)):
        assert send_message(WEBHOOK, "hi").kind == "transient"


def test_send_connection_error_is_transient():
    with patch("discord_client.requests.post", side_effect=requests.ConnectionError("down")):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "transient"
    assert "down" in result.error


def test_send_404_is_gone():
    with patch("discord_client.requests.post", return_value=_resp(404)):
        assert send_message(WEBHOOK, "hi").kind == "gone"


def test_send_401_is_gone():
    with patch("discord_client.requests.post", return_value=_resp(401)):
        assert send_message(WEBHOOK, "hi").kind == "gone"


def test_send_400_is_invalid():
    with patch("discord_client.requests.post", return_value=_resp(400, {"message": "bad"})):
        result = send_message(WEBHOOK, "hi")
    assert result.kind == "invalid"
    assert "bad" in result.error


def test_send_with_pdf_uses_multipart(tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with patch("discord_client.requests.post", return_value=_resp(200, {"id": "1"})) as post:
        result = send_message(WEBHOOK, "hi", pdf_path=str(pdf))
    assert result.ok
    kwargs = post.call_args.kwargs
    assert "json" not in kwargs
    assert kwargs["data"] == {"payload_json": '{"content": "hi"}'}
    filename, _fh, ctype = kwargs["files"]["files[0]"]
    assert filename == "r.pdf"
    assert ctype == "application/pdf"


def test_delivery_result_ok_property():
    assert DeliveryResult("ok", None, None).ok
    assert not DeliveryResult("transient", None, None).ok
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_discord_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_CONTENT' from 'src.discord_client'`

- [ ] **Step 3: Implement `src/discord_client.py`**

Replace the file with:

```python
import json
from dataclasses import dataclass
from pathlib import Path

import requests

MAX_CONTENT = 2000
_ELLIPSIS = "…"


@dataclass(frozen=True)
class DeliveryResult:
    kind: str  # "ok" | "transient" | "gone" | "invalid"
    retry_after: float | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def format_link_only(company: str, role: str, location: str, url: str, note: str | None = None) -> str:
    head = f"🆕 **{company}** — {role}"
    if note:
        head += f" ({note})"
    return f"{head}\n📍 {location}\n🔗 {url}"


def cap_content(header: str, lists: list[str], tail: str = "") -> str:
    """Join header + list lines + tail under MAX_CONTENT.

    List lines (gaps, unknowns) are truncated first, then the tail (reasoning),
    so an oversized list never produces a request Discord will reject forever.
    """
    parts = [header, *lists] + ([tail] if tail else [])
    budget = MAX_CONTENT - (len(parts) - 1)  # newlines
    fixed = len(header)
    remaining = budget - fixed - (len(tail) if tail else 0)
    capped_lists = []
    for line in lists:
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[: max(remaining - 1, 0)] + _ELLIPSIS
        capped_lists.append(line)
        remaining -= len(line)
    out = "\n".join([header, *capped_lists] + ([tail] if tail else []))
    if len(out) > MAX_CONTENT:
        out = out[: MAX_CONTENT - 1] + _ELLIPSIS
    return out


def send_message(webhook_url: str, content: str, pdf_path: str | None = None) -> DeliveryResult:
    """POST to a Discord webhook with ?wait=true. Success is 200 with a message body only."""
    try:
        if pdf_path:
            with open(pdf_path, "rb") as fh:
                resp = requests.post(
                    webhook_url,
                    params={"wait": "true"},
                    data={"payload_json": json.dumps({"content": content})},
                    files={"files[0]": (Path(pdf_path).name, fh, "application/pdf")},
                    timeout=30,
                )
        else:
            resp = requests.post(webhook_url, params={"wait": "true"}, json={"content": content}, timeout=10)
    except requests.RequestException as e:
        return DeliveryResult("transient", None, str(e))

    status = resp.status_code
    if status == 200:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("id"):
            return DeliveryResult("ok", None, None)
        return DeliveryResult("transient", None, "200 without message body")
    if status == 429:
        retry_after = _retry_after(resp)
        return DeliveryResult("transient", retry_after, "rate limited")
    if status in (401, 404):
        return DeliveryResult("gone", None, f"webhook returned {status}")
    if status >= 500 or status == 204:
        return DeliveryResult("transient", None, f"unconfirmed: HTTP {status}")
    return DeliveryResult("invalid", None, f"HTTP {status}: {resp.text[:200]}")


def _retry_after(resp) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        try:
            raw = resp.json().get("retry_after")
        except ValueError:
            raw = None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_discord_client.py -v`
Expected: 15 passed

Note: `src/main.py` still imports `format_message` and `send_notification` at this point and will fail to import until Task 8 rewrites it. The test suite does not import `main`, so this is fine.

- [ ] **Step 5: Commit**

```bash
git add src/discord_client.py tests/test_discord_client.py
git commit -m "feat: confirmed Discord delivery with wait=true, error classes and content cap"
```

---

### Task 6: Poller

> **Corrected after final review:** a README that cannot be fetched at the new SHA must *not* advance `last_sha` — the poller logs a warning and returns, retrying next interval (otherwise a first poll against a lagging raw.githubusercontent.com leaves `last_sha` set with zero jobs and the next poll floods every row). Seeding is `previous_sha is None or not known`: a feed with no jobs is always seeded silently. The test `test_readme_missing_advances_sha_without_changes` below became `test_readme_missing_leaves_sha_and_jobs_untouched` (plus `test_readme_missing_then_present_seeds_without_evaluations` and `test_feed_with_sha_but_no_jobs_is_seeded`).

**Files:**
- Create: `src/poller.py`
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `db.Feed`, `db.Job`, `db.Evaluation`, `parser.parse_sections`, `parser.url_key`, `users.User.wants`, `config.FeedSpec`
- Produces: `poller.poll_feed(session, spec: FeedSpec, users: list[User], get_latest_sha: Callable[[str, str], str], get_readme_content: Callable[[str, str], str | None]) -> PollResult(sha_changed: bool, jobs_added: int, evaluations_added: int)`. The two callables take `(repo, branch)` and `(repo, sha)` respectively — same shape as `github_client` minus the token, which `main` binds with `functools.partial`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_poller.py`:

```python
import pytest

from config import FeedSpec
from db import Evaluation, Feed, Job, ensure_feeds, STAGE_DELIVER
from poller import poll_feed
from users import User

SPEC = FeedSpec("internships", "a/b", "dev")

README_V1 = """\
## 💻 Software Engineering Internship Roles
<table><tbody>
<tr><td><strong>Stripe</strong></td><td>SWE Intern</td><td>SF</td>
<td><div align="center"><a href="https://stripe.com/j?gh_jid=1&utm_source=Simplify"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>

## 💰 Quantitative Finance Internship Roles
<table><tbody>
<tr><td><strong>Jane</strong></td><td>Quant Intern</td><td>NY</td>
<td><div align="center"><a href="https://jane.com/q"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>
"""

README_V2 = README_V1.replace(
    "</tbody></table>\n\n## 💰",
    """<tr><td>↳</td><td>Backend Intern</td><td>NY</td>
<td><div align="center"><a href="https://stripe.com/j?gh_jid=2&utm_source=Simplify"><img alt="Apply"></a></div></td><td>0d</td></tr>
</tbody></table>

## 💰""",
)


def _user(uid, feeds, sections):
    return User(id=uid, cv="/x", discord_webhook="https://d/1", feeds=feeds, sections=sections)


RON = _user("ron", ["internships", "new-grad"], ["software engineering"])
COUSIN = _user("cousin", ["new-grad"], ["software engineering"])


def _github(sha, readme):
    return (lambda repo, branch: sha), (lambda repo, s: readme)


@pytest.fixture
def db(session):
    ensure_feeds(session, [SPEC, FeedSpec("new-grad", "a/c", "dev")])
    session.commit()
    return session


def test_first_poll_seeds_jobs_without_evaluations(db):
    sha, readme = _github("s1", README_V1)
    result = poll_feed(db, SPEC, [RON], sha, readme)
    assert result.sha_changed and result.jobs_added == 2 and result.evaluations_added == 0
    assert db.query(Evaluation).count() == 0
    assert db.query(Feed).filter_by(name="internships").one().last_sha == "s1"


def test_unchanged_sha_is_noop(db):
    sha, readme = _github("s1", README_V1)
    poll_feed(db, SPEC, [RON], sha, readme)
    calls = []
    result = poll_feed(db, SPEC, [RON], sha, lambda r, s: calls.append(s) or README_V1)
    assert not result.sha_changed
    assert calls == []


def test_new_row_creates_job_and_evaluation_for_matching_user(db):
    poll_feed(db, SPEC, [RON, COUSIN], *_github("s1", README_V1))
    result = poll_feed(db, SPEC, [RON, COUSIN], *_github("s2", README_V2))
    assert result.jobs_added == 1
    assert result.evaluations_added == 1
    ev = db.query(Evaluation).one()
    assert ev.user_id == "ron"           # cousin is not subscribed to internships
    assert ev.stage == STAGE_DELIVER
    assert ev.job.url_key == "https://stripe.com/j?gh_jid=2"
    assert ev.job.company == "Stripe"    # ↳ row inherited the company
    assert ev.job.section == "software engineering internship roles"


def test_job_in_unsubscribed_section_gets_no_evaluation(db):
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1.replace("https://jane.com/q", "https://old.com/q")))
    # jane.com/q appears as a *new* quant row in s2
    result = poll_feed(db, SPEC, [RON], *_github("s2", README_V1))
    assert result.jobs_added == 1
    assert result.evaluations_added == 0


def test_readme_missing_leaves_sha_and_jobs_untouched(db):
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    result = poll_feed(db, SPEC, [RON], (lambda r, b: "s2"), (lambda r, s: None))
    assert result.sha_changed is True and result.jobs_added == 0
    assert db.query(Feed).filter_by(name="internships").one().last_sha == "s1"
    assert db.query(Job).count() == 2


def test_same_url_in_two_feeds_yields_two_jobs(db):
    other = FeedSpec("new-grad", "a/c", "dev")
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    poll_feed(db, other, [RON], *_github("n1", README_V1))
    assert db.query(Job).filter_by(url_key="https://stripe.com/j?gh_jid=1").count() == 2


def test_failure_mid_poll_writes_nothing(db, monkeypatch):
    poll_feed(db, SPEC, [RON], *_github("s1", README_V1))
    import poller as poller_mod

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(poller_mod, "_evaluations_for", boom)
    with pytest.raises(RuntimeError):
        poll_feed(db, SPEC, [RON], *_github("s2", README_V2))
    db.rollback()
    assert db.query(Job).count() == 2
    assert db.query(Feed).filter_by(name="internships").one().last_sha == "s1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_poller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.poller'`

- [ ] **Step 3: Implement `src/poller.py`**

```python
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from config import FeedSpec
from db import Evaluation, Feed, Job
from parser import parse_sections, url_key
from users import User

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollResult:
    sha_changed: bool
    jobs_added: int
    evaluations_added: int


def poll_feed(
    session: Session,
    spec: FeedSpec,
    users: list[User],
    get_latest_sha: Callable[[str, str], str],
    get_readme_content: Callable[[str, str], str | None],
) -> PollResult:
    """Diff one feed against the jobs table. All writes happen in one commit.

    First run (no last_sha) seeds every row without evaluations so nothing is
    notified for postings that predate the tracker.
    """
    feed = session.query(Feed).filter_by(name=spec.name).one()
    current_sha = get_latest_sha(spec.repo, spec.branch)
    if current_sha == feed.last_sha:
        return PollResult(False, 0, 0)

    previous_sha = feed.last_sha
    readme = get_readme_content(spec.repo, current_sha)
    if readme is None:
        # Log and return without touching last_sha; the poll repeats next interval.
        log.warning("[%s] README missing at %s; will retry next poll", spec.name, current_sha[:7])
        return PollResult(True, 0, 0)

    known = {k for (k,) in session.query(Job.url_key).filter_by(feed_id=feed.id).all()}
    # A feed with no jobs is always seeded silently, whatever last_sha says.
    seeding = previous_sha is None or not known
    jobs_added = evals_added = 0
    for section, rows in parse_sections(readme).items():
        for row in rows:
            key = url_key(row["url"])
            if key in known:
                continue
            known.add(key)
            job = Job(
                feed=feed, url_key=key, company=row["company"], role=row["role"],
                location=row["location"], url=row["url"], section=section,
            )
            session.add(job)
            jobs_added += 1
            if not seeding:
                evals = _evaluations_for(job, spec.name, section, users)
                session.add_all(evals)
                evals_added += len(evals)

    feed.last_sha = current_sha
    session.commit()
    log.info("[%s] %s → %s: +%d jobs, +%d evaluations%s",
             spec.name, (previous_sha or "none")[:7], current_sha[:7], jobs_added, evals_added,
             " (seeded)" if seeding else "")
    return PollResult(True, jobs_added, evals_added)


def _evaluations_for(job: Job, feed_name: str, section: str, users: list[User]) -> list[Evaluation]:
    return [Evaluation(job=job, user_id=u.id) for u in users if u.wants(feed_name, section)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_poller.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/poller.py tests/test_poller.py
git commit -m "feat: transactional feed poller creating per-user evaluations"
```

---

### Task 7: Worker — pause map, backoff, deliver stage

> **Corrected after final review:** (1) a `transient` result also pauses `discord:<user_id>` until the row's `next_attempt_at`, so the same user's other rows are not attempted while one backs off (`invalid` stays per-row); (2) a `user_id` missing from `users.yaml` pauses `discord:<user_id>` until restart and leaves the row at its stage — it is never closed; (3) an exception raised by the sender is recorded as a `transient` attempt, not a worker crash. The code block below carries these edits.

**Files:**
- Create: `src/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `db.Evaluation`, `db.Job`, `db.STAGE_DELIVER`, `db.STAGE_CLOSED`, `db.utcnow`, `discord_client.send_message`, `discord_client.format_link_only`, `users.User`
- Produces:
  - `worker.BACKOFF_SECONDS = (30, 60, 300, 900, 3600)`; `worker.backoff(attempt: int) -> int` (attempt is 1-based, saturates at the last value)
  - `worker.DELIVERY_BUDGET = 10`
  - `worker.Worker(session_factory, users: list[User], send=send_message, now=utcnow)` with:
    - `paused: dict[str, datetime | None]` — key `"discord:<user_id>"` (and `"llm"` in later plans); value `None` = until restart
    - `is_paused(name: str) -> bool`
    - `run_once() -> bool` — does at most one unit of work; returns whether it did anything
    - `deliver(session, ev: Evaluation) -> None` — `ok` → `closed`; `gone` → pause `discord:<user_id>` until restart; `transient` → count the attempt, back off, and pause `discord:<user_id>` until `next_attempt_at`; `invalid` → count the attempt and back off (row only); unknown `user_id` → pause `discord:<user_id>` until restart, row untouched; sender exception → treated as `transient`
    - `run_forever(idle_sleep: float = 3.0) -> None`
  - `worker.message_for(ev: Evaluation) -> str` — builds the Discord content from the row; in this plan only the link-only variant exists (`outcome=None` → no note; `outcome="fetch_failed"` or `"score_failed"` → note "couldn't read the description"). Later plans extend it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worker.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.worker'`

- [ ] **Step 3: Implement `src/worker.py`**

```python
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from db import STAGE_CLOSED, STAGE_DELIVER, Evaluation, utcnow
from discord_client import DeliveryResult, format_link_only, send_message
from users import User

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 60, 300, 900, 3600)
DELIVERY_BUDGET = 10

_LINK_ONLY_NOTES = {
    "fetch_failed": "couldn't read the description",
    "score_failed": "couldn't read the description",
}


def backoff(attempt: int) -> int:
    """Seconds to wait after the given 1-based attempt number."""
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]


def message_for(ev: Evaluation) -> str:
    job = ev.job
    return format_link_only(job.company, job.role, job.location, job.url, note=_LINK_ONLY_NOTES.get(ev.outcome))


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker,
        users: list[User],
        send: Callable[..., DeliveryResult] = send_message,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._users = {u.id: u for u in users}
        self._send = send
        self._now = now
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
            log.error("Pausing %s (%s)%s", name, why, "" if until else " until restart")
        self.paused[name] = until

    # -- loop ---------------------------------------------------------------

    def run_forever(self, idle_sleep: float = 3.0) -> None:
        while True:
            try:
                did_work = self.run_once()
            except Exception:
                log.exception("Worker iteration failed")
                did_work = False
            if not did_work:
                time.sleep(idle_sleep)

    def run_once(self) -> bool:
        with self._sessions() as session:
            ev = self._next_deliverable(session)
            if ev is None:
                return False
            self.deliver(session, ev)
            session.commit()
            return True

    def _next_deliverable(self, session: Session) -> Evaluation | None:
        now = self._now()
        candidates = (
            session.query(Evaluation)
            .filter(Evaluation.stage == STAGE_DELIVER, Evaluation.next_attempt_at <= now)
            .order_by(Evaluation.next_attempt_at, Evaluation.id)
            .all()
        )
        for ev in candidates:
            if not self.is_paused(f"discord:{ev.user_id}"):
                return ev
        return None

    # -- deliver stage ------------------------------------------------------

    def deliver(self, session: Session, ev: Evaluation) -> None:
        user = self._users.get(ev.user_id)
        if user is None:
            self._pause(f"discord:{ev.user_id}", None, f"user {ev.user_id!r} not in users.yaml")
            return

        try:
            result = self._send(user.discord_webhook, message_for(ev), ev.pdf_path)
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

        ev.delivery_attempts += 1
        ev.delivery_error = result.error
        delay = result.retry_after if result.retry_after is not None else backoff(ev.delivery_attempts)
        ev.next_attempt_at = self._now() + timedelta(seconds=delay)
        level = logging.ERROR if ev.delivery_attempts > DELIVERY_BUDGET else logging.WARNING
        log.log(level, "Delivery to %s failed (attempt %d, %s): %s; retry in %ss",
                ev.user_id, ev.delivery_attempts, result.kind, result.error, delay)
        if result.kind == "transient":
            self._pause(f"discord:{ev.user_id}", ev.next_attempt_at, result.error or result.kind)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_worker.py -v`
Expected: 15 passed

- [ ] **Step 5: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: all passed, no warnings about unawaited/unclosed resources

- [ ] **Step 6: Commit**

```bash
git add src/worker.py tests/test_worker.py
git commit -m "feat: worker with deliver stage, backoff and per-destination pausing"
```

---

### Task 8: `main.py` — wire it together

**Files:**
- Rewrite: `src/main.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: everything above plus `migration.migrate_state`, `github_client.get_latest_sha`, `github_client.get_readme_content`
- Produces: `main.build(settings, users) -> tuple[sessionmaker, Worker]`; `main.poll_all(session_factory, users, settings) -> None`; `main.main() -> None`

- [ ] **Step 1: Replace `src/main.py`**

```python
import functools
import logging
import os
import threading
import time

from dotenv import load_dotenv

from config import FEEDS, Settings, load_settings
from db import ensure_feeds, import_legacy_state, init_db, make_engine, make_session_factory
from github_client import get_latest_sha, get_readme_content
from migration import migrate_state
from poller import poll_feed
from users import User, load_users
from worker import Worker

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")


def build(settings: Settings, users: list[User]):
    engine = make_engine(os.path.join(settings.data_dir, "tracker.db"))
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        ensure_feeds(session, FEEDS.values())
        seeded = import_legacy_state(session, settings.data_dir)
        session.commit()
    if seeded:
        log.info("Imported %d jobs from legacy known_urls.json", seeded)
    return session_factory, Worker(session_factory, users)


def poll_all(session_factory, users: list[User], settings: Settings) -> None:
    latest_sha = functools.partial(get_latest_sha, token=settings.github_token)
    readme = functools.partial(get_readme_content, token=settings.github_token)
    for spec in FEEDS.values():
        try:
            with session_factory() as session:
                poll_feed(session, spec, users, latest_sha, readme)
        except Exception:
            log.exception("[%s] poll failed", spec.name)


def _poll_loop(session_factory, users, settings) -> None:
    while True:
        poll_all(session_factory, users, settings)
        time.sleep(settings.poll_interval)


def main() -> None:
    settings = load_settings(os.environ)
    users = load_users(os.path.join(settings.data_dir, "users.yaml"))
    log.info("Tracker starting: %d users, feeds %s, interval %ds",
             len(users), list(FEEDS), settings.poll_interval)

    # Bring pre-pipeline state files to version 2 first (PR #1). Retry until GitHub answers.
    internships = FEEDS["internships"]
    while not migrate_state(settings.data_dir,
                            lambda sha: get_readme_content(internships.repo, sha, settings.github_token)):
        log.error("State migration failed; retrying in 60s")
        time.sleep(60)

    session_factory, worker = build(settings, users)

    threading.Thread(target=_poll_loop, args=(session_factory, users, settings), name="poller", daemon=True).start()
    worker.run_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Update `.env.example`**

```
GITHUB_TOKEN=ghp_YOUR_TOKEN_HERE
POLL_INTERVAL_SECONDS=300
DATA_DIR=/data
# Per-user Discord webhooks and section filters live in $DATA_DIR/users.yaml — see data/users.example.yaml
```

Create `data/users.example.yaml`:

```yaml
- id: ron
  cv: /data/cvs/ron.yaml
  discord_webhook: https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN
  feeds: [internships, new-grad]
  sections: [software engineering, data science]
  threshold: 60
```

Add `!data/users.example.yaml` to `.gitignore` after the `!data/.gitkeep` line.

- [ ] **Step 3: Verify imports and the full suite**

Run: `PYTHONPATH=src python3 -c "import main"` — expected: no output, exit 0 (it must not start threads on import).
Run: `python3 -m pytest -q` — expected: all passed.

- [ ] **Step 4: Manual smoke run against a temp data dir**

```bash
mkdir -p /tmp/it-smoke && cp data/users.example.yaml /tmp/it-smoke/users.yaml
# Put a real (test-channel) webhook in /tmp/it-smoke/users.yaml first.
DATA_DIR=/tmp/it-smoke POLL_INTERVAL_SECONDS=20 PYTHONPATH=src timeout 60 python3 src/main.py
```

Expected log lines, in order: `Tracker starting`, `[internships] ... (seeded)`, `[new-grad] ... (seeded)`, then nothing delivered (seeding creates no evaluations). `sqlite3 /tmp/it-smoke/tracker.db 'select name,last_sha from feeds; select count(*) from jobs; select count(*) from evaluations'` shows two SHAs, a few thousand jobs, zero evaluations.

To see a delivery end to end without waiting for a real new posting: `sqlite3 /tmp/it-smoke/tracker.db "insert into evaluations (job_id,user_id,stage,attempts,next_attempt_at,delivery_attempts,page_overflow,updated_at) select id,'ron','deliver',0,datetime('now'),0,0,datetime('now') from jobs where section like '%software%' limit 1"` then start again; expect one `Delivered to ron:` line and one Discord message.

- [ ] **Step 5: Commit**

```bash
git add src/main.py .env.example data/users.example.yaml .gitignore
git commit -m "feat: run poller and worker threads over the SQLite queue"
```

---

### Task 9: Deployment files

**Files:**
- Modify: `docker-compose.yml`, `.github/workflows/docker-publish.yml`, `Dockerfile` (no change needed but verify)

- [ ] **Step 1: Compose**

`docker-compose.yml` — no functional change needed for this plan (`./data:/data` already covers `users.yaml` and `tracker.db`). Add a comment line so the next person knows:

```yaml
services:
  internship-tracker:
    image: ronp2805/internship-tracker:latest
    restart: always
    volumes:
      - ./data:/data   # users.yaml, tracker.db, (later) cvs/ and output/
    env_file:
      - .env
```

- [ ] **Step 2: CI — build the image on every branch, push only on main**

In `.github/workflows/docker-publish.yml`, replace the `docker` job with:

```yaml
  docker:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: docker/build-push-action@v6
        with:
          context: .
          push: false
          load: true
          tags: internship-tracker:ci

      - name: Smoke-test the image imports cleanly
        run: docker run --rm -e DISCORD_WEBHOOK_URL=x internship-tracker:ci python -c "import main"

      - uses: docker/login-action@v3
        if: github.ref == 'refs/heads/main' && github.event_name == 'push'
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - uses: docker/build-push-action@v6
        if: github.ref == 'refs/heads/main' && github.event_name == 'push'
        with:
          context: .
          push: true
          tags: ${{ secrets.DOCKERHUB_USERNAME }}/internship-tracker:latest
```

(`DISCORD_WEBHOOK_URL=x` is harmless — nothing reads it anymore — and is left out once you confirm the import smoke passes.)

- [ ] **Step 3: Build locally and run the import smoke**

Run: `docker build -t internship-tracker:local . && docker run --rm internship-tracker:local python -c "import main"`
Expected: exit 0, no output.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .github/workflows/docker-publish.yml
git commit -m "ci: build image on every branch and smoke-test imports"
```

---

### Task 10: Deploy checklist (manual, not code)

- [ ] Merge PR #1 first and let the old-style image run at least one poll so `state_version.txt` = 2 exists in `data/`. (If skipped, `main` runs the migration itself; this is just the tidier path.)
- [ ] On the server: create `data/users.yaml` from `data/users.example.yaml` with both users' real webhooks; `cv:` paths can point at not-yet-existing files — nothing reads them in this plan.
- [ ] Remove `DISCORD_WEBHOOK_URL` and `FILTER_SECTIONS` from the server's `.env`.
- [ ] Merge this plan's branch; Watchtower pulls; check logs for `Imported N jobs from legacy known_urls.json`, then `[internships] ... +0 jobs` and `[new-grad] ... (seeded)`.
- [ ] Wait for the next real posting in a subscribed section; confirm one Discord message per subscribed user.
- [ ] Point a throwaway user at a deleted webhook, restart, confirm one `Pausing discord:<id>` ERROR line and no repeated attempts.

---

## Self-review

**Spec coverage (Build order step 2 and supporting sections):**
- Poller thread, one transaction per feed, seeding without evaluations — Task 6.
- Per-feed uniqueness `(feed_id, url_key)` — Task 2.
- Worker: `stage` = next action, `closed` only on confirmed send, per-destination pause map cleared on restart, backoff schedule, delivery budget, separate `delivery_attempts`/`delivery_error` — Task 7.
- Discord contract: `wait=true`, 200+body, 2,000-char cap with lists truncated first, 429 `Retry-After`, 404/401 = gone, 204 not accepted — Task 5.
- Users from `users.yaml`, substring section match, unknown feed rejected — Task 3.
- Legacy import with SHA preserved; `migrate_state` first — Tasks 4 and 8.
- Configuration: `DISCORD_WEBHOOK_URL`/`FILTER_SECTIONS` removed — Task 8.
- Deployment: `data/*` ignored (PR #1), CI image smoke — Task 9. WeasyPrint deps and LiteLLM network belong to Plans 3–4.
- Not in this plan by design: fetch stage, LLM pause key, CV snapshot, score/tailor/render — schema columns exist (Task 2) so later plans add behaviour without migrations.

**Placeholder scan:** none. One deliberately-marked throwaway line in Task 7's test is called out for deletion.

**Type consistency:** `send(webhook, content, pdf_path=None) -> DeliveryResult` used identically in Task 5 (definition), Task 7 (`FakeSender`, `Worker._send`). `poll_feed` callables `(repo, branch)` / `(repo, sha)` match `github_client` signatures bound with `partial(token=...)` in Task 8. `User.wants(feed_name, section)` used by `poller._evaluations_for`. `STAGE_DELIVER`/`STAGE_CLOSED` defined in Task 2, used in Tasks 6–8.

**Known follow-ups (not blockers):** `parser.find_new_rows` and the old `state.write_*` helpers become dead once this ships; remove in a cleanup commit after Plan 4. Plan 2 (fetcher) adds the `fetch` action to `Worker.run_once` ahead of `_next_deliverable`.

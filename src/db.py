from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

from config import FeedSpec
from state import read_known_urls, read_last_sha

STAGE_SCORE = "score"
STAGE_TAILOR = "tailor"
STAGE_RENDER = "render"
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
    fetch_strategy: Mapped[str | None] = mapped_column(String, nullable=True)
    has_requirements: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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

    # Next action to run: score → tailor → render → deliver → closed.
    stage: Mapped[str] = mapped_column(String, default=STAGE_SCORE)
    # Written once at a fallback decision; never overwritten.
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)

    cv_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_confirmed: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_unknown: Mapped[list | None] = mapped_column(JSON, nullable=True)
    score_model: Mapped[str | None] = mapped_column(String, nullable=True)
    score_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tailored: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tailor_model: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # Why a match went out without a PDF. Diagnostic; `outcome` stays "matched".
    resume_error: Mapped[str | None] = mapped_column(Text, nullable=True)
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


def drain_resume_stages(session: Session) -> int:
    """Move rows queued for tailoring or rendering to delivery.

    Called from Worker.startup() when this process has no tailor client or no output directory.
    Those rows were queued by a process that did, and nothing here will ever pick them up; without
    this they sit at their stage forever while the user waits for a notification already paid for.
    """
    rows = session.query(Evaluation).filter(Evaluation.stage.in_((STAGE_TAILOR, STAGE_RENDER))).all()
    for ev in rows:
        ev.resume_error = ev.resume_error or "tailoring not configured in this process"
        ev.stage, ev.attempts = STAGE_DELIVER, 0
        ev.pdf_path, ev.page_overflow = None, False
        ev.next_attempt_at = utcnow()
    return len(rows)


def ensure_columns(engine: Engine, metadata: MetaData = Base.metadata) -> list[str]:
    """Add columns that exist in the models but not in an existing database.

    create_all() only creates missing tables. Each plan that adds a column
    relies on this to upgrade a data dir that predates it. SQLite supports
    ADD COLUMN for nullable columns without a rebuild, which is all we need.
    """
    added: list[str] = []
    inspector = inspect(engine)
    for table in metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                raise RuntimeError(
                    f"ensure_columns can only add nullable columns; {table.name}.{column.name} is NOT NULL"
                )
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
            with engine.begin() as conn:
                conn.execute(text(ddl))
            added.append(f"{table.name}.{column.name}")
    return added

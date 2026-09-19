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

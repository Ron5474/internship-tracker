import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from config import FeedSpec
from db import STAGE_SCORE, Evaluation, Feed, Job
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
        # raw.githubusercontent.com can lag the commits API. Leave last_sha alone so the
        # next poll retries this SHA instead of treating the whole README as new.
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
    return [Evaluation(job=job, user_id=u.id, stage=STAGE_SCORE) for u in users if u.wants(feed_name, section)]
